"""SDK alias inputs and staged skill transfer, with in-process child turns."""

import asyncio
import os
from pathlib import Path
import stat
from types import SimpleNamespace
from unittest.mock import call, patch

import pytest
from openai_codex import AsyncCodex, CodexConfig, SkillInput, TextInput
from openai_codex.generated.v2_all import ConfigReadResponse, SkillsExtraRootsSetResponse, SkillsListResponse

from kotoyose import child_turn, session, skill_folders
from kotoyose.child_protocol import CODEX_TURNS_FEATURE, SDK_SKILLS_FEATURE, SKILL_FOLDERS_FEATURE, Frame, decode_frame, encode_frame
from kotoyose.child_sdk_skills import MAX_SKILL_BYTES, MAX_SKILLS, MAX_SKILLS_BYTES, MAX_TURN_FILE_BYTES
from kotoyose.config import HarnessConfig, use_config
from kotoyose.runtime import claude_options, codex, skill_resources
from kotoyose.runtime.base import RuntimeResult, RuntimeTurnError
from kotoyose.runtime.child import build_turn_payload
from kotoyose.runtime.translation import RuntimeSkill
from tests.test_child_k3 import _host_config, _policy
from tests.test_child_k8 import _sdk_with_config
from tests.test_codex_runtime import TurnStatus, _FakeHandle, _FakeThread, _turn_completed


_FORMATS = {"claude.agent-sdk": "claude_plugin", "codex.app-server": "codex_skill_input"}
_NEW = SimpleNamespace(features={CODEX_TURNS_FEATURE, SDK_SKILLS_FEATURE})
_OLD = SimpleNamespace(features={CODEX_TURNS_FEATURE})
_FOLDERS_ONLY = SimpleNamespace(features={CODEX_TURNS_FEATURE, SKILL_FOLDERS_FEATURE})
_FOLDERS = SimpleNamespace(features={CODEX_TURNS_FEATURE, SDK_SKILLS_FEATURE, SKILL_FOLDERS_FEATURE})
_ATTACHMENT = Path("source", "index.html")
_ATTACHMENT_BYTES = b"\x00\xfffixture"
_PROMPT = "二番目\n\n最初のファイル\n行末"


@pytest.fixture
def alias_config(tmp_path, monkeypatch):
    def make(driver):
        provider = driver.split(".")[0]
        body = _host_config(tmp_path, mcp_refs=[])
        body["routes"]["dev-route"]["driver"] = driver
        body["connections"]["dev-conn"]["provider"] = provider
        if provider == "codex":
            body["connections"]["dev-conn"].pop("config_dir")
            body["routes"]["dev-route"]["model"] = "gpt-5"
        body["models"]["dev"]["prompt_file_refs"] = ["second", "brief"]
        body["models"]["dev"]["skill_refs"] = ["review", "demo"]
        body["skill_definitions"]["review"] = body["skill_definitions"]["demo"].copy()
        body["skill_definitions"]["review"]["skill"] = "z-review"
        second = tmp_path / "second.txt"
        second.write_bytes("二番目\r\n".encode())
        body["prompt_files"]["second"] = {"path": str(second)}
        Path(body["prompt_files"]["brief"]["path"]).write_bytes("最初のファイル\r行末".encode())
        plugin = Path(body["skill_definitions"]["demo"]["plugin"])
        for name in ("z-review", "demo"):
            folder = plugin / "skills" / name
            folder.mkdir(exist_ok=True)
            (folder / "SKILL.md").write_bytes(f"---\r\nname: {name}\r\ndescription: 説明\r\n---\r\n\r\n本文\r\n".encode())
            attachment = folder / _ATTACHMENT
            attachment.parent.mkdir()
            attachment.write_bytes(_ATTACHMENT_BYTES)
            attachment.chmod(0o755)
        fallback = tmp_path / "fallback.txt"
        fallback.write_text("---\nname: fallback\n---\nlegacy review\n", encoding="utf-8")
        body["skill_definitions"]["review"]["fallback_prompt"] = str(fallback)
        home = tmp_path / "child"
        _policy(home, tmp_path, adapters=[driver])
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "test-claude"))
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "test-codex"))
        launch = {"cli_path": tmp_path / "fake-claude"} if provider == "claude" else {
            "child_launch": codex.ChildCodexLaunch((str(tmp_path / "fake-codex"),), bundled=True),
        }
        monkeypatch.setattr(child_turn, "child_builder_options", lambda *_: launch)
        return HarnessConfig.model_validate(body), home, plugin

    return make


