"""K3: host turns on a child, over localhost TLS, with a fake runtime."""

from __future__ import annotations

import asyncio
import base64
from contextlib import suppress
import json
import os
import subprocess
from pathlib import Path

import pytest
from claude_agent_sdk import ClaudeSDKClient
from pydantic import ValidationError

from kotoyose.chat_policy import ChatPolicyViolation, validate_cwd
from kotoyose.child_client import _hello_features, child_session, enroll_child
from kotoyose.child_protocol import SDK_SKILLS_FEATURE, SKILL_FOLDERS_FEATURE
from kotoyose.child_server import ChildHub
from kotoyose.config import Connection, HarnessConfig, ModelPreset
from kotoyose.registry import registry
from kotoyose.runtime.base import RuntimeEvent, RuntimeImageInput, RuntimeProviderSpec, RuntimeResult, RuntimeTextInput, RuntimeTurnError
from kotoyose.session import SDKConnectionError, get_or_resume_client, sessions
from tests.test_child_k1 import _wait_child


def _address(hub: ChildHub) -> str:
    return f"127.0.0.1:{hub.port}"


def _plugin(tmp_path: Path) -> Path:
    plugin = tmp_path / "plugin"
    (plugin / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (plugin / ".claude-plugin" / "plugin.json").write_text("{}\n", encoding="utf-8")
    skill = plugin / "skills" / "demo"
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text("SKILL_BODY_UNIQUE\n", encoding="utf-8")
    return plugin


def _host_config(tmp_path: Path, *, on: str = "child:dev", kind: str = "subscription", mcp_refs: list[str] | None = None, mcp_servers: dict | None = None) -> dict:
    plugin = _plugin(tmp_path)
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("PROMPT_BODY_UNIQUE\n", encoding="utf-8")
    connection: dict = {
        "provider": "claude",
        "kind": kind,
        "data_policy": "no_training",
        "on": on,
    }
    if kind == "subscription":
        connection["config_dir"] = str(tmp_path / "HOST_ONLY_CONFIG_DIR")
    elif kind == "api_key":
        connection["secret"] = "env:SUPER_SECRET_TOKEN"
    else:
        connection["endpoint"] = "https://example.invalid/v1"
        connection["secret"] = "env:SUPER_SECRET_TOKEN"
    preset: dict = {
        "route": "dev-route",
        "on": on,
        "description": "child alias",
        # cwd の上書きは alias でも許可が要る (host は文字列で、child は解決して確かめる)。
        "allow_cwd_override": True,
        "cwd_allowed_roots": [str(tmp_path)],
        "mcp_refs": ["files"] if mcp_refs is None else mcp_refs,
        "prompt_file_refs": ["brief"],
        "skill_refs": ["demo"],
    }
    if mcp_servers is not None:
        preset["mcp_servers"] = mcp_servers
    return {
        "connections": {"dev-conn": connection},
        "routes": {
            "dev-route": {
                "driver": "claude.agent-sdk",
                "model": "claude-sonnet-4-5",
                "connection": "dev-conn",
            },
        },
        "models": {"dev": preset},
        "mcps": {
            "files": {
                "description": "host files",
                "server": {
                    "command": "npx",
                    "args": ["secret-server"],
                    "env": {"TOKEN": "SUPER_SECRET_MCP"},
                },
            },
        },
        "prompt_files": {"brief": {"path": str(prompt)}},
        "skill_definitions": {"demo": {"plugin": str(plugin), "skill": "demo"}},
    }


def _policy(home: Path, root: Path, *, adapters: list[str] | None = None, mcp: dict | None = None) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "policy.json").write_text(json.dumps({
        "workspace_roots": [str(root)],
        "adapters": ["claude.agent-sdk"] if adapters is None else adapters,
        "mcp": {
            "files": {
                "description": "workspace files",
                "command": "child-local-mcp",
                "args": ["--from-policy"],
                "env": {"CHILD_TOKEN": "POLICY_SECRET"},
            },
        } if mcp is None else mcp,
    }), encoding="utf-8")


