import pytest

from kotoyose.child_workspace import parse_workspace_policy_report, workspace_alias_warnings
from kotoyose.config import AgentRoute, ModelPreset


def report(roots, sandbox="read-only"):
    return {"workspace_roots": roots, "codex_sandbox_max": sandbox}


@pytest.mark.parametrize("value", [
    report([]), report(["/", "/tmp/with space", r"C:\work", r"\\server\share"]),
    report([f"/root/{i}" for i in range(64)], "workspace-write"),
    report(["/" + "a" * 4095] * 4, "danger-full-access"),
])
def test_workspace_report_accepts_absolute_paths_and_values_at_the_limits(value):
    assert parse_workspace_policy_report(value) == value


@pytest.mark.parametrize("value", [
    None, [], {"workspace_roots": []}, {**report([]), "mcp": {}},
    report("/tmp"), report([42]), report(["relative"]), report([r"C:relative"]), report([r"\rooted"]),
    report(["/valid", "relative"]), report(["/tmp\x00bad"]), report(["/tmp\x7f"]), report(["/tmp\ud800"]),
    report([], "full-access"), report([], ["read-only"]),
    report([f"/root/{i}" for i in range(65)]), report(["/" + "a" * 4096]),
    report(["/" + "é" * 2048]), report(["/" + "a" * 4095] * 4 + ["/"]),
])
def test_workspace_report_ignores_malformed_or_oversized_metadata(value):
    assert parse_workspace_policy_report(value) is None


@pytest.mark.parametrize(("workspace", "allowed", "warns"), [
    ([r"C:\programming\tmp\work"], [r"D:\Programming\ai\tmp"], True),
    ([r"C:\Programming\tmp\work"], ["c:/programming/TMP/work/"], False),
    ([r"C:\Programming\tmp\work"], [r"D:\elsewhere", r"c:/programming\tmp//WORK\nested"], False),
    ([r"C:\work"], [r"C:\workbench"], True),
    ([r"C:\work\nested"], [r"C:\work"], True),
    ([r"\\Server\Share\Work"], ["//server/share/work/nested"], False),
    ([r"\\?\UNC\Server\Share\Work"], [r"\\server\share\work"], False),
    ([r"\\?\C:\work"], ["c:/work/nested"], False),
    (["/work"], ["/Work/nested"], True),
    (["/work"], ["/work/nested"], False),
    (["/work"], ["/work/../outside"], True),
    (["/work"], [r"C:\work"], True),
    ([], ["/work"], True),
])
def test_workspace_warning_requires_an_allowed_root_inside_the_child_workspace(workspace, allowed, warns):
    alias = ModelPreset(on="child:win-lut", allow_cwd_override=True, cwd_allowed_roots=allowed)
    warnings = workspace_alias_warnings("win-lut", report(workspace), {"dev": alias}, {})
    if warns:
        assert warnings == [{
            "alias": "dev",
            "message": f"Alias dev allows {', '.join(allowed)} but child win-lut's workspace has "
                       f"{', '.join(workspace) or 'no folders'}.",
        }]
    else:
        assert warnings == []


def test_sandbox_warning_uses_the_route_and_only_applies_to_the_target_child():
    routes = {
        "codex": AgentRoute(driver="codex.app-server", model="fixture", connection="subscription"),
        "claude": AgentRoute(driver="claude.agent-sdk", model="fixture", connection="claude-sub"),
    }
    models = {
        "dev": ModelPreset(on="child:win-lut", route="codex", codex={"sandbox": "workspace-write"}),
        "read": ModelPreset(on="child:win-lut", route="codex", codex={"sandbox": "read-only"}),
        "other": ModelPreset(on="child:other", route="codex", codex={"sandbox": "workspace-write"}),
        "local": ModelPreset(route="codex", codex={"sandbox": "workspace-write"}),
        "claude": ModelPreset(on="child:win-lut", route="claude", codex={"sandbox": "workspace-write"}),
        "override-disabled": ModelPreset(on="child:win-lut", cwd_allowed_roots=["/mismatch"]),
    }
    assert workspace_alias_warnings("win-lut", report([]), models, routes) == [{
        "alias": "dev",
        "message": "Alias dev requests Codex sandbox workspace-write but child win-lut's limit is read-only.",
    }]
    assert workspace_alias_warnings("win-lut", report([], "workspace-write"), models, routes) == []