def _payload(config, root, *, live=_NEW, key=None):
    return build_turn_payload(
        alias="dev", config=config, preset=config.models["dev"], conversation_key=key, cwd=str(root), prompt="test", live=live,
    )


async def _empty_secrets(*_):
    return {}, {}


async def test_claude_sdk_receives_same_selected_skills_and_prompt(alias_config, tmp_path, monkeypatch):
    config, home, plugin = alias_config("claude.agent-sdk")
    observed = []

    class SDK:
        def __init__(self, *, options):
            observed.append(options)

        async def connect(self):
            pass

        async def disconnect(self):
            pass

    monkeypatch.setattr(session, "ClaudeSDKClient", SDK)
    monkeypatch.setattr(claude_options, "bundled_claude_cli", lambda: tmp_path / "fake-claude")
    host = claude_options.build_claude_options(
        config, config.preset_for("dev"), model_name="dev", cwd_override=str(tmp_path),
        request_headers=None, env={}, session_store=None,
    )
    local = config.model_copy(deep=True)
    local.models["dev"].on = "local"
    local.connections["dev-conn"].on = "local"
    monkeypatch.setattr(session, "resolve_preset_secrets_with_versions_async", _empty_secrets)
    with monkeypatch.context() as patches:
        patches.setattr(session, "build_options", lambda *_args, **_kwargs: host)
        host_runtime, _ = await session.get_or_resume_client(None, "dev", config_snapshot=local)
    host_stage = Path(observed.pop().plugins[0]["path"])
    try:
        assert host_stage != plugin
        for name in host.skills:
            assert (host_stage / "skills" / name / _ATTACHMENT).read_bytes() == _ATTACHMENT_BYTES
    finally:
        await host_runtime.disconnect()
    assert not host_stage.exists()

    payload = _payload(config, tmp_path, live=_FOLDERS)
    assert payload["skill_format"] == "claude_plugin"
    assert str(plugin) not in encode_frame(1, "turn", payload)
    prepared = child_turn._prepare(home, decode_frame(encode_frame(1, "turn", payload)).payload)
    try:
        runtime = await child_turn._build_runtime(prepared)
        try:
            [options] = observed
            assert options.system_prompt == host.system_prompt
            assert options.system_prompt["append"] == _PROMPT
            assert options.skills == host.skills == ["z-review", "demo"]
            [local_plugin] = options.plugins
            staged = Path(local_plugin["path"])
            if child_turn.sys.platform == "linux":
                assert stat.S_IMODE(staged.parent.stat().st_mode) == 0o700
            for name in host.skills:
                relative = Path("skills", name, "SKILL.md")
                assert (staged / relative).read_bytes() == (plugin / relative).read_bytes()
                attachment = (staged / relative).parent / _ATTACHMENT
                assert attachment.read_bytes() == _ATTACHMENT_BYTES
                assert attachment.stat().st_mode & 0o111 == 0
        finally:
            await runtime.disconnect()
    finally:
        prepared["cleanup"]()
    assert not staged.parent.exists()