class _FakeRuntime:
    provider = "claude"

    def __init__(self, cwd: str | None, mode: str):
        self.cwd = cwd
        self.mode = mode
        self.prompts: list = []
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.disconnected = False
        self.proc: subprocess.Popen | None = None
        if mode == "block":
            self.proc = subprocess.Popen(["sleep", "30"], start_new_session=True)

    async def disconnect(self) -> None:
        self.disconnected = True
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()

    async def ask(self, prompt):
        raise AssertionError("child should stream")

    async def stream(self, prompt):
        self.prompts.append(prompt)
        self.started.set()
        if self.mode == "error":
            yield "part"
            raise RuntimeTurnError(
                "provider failed",
                partial_result=RuntimeResult(text="part", usage={"output_tokens": 1}, provider="claude"),
                diagnostics={"terminal_reason": "error", "subtype": "fake"},
                retryable=False,
            )
        if self.mode == "block":
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
        yield "hel"
        yield "lo"
        yield RuntimeEvent(kind="tool", tool_name="read", summary="notes")
        yield RuntimeResult(text="hello", usage={"output_tokens": 2}, provider="claude")


@pytest.fixture
async def hub(tmp_path: Path):
    child_hub = ChildHub(tmp_path / "host", heartbeat_interval=0.05)
    await child_hub.start("127.0.0.1", 0)
    try:
        yield child_hub
    finally:
        await child_hub.stop()


@pytest.fixture
def fake_runtime(monkeypatch: pytest.MonkeyPatch):
    built: list[_FakeRuntime] = []
    mode = {"value": "stream"}

    def build(preset, cwd, session_id, headers, **kwargs):
        runtime = _FakeRuntime(cwd, mode["value"])
        built.append(runtime)
        return runtime

    previous = registry.runtime_providers.get("claude")
    registry.runtime_providers["claude"] = RuntimeProviderSpec(build, "claude")
    registry.override_child_builder("claude", lambda _alias, preset, cwd, **options: build(preset, cwd, None, None, **options))

    async def _refuse_real_cli(self):
        raise AssertionError("real CLI")

    monkeypatch.setattr(ClaudeSDKClient, "connect", _refuse_real_cli)
    try:
        yield built, mode
    finally:
        registry.override_child_builder("claude", None)
        if previous is None:
            registry.runtime_providers.pop("claude", None)
        else:
            registry.runtime_providers["claude"] = previous
        sessions.pop("conv-dev", None)
        sessions.clear()


@pytest.mark.parametrize(("platform", "sdk", "folders"), [
    ("linux-x64", True, True),
    ("linux-arm64", True, True),
    ("windows-x64", True, True),
    ("macos-arm64", False, False),
])
def test_hello_gates_native_and_folder_skills_independently(platform, sdk, folders):
    features = _hello_features(platform)
    assert (SDK_SKILLS_FEATURE in features) == sdk
    assert (SKILL_FOLDERS_FEATURE in features) == folders


def test_on_accepts_child_names_and_rejects_other_places():
    """`on` is local or child:<name>, and the child name uses the enrollment alphabet."""
    assert Connection(
        provider="codex", kind="subscription", data_policy="no_training", on="child:win1",
    ).on == "child:win1"
    assert ModelPreset(on="child:dev").on == "child:dev"
    for bad in ("remote", "child:", "child:.hidden"):
        with pytest.raises(ValidationError):
            Connection(provider="codex", kind="subscription", data_policy="no_training", on=bad)
        with pytest.raises(ValidationError):
            ModelPreset(on=bad)


