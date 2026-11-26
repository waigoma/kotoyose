"""Alias skill prefixes at the host CLI and child turn boundaries."""

import json
import re
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kotoyose import child_turn, provider_data_runtime, session
from kotoyose.child_protocol import PROMPT_PREFIX_SKILLS_FEATURE, REPOSITORY_SKILLS_FEATURE, SKILL_FOLDERS_FEATURE, decode_frame, encode_frame
from kotoyose.child_sdk_skills import MAX_SKILL_BYTES, MAX_SKILLS, MAX_SKILLS_BYTES, MAX_TURN_FILE_BYTES
from kotoyose.config import HarnessConfig
from kotoyose.provider_data_child import approve, use_approved
from kotoyose.runtime import acp, child, print_cli, skill_resources
from kotoyose.runtime.base import RuntimeTurnError, SDKConnectionError
from kotoyose.runtime.child import build_turn_payload
from tests.test_acp import FAKE_ACP, seen
from tests.test_provider_data import _GROK_DECLARATION, _child_home, _declaration, _mock_landlock, _offer


_PREFIX = "## Skill: review\n\n要約\n\n## Skill: demo\n\n本文"
_INSTRUCTIONS = f"{_PREFIX}\n\n先\n\n後"
_TURN = f"{_INSTRUCTIONS}\n\nquestion"


@pytest.fixture
def cli_alias(tmp_path, monkeypatch):
    home = _child_home(tmp_path / "child")
    login = tmp_path / "login"
    login.mkdir()
    monkeypatch.setenv("HOME", str(login))
    plugin = tmp_path / "plugin"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin/plugin.json").write_text('{"name":"fixture"}')
    folder = plugin / "skills/demo"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_bytes("---\r\nname: demo\r\ndescription: Trigger\r\n---\r\n本文\r\n".encode())
    (folder / "source").mkdir()
    (folder / "source/index.html").write_bytes(b"fixture")
    (folder / "source/index.html").chmod(0o755)
    fallback = tmp_path / "fallback.md"
    fallback.write_text("要約\n", encoding="utf-8")
    first, second = tmp_path / "first.txt", tmp_path / "second.txt"
    first.write_bytes("先\r\n".encode())
    second.write_text("後", encoding="utf-8")

    fake = tmp_path / "fake-cli"
    fake.write_text(f"#!{sys.executable}\n" + f"""
import json, runpy, sys
if sys.argv[1:2] == ["acp"] or ("agent" in sys.argv[1:] and "stdio" in sys.argv[1:]):
    sys.argv[1:] = []
    runpy.run_path({str(FAKE_ACP)!r}, run_name="__main__")
elif not sys.argv[1:]:
    sys.stdout.write(sys.stdin.read())
else:
    print(json.dumps({{"type": "result", "subtype": "success", "result": sys.argv[-1]}}))
""")
    fake.chmod(0o700)
    monkeypatch.setattr(acp, "resolve_print_cli_binary", lambda *_: fake)
    monkeypatch.setattr(print_cli, "resolve_print_cli_binary", lambda *_: fake)
    monkeypatch.setattr(provider_data_runtime, "managed_binary", lambda *_: fake)

    def config(driver, declaration=None):
        provider = declaration.id if declaration else ("grok" if driver == "cli.acp" else "cursor")
        on = "child:box" if declaration else "local"
        connection = {"kind": "subscription", "provider": provider, "profile": provider, "on": on, "data_policy": "no_training"}
        if not declaration:
            connection["config_dir"] = str(login)
        return HarnessConfig.model_validate({
            "connections": {"c": connection},
            "routes": {"r": {"driver": driver, "profile": provider, "connection": "c", "model": "model-x"}},
            "models": {"dev": {"on": on, "route": "r", "skill_refs": ["summary", "example"], "prompt_file_refs": ["first", "second"]}},
            "skill_definitions": {
                "summary": {"plugin": str(plugin), "skill": "review", "fallback_prompt": str(fallback)},
                "example": {"plugin": str(plugin), "skill": "demo"},
            },
            "prompt_files": {"first": {"path": str(first)}, "second": {"path": str(second)}},
        })

    return home, config


def _payload(config, *, features=(PROMPT_PREFIX_SKILLS_FEATURE,), prompt="question"):
    return decode_frame(encode_frame(1, "turn", build_turn_payload(
        alias="dev", config=config, preset=config.models["dev"], conversation_key=None, cwd=None, prompt=prompt,
        live=SimpleNamespace(name="box", platform=provider_data_runtime.platform_key(), features=features),
    ))).payload


def _acp_prompt(runtime):
    return next(row["params"]["prompt"][0]["text"] for row in seen(runtime.cwd) if row.get("method") == "session/prompt")