async def test_codex_sdk_receives_same_skill_inputs_and_cleans_one_shot_turn(alias_config, tmp_path, monkeypatch):
    config, home, plugin = alias_config("codex.app-server")
    sdk_host, sdk_child = [_sdk_with_config({}) for _ in range(2)]
    for sdk in (sdk_host, sdk_child):
        sdk.thread_start.return_value = _FakeThread(handle=_FakeHandle([_turn_completed(TurnStatus.completed)]))
    sdks = iter((sdk_host, sdk_child))
    sdk_configs = []

    def sdk_factory(config):
        sdk_configs.append(config)
        sdk = next(sdks)
        thread = sdk.thread_start.return_value

        async def start(**_):
            assert sdk._client.request.await_args.args[0] == "skills/extraRoots/set"
            return thread

        sdk.thread_start.side_effect = start
        return sdk

    monkeypatch.setattr(codex, "AsyncCodex", sdk_factory)
    with use_config(config):
        host = codex.build_codex_session(config.preset_for("dev"), str(tmp_path), None, None)
    try:
        await host.ask("test")
        await host.ask("test")
        first_inputs, _ = sdk_host.thread_start.return_value.turn_calls[0]
        host_files = [Path(item.path) for item in first_inputs if isinstance(item, SkillInput)]
        for path in host_files:
            attachment = path.parent / _ATTACHMENT
            assert attachment.read_bytes() == _ATTACHMENT_BYTES
            assert attachment.stat().st_mode & 0o111 == 0
    finally:
        await host.disconnect()

    assert all(not path.exists() for path in host_files)

    observed = []
    child_thread = sdk_child.thread_start.return_value
    real_turn = child_thread.turn

    async def capture(inputs, **kwargs):
        for item in inputs:
            if isinstance(item, SkillInput):
                path = Path(item.path)
                attachment = path.parent / _ATTACHMENT
                assert attachment.read_bytes() == _ATTACHMENT_BYTES
                assert attachment.stat().st_mode & 0o111 == 0
                observed.append(path.read_bytes())
        return await real_turn(inputs, **kwargs)

    monkeypatch.setattr(child_thread, "turn", capture)
    frames = []

    async def send(stream, kind, payload):
        frames.append((stream, kind, payload))

    turns = child_turn.ChildTurns(home, send, None)
    payload = _payload(config, tmp_path, live=_FOLDERS)
    assert payload["skill_format"] == "codex_skill_input"
    await turns.handle(decode_frame(encode_frame(1, "turn", payload)))
    await turns.finish()
    assert frames[-1][1] == "result"
    for sdk in (sdk_host, sdk_child):
        assert sdk.thread_start.call_args.kwargs["developer_instructions"] == _PROMPT
        sdk.close.assert_awaited_once()
    for sdk_config in sdk_configs:
        assert sdk_config.env is None
        assert all("skills" not in arg for arg in sdk_config.config_overrides)
    [first_host_turn, second_host_turn] = sdk_host.thread_start.return_value.turn_calls
    assert first_host_turn == second_host_turn
    (host_review, host_demo, host_prompt), _ = first_host_turn
    [((child_review, child_demo, child_prompt), _)] = child_thread.turn_calls
    assert host_prompt == child_prompt == TextInput("test")
    for host_input, child_input, content, name in zip(
        (host_review, host_demo), (child_review, child_demo), observed, ("z-review", "demo"), strict=True,
    ):
        relative = Path("skills", name, "SKILL.md")
        assert host_input.name == name
        assert Path(host_input.path).parts[-3:] == relative.parts
        assert not Path(host_input.path).is_relative_to(plugin)
        child_file = Path(child_input.path)
        assert child_input == SkillInput(name=name, path=str(child_file))
        assert child_file.parts[-3:] == relative.parts
        assert child_file.parents[3].name.startswith("kotoyose-child-prompt-")
        assert content == (plugin / relative).read_bytes()
        assert not child_file.parents[3].exists()

    def registration(*inputs):
        return call(
            "skills/extraRoots/set", {"extraRoots": [str(Path(item.path).parent) for item in inputs]},
            response_model=SkillsExtraRootsSetResponse,
        )

    assert sdk_host._client.request.await_args_list == [registration(host_review, host_demo)]
    assert sdk_child._client.request.await_args_list == [
        call("config/read", {"cwd": str(tmp_path)}, response_model=ConfigReadResponse),
        registration(child_review, child_demo),
    ]


async def test_codex_skill_registration_failure_closes_app_server(alias_config, tmp_path, monkeypatch):
    config, _, _ = alias_config("codex.app-server")
    sdk = _sdk_with_config({})
    sdk._client.request.side_effect = RuntimeError("private app-server error")
    monkeypatch.setattr(codex, "AsyncCodex", lambda *_: sdk)
    with use_config(config):
        runtime = codex.build_codex_session(config.preset_for("dev"), str(tmp_path), None)

    with pytest.raises(RuntimeTurnError, match="could not register alias skill roots") as error:
        await runtime.ask("test")
    assert "private app-server error" not in str(error.value)
    sdk.thread_start.assert_not_awaited()
    sdk.close.assert_awaited_once()


