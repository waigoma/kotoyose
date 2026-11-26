"""Validation and advisory comparison of the workspace policy advertised by a child."""

from collections.abc import Mapping
from pathlib import PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, TypedDict

from .child_registry import child_on_name
from .child_windows import strip_extended_prefix
from .provider_spec import CODEX_CHILD_DRIVER, CODEX_SANDBOX_CAP

if TYPE_CHECKING:
    from .config import AgentRoute, ModelPreset

_MAX_ROOTS = 64
_MAX_ROOT_BYTES = 4096
_MAX_TOTAL_ROOT_BYTES = 16 * 1024


class WorkspacePolicyReport(TypedDict):
    """The two owner-controlled policy fields shared with the host for display."""

    workspace_roots: list[str]
    codex_sandbox_max: str


def _absolute_path(value: str) -> PurePosixPath | PureWindowsPath | None:
    windows = PureWindowsPath(value)
    if windows.is_absolute():
        conventional = PureWindowsPath(strip_extended_prefix(value))
        return conventional if conventional.is_absolute() else windows
    posix = PurePosixPath(value)
    return posix if posix.is_absolute() else None


def parse_workspace_policy_report(value: object) -> WorkspacePolicyReport | None:
    """Accept a bounded report atomically. Missing or invalid metadata leaves hello usable."""
    if not isinstance(value, dict) or value.keys() != {"workspace_roots", "codex_sandbox_max"}:
        return None
    roots = value["workspace_roots"]
    sandbox = value["codex_sandbox_max"]
    if not isinstance(roots, list) or len(roots) > _MAX_ROOTS or sandbox not in CODEX_SANDBOX_CAP.order:
        return None
    total = 0
    for root in roots:
        if not isinstance(root, str) or len(root) > _MAX_ROOT_BYTES:
            return None
        try:
            size = len(root.encode("utf-8"))
        except UnicodeEncodeError:
            return None
        total += size
        if size > _MAX_ROOT_BYTES or total > _MAX_TOTAL_ROOT_BYTES:
            return None
        if any(ord(char) < 32 or ord(char) == 127 for char in root) or _absolute_path(root) is None:
            return None
    return {"workspace_roots": list(roots), "codex_sandbox_max": sandbox}


def _comparable_paths(roots: list[str]) -> list[PurePosixPath | PureWindowsPath]:
    # Resolving a parent component could depend on symlinks that only the child can see.
    return [path for root in roots if (path := _absolute_path(root)) is not None and ".." not in path.parts]


def workspace_alias_warnings(
    child: str,
    report: WorkspacePolicyReport,
    models: Mapping[str, "ModelPreset"],
    routes: Mapping[str, "AgentRoute"],
) -> list[dict[str, str]]:
    """Return display-only warnings for aliases targeting this child's reported policy."""
    workspace_roots = report["workspace_roots"]
    workspace_paths = _comparable_paths(workspace_roots)
    warnings = []
    for name, preset in models.items():
        if child_on_name(preset.on) != child:
            continue
        roots = preset.cwd_allowed_roots or []
        if preset.allow_cwd_override and roots and not any(
            type(requested) is type(workspace) and requested.is_relative_to(workspace)
            for requested in _comparable_paths(roots)
            for workspace in workspace_paths
        ):
            folders = ", ".join(workspace_roots) or "no folders"
            warnings.append({
                "alias": name,
                "message": f"Alias {name} allows {', '.join(roots)} but child {child}'s workspace has {folders}.",
            })
        route = routes.get(preset.route) if preset.route else None
        is_codex = route.driver == CODEX_CHILD_DRIVER if route else preset.provider == "codex"
        if is_codex and preset.codex.get("sandbox") == "workspace-write" and report["codex_sandbox_max"] == "read-only":
            warnings.append({
                "alias": name,
                "message": f"Alias {name} requests Codex sandbox workspace-write but child {child}'s limit is read-only.",
            })
    return warnings