def test_child_alias_rules_at_load(tmp_path: Path):
    """Subscription placement must match, and child aliases cannot carry API keys or kotoyose."""
    host = _host_config(tmp_path)
    loaded = HarnessConfig.model_validate(host)
    assert loaded.models["dev"].on == "child:dev"
    assert loaded.connections["dev-conn"].on == "child:dev"

    mismatched = _host_config(tmp_path)
    mismatched["connections"]["dev-conn"]["on"] = "local"
    with pytest.raises(ValidationError, match="must equal"):
        HarnessConfig.model_validate(mismatched)

    for kind in ("api_key", "endpoint"):
        denied = _host_config(tmp_path, kind=kind)
        with pytest.raises(ValidationError, match="cannot be used"):
            HarnessConfig.model_validate(denied)

    kotoyose = _host_config(tmp_path, mcp_refs=["kotoyose"])
    with pytest.raises(ValidationError, match="kotoyose"):
        HarnessConfig.model_validate(kotoyose)

    inline = _host_config(tmp_path, mcp_servers={"files": {"command": "npx", "args": ["x"]}})
    with pytest.raises(ValidationError, match="mcp_servers"):
        HarnessConfig.model_validate(inline)

    deferred = _host_config(tmp_path, mcp_refs=["not-on-the-host"])
    deferred["mcps"] = {}
    HarnessConfig.model_validate(deferred)

    local = _host_config(tmp_path, on="local", mcp_refs=["not-on-the-host"])
    local["mcps"] = {}
    with pytest.raises(ValidationError, match="不明な MCP"):
        HarnessConfig.model_validate(local)


def test_child_alias_does_not_resolve_cwd_on_the_host(monkeypatch: pytest.MonkeyPatch):
    """The host keeps a child cwd as a string. The child is the one that resolves it."""
    def boom(*_args, **_kwargs):
        raise AssertionError("host resolved cwd")

    preset = ModelPreset(on="child:dev", allow_cwd_override=True, cwd_allowed_roots=["/only"])
    monkeypatch.setattr(os.path, "isdir", boom)
    monkeypatch.setattr(Path, "resolve", boom)
    validate_cwd(preset, "dev", "/only/on/the/child")


def test_local_alias_still_resolves_cwd(tmp_path: Path):
    """A local alias still refuses a cwd outside its allowed roots."""
    preset = ModelPreset(allow_cwd_override=True, cwd_allowed_roots=[str(tmp_path)])
    outside = tmp_path.parent / "outside-k3"
    outside.mkdir(exist_ok=True)
    with pytest.raises(ChatPolicyViolation, match="許可ルート外"):
        validate_cwd(preset, "local", str(outside))


async def _wait_until_built(built: list, task: asyncio.Task) -> None:
    """The turn task has to run before the fake exists. Then its start event is meaningful."""
    deadline = asyncio.get_running_loop().time() + 2
    while not built:
        if task.done():
            task.result()
            raise AssertionError("turn ended before a runtime was built")
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("runtime was not built")
        await asyncio.sleep(0.01)


async def _enroll(hub: ChildHub, home: Path, root: Path, name: str = "dev", **policy) -> None:
    _policy(home, root, **policy)
    code = hub.issue_enrollment_code(_address(hub))
    await enroll_child(code, home, name=name)


async def test_turn_streams_text_events_and_reuses_the_child_runtime(hub: ChildHub, tmp_path: Path, fake_runtime):
    """A child turn yields text, a runtime event, and the final result, then keeps the runtime."""
    built, _mode = fake_runtime
    root = tmp_path / "work"
    nested = root / "proj"
    nested.mkdir(parents=True)
    home = tmp_path / "child"
    await _enroll(hub, home, root)
    config = HarnessConfig.model_validate(_host_config(tmp_path))
    asset = Path(config.skill_definitions["demo"].plugin) / "skills/demo/asset.bin"
    data = b"\xff" * (800 * 1024)
    asset.write_bytes(data)
    requested = str(nested / ".." / "proj")
    prompt = [RuntimeTextInput("see"), RuntimeImageInput("data:image/png;base64,AAAA", "auto")]
    received = []
    async with child_session(home, heartbeat_interval=0.05, received_turns=received):
        await _wait_child(hub, "dev", online=True)
        client, resumed = await get_or_resume_client("conv-dev", "dev", requested, config_snapshot=config)
        assert resumed is False
        items = [item async for item in client.stream(prompt)]
        again, _ = await get_or_resume_client("conv-dev", "dev", requested, config_snapshot=config)
        assert again is client
        second = [item async for item in client.stream("next")]
    assert [item for item in items if isinstance(item, str)] == ["hel", "lo"]
    assert any(isinstance(item, RuntimeEvent) and item.tool_name == "read" for item in items)
    result = items[-1]
    assert isinstance(result, RuntimeResult) and result.text == "hello"
    assert second[-1].text == "hello"
    assert len(built) == 1
    assert built[0].cwd == str(nested.resolve())
    assert built[0].prompts[0] == prompt
    raw = json.dumps(received)
    assert "PROMPT_BODY_UNIQUE" in raw
    [folder] = received[0]["skill_folders"]
    assert base64.b64decode(folder["files"]["SKILL.md"]) == b"SKILL_BODY_UNIQUE\n"
    assert base64.b64decode(folder["files"]["asset.bin"]) == data
    assert requested in raw
    for forbidden in ("HOST_ONLY_CONFIG_DIR", "SUPER_SECRET_MCP", "npx", "secret-server", "child-local-mcp", "POLICY_SECRET"):
        assert forbidden not in raw