async def test_bundled_codex_discovers_only_registered_alias_skills(tmp_path):
    cwd, home, codex_home = (tmp_path / name for name in ("work", "home", "codex-home"))
    for folder in (cwd, home, codex_home):
        folder.mkdir()
    skill_root = tmp_path / "plugin" / "skills"
    for name in ("zebra-rule", "unselected-rule"):
        folder = skill_root / name
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: Test skill\n---\n\nTest.\n", encoding="utf-8")
    file = skill_root / "zebra-rule" / "SKILL.md"
    config = CodexConfig(cwd=str(cwd), config_overrides=("features.plugins=false", "features.apps=false"))
    env = {
        **{key: os.environ[key] for key in ("SYSTEMROOT", "PATH") if key in os.environ},
        "HOME": str(home), "USERPROFILE": str(home), "APPDATA": str(home), "LOCALAPPDATA": str(home),
        "CODEX_HOME": str(codex_home), "XDG_CONFIG_HOME": str(home / "config"),
        "XDG_DATA_HOME": str(home / "data"), "XDG_CACHE_HOME": str(home / "cache"),
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
    }

    async def alias_skills(client):
        response = await client._client.request("skills/list", {"cwds": [str(cwd)]}, response_model=SkillsListResponse)
        [entry] = response.data
        return {(skill.name, Path(skill.path.root), skill.enabled) for skill in entry.skills if skill.name in {"zebra-rule", "unselected-rule"}}

    with patch.dict(os.environ, env, clear=True):
        async with AsyncCodex(config) as session, AsyncCodex(config) as other_session:
            assert not await alias_skills(session)
            await codex._register_skill_roots(session, (RuntimeSkill(name="zebra-rule", path=str(file)),))
            assert await alias_skills(session) == {("zebra-rule", file, True)}
            assert not await alias_skills(other_session)
        async with AsyncCodex(config) as fresh_session:
            assert not await alias_skills(fresh_session)
        # The settings exposed by this pin do not disable project skill discovery.
        for vendor, name in ((".agents", "agents-project"), (".codex", "codex-project")):
            folder = cwd / vendor / "skills" / name
            folder.mkdir(parents=True)
            (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: Test skill\n---\nTest.\n")
        for extra in ("features.skip_host_skill_discovery=true", "project_doc_max_bytes=0"):
            narrowed = CodexConfig(cwd=str(cwd), config_overrides=(*config.config_overrides, extra))
            async with AsyncCodex(narrowed) as client:
                response = await client._client.request("skills/list", {"cwds": [str(cwd)]}, response_model=SkillsListResponse)
                [entry] = response.data
                assert {"agents-project", "codex-project"} <= {skill.name for skill in entry.skills if skill.enabled}
    assert not (codex_home / "config.toml").exists()
    assert {entry.name for entry in cwd.iterdir()} == {".agents", ".codex"}


@pytest.mark.parametrize("driver", _FORMATS)
@pytest.mark.parametrize("live", [_OLD, _FOLDERS_ONLY], ids=["legacy", "folders-without-sdk"])
def test_links_without_sdk_feature_use_legacy_body_only_payload(alias_config, tmp_path, driver, live):
    config, home, _ = alias_config(driver)
    payload = _payload(config, tmp_path, live=live)
    assert "skill_format" not in payload
    assert "skill_folders" not in payload
    assert payload["skills"] == [{"name": "review", "text": "legacy review"}, {"name": "demo", "text": "本文"}]
    prepared = child_turn._prepare(home, payload)
    try:
        rebuilt = prepared["config"]
        assert rebuilt.resolve_resources(rebuilt.models["dev"])[1] == ["review", "demo"]
        [plugin] = rebuilt.models["dev"].plugins
        assert not (Path(plugin) / "skills" / "demo" / _ATTACHMENT).exists()
        if driver == "codex.app-server":
            content = (Path(plugin) / "skills" / "review" / "SKILL.md").read_text(encoding="utf-8")
            assert content.startswith('---\nname: "review"\ndescription: ')
            assert content.endswith("\n---\n\nlegacy review")
    finally:
        prepared["cleanup"]()


@pytest.mark.parametrize("driver", _FORMATS)
def test_sdk_only_links_read_only_bounded_skill_markdown(alias_config, tmp_path, monkeypatch, driver):
    config, _, plugin = alias_config(driver)
    monkeypatch.setattr(skill_resources, "read_skill_folder", lambda *_a, **_kw: pytest.fail("read an attachment folder"))
    markdown = plugin / "skills/demo/SKILL.md"
    markdown.write_bytes(b"x" * MAX_SKILL_BYTES)
    payload = _payload(config, tmp_path)
    assert payload["skill_format"] == _FORMATS[driver]
    assert "skill_folders" not in payload
    assert payload["skills"][1] == {"name": "demo", "text": "x" * MAX_SKILL_BYTES}
    markdown.write_bytes(b"x" * (MAX_SKILL_BYTES + 1))
    with pytest.raises(ValueError, match=f"exceeds {MAX_SKILL_BYTES} bytes"):
        _payload(config, tmp_path)


def test_codex_removes_skill_temp_when_placement_fails_midway(alias_config, tmp_path, monkeypatch):
    config, _, _ = alias_config("codex.app-server")
    runtime = codex.build_codex_session(config.preset_for("dev"), None, None, None, config=config)
    write_file = skill_folders._write_file
    written = []

    def fail_after_first_file(directory, parent, parts, data):
        if not written:
            write_file(directory, parent, parts, data)
            written.append(directory / parts[0])
            return
        assert written[0].is_file()
        raise OSError("fixture write failure")

    monkeypatch.setattr(skill_folders, "_write_file", fail_after_first_file)
    with pytest.raises(ValueError, match="could not write skill folders"):
        runtime._place_skills()
    assert runtime._skill_temp is None
    [file] = written
    assert not file.parents[2].exists()


@pytest.mark.parametrize(("case", "message"), [
    ("per_skill", "32768 bytes"),
    ("total_skills", "131072 bytes"),
    ("count", "up to 16 skills"),
    ("combined", "262144 bytes"),
    ("traversal", "invalid child skill name"),
    ("drive", "invalid child skill name"),
    ("device", "invalid child skill name"),
    ("duplicate", "duplicate child skill name"),
    ("frontmatter", "unsupported YAML frontmatter"),
    ("command", "contains command expansion"),
])
def test_host_and_child_reject_invalid_sdk_skill_payloads(alias_config, tmp_path, case, message):
    config, home, plugin = alias_config("claude.agent-sdk")
    payload = _payload(config, tmp_path)
    skills = payload["skills"][:1]
    if case == "per_skill":
        skills[0]["text"] = "日" * (MAX_SKILL_BYTES // 3 + 1)
    elif case == "total_skills":
        skills = [{"name": f"skill-{i}", "text": "x" * MAX_SKILL_BYTES} for i in range(MAX_SKILLS_BYTES // MAX_SKILL_BYTES + 1)]
    elif case == "count":
        skills = [{"name": f"skill-{i}", "text": "x"} for i in range(MAX_SKILLS + 1)]
    elif case == "combined":
        payload["prompts"] = ["x" * MAX_TURN_FILE_BYTES]
        Path(config.prompt_files["second"].path).write_text(payload["prompts"][0], encoding="utf-8")
        config.models["dev"].prompt_file_refs = ["second"]
    elif case == "duplicate":
        skills.append({"name": "Z-REVIEW", "text": "x"})
    elif case == "frontmatter":
        skills[0]["text"] = "---\ndescription: |\n  ---\nallowed-tools: Bash\n---\ntext"
    elif case == "command":
        skills[0]["text"] = "Before answering: !`example`"
    else:
        skills[0]["name"] = {"traversal": "../escape", "drive": "C:escape", "device": "NUL"}[case]

    config.models["dev"].skill_refs = []
    for index, skill in enumerate(skills):
        name = skill["name"]
        if case not in {"traversal", "drive", "device"}:
            folder = plugin / "skills" / name
            folder.mkdir(exist_ok=True)
            (folder / "SKILL.md").write_bytes(skill["text"].encode())
        ref = f"ref-{index}"
        config.skill_definitions[ref] = config.skill_definitions["review"].model_copy(update={"skill": name})
        config.models["dev"].skill_refs.append(ref)
    with pytest.raises(ValueError, match=message):
        _payload(config, tmp_path)
    with pytest.raises(child_turn.ChildTurnError, match=message):
        child_turn._prepare(home, {**payload, "skills": skills})


def test_missing_files_are_reported_before_sending(alias_config, tmp_path):
    config, _, plugin = alias_config("codex.app-server")
    prompt = Path(config.prompt_files["brief"].path)
    prompt.unlink()
    with pytest.raises(ValueError, match="child prompt files are unavailable") as error:
        _payload(config, tmp_path)
    assert str(prompt) not in str(error.value)
    (plugin / "skills" / "z-review" / "SKILL.md").unlink()
    with pytest.raises(ValueError, match="SKILL.md.*z-review"):
        _payload(config, tmp_path)


def test_turn_frame_cap_counts_json_escaping(alias_config, tmp_path):
    config, _, _ = alias_config("claude.agent-sdk")
    # Below the UTF-8 body cap, but the wire representation uses six bytes per character.
    Path(config.prompt_files["brief"].path).write_bytes(b"\x01" * (192 * 1024))
    with pytest.raises(ValueError, match="child turn frame exceeds 1048576 bytes"):
        _payload(config, tmp_path)


def test_legacy_wire_payload_cannot_bypass_frontmatter_validation(alias_config, tmp_path):
    config, home, _ = alias_config("claude.agent-sdk")
    payload = _payload(config, tmp_path, live=_OLD)
    payload["skills"][0]["text"] = "---\nallowed-tools: Bash\n---\ntext"
    with pytest.raises(child_turn.ChildTurnError, match="unsupported YAML frontmatter"):
        child_turn._prepare(home, payload)


async def test_skill_files_are_removed_when_sdk_start_fails(alias_config, tmp_path, monkeypatch):
    config, home, _ = alias_config("codex.app-server")
    staged = []
    frames = []

    async def fail(prepared):
        [path] = prepared["config"].models["dev"].plugins
        private_dir = Path(path).parent
        assert private_dir.is_dir()
        staged.append(private_dir)
        raise RuntimeError("SDK start failed")

    async def send(_stream, kind, payload):
        frames.append((kind, payload))

    monkeypatch.setattr(child_turn, "_build_runtime", fail)
    turns = child_turn.ChildTurns(home, send, None)
    try:
        await turns.handle(Frame(1, "turn", _payload(config, tmp_path, key="warm")))
        [(kind, payload)] = frames
        assert kind == "error" and payload["message"] == "SDK start failed"
        [private_dir] = staged
        assert not private_dir.exists()
    finally:
        await turns.finish()


async def test_warm_skill_files_reused_replaced_and_released(alias_config, tmp_path, monkeypatch):
    config, home, plugin = alias_config("codex.app-server")
    staged = []

    class Runtime:
        async def stream(self, _prompt):
            yield RuntimeResult(text="ok")

    async def build(prepared):
        [path] = prepared["config"].models["dev"].plugins
        staged.append(Path(path).parent)
        return Runtime()

    async def send(_stream, kind, _payload):
        assert kind == "result"

    monkeypatch.setattr(child_turn, "_build_runtime", build)
    turns = child_turn.ChildTurns(home, send, None)
    try:
        for stream in (1, 2):
            await turns.handle(Frame(stream, "turn", _payload(config, tmp_path, live=_FOLDERS, key="same")))
        [first] = staged
        assert first.is_dir()
        (plugin / "skills/z-review" / _ATTACHMENT).write_bytes(b"updated")
        await turns.handle(Frame(3, "turn", _payload(config, tmp_path, live=_FOLDERS, key="same")))
        assert len(staged) == 2
        assert not first.exists() and staged[-1].is_dir()
        assert (staged[-1] / "plugin/skills/z-review" / _ATTACHMENT).read_bytes() == b"updated"
        await turns.handle(Frame(3, "release", {"conversation_key": "same"}))
        assert not staged[-1].exists()
    finally:
        await turns.finish()


async def test_aliases_share_cwd_with_separate_warm_skill_folders(alias_config, tmp_path, monkeypatch):
    config, home, _ = alias_config("claude.agent-sdk")
    config.models["dev"].skill_refs = ["demo"]
    config.models["reviewer"] = config.models["dev"].model_copy(update={"skill_refs": ["review"]})
    barrier = asyncio.Barrier(2)
    observed = {}
    terminal = []

    async def build(prepared):
        alias = prepared["alias"]
        [plugin] = prepared["config"].models[alias].plugins
        root = Path(plugin) / "skills"

        class Runtime:
            async def disconnect(self):
                pass

            async def stream(self, _):
                await asyncio.wait_for(barrier.wait(), 2)
                assert prepared["cwd"] == str(tmp_path)
                observed[alias] = (root, {entry.name for entry in root.iterdir()})
                yield RuntimeResult(text="ok")

        return Runtime()

    async def send(_stream, kind, _payload):
        terminal.append(kind)

    monkeypatch.setattr(child_turn, "_build_runtime", build)
    turns = child_turn.ChildTurns(home, send, None)
    try:
        tasks = []
        for stream, alias in enumerate(("dev", "reviewer"), 1):
            payload = build_turn_payload(
                alias=alias, config=config, preset=config.models[alias], conversation_key=alias,
                cwd=str(tmp_path), prompt="test", live=_FOLDERS,
            )
            tasks.append(turns.handle(decode_frame(encode_frame(stream, "turn", payload))))
        await asyncio.gather(*tasks)
        assert terminal == ["result", "result"]
        dev, dev_names = observed["dev"]
        review, review_names = observed["reviewer"]
        assert dev != review
        assert dev_names == {"demo"} and review_names == {"z-review"}
        assert dev.is_dir() and review.is_dir()
        assert not dev.is_relative_to(tmp_path) and not review.is_relative_to(tmp_path)
    finally:
        await turns.finish()
    assert not dev.exists() and not review.exists()


async def test_host_aliases_share_cwd_with_separate_skill_folders(alias_config, tmp_path, monkeypatch):
    config, _, plugin = alias_config("claude.agent-sdk")
    config.connections["dev-conn"].on = "local"
    config.models["dev"].on = "local"
    config.models["dev"].skill_refs = ["demo"]
    config.models["reviewer"] = config.models["dev"].model_copy(update={"skill_refs": ["review"]})
    barrier = asyncio.Barrier(2)
    observed = {}

    class SDK:
        def __init__(self, *, options):
            self.options = options

        async def connect(self):
            await asyncio.wait_for(barrier.wait(), 2)
            [item] = self.options.plugins
            [name] = self.options.skills
            root = Path(item["path"]) / "skills"
            assert self.options.cwd == str(tmp_path)
            assert {entry.name for entry in root.iterdir()} == {name}
            observed[name] = root

        async def disconnect(self):
            pass

    def options(alias, **kwargs):
        return claude_options.build_claude_options(
            config, config.preset_for(alias), model_name=alias, cwd_override=kwargs["cwd_override"],
            request_headers=None, env={}, session_store=None,
        )

    monkeypatch.setattr(session, "ClaudeSDKClient", SDK)
    monkeypatch.setattr(session, "build_options", options)
    monkeypatch.setattr(session, "resolve_preset_secrets_with_versions_async", _empty_secrets)
    monkeypatch.setattr(claude_options, "bundled_claude_cli", lambda: tmp_path / "fake-claude")
    clients = await asyncio.gather(*(
        session.get_or_resume_client(None, alias, str(tmp_path), config_snapshot=config)
        for alias in ("dev", "reviewer")
    ))
    try:
        assert observed["demo"] != observed["z-review"]
        assert all(root.is_dir() and not root.is_relative_to(plugin) for root in observed.values())
    finally:
        await asyncio.gather(*(runtime.disconnect() for runtime, _ in clients))
    assert all(not root.exists() for root in observed.values())
