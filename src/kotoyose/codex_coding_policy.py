"""Versioned, opt-in authority for Codex builtins inside the existing provider jail."""

import hashlib
import json
import stat
from pathlib import Path
from typing import Literal

DEFAULT_CODEX_MODE = "sandbox_exec_v1"
HOST_MODE = "host_native_coding_v1"
READ_MODE = "room_read_v1"
HOST_TOOL_REF = "builtin:host-coding@1"
READ_TOOL_REF = "builtin:workspace-read@1"
CodexSandbox = Literal["read-only", "workspace-write", "danger-full-access"]
CODEX_SANDBOXES = frozenset({"read-only", "workspace-write", "danger-full-access"})
PATCH_GRAMMAR_SHA256 = "d6367f4826ed608c424b0a308f3d6163527df63c22513d089b91863552f8bfeb"
HOST_TOOLS = frozenset({"exec_command", "write_stdin", "apply_patch"})
# A CLI/rootfs change is separately pinned by admission. This digest pins the reviewed
# outer effect boundary, not a claim that every builtin owns a separate CommandExecution.
# The independently selected SDK sandbox is pinned in the Attempt's codex_sandbox;
# it never changes the outer mounts, broker authority or process/resource ownership.
_POLICY = {
    "version": HOST_MODE, "tool_ref": HOST_TOOL_REF,
    "tools": sorted(HOST_TOOLS), "workspace": "/workspace",
    "network": "fixed_subscription_broker_only", "credential": "ephemeral_broker_capability",
    "effect_owner": "provider_process", "resource_limits": "whole_provider_systemd_cgroup",
    "soft_stop": "progress_notification_only", "hard_stop": "original_attempt_deadline",
}
TOOL_POLICY_DIGEST = hashlib.sha256(json.dumps(_POLICY, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
EXTERNAL_SANDBOX_POLICY = "external_sandbox_restricted_v1"
EXTERNAL_TOOL_POLICY_DIGEST = hashlib.sha256(json.dumps({
    **_POLICY, "sdk_sandbox": EXTERNAL_SANDBOX_POLICY,
    "protected_workspace_entries": [".codex", ".agents"], "git": "workspace_read_write",
}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
# 閲覧部屋は coding の digest を借りない。resume が read と write を取り違えられないようにする。
READ_SANDBOX_POLICY = "workspace_read_sandbox_v1"
_READ_POLICY = {
    "version": READ_MODE, "tool_ref": READ_TOOL_REF, "workspace": "/workspace",
    "workspace_mount": "read-only", "network": "fixed_subscription_broker_only",
    "credential": "ephemeral_broker_capability", "effect_owner": "provider_process",
    "resource_limits": "whole_provider_systemd_cgroup", "soft_stop": "progress_notification_only",
    "hard_stop": "original_attempt_deadline", "sdk_sandbox": "read-only", "git": "workspace_read_only",
    "protected_workspace_entries": "optional",
}
READ_TOOL_POLICY_DIGEST = hashlib.sha256(
    json.dumps(_READ_POLICY, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
# tools・通信・mount が同一で soft 停止だけを通知へ変える、レビュー済み二組に限定する。
# 現在の _POLICY から旧値を逆算しない。将来 tools 等が変わった場合は一致せず拒否する。
_PROGRESS_ONLY_TRANSITIONS = frozenset({
    ("56283f6706f6175930d9eba87d9402f1780a9357af57cff0e1ba4b1be53ef42b",
     "46c625672a4b2a2c681000dc40baa869ff905c3034372372ab6c586135a0b89d"),
    ("ba6544c392b896e914946a933759a45610df527a444f68fb41d5b8436698f51b",
     "394a8d2cbb682508e3f03397864b1d5cd4a5f4244a24ba5c91a4251ecae92a34"),
})
_UNPINNED = object()


def progress_only_policy_transition(previous: str | None, current: str | None) -> bool:
    """新 Grant に束縛した復旧で許す既知の soft 通知化だけを判定する。欠落や他の更新は拒否する。"""
    return isinstance(previous, str) and isinstance(current, str) and (previous, current) in _PROGRESS_ONLY_TRANSITIONS


def validate_coding_policy(environment: str | None, policy: str | None, digest: str | None,
                           *, mode: str | None = None) -> bool:
    """その mode の cell が宣言した pin だけを許す。省略時は従来の host coding。"""
    from .agent_spec import admitted_execution_cell

    cell = admitted_execution_cell("codex", mode or HOST_MODE)
    if cell is None or cell.isolated_tool_policy_digest is None:
        raise ValueError("invalid_codex_coding_policy")
    if environment == "isolated":
        if policy != cell.sandbox_policy or digest != cell.isolated_tool_policy_digest:
            raise ValueError("invalid_codex_coding_policy")
        return True
    if (cell.host_tool_policy_digest is None or environment is not None or policy is not None
            or digest != cell.host_tool_policy_digest):
        raise ValueError("invalid_codex_coding_policy")
    return False


def protected_workspace_entries(workspace: Path, expected: object = _UNPINNED, *, optional: bool = False) -> list[dict]:
    """Pin only prepared metadata mountpoints; never create hidden placeholder paths.

    Git writes stay in the dedicated repository. Linked/shared Git directories
    are unsupported; this function never mounts their external metadata.
    """
    validate_workspace(workspace)
    git = workspace / ".git"
    if git.exists() or git.is_symlink():
        if not git.is_dir() or git.is_symlink():
            raise ValueError("isolated_codex_requires_self_contained_git_directory")
        if any((git / name).exists() or (git / name).is_symlink()
               for name in ("commondir", "objects/info/alternates", "objects/info/http-alternates")):
            raise ValueError("isolated_codex_does_not_support_shared_git_metadata")
    entries = []
    for name in (".codex", ".agents"):
        path = workspace / name
        try:
            info = path.lstat()
        except FileNotFoundError as exc:
            if optional:
                continue
            raise ValueError("isolated_codex_requires_prepared_codex_and_agents_directories") from exc
        except OSError as exc:
            raise ValueError("isolated_codex_requires_prepared_codex_and_agents_directories") from exc
        if not stat.S_ISDIR(info.st_mode) or path.resolve(strict=True) != path:
            raise ValueError("invalid_codex_protected_workspace_entry")
        entries.append({"name": name, "device": info.st_dev, "inode": info.st_ino})
    if expected is not _UNPINNED:
        if (not isinstance(expected, list) or len(expected) > 2 or (not optional and len(expected) != 2)
                or any(not isinstance(entry, dict) or set(entry) != {"name", "device", "inode"}
                       or type(entry["device"]) is not int or type(entry["inode"]) is not int
                       for entry in expected) or expected != entries):
            raise ValueError("codex_protected_workspace_changed")
    return entries


def resolve_codex_sandbox(mode: str, value: object = None) -> CodexSandbox:
    """Resolve a saved choice or an old absent pin without changing historical meaning.

    New host admissions persist the result explicitly. An absent historical pin
    means the previously hard-coded policy for that execution mode, never the
    current alias setting. The outer jail remains independent of this SDK layer.
    """
    from .agent_spec import resolve_saved_sandbox
    return resolve_saved_sandbox(mode, value)


def validate_thread_sandbox(thread: dict, sandbox: CodexSandbox) -> None:
    """Check SDK-applied settings before sending any model input; never downgrade."""
    policy = thread.get("sandbox")
    if (thread.get("cwd") != "/workspace" or thread.get("approvalPolicy") != "never"
            or not isinstance(policy, dict)
            or "runtimeWorkspaceRoots" in thread and thread["runtimeWorkspaceRoots"] != ["/workspace"]):
        raise ValueError("sandbox_policy_mismatch")
    if sandbox == "danger-full-access":
        valid = policy == {"type": "dangerFullAccess"}
    elif sandbox == "read-only":
        valid = (set(policy) == {"type", "networkAccess"} and policy.get("type") == "readOnly"
                 and policy.get("networkAccess") is False)
    else:
        roots = policy.get("writableRoots")
        valid = (set(policy) == {"type", "writableRoots", "networkAccess", "excludeTmpdirEnvVar", "excludeSlashTmp"}
                 and policy.get("type") == "workspaceWrite" and policy.get("networkAccess") is False
                 and isinstance(roots, list) and all(root == "/workspace" for root in roots)
                 and isinstance(policy.get("excludeTmpdirEnvVar"), bool)
                 and isinstance(policy.get("excludeSlashTmp"), bool))
    if not valid:
        raise ValueError("sandbox_policy_mismatch")


def validate_mode(mode: str) -> bool:
    """既知の mode だけを受け、部屋の cell なら True。未知の pin は従来どおり拒否する。"""
    from .agent_spec import admitted_execution_cell, runtime_for

    cell = admitted_execution_cell("codex", mode)
    if cell is None:
        raise ValueError("invalid_codex_execution_mode")
    return runtime_for(cell.surface) == "room"


def validate_workspace(workspace: Path | None, *, device: int | None = None, inode: int | None = None) -> Path:
    """Only one canonical, already-admitted non-root workspace may cross the mount boundary."""
    if (not isinstance(workspace, Path) or not workspace.is_absolute() or workspace == Path("/")
            or workspace.resolve(strict=True) != workspace or workspace.is_symlink() or not workspace.is_dir()
            or any(c in str(workspace) for c in ("\x00", "\r", "\n"))):
        raise ValueError("invalid_codex_workspace")
    info = workspace.stat()
    if ((device is not None and info.st_dev != device) or (inode is not None and info.st_ino != inode)):
        raise ValueError("invalid_codex_workspace")
    return workspace


def validate_builtin_tools(tools: object) -> None:
    """Accept only the reviewed direct builtin surface; never namespaces or external tools.

    Exact CLI bytes are pinned before launch. Schema shape/keys are also checked here,
    so a changed provider tool catalog fails closed rather than silently adding tools.
    Descriptions are SDK-owned text and grant no authority.
    """
    if not isinstance(tools, list) or len(tools) != 3:
        raise ValueError("egress_denied")
    seen = set()
    fields = {
        "exec_command": {"cmd", "workdir", "tty", "yield_time_ms", "max_output_tokens", "shell", "login",
                         "sandbox_permissions", "justification", "prefix_rule"},
        "write_stdin": {"session_id", "chars", "yield_time_ms", "max_output_tokens"},
    }
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("name") not in HOST_TOOLS or tool["name"] in seen:
            raise ValueError("egress_denied")
        name = tool["name"]
        seen.add(name)
        if name == "apply_patch":
            fmt = tool.get("format")
            if (tool.get("type") != "custom" or set(tool) - {"type", "name", "description", "format"}
                    or not isinstance(fmt, dict) or set(fmt) != {"type", "syntax", "definition"}
                    or fmt.get("type") != "grammar" or fmt.get("syntax") != "lark"
                    or not isinstance(fmt.get("definition"), str)
                    or hashlib.sha256(fmt["definition"].encode()).hexdigest() != PATCH_GRAMMAR_SHA256):
                raise ValueError("egress_denied")
        else:
            schema = tool.get("parameters")
            required = ["cmd"] if name == "exec_command" else ["session_id"]
            if (tool.get("type") != "function" or set(tool) - {"type", "name", "description", "parameters", "strict"}
                    or tool.get("strict", False) is not False or not isinstance(schema, dict)
                    or set(schema) - {"type", "properties", "required", "additionalProperties"}
                    or schema.get("type") != "object" or schema.get("additionalProperties") is not False
                    or schema.get("required") != required or not isinstance(schema.get("properties"), dict)
                    or not set(required).issubset(schema["properties"])
                    or set(schema["properties"]) - fields[name]):
                raise ValueError("egress_denied")