def _folder_from_prompt(prompt):
    match = re.search(r"This skill's files are at (.+)\. Use your file-reading tool .+ Files: (.+)", prompt)
    assert match is not None
    assert json.loads(match[2]) == ["SKILL.md", "source/index.html"]
    assert prompt.replace("\n\n" + match[0], "") == _TURN
    return Path(json.loads(match[1]))


def _check_attachment(path):
    attachment = path / "source/index.html"
    assert attachment.read_bytes() == b"fixture"
    assert attachment.stat().st_mode & 0o111 == 0


async def test_same_alias_prefix_reaches_host_acp_host_print_and_child_acp(cli_alias):
    home, config = cli_alias
    cfg = config("cli.acp")
    runtime = acp.build_acp_session(cfg.models["dev"], None, None, None, config=cfg)
    try:
        await runtime.ask("question")
        host_path = _folder_from_prompt(_acp_prompt(runtime))
        _check_attachment(host_path)
        assert not host_path.is_relative_to(runtime.cwd)
    finally:
        await runtime.disconnect()
    assert not host_path.exists()
    cfg = config("cli.print")
    runtime = print_cli.build_print_cli_session(cfg.models["dev"], None, None, None, config=cfg)
    execute = runtime.worker.execute

    async def inspect_while_live(job, *args, **kwargs):
        path = _folder_from_prompt(job.instructions + "\n\nquestion")
        _check_attachment(path)
        assert not path.is_relative_to(job.workspace)
        return await execute(job, *args, **kwargs)

    runtime.worker.execute = inspect_while_live
    print_path = _folder_from_prompt((await runtime.ask("question")).text)
    assert not print_path.exists()

    row = _offer(_declaration(provider_data_runtime.platform_key()), home)
    approve(home, row.id, sha256=row.sha256)
    with use_approved(home):
        payload = _payload(config("cli.acp", row), features=(SKILL_FOLDERS_FEATURE,))
        assert payload["skill_format"] == "prompt_prefix"
        assert payload["skills"] == [{"name": "review", "text": "要約"}, {"name": "demo", "text": "本文"}]
        prepared = child_turn._prepare(home, payload)
        runtime = await child_turn._build_runtime(prepared)
        try:
            await runtime.ask("question")
            child_path = _folder_from_prompt(_acp_prompt(runtime))
            _check_attachment(child_path)
            assert child_path != host_path
            assert not prepared["config"].skill_definitions
        finally:
            await runtime.disconnect()
            prepared["cleanup"]()
    assert not child_path.exists()


@pytest.mark.skipif(provider_data_runtime.platform_key() != "linux-x64", reason="native Grok layout is verified on Linux x64")
async def test_grok_child_places_delivered_folders_in_its_native_home(cli_alias, tmp_path, monkeypatch):
    home, config = cli_alias
    _mock_landlock(monkeypatch)
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(provider_data_runtime, "_real_grok_home", lambda: state)
    launch = acp.AcpClient.spawn
    captured = {}

    async def spawn(argv, *, cwd, env):
        captured.update(env)
        return await launch(argv, cwd=cwd, env=env)

    monkeypatch.setattr(acp.AcpClient, "spawn", spawn)
    row = _offer(_GROK_DECLARATION.read_bytes(), home)
    policy_file = home / "policy.json"
    policy = json.loads(policy_file.read_text())
    policy_file.write_text(json.dumps({**policy, "adapters": [row.id]}))
    approve(home, row.id, sha256=row.sha256)
    with use_approved(home):
        cfg = config("cli.acp", row)
        cfg.models["dev"].repository_skills = "include"
        payload = _payload(cfg, features=(SKILL_FOLDERS_FEATURE, REPOSITORY_SKILLS_FEATURE))
        prepared = child_turn._prepare(home, payload)
        runtime = await child_turn._build_runtime(prepared)
        try:
            await runtime.ask("question")
            private = Path(captured["HOME"])
            native = private / ".agents/skills/demo"
            _check_attachment(native)
            assert (native / "SKILL.md").read_bytes().startswith(b"---\r\n")
            assert captured["GROK_HOME"] == str(state)
            assert captured["GROK_CLAUDE_SKILLS_ENABLED"] == "1"
            assert _acp_prompt(runtime) == "## Skill: review\n\n要約\n\n先\n\n後\n\nquestion"
        finally:
            await runtime.disconnect()
            prepared["cleanup"]()
    assert not private.exists()


