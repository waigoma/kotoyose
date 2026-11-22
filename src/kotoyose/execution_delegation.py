"""Pinned tool contracts and authority for shared-workspace delegation."""

import asyncio
from datetime import datetime

from .agent_spec import admitted_execution_cell
from .delegation import MAX_CONCURRENT, MCP_NAME
from .execution_resources import ExecutionError, agent_profile_allowed

LIST_AGENTS = "builtin:kotoyose-list-agents@1"
ASK_AGENT = "builtin:kotoyose-ask-agent@1"
MAX_PER_ATTEMPT = 32
MAX_TASK_BYTES = 256 * 1024
WORKSPACE_IDENTITY = ("workspace_id", "workspace_ref", "generation", "workspace_path",
                      "workspace_device", "workspace_inode")

TOOL_SPECS = {
    ref: {
        "resource_ref": ref, "name": name, "description": description,
        "input_schema": {"type": "object", "properties": properties, "required": required,
                         "additionalProperties": False},
        "effect": "read", "max_payload_bytes": MAX_TASK_BYTES, "timeout_seconds": 3600,
    }
    for ref, name, description, properties, required in (
        (LIST_AGENTS, "list_agents", "List the read-only aliases that can read this workspace.", {}, []),
        (ASK_AGENT, "ask_agent", "Give a read-only workspace alias a self-contained task.",
         {"alias": {"type": "string"}, "task": {"type": "string"},
          "conversation_id": {"type": ["string", "null"]}}, ["alias", "task"]),
    )
}


def only_delegation_tools(specs, refs, callback) -> bool:
    """Container and Codex adapters accept only the exact server-defined contracts."""
    tool_refs = set(refs) & TOOL_SPECS.keys()
    return (len(specs) == len(tool_refs)
            and all(isinstance(spec, dict) and spec.get("resource_ref") in tool_refs
                    and spec == TOOL_SPECS[spec["resource_ref"]] for spec in specs)
            and {spec["resource_ref"] for spec in specs} == tool_refs
            and (not specs or callback is not None))


def targets(config, parent_alias, policy, settings) -> list[dict]:
    parent = config.models.get(parent_alias)
    if parent is None or not parent.is_jailed or MCP_NAME not in parent.mcp_refs:
        return []
    return [
        {"alias": alias, "description": preset.description, "model": config.preset_for(alias).model, "place": "jail"}
        for alias, preset in config.models.items()
        if alias != parent_alias and alias in parent.delegates and preset.delegable and preset.is_jailed_read
        and agent_profile_allowed(alias, policy, settings, config=config)
    ]


def _readonly_runtime(snapshot: dict) -> bool:
    if snapshot.get("backend") == "print_cli":
        return snapshot.get("execution_environment") == "isolated"
    if snapshot.get("backend") != "native":
        return False
    provider = snapshot.get("provider")
    cell = admitted_execution_cell(provider, snapshot.get("codex_execution_mode"))
    return bool(cell and cell.workspace_readonly and
                (provider == "claude" or snapshot.get("execution_environment") == "isolated"))


def require_live_parent(parent: dict, snapshot: dict) -> None:
    pinned = parent["snapshot"]
    if (parent["status"] != "running" or pinned.get("isolation") != "sandbox"
            or pinned.get("parent_attempt_id") or ASK_AGENT not in pinned.get("tool_refs", ())
            or snapshot.get("delegated_by") != pinned.get("alias") or snapshot.get("depth") != 1
            or snapshot.get("kind") != "agent" or snapshot.get("surface") != "read" or not _readonly_runtime(snapshot)
            or snapshot.get("isolation") != "sandbox" or snapshot.get("delegation_tools") or TOOL_SPECS.keys() & set(snapshot.get("tool_refs", ()))
            or any(snapshot[key] != pinned[key] for key in WORKSPACE_IDENTITY)
            or datetime.fromisoformat(snapshot["deadline"]) > datetime.fromisoformat(pinned["deadline"])):
        raise ExecutionError("request_conflict", "Shared workspace parent or child binding changed", 409)


class DelegationCalls:
    """Own the child admissions and executions until the parent releases its workspace."""

    def __init__(self, cancel: asyncio.Event):
        self.cancel = cancel
        self.closed = False
        self.total = 0
        self.pending: dict[asyncio.Task, asyncio.Event] = {}
        self.watcher: asyncio.Task | None = None

    async def run(self, start):
        if self.closed or self.cancel.is_set():
            raise ExecutionError("tool_denied", "Parent execution is stopping", 409)
        if len(self.pending) >= MAX_CONCURRENT or self.total >= MAX_PER_ATTEMPT:
            raise ExecutionError("delegation_limit", "Delegation call limit reached", 429)
        self.total += 1
        if self.watcher is None:
            self.watcher = asyncio.create_task(self._watch_cancel())
        stop = asyncio.Event()
        task = asyncio.create_task(start(stop))
        self.pending[task] = stop
        task.add_done_callback(self.pending.pop)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            stop.set()
            raise

    async def _watch_cancel(self):
        await self.cancel.wait()
        self.closed = True
        for stop in self.pending.values():
            stop.set()

    async def close(self):
        self.closed = True
        for stop in self.pending.values():
            stop.set()
        if self.watcher is not None:
            self.watcher.cancel()
        tasks = list(self.pending)
        if self.watcher is not None:
            tasks.append(self.watcher)
        await asyncio.shield(asyncio.gather(*tasks, return_exceptions=True))
