"""Exercise repository discovery and the verified Grok session layout offline."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
from textwrap import dedent
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kotoyose.acp_turn import AcpPermissionPolicy
from kotoyose.child_protocol import REPOSITORY_SKILLS_FEATURE
from kotoyose.cli_install_spec import platform_key
from kotoyose.config import HarnessConfig, build_options
from kotoyose.grok_skills import grok_skill_env, uses_grok_native_skills
from kotoyose.print_cli_agent import PrintCliJob, PrintCliWorker
from kotoyose.print_cli_grok_profile import grok_profile
from kotoyose.provider_data import parse_declaration
from kotoyose.runtime import acp
from kotoyose.runtime.child import build_turn_payload
from kotoyose.runtime.skill_resources import CliInstructions
from kotoyose.skill_folders import SkillFolder

ROOT = Path(__file__).resolve().parents[1]
GROK = ROOT / ".grok-bin/grok"
EXAMPLE = ROOT / "examples/provider-declarations/grok-build.json"


def instructions(name):
    body = f"---\nname: {name}\ndescription: Fixture skill\n---\n\nUse the attachment.\n"
    return CliInstructions(
        ["system instructions"],
        [{"name": name, "text": "native body"}, {"name": "legacy", "text": "fallback body"}],
        (SkillFolder(name, {"SKILL.md": body.encode(), "source/example.txt": b"attachment"}),),
    )


@pytest.mark.parametrize(
    ("mode", "sources", "bare"),
    [(None, ["user", "local"], True), ("include", ["user", "local", "project"], False), ("exclude", ["user"], True)],
)
def test_claude_repository_skills_modes(mode, sources, bare):
    config = HarnessConfig(models={"alias": {"repository_skills": mode, "setting_sources": ["user", "local"]}})
    options = build_options("alias", config=config)
    assert options.setting_sources == sources
    assert ("bare" in options.extra_args) is bare


def test_codex_exclude_is_rejected_and_include_keeps_project_roots():
    assert HarnessConfig(models={"alias": {"provider": "codex", "repository_skills": "include"}}).models["alias"].repository_skills == "include"
    with pytest.raises(ValueError, match=r"alias.repository_skills: exclude.*Codex app-server 0.170.0"):
        HarnessConfig(models={"alias": {"provider": "codex", "repository_skills": "exclude"}})


def test_grok_exclude_fails_closed_when_real_grok_home_is_required(tmp_path):
    with pytest.raises(ValueError, match=r"alias.repository_skills: exclude.*native project skill discovery"):
        HarnessConfig.model_validate({
            "connections": {"grok": {
                "provider": "grok", "profile": "grok", "kind": "subscription",
                "config_dir": str(tmp_path), "data_policy": "no_training",
            }},
            "routes": {"grok": {"driver": "cli.acp", "profile": "grok", "connection": "grok", "model": "fake"}},
            "models": {"alias": {"route": "grok", "repository_skills": "exclude"}},
        })


def test_explicit_repository_skills_requires_an_upgraded_child():
    config = HarnessConfig(models={"alias": {"repository_skills": "exclude"}})
    args = dict(alias="alias", config=config, preset=config.models["alias"], conversation_key="one", cwd="/tmp", prompt="hello")
    with pytest.raises(ValueError, match=REPOSITORY_SKILLS_FEATURE):
        build_turn_payload(**args, live=SimpleNamespace(name="old", features=()))
    frame = build_turn_payload(**args, live=SimpleNamespace(name="new", features=(REPOSITORY_SKILLS_FEATURE,)))
    assert frame["preset"]["repository_skills"] == "exclude"
    frame = build_turn_payload(**(args | {"preset": config.models["alias"].model_copy(update={"repository_skills": None})}))
    assert "repository_skills" not in frame["preset"]


@pytest.mark.parametrize(("key", "value"), [("layout", "../paths"), ("digest", "aa" * 32), ("version", "1.0.90")])
def test_declaration_allows_only_the_verified_native_layout(key, value):
    body = json.loads(EXAMPLE.read_bytes())
    if key == "layout":
        body["native_skill_layout"] = value
    elif key == "digest":
        body["platforms"]["linux-x64"]["install"]["sha256"] = value
    else:
        for platform in body["platforms"].values():
            platform["install"]["version"] = value
    with pytest.raises(ValueError, match="unsupported native_skill_layout"):
        parse_declaration(json.dumps(body).encode())
    del body["native_skill_layout"]
    assert parse_declaration(json.dumps(body).encode()).profile().native_skill_layout is None


async def test_native_acp_sessions_have_separate_homes_in_the_same_workspace(tmp_path, monkeypatch):
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    login = tmp_path / "login"
    login.mkdir()
    opened = []
    monkeypatch.setattr(acp, "uses_grok_native_skills", lambda *_: True)

    async def spawn(argv, *, cwd, env):
        opened.append((cwd, env))
        return SimpleNamespace(
            process=SimpleNamespace(pid=None, returncode=None), session_id=env["HOME"],
            start_session=AsyncMock(return_value=False), close=AsyncMock(return_value="confirmed"),
        )

    monkeypatch.setattr(acp.AcpClient, "spawn", spawn)
    sessions = [
        acp.AcpRuntimeSession(
            grok_profile(), Path("/fake-grok"), login, cwd, "fake", 1.0, "",
            [], AcpPermissionPolicy("read", "host"), None, resources=instructions(name), repository_skills="include",
        )
        for name in ("one", "two")
    ]
    try:
        await asyncio.gather(*(session._connect() for session in sessions))
        homes = [Path(env["HOME"]) for _, env in opened]
        assert len(set(homes)) == 2
        for (work, env), home, name, session in zip(opened, homes, ("one", "two"), sessions, strict=True):
            assert work == cwd
            assert env["GROK_HOME"] == str(login)
            assert env["GROK_CLAUDE_SKILLS_ENABLED"] == env["GROK_CURSOR_SKILLS_ENABLED"] == "1"
            assert env["GROK_CLAUDE_HOOKS_ENABLED"] == env["GROK_CLAUDE_MCPS_ENABLED"] == "0"
            assert [p.name for p in (home / ".agents/skills").iterdir()] == [name]
            assert (home / ".agents/skills" / name / "source/example.txt").read_bytes() == b"attachment"
            assert session._effective_instructions == "## Skill: legacy\n\nfallback body\n\nsystem instructions"
    finally:
        await asyncio.gather(*(session.disconnect() for session in sessions))
    assert not any(home.exists() for home in homes)
    assert not list(cwd.iterdir()) and not list(login.iterdir())


async def test_grok_print_places_skills_for_the_process_lifetime(tmp_path, monkeypatch):
    monkeypatch.setattr("kotoyose.print_cli_agent.uses_grok_native_skills", lambda *_: True)
    binary = tmp_path / "fixture"
    login = tmp_path / "login"
    cwd = tmp_path / "repo"
    for directory in (login, cwd):
        directory.mkdir()
    binary.write_text(f"#!{sys.executable}\n" + dedent("""\
        import json, os, sys
        from pathlib import Path
        home = Path.home()
        assert home != Path(os.environ["GROK_HOME"])
        assert (home / ".agents/skills/demo/source/example.txt").read_bytes() == b"attachment"
        assert "native body" not in sys.argv[-1]
        assert "fallback body" in sys.argv[-1]
        print(json.dumps({"type": "text", "text": str(home)}))
        print('{"type":"end","sessionId":"fixture","stopReason":"end_turn"}')
        """))
    binary.chmod(0o700)
    job = PrintCliJob(
        profile_id="grok", cli_binary=binary, model="fake", workspace=cwd,
        login_home=login, surface="read", instructions="", prompt="hello", output_contract="text",
        timeout_seconds=5, grace_seconds=0.2, output_limit_bytes=1024, memory_bytes=256 * 1024**2, pids_limit=8, cpus=1,
        alias_skills=instructions("demo"), repository_skills="include",
    )
    result = await PrintCliWorker(chat=True).execute(job, asyncio.Event(), on_process_started=AsyncMock(), on_event=AsyncMock())
    assert result.status == "succeeded"
    assert not Path(result.content.decode()).exists()
    assert not list(cwd.iterdir()) and not list(login.iterdir())


@pytest.mark.skipif(platform_key() != "linux-x64" or not GROK.is_file(), reason="pinned Linux Grok test binary is absent")
def test_pinned_grok_inspect_lists_native_and_trusted_repository_skills(tmp_path):
    row = parse_declaration(EXAMPLE.read_bytes())
    profile = row.profile()
    assert uses_grok_native_skills(grok_profile(), GROK)
    assert uses_grok_native_skills(profile, GROK)
    cwd = tmp_path / "repo"
    state = tmp_path / "grok-state"
    state.mkdir()
    (cwd / ".git/objects").mkdir(parents=True)
    (cwd / ".git/refs").mkdir()
    (cwd / ".git/HEAD").write_text("ref: refs/heads/main\n")
    for vendor in (".grok", ".agents", ".claude", ".cursor", ".codex"):
        name = f"repo-{vendor[1:]}"
        folder = cwd / vendor / "skills" / name
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: Fixture skill\n---\nFixture\n")

    def inspect(home, mode, **overrides):
        env = {"PATH": "/usr/bin:/bin", "GROK_HOME": str(state)}
        grok_skill_env(env, home, mode, profile)
        result = subprocess.run([GROK, "inspect", "--json"], cwd=cwd, env=env | overrides, capture_output=True, check=True, timeout=15)
        report = json.loads(result.stdout)
        assert report["grokVersion"] == "1.0.89"
        assert report["hooks"] == report["plugins"] == report["mcpServers"] == []
        return report, {skill["name"]: skill["source"]["path"] for skill in report["skills"] if not skill.get("disabled")}

    homes = [tmp_path / name for name in ("one", "two")]
    for home in homes:
        home.mkdir(mode=0o700)
        instructions(home.name).place_grok(home)
    with ThreadPoolExecutor(max_workers=2) as workers:
        runs = [workers.submit(inspect, home, None) for home in homes]
        for home, run in zip(homes, runs, strict=True):
            report, skills = run.result()
            assert report["projectTrusted"] is False
            assert skills == {home.name: str(home / ".agents/skills" / home.name / "SKILL.md")}
    (state / "trusted_folders.toml").write_text(f"[folders.{json.dumps(str(cwd))}]\ntrusted = true\n")
    report, skills = inspect(homes[0], None)
    assert report["projectTrusted"] is True
    assert set(skills) == {"one", "repo-grok", "repo-agents"}
    _, ignored = inspect(homes[0], None, GROK_CONFIG=json.dumps({"skills": {"ignore": [str(cwd)]}}))
    assert ignored == skills
    report, skills = inspect(homes[1], "include")
    assert report["projectTrusted"] is True
    assert set(skills) == {"two", "repo-grok", "repo-agents", "repo-claude", "repo-cursor"}
    assert {
        (cell["vendor"], cell["surface"]) for cell in report["externalCompat"]["cells"]
        if cell["enabled"] and cell["surface"] != "sessions"
    } == {("claude", "skills"), ("cursor", "skills")}