async def test_child_error_keeps_the_partial_result(hub: ChildHub, tmp_path: Path, fake_runtime):
    """A child RuntimeTurnError comes back with the partial text and diagnostics."""
    _built, mode = fake_runtime
    mode["value"] = "error"
    root = tmp_path / "work"
    root.mkdir()
    await _enroll(hub, tmp_path / "child", root)
    config = HarnessConfig.model_validate(_host_config(tmp_path))
    async with child_session(tmp_path / "child", heartbeat_interval=0.05):
        await _wait_child(hub, "dev", online=True)
        client, _ = await get_or_resume_client("conv-dev", "dev", str(root), config_snapshot=config)
        with pytest.raises(RuntimeTurnError, match="provider failed") as caught:
            await client.ask("break")
    assert caught.value.partial_result.text == "part"
    assert caught.value.diagnostics["terminal_reason"] == "error"
    assert caught.value.diagnostics["subtype"] == "fake"


async def test_cancel_mid_turn_cancels_the_child(hub: ChildHub, tmp_path: Path, fake_runtime):
    """Closing the host stream sends cancel, and the child stops the turn."""
    built, mode = fake_runtime
    mode["value"] = "block"
    root = tmp_path / "work"
    root.mkdir()
    await _enroll(hub, tmp_path / "child", root)
    config = HarnessConfig.model_validate(_host_config(tmp_path))
    async with child_session(tmp_path / "child", heartbeat_interval=0.05):
        await _wait_child(hub, "dev", online=True)
        client, _ = await get_or_resume_client("conv-dev", "dev", str(root), config_snapshot=config)
        async def consume():
            async for _item in client.stream("stay"):
                pass

        task = asyncio.create_task(consume())
        await _wait_until_built(built, task)
        await built[0].started.wait()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        await asyncio.wait_for(built[0].cancelled.wait(), timeout=2)


async def test_link_drop_kills_the_fake_process(hub: ChildHub, tmp_path: Path, fake_runtime):
    """When the mux drops, the child kills the turn's process tree."""
    built, mode = fake_runtime
    mode["value"] = "block"
    root = tmp_path / "work"
    root.mkdir()
    await _enroll(hub, tmp_path / "child", root)
    config = HarnessConfig.model_validate(_host_config(tmp_path))
    async with child_session(tmp_path / "child", heartbeat_interval=0.05):
        await _wait_child(hub, "dev", online=True)
        client, _ = await get_or_resume_client("conv-dev", "dev", str(root), config_snapshot=config)
        async def consume():
            async for _item in client.stream("stay"):
                pass

        task = asyncio.create_task(consume())
        await _wait_until_built(built, task)
        await built[0].started.wait()
        proc = built[0].proc
        assert proc is not None
        await hub.disconnect("dev")
        assert await asyncio.to_thread(proc.wait, 2) < 0 or proc.returncode is not None
        if not task.done():
            task.cancel()
        # The stream may end by cancellation (BaseException) or by the offline error; both are fine here.
        with suppress(Exception, asyncio.CancelledError):
            await task
    assert proc.poll() is not None


