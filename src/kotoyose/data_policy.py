"""Training-risk decisions for configured routes and their aliases."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from .agent_spec import DataPolicy

TrainingCapability = Literal["tools", "mcp", "skills", "workspace", "attachments"]
_TOOLS_WITHOUT_WORKSPACE = frozenset({"WebFetch", "WebSearch"})


def connection_data_policy(connection) -> DataPolicy:
    return connection.data_policy


def training_digest(config, alias: str, capabilities) -> str:
    preset = config.models[alias]
    route = config.routes[preset.route]
    connection = config.connections[route.connection]
    shape = {
        "alias": alias,
        "preset": preset.model_dump(mode="json"),
        "route": route.model_dump(mode="json"),
        "connection": connection.model_dump(mode="json"),
        "policy": connection_data_policy(connection),
        "capabilities": sorted(set(capabilities)),
        "resources": config.resolve_resources(preset),
        "agents": {name: agent.model_dump(mode="json")
                   for name, agent in config.resolve_agents(preset).items()},
    }
    if route.profile is not None:
        from .print_cli_profiles import get_print_cli_profile
        shape["profile_digest"] = get_print_cli_profile(route.profile).profile_digest(acp=route.driver == "cli.acp")
    return hashlib.sha256(json.dumps(shape, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class AliasDataPolicy:
    alias: str
    policy: DataPolicy
    restricted: bool
    unlocked: frozenset[str] = frozenset()

    def refusal(self, capabilities) -> str | None:
        missing = set(capabilities) - self.unlocked
        if self.restricted and missing:
            return (f"Alias '{self.alias}' has data_policy={self.policy}. "
                    f"Acknowledge {', '.join(sorted(missing))} in training_acks before use.")
        return None

    def view(self) -> dict:
        return {"data_policy": self.policy, "training_ack": bool(self.unlocked)}


def alias_data_policy(config, alias: str) -> AliasDataPolicy:
    models = getattr(config, "models", None)
    preset = models.get(alias) if isinstance(models, Mapping) else None
    route_name = getattr(preset, "route", None)
    route = (getattr(config, "routes", {}) or {}).get(route_name) if route_name else None
    connection_name = getattr(route, "connection", None)
    connection = (getattr(config, "connections", {}) or {}).get(connection_name) if connection_name else None
    if connection is None:
        # Route-less direct presets have no connection policy.
        return AliasDataPolicy(alias, "unknown", False)
    policy = connection_data_policy(connection)
    if policy == "no_training":
        return AliasDataPolicy(alias, policy, False)
    for ack in getattr(config, "training_acks", ()):
        if ack.alias == alias and ack.digest == training_digest(config, alias, ack.capabilities):
            return AliasDataPolicy(alias, policy, True, frozenset(ack.capabilities))
    return AliasDataPolicy(alias, policy, True)


def configured_capabilities(preset) -> set[str]:
    capabilities = set()
    if not preset.bare or preset.setting_sources:
        capabilities.update(("workspace", "tools", "mcp", "skills"))
    if preset.tools or preset.agents or preset.agent_refs:
        capabilities.add("tools")
    if set(preset.tools or ()) - _TOOLS_WITHOUT_WORKSPACE or preset.agents or preset.agent_refs:
        capabilities.add("workspace")
    if preset.mcp_refs or preset.mcp_servers:
        capabilities.add("mcp")
    if preset.skill_refs or preset.skills or preset.plugins:
        capabilities.add("skills")
    allow = preset.allow
    if preset.allow_cwd_override:
        capabilities.add("workspace")
    if allow is not None:
        if allow.workspace != "none" or allow.full:
            capabilities.add("workspace")
        if allow.capabilities or allow.egress or allow.full:
            capabilities.add("tools")
        if allow.full:
            capabilities.update(("mcp", "skills"))
    return capabilities


def request_capabilities(value) -> set[str]:
    """Detect capabilities before the request is translated for the provider."""
    caps = set()
    if getattr(value, "tools", None) or getattr(value, "tool_choice", None) is not None:
        caps.add("tools")
    if getattr(value, "cwd", None) is not None:
        caps.add("workspace")
    if any(getattr(item, field, None) is not None
           for item in (value, *getattr(value, "messages", ()))
           for field in ("attachments", "files", "images")):
        caps.add("attachments")
    for message in getattr(value, "messages", ()):
        if isinstance(message.content, list) and any((part.get("type") if isinstance(part, dict) else part.type) != "text"
               for part in message.content):
            caps.add("attachments")
        if getattr(message, "role", None) in {"tool", "function"} or getattr(message, "tool_calls", None):
            caps.add("tools")
    return caps


def training_binding(config, alias: str) -> str | None:
    decision = alias_data_policy(config, alias)
    if decision.restricted:
        return training_digest(config, alias, decision.unlocked)
    return None