@pytest.mark.parametrize("folder_support", [False, True])
async def test_data_print_uses_child_local_login_and_stdin(cli_alias, folder_support):
    home, config = cli_alias
    document = json.loads(_declaration(provider_data_runtime.platform_key()))
    del document["acp"]
    document["driver"]["id"] = "cli.print"
    document["print"] = {"argv": [], "placeholders": ["prompt"], "prompt": "stdin"}
    row = _offer(json.dumps(document).encode(), home)
    approve(home, row.id, sha256=row.sha256)
    features = (PROMPT_PREFIX_SKILLS_FEATURE,)
    if folder_support:
        features += (SKILL_FOLDERS_FEATURE,)
    with use_approved(home):
        payload = _payload(config("cli.print", row), features=features)
        prepared = child_turn._prepare(home, payload)
        try:
            runtime = await child_turn._build_runtime(prepared)
            assert runtime.login_home == Path.home()
            text = (await runtime.ask("question")).text
            if folder_support:
                path = _folder_from_prompt(text)
                _check_attachment(path)
            else:
                assert text == _TURN
        finally:
            prepared["cleanup"]()

    if folder_support:
        assert not path.exists()


async def test_host_acp_reuses_a_session_until_skill_text_changes(cli_alias, monkeypatch):
    _, config = cli_alias
    cfg = config("cli.acp")
    for name in ("sessions", "session_last_used", "session_cache_identities"):
        monkeypatch.setattr(session, name, {})
    lookup = AsyncMock(return_value=None)
    monkeypatch.setattr(session, "lookup_runtime_session", lookup)
    first, _ = await session.get_or_resume_client("conversation", "dev", config_snapshot=cfg)
    try:
        same, _ = await session.get_or_resume_client("conversation", "dev", config_snapshot=cfg)
        assert same is first
        assert first.instructions == _INSTRUCTIONS
        Path(cfg.skill_definitions["summary"].fallback_prompt).write_text("新版", encoding="utf-8")
        current, _ = await session.get_or_resume_client("conversation", "dev", config_snapshot=cfg)
        try:
            assert current is not first
            assert current.instructions == _INSTRUCTIONS.replace("要約", "新版")
            lookup.assert_awaited_once()
        finally:
            await current.disconnect()
    finally:
        await first.disconnect()


async def test_child_proxy_refreshes_when_alias_skill_order_changes(cli_alias, monkeypatch):
    home, config = cli_alias
    row = _offer(_declaration(provider_data_runtime.platform_key()), home)
    approve(home, row.id, sha256=row.sha256)
    for name in ("sessions", "session_last_used", "session_cache_identities"):
        monkeypatch.setattr(session, name, {})
    monkeypatch.setattr(session, "require_child_link", lambda *_: None)
    with use_approved(home):
        cfg = config("cli.acp", row)
        first, _ = await session.get_or_resume_client("conversation", "dev", config_snapshot=cfg)
        changed = cfg.model_copy(deep=True)
        changed.models["dev"].skill_refs.reverse()
        current, _ = await session.get_or_resume_client("conversation", "dev", config_snapshot=changed)
        assert current is not first
        assert current.preset.skill_refs == ["example", "summary"]
        await current.disconnect()


async def test_child_proxy_refreshes_when_attachment_contents_or_path_change(cli_alias, monkeypatch):
    home, config = cli_alias
    row = _offer(_declaration(provider_data_runtime.platform_key()), home)
    approve(home, row.id, sha256=row.sha256)
    for name in ("sessions", "session_last_used", "session_cache_identities"):
        monkeypatch.setattr(session, name, {})
    monkeypatch.setattr(session, "require_child_link", lambda *_: None)
    live = SimpleNamespace(features={SKILL_FOLDERS_FEATURE})
    monkeypatch.setattr(child, "active_child_hub", lambda: SimpleNamespace(live_for=lambda _: live))
    with use_approved(home):
        cfg = config("cli.acp", row)
        asset = Path(cfg.skill_definitions["example"].plugin) / "skills/demo/source/index.html"
        first, _ = await session.get_or_resume_client("conversation", "dev", config_snapshot=cfg)
        same, _ = await session.get_or_resume_client("conversation", "dev", config_snapshot=cfg)
        assert same is first
        asset.write_bytes(b"changed")
        edited, _ = await session.get_or_resume_client("conversation", "dev", config_snapshot=cfg)
        assert edited is not first
        asset.rename(asset.with_name("moved.html"))
        renamed, _ = await session.get_or_resume_client("conversation", "dev", config_snapshot=cfg)
        assert renamed is not edited
        await renamed.disconnect()