async def test_child_rejects_cwd_adapter_and_unknown_mcp_before_spawn(hub: ChildHub, tmp_path: Path, fake_runtime):
    """Policy failures happen on the child and do not construct a runtime."""
    built, _mode = fake_runtime
    root = tmp_path / "work"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    home = tmp_path / "child"
    await _enroll(hub, home, root)
    config = HarnessConfig.model_validate(_host_config(tmp_path))
    async with child_session(home, heartbeat_interval=0.05):
        await _wait_child(hub, "dev", online=True)
        client, _ = await get_or_resume_client("conv-dev", "dev", str(outside), config_snapshot=config)
        with pytest.raises(RuntimeTurnError, match="workspace_roots"):
            await client.ask("nope")
    assert built == []

    await hub.revoke("dev")
    home_adapter = tmp_path / "child-adapter"
    await _enroll(hub, home_adapter, root, adapters=[])
    async with child_session(home_adapter, heartbeat_interval=0.05):
        await _wait_child(hub, "dev", online=True)
        client, _ = await get_or_resume_client("conv-adapter", "dev", str(root), config_snapshot=config)
        with pytest.raises(RuntimeTurnError, match="not allowed"):
            await client.ask("nope")
    assert built == []

    await hub.revoke("dev")
    home_mcp = tmp_path / "child-mcp"
    unknown = _host_config(tmp_path, mcp_refs=["missing-mcp"])
    unknown["mcps"] = {}
    unknown_config = HarnessConfig.model_validate(unknown)
    await _enroll(hub, home_mcp, root, mcp={})
    async with child_session(home_mcp, heartbeat_interval=0.05):
        await _wait_child(hub, "dev", online=True)
        client, _ = await get_or_resume_client("conv-mcp", "dev", str(root), config_snapshot=unknown_config)
        with pytest.raises(RuntimeTurnError, match="unknown MCP"):
            await client.ask("nope")
    assert built == []


async def test_revoke_and_reenroll_starts_a_new_session(hub: ChildHub, tmp_path: Path, fake_runtime):
    """Cache identity includes the child generation, so a re-enrolled child does not reuse it."""
    root = tmp_path / "work"
    root.mkdir()
    home = tmp_path / "child"
    await _enroll(hub, home, root)
    config = HarnessConfig.model_validate(_host_config(tmp_path))
    async with child_session(home, heartbeat_interval=0.05):
        await _wait_child(hub, "dev", online=True)
        first, _ = await get_or_resume_client("conv-dev", "dev", str(root), config_snapshot=config)
        again, _ = await get_or_resume_client("conv-dev", "dev", str(root), config_snapshot=config)
        assert again is first
        await hub.revoke("dev")
    home_again = tmp_path / "child-again"
    await _enroll(hub, home_again, root)
    async with child_session(home_again, heartbeat_interval=0.05):
        await _wait_child(hub, "dev", online=True)
        fresh, _ = await get_or_resume_client("conv-dev", "dev", str(root), config_snapshot=config)
        assert fresh is not first


async def test_offline_child_reports_a_clear_error(hub: ChildHub, tmp_path: Path, fake_runtime):
    """A turn to a child with no live link fails before a runtime exists."""
    built, _mode = fake_runtime
    root = tmp_path / "work"
    root.mkdir()
    await _enroll(hub, tmp_path / "child", root)
    config = HarnessConfig.model_validate(_host_config(tmp_path))
    with pytest.raises(SDKConnectionError, match="child dev is offline"):
        await get_or_resume_client("conv-dev", "dev", str(root), config_snapshot=config)
    assert built == []


async def test_missing_cwd_uses_a_private_temp_dir_on_the_child(hub: ChildHub, tmp_path: Path, fake_runtime):
    """No requested cwd becomes a private directory on the child, not the host alias cwd."""
    built, _mode = fake_runtime
    root = tmp_path / "work"
    root.mkdir()
    await _enroll(hub, tmp_path / "child", root)
    config = HarnessConfig.model_validate(_host_config(tmp_path))
    async with child_session(tmp_path / "child", heartbeat_interval=0.05):
        await _wait_child(hub, "dev", online=True)
        client, _ = await get_or_resume_client("conv-dev", "dev", None, config_snapshot=config)
        await client.ask("temp")
        assert built[0].cwd is not None
        assert "kotoyose-child-chat-" in built[0].cwd
        assert Path(built[0].cwd).is_dir()


def test_host_keeps_alias_cwd_policy_for_child_aliases():
    """Host checks allow_cwd_override and cwd_allowed_roots as strings, without touching its filesystem."""
    from types import SimpleNamespace

    from kotoyose.chat_policy import ChatPolicyViolation, validate_cwd

    denied = SimpleNamespace(on="child:dev", allow_cwd_override=False, cwd_allowed_roots=["/work"])
    with pytest.raises(ChatPolicyViolation, match="allow_cwd_override"):
        validate_cwd(denied, "a", "/work/x")
    allowed = SimpleNamespace(on="child:dev", allow_cwd_override=True, cwd_allowed_roots=["/work", "C:\\Proj"])
    validate_cwd(allowed, "a", "/work/repo")          # does not need to exist on the host
    validate_cwd(allowed, "a", "c:\\proj\\game")
    for bad in ("/etc", "/work/../etc", "relative/path", "C:\\Projx"):
        with pytest.raises(ChatPolicyViolation, match="許可ルート外"):
            validate_cwd(allowed, "a", bad)


def test_child_requires_both_policy_and_alias_roots(tmp_path):
    from types import SimpleNamespace

    from kotoyose.child_turn import ChildTurnError, _resolve_cwd

    inside, other = tmp_path / "ws" / "repo", tmp_path / "other"
    inside.mkdir(parents=True)
    other.mkdir()
    policy = SimpleNamespace(workspace_roots=[str(tmp_path)])
    assert _resolve_cwd(str(inside), policy, alias_roots=[str(tmp_path / "ws")]) == inside.resolve()
    with pytest.raises(ChildTurnError, match="alias cwd_allowed_roots"):
        _resolve_cwd(str(other), policy, alias_roots=[str(tmp_path / "ws")])
    with pytest.raises(ChildTurnError, match="alias cwd_allowed_roots"):
        _resolve_cwd(str(inside), policy, alias_roots=[])


def test_child_claude_subscription_uses_the_local_cli_login(monkeypatch, tmp_path):
    from kotoyose.child_turn import _strip_connection, _use_local_cli_login

    sent = {"provider": "claude", "kind": "subscription", "data_policy": "no_training",
            "config_dir": "/host/only", "secret": "env:HOST_SECRET"}
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    local = _use_local_cli_login(_strip_connection(dict(sent)))
    assert local["config_dir"] == str(tmp_path / ".claude") and "secret" not in local
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "custom"))
    assert _use_local_cli_login(_strip_connection(dict(sent)))["config_dir"] == str(tmp_path / "custom")
    api = {"provider": "claude", "kind": "api_key"}
    assert "config_dir" not in _use_local_cli_login(dict(api))


def test_default_claude_login_dir_does_not_set_claude_config_dir(monkeypatch, tmp_path):
    # With CLAUDE_CONFIG_DIR=~/.claude the CLI looks for ~/.claude/.claude.json instead of ~/.claude.json.
    from kotoyose.config import HarnessConfig
    from kotoyose.runtime.claude_auth import build_auth_env

    monkeypatch.setenv("HOME", str(tmp_path))
    def config_for(path):
        return HarnessConfig.model_validate({
            "connections": {"c": {"provider": "claude", "kind": "subscription", "data_policy": "no_training",
                                  "config_dir": str(path)}},
            "routes": {"r": {"driver": "claude.agent-sdk", "connection": "c", "model": "claude-haiku-5-5"}},
            "models": {"a": {"route": "r"}},
        })
    default = config_for(tmp_path / ".claude")
    assert "CLAUDE_CONFIG_DIR" not in build_auth_env(default.preset_for("a"), default, lambda: None)
    custom = config_for(tmp_path / "login-home")
    assert build_auth_env(custom.preset_for("a"), custom, lambda: None)["CLAUDE_CONFIG_DIR"] == str(tmp_path / "login-home")


def test_turn_timing_reports_phases_without_text(capsys):
    from kotoyose.child_turn import _TurnTiming

    timing = _TurnTiming()
    timing.mark("prepare")
    timing.cold = True
    timing.mark("start")
    timing.first_output()
    timing.first_output()
    timing.outcome = "ok"
    timing.report(7)
    line = capsys.readouterr().err.strip()
    assert line.startswith("turn 7 ok (cold): prepare ") and line.count("first output") == 1 and "total" in line