def test_prefix_only_link_does_not_read_attachment_folders(cli_alias, monkeypatch):
    home, config = cli_alias
    row = _offer(_declaration(provider_data_runtime.platform_key()), home)
    approve(home, row.id, sha256=row.sha256)
    live = SimpleNamespace(features={PROMPT_PREFIX_SKILLS_FEATURE})
    monkeypatch.setattr(child, "active_child_hub", lambda: SimpleNamespace(live_for=lambda _: live))
    monkeypatch.setattr(skill_resources, "read_skill_folder", lambda *_a, **_kw: pytest.fail("read an attachment folder"))
    with use_approved(home):
        cfg = config("cli.acp", row)
        assert child.child_cli_binding(cfg, cfg.preset_for("dev"), None, False)
        payload = _payload(cfg)
    assert payload["skill_format"] == "prompt_prefix"
    assert payload["skills"] == [{"name": "review", "text": "要約"}, {"name": "demo", "text": "本文"}]
    assert "skill_folders" not in payload


@pytest.mark.parametrize("case, error", [
    ("name", "invalid child skill name"), ("reserved", "invalid child skill name"),
    ("duplicate", "duplicate child skill name"), ("count", f"up to {MAX_SKILLS} skills"),
    ("single", f"exceeds {MAX_SKILL_BYTES} bytes"), ("total", f"exceed {MAX_SKILLS_BYTES} bytes"),
    ("combined", f"exceed {MAX_TURN_FILE_BYTES} bytes"),
])
def test_prefix_caps_and_names_are_checked_on_both_sides(cli_alias, case, error):
    home, config = cli_alias
    row = _offer(_declaration(provider_data_runtime.platform_key()), home)
    approve(home, row.id, sha256=row.sha256)
    with use_approved(home):
        child_cfg = config("cli.acp", row)
        host_cfg = config("cli.print")
        payload = _payload(child_cfg)
        skills = payload["skills"]
        match case:
            case "name" | "reserved" | "duplicate":
                skills[0]["name"] = {"name": "../bad", "reserved": "NUL", "duplicate": "DEMO"}[case]
            case "count":
                skills = [{"name": f"s{i}", "text": "x"} for i in range(MAX_SKILLS + 1)]
            case "single":
                skills[0]["text"] = "日" * (MAX_SKILL_BYTES // 3 + 1)
            case "total":
                skills = [{"name": f"s{i}", "text": "x" * MAX_SKILL_BYTES} for i in range(MAX_SKILLS_BYTES // MAX_SKILL_BYTES + 1)]
            case "combined":
                payload["prompts"] = ["x" * MAX_TURN_FILE_BYTES]
                Path(child_cfg.prompt_files["first"].path).write_text(payload["prompts"][0])
                child_cfg.models["dev"].prompt_file_refs = host_cfg.models["dev"].prompt_file_refs = ["first"]
        for cfg in (host_cfg, child_cfg):
            definition = cfg.skill_definitions["summary"]
            cfg.models["dev"].skill_refs = []
            for i, skill in enumerate(skills):
                path = home / f"fallback-{i}.md"
                path.write_text(skill["text"], encoding="utf-8")
                ref = f"ref-{i}"
                cfg.skill_definitions[ref] = definition.model_copy(update={"skill": skill["name"], "fallback_prompt": str(path)})
                cfg.models["dev"].skill_refs.append(ref)
        with pytest.raises(SDKConnectionError, match=error):
            print_cli.build_print_cli_session(host_cfg.models["dev"], None, None, None, config=host_cfg)
        with pytest.raises(ValueError, match=error):
            _payload(child_cfg)
        with pytest.raises(child_turn.ChildTurnError, match=error):
            child_turn._prepare(home, {**payload, "skills": skills})


async def test_old_link_keeps_legacy_data_turn_and_cli_enforces_composed_limit(cli_alias):
    home, config = cli_alias
    row = _offer(_declaration(provider_data_runtime.platform_key()), home)
    approve(home, row.id, sha256=row.sha256)
    with use_approved(home):
        cfg = config("cli.acp", row)
        old = _payload(cfg, features=())
        assert "skill_format" not in old and old["skills"] == []
        old["skills"] = [{"name": "summary", "text": "old host's fallback"}]
        prepared = child_turn._prepare(home, old)
        try:
            runtime = await child_turn._build_runtime(prepared)
            assert runtime.instructions == "先\n\n後"
        finally:
            prepared["cleanup"]()
        with pytest.raises(ValueError, match="child turn frame exceeds"):
            _payload(cfg, prompt="\x01" * (192 * 1024))
    for driver, builder in [("cli.acp", acp.build_acp_session), ("cli.print", print_cli.build_print_cli_session)]:
        cfg = config(driver)
        runtime = builder(cfg.models["dev"], None, None, None, config=cfg)
        try:
            prompt = "x" * runtime.profile.max_prompt_bytes
            with pytest.raises(RuntimeTurnError) as error:
                await runtime.ask(prompt)
            assert error.value.terminal_reason == "unsupported_input"
        finally:
            await runtime.disconnect()
