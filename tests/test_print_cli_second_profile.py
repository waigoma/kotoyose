"""Exercise the extension points using a test-only CLI and an unrelated output format."""

import asyncio
from dataclasses import replace
from pathlib import Path
import sys
from textwrap import dedent
from types import SimpleNamespace

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from kotoyose import cli_login, connections_probe, container_print, model_discovery, print_cli_hooks, print_cli_profiles
from kotoyose.agent_deadlines import AgentStartRejected
from kotoyose.agent_route import resolve_declared_agent
from kotoyose.config import HarnessConfig
from kotoyose.connection_state_store import ConnectionStateStore
from kotoyose.connections_probe import probe_connection
from kotoyose.execution_resources import DriverInstall
from kotoyose.print_cli_agent import PrintCliJob, PrintCliWorker
from kotoyose.print_cli_events import PrintCliStreamOutcome
from kotoyose.print_cli_home import PrintCliHomeBusyError
from kotoyose.print_cli_profiles import PrintCliLoginSpec, PrintCliProfile, QuotaErrorPattern, SurfaceCliSpec
from kotoyose.routes import admin_settings
from kotoyose.runtime.base import RuntimeEvent


class FakeParser:
    def __init__(self):
        self.content = ""

    def feed_line(self, line):
        prefix, self.content = line.rstrip("\r\n").split(":", 1)
        assert prefix == "DONE"
        return [RuntimeEvent(kind="assistant_text", summary=self.content)]

    def finalize(self):
        return PrintCliStreamOutcome(saw_result=True, result_status="SUCCESS", content=self.content)


def fake_models(stdout):
    label, model = stdout.decode().strip().split("|")
    return [{"id": model, "label": label, "name": label}]


def fake_quota(stdout: bytes, *, stderr: bytes) -> print_cli_hooks.ParsedQuota:
    assert stderr == b""
    return {
        "auth": "ok",
        "quota": [{"window": "session", "used_percent": int(stdout)}],
        "last_error": None,
    }


@pytest.fixture
def fake_profile(monkeypatch, request):
    profile = PrintCliProfile(
        id="fakecli",
        # Sharing the provider with agy detects any lookup that silently picks its default.
        provider=getattr(request, "param", "antigravity"),
        binary="fakecli-not-on-path",
        min_version=(1,),
        argv_template=("ask", "{model}", "{prompt}"),
        surfaces={"read": SurfaceCliSpec()},
        settings_flag=None,
        parser="fake_stream",
        login_home_env="FAKE_HOME",
        exit_codes={},
        quota_error_patterns=(QuotaErrorPattern("custom_quota", http_statuses=(429,)),),
        classify_stderr="fake_stderr",
        parse_models="fake_models",
        parse_quota="fake_quota",
        models_command=("catalog",),
        quota_command=("balance",),
        fixed_env={"CLI_MODE": "managed"},
        login_flow="fake_login",
        login=PrintCliLoginSpec(
            command=("signin",),
            url_pattern=r"https://example\.invalid/login",
            url_hosts=("example.invalid",),
            code_prompt_pattern=r"Enter demo value",
            cli_timeout_seconds=10,
            success_json_field=None,
        ),
    )
    for module, attr in [
        (print_cli_profiles, "_REGISTRY"),
        (print_cli_hooks, "_parsers"),
        (print_cli_hooks, "_stderr"),
        (print_cli_hooks, "_models"),
        (print_cli_hooks, "_quota"),
        (print_cli_hooks, "_login"),
    ]:
        # Registration itself is part of the test, with registry state restored at teardown.
        monkeypatch.setattr(module, attr, dict(getattr(module, attr)))
    monkeypatch.setattr(
        print_cli_profiles, "PRINT_CLI_PROFILES", print_cli_profiles._REGISTRY,
    )
    print_cli_profiles.register_print_cli_profile(profile)
    print_cli_hooks.register_parser("fake_stream", FakeParser)
    print_cli_hooks.register_stderr_classifier(
        "fake_stderr", lambda stderr: {"http_status": 429} if stderr.strip() == "BUSY" else {},
    )
    print_cli_hooks.register_models_parser("fake_models", fake_models)
    print_cli_hooks.register_quota_parser("fake_quota", fake_quota)
    paste_code = print_cli_hooks.get_login_flow("paste_code")
    print_cli_hooks.register_login_flow("fake_login", replace(
        paste_code, succeeded=lambda spec, code, output: code == 0 and "accepted" in output,
    ))
    return profile


@pytest.fixture
def fake_binary(tmp_path):
    binary = tmp_path / "configured-executable"
    binary.write_text(f"#!{sys.executable}\n" + dedent("""
        import os
        from pathlib import Path
        import select
        import sys
        import time

        action, *args = sys.argv[1:]
        assert os.environ["CLI_MODE"] == "managed"
        assert "PARENT_ONLY" not in os.environ
        if action == "--version":
            print("Fake CLI 1.2")
            sys.exit(0)

        assert "FAKE_HOME" in os.environ
        if action == "ask":
            model, prompt = args
            print(f"DONE:{model}/{prompt}")
            if prompt == "over quota":
                print("BUSY", file=sys.stderr)
                sys.exit(1)
        elif action == "catalog":
            print("Fake Model|fake-model")
        elif action == "balance":
            print("21")
        elif action in {"browser", "device", "untrusted"}:
            if action == "untrusted":
                print("https://example.invalid@untrusted.example/login", flush=True)
                print("private PTY diagnostic")
                sys.exit(1)
            print("https://example.invalid/login", flush=True)
            if action == "device":
                print("User code: DEMO-123", flush=True)
            finish = Path(os.environ["FAKE_HOME"]) / "finish"
            while not finish.exists():
                assert not select.select([sys.stdin], [], [], 0.01)[0]
        elif action == "status":
            home = Path(os.environ["FAKE_HOME"])
            (home / "status-checked").touch()
            while (home / "hold-status").exists():
                time.sleep(0.01)
            print('{"signed_in": false}' if (home / "reject").exists() else '{"signed_in": true}')
        elif action == "signin":
            print("https://example.invalid/login", flush=True)
            print("Enter demo value", flush=True)
            if input() == "demo-value":
                print("accepted")
        else:
            sys.exit(2)
        """), encoding="utf-8")
    binary.chmod(0o700)
    return binary


@pytest.mark.parametrize(
    "fake_profile", ["antigravity", "second-provider"],
    indirect=["fake_profile"],
)
async def test_second_profile_across_host_entry_points(
    tmp_path, monkeypatch, fake_profile, fake_binary,
):
    home = tmp_path / "login"
    home.mkdir(mode=0o700)
    connection = {"provider": fake_profile.provider, "kind": "subscription", "data_policy": "no_training", "config_dir": home}
    connection["profile"] = fake_profile.id
    cfg = HarnessConfig(
        connections={"fake": connection},
        routes={"fake": {
            "driver": "cli.print", "profile": fake_profile.id,
            "connection": "fake", "model": "fake-model",
        }},
        models={"fake": {"route": "fake", "agent": {"place": "host", "surface": "read"}}},
    )
    settings = SimpleNamespace(drivers={"cli.print": DriverInstall(
        profile_binaries={"agy": Path("/unused/agy-binary"), fake_profile.id: fake_binary},
    )})
    service = SimpleNamespace(config=settings)
    monkeypatch.setattr(model_discovery, "get_config", lambda: cfg)
    model_discovery.clear_connection_discovery_cache()
    monkeypatch.setenv("CLI_MODE", "parent")
    monkeypatch.setenv("PARENT_ONLY", "fake-secret")

    resolved = resolve_declared_agent(
        "fake", cfg.preset_for("fake"), settings, cfg, resolve_preferences=False, resolve_prompts=False,
    )
    assert resolved.profile == fake_profile.id
    assert resolved.cli_binary == fake_binary
    assert print_cli_profiles.resolve_print_cli_binary(print_cli_profiles.AGY_PROFILE, settings) == Path(
        "/unused/agy-binary",
    )

    job = PrintCliJob(
        profile_id=resolved.profile, cli_binary=resolved.cli_binary, model=resolved.model,
        workspace=tmp_path, login_home=resolved.login_home, surface="read", instructions="",
        prompt="hello", output_contract="text", timeout_seconds=3, grace_seconds=0.2,
        output_limit_bytes=4096, memory_bytes=128 * 1024 * 1024, pids_limit=32, cpus=1.0,
    )
    events = []

    async def started(*args, **kwargs):
        pass

    async def event(kind, payload):
        events.append((kind, payload))

    worker = PrintCliWorker()
    result = await worker.execute(job, asyncio.Event(), on_process_started=started, on_event=event)
    assert result.status == "succeeded"
    assert result.content == b"fake-model/hello"
    assert any(payload.get("summary") == "fake-model/hello" for _, payload in events)
    limited = await worker.execute(
        replace(job, prompt="over quota"), asyncio.Event(), on_process_started=started, on_event=event,
    )
    assert limited.reason == "provider_usage_limit"

    discovered = await model_discovery.discover_models(
        fake_profile.provider, connection="fake", execution_service=service,
    )
    assert discovered["models"] == ["fake-model"]
    assert discovered["source"] == "fakecli_cli_models"
    via_alias = await model_discovery.discover_models(
        fake_profile.provider, preset=cfg.preset_for("fake"), execution_service=service,
    )
    assert via_alias == discovered

    store = ConnectionStateStore()
    state = await probe_connection("fake", cfg=cfg, execution_service=service, store=store)
    assert state["auth"] == "ok"
    assert state["cli_version"] == "Fake CLI 1.2"
    assert state["quota"][0]["used_percent"] == 21
    assert state["quota_source"] == "print_cli_usage"

    try:
        session = await cli_login.start_cli_login_session("fake", cfg=cfg, execution_service=service)
        assert session["url"] == "https://example.invalid/login"
        result = await cli_login.submit_cli_login_code(
            "fake", session["session_id"], "demo-value", cfg=cfg, execution_service=service, state_store=store,
        )
        assert result == {"state": "completed"}
    finally:
        await cli_login.shutdown_cli_login_sessions()


async def test_profile_without_quota_hook_reports_unknown(tmp_path, fake_profile, fake_binary):
    profile = replace(fake_profile, id="fake-no-quota", parse_quota=None)
    print_cli_profiles.register_print_cli_profile(profile)
    cfg = HarnessConfig(connections={"fake": {
        "provider": profile.provider, "kind": "subscription", "data_policy": "no_training", "profile": profile.id, "config_dir": tmp_path,
    }})
    service = SimpleNamespace(config=SimpleNamespace(drivers={"cli.print": DriverInstall(
        profile_binaries={profile.id: fake_binary},
    )}))
    state = await probe_connection("fake", cfg=cfg, execution_service=service, store=ConnectionStateStore())
    assert state["auth"] == "unknown"
    assert state["quota"] == []
    assert state["quota_source"] is None
    assert state["last_error"] is None


def test_route_rejects_different_profile_for_same_provider(tmp_path, fake_profile):
    with pytest.raises(ValueError, match=r"connections\.fake\.profile"):
        HarnessConfig(
            connections={"fake": {
                "provider": fake_profile.provider,
                "kind": "subscription", "data_policy": "no_training",
                "profile": "agy",
                "config_dir": tmp_path,
            }},
            routes={"fake": {
                "driver": "cli.print", "profile": fake_profile.id,
                "connection": "fake", "model": "fake-model",
            }},
        )

@pytest.fixture
def login_setup(tmp_path, fake_profile, fake_binary):
    def configure(profile):
        home = tmp_path / "login"
        home.mkdir()
        conn = {"provider": profile.provider, "kind": "subscription", "data_policy": "no_training", "profile": profile.id, "config_dir": home}
        cfg = HarnessConfig(connections={"fake": conn, "alias": conn})
        service = SimpleNamespace(config=SimpleNamespace(drivers={"cli.print": DriverInstall(
            profile_binaries={profile.id: fake_binary},
        )}))
        return home, cfg, service

    return configure


@pytest.mark.parametrize(("flow", "action", "code", "outcome"), [
    ("browser_wait", "browser", None, "completed"),
    ("device_code", "device", "DEMO-123", "completed"),
    ("device_code", "device", "DEMO-123", "failed"),
    ("browser_wait", "browser", None, "expired"),
    ("browser_wait", "browser", None, "cancelled"),
    ("browser_wait", "untrusted", None, "startup_error"),
])
async def test_browser_login_flows(fake_profile, login_setup, monkeypatch, flow, action, code, outcome):
    profile = replace(fake_profile, id=flow, login_flow=flow, login=replace(
        fake_profile.login, command=(action,), url_pattern=r"https://\S+",
        user_code_pattern=r"User code: ([A-Z0-9-]+)\n",
        status_command=("status",), success_json_field="signed_in", success_json_value=True,
    ))
    print_cli_profiles.register_print_cli_profile(profile)
    home, cfg, service = login_setup(profile)
    kwargs = {"cfg": cfg, "execution_service": service}
    try:
        if outcome == "startup_error":
            with pytest.raises(cli_login.CliLoginStartupError) as exc:
                await cli_login.start_cli_login_session("fake", **kwargs)
            assert "untrusted.example" not in str(exc.value)
            assert "private PTY diagnostic" not in str(exc.value)
            # Startup failure released the exclusive reservation.
            await probe_connection("fake", store=ConnectionStateStore(), **kwargs)
            return
        started = await cli_login.start_cli_login_session(
            "fake", **kwargs, ttl_seconds=1 if outcome == "expired" else 10,
        )
        sid = started["session_id"]
        assert started["flow"] == flow
        assert started["state"] == "waiting_for_browser"
        assert started["url"] == "https://example.invalid/login"
        assert started.get("user_code") == code
        assert ("user_code" in started) == (code is not None)
        assert await cli_login.get_cli_login_status("fake", sid) == started
        app = FastAPI()
        app.include_router(admin_settings.router)
        app.dependency_overrides[admin_settings.require_basic_auth] = lambda: None
        app.state.execution_service = service
        monkeypatch.setattr(admin_settings, "get_config", lambda: cfg)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            sent = await client.post(f"/admin/connections/fake/cli-login/{sid}/code", json={"code": "fake-value"})
            assert sent.status_code == 422
            assert sent.json()["detail"] == "This login flow does not accept a code"
            probe = await client.post("/admin/connections/alias/probe")
            assert probe.status_code == 409
            assert "login in progress" in probe.json()["detail"]
        assert not (home / "status-checked").exists()
        if outcome in {"completed", "failed"}:
            if outcome == "failed":
                (home / "reject").touch()
            (home / "hold-status").touch()
            (home / "finish").touch()
            async with asyncio.timeout(3):
                while not (home / "status-checked").exists():
                    await asyncio.sleep(0.01)
            with pytest.raises(PrintCliHomeBusyError, match="login in progress"):
                await probe_connection("alias", store=ConnectionStateStore(), **kwargs)
            (home / "hold-status").unlink()
        if outcome == "cancelled":
            await cli_login.cancel_cli_login_session("fake", sid)
        async with asyncio.timeout(3):
            while (status := await cli_login.get_cli_login_status("fake", sid))["state"] == "waiting_for_browser":
                await asyncio.sleep(0.01)
        assert status["state"] == outcome
        assert status["url"] is None
        assert "user_code" not in status
        assert (home / "status-checked").exists() == (outcome in {"completed", "failed"})
        await probe_connection("fake", store=ConnectionStateStore(), **kwargs)
    finally:
        await cli_login.shutdown_cli_login_sessions()


@pytest.mark.parametrize("operation", ["host", "jail", "probe"])
async def test_runs_and_probes_conflict_with_login_in_both_directions(
    tmp_path, monkeypatch, fake_profile, fake_binary, login_setup, operation,
):
    home, cfg, service = login_setup(fake_profile)
    kwargs = {"cfg": cfg, "execution_service": service}
    entered = asyncio.Event()
    release = asyncio.Event()

    async def pause(*args, **kwargs):
        entered.set()
        await release.wait()

    async def on_event(*args):
        pass

    async def run():
        if operation == "probe":
            return await probe_connection("alias", store=ConnectionStateStore(), **kwargs)
        job = PrintCliJob(
            profile_id=fake_profile.id, cli_binary=fake_binary, model="model", workspace=tmp_path,
            login_home=home, surface="read", instructions="", prompt="hello", output_contract="text",
            timeout_seconds=10, grace_seconds=0.2, output_limit_bytes=4096,
            memory_bytes=128 * 1024 * 1024, pids_limit=32, cpus=1.0,
            execution_environment="isolated" if operation == "jail" else "host",
        )
        worker = PrintCliWorker(container_runtime=(None, None, None))
        return await worker.execute(job, asyncio.Event(), on_process_started=pause, on_event=on_event)

    if operation == "jail":
        monkeypatch.setattr(container_print, "execute_container_print", pause)
    if operation == "probe":
        original = connections_probe.probe_cli_version

        async def paused_probe(*args, **kwargs):
            await pause()
            return await original(*args, **kwargs)

        monkeypatch.setattr(connections_probe, "probe_cli_version", paused_probe)

    task = asyncio.create_task(run())
    try:
        await asyncio.wait_for(entered.wait(), 3)
        with pytest.raises(cli_login.CliLoginConflictError, match="runs or probes are active"):
            await cli_login.start_cli_login_session("fake", **kwargs)
        release.set()
        await task

        started = await cli_login.start_cli_login_session("fake", **kwargs)
        with pytest.raises(cli_login.CliLoginConflictError, match="login in progress"):
            await cli_login.start_cli_login_session("alias", **kwargs)
        error = PrintCliHomeBusyError if operation == "probe" else AgentStartRejected
        with pytest.raises(error, match="login in progress"):
            await run()
        await cli_login.cancel_cli_login_session("fake", started["session_id"])
        await run()
    finally:
        release.set()
        try:
            await task
        finally:
            await cli_login.shutdown_cli_login_sessions()


@pytest.mark.asyncio
async def test_api_key_fakecli_uses_only_declared_host_environment(tmp_path, fake_profile, monkeypatch, caplog):
    from unittest.mock import AsyncMock
    from kotoyose.secret_store import SecretStore

    profile = replace(fake_profile, api_key_env="FAKECLI_API_KEY")
    monkeypatch.setitem(print_cli_profiles._REGISTRY, profile.id, profile)
    fake_secret = "synthetic-fakecli-secret"
    store = SecretStore(master_key=b"x" * 32)
    await store.set("fake-cli-key", fake_secret)
    monkeypatch.setattr("kotoyose.secret_store._ACTIVE_STORE", store)
    monkeypatch.setenv("PARENT_ONLY", fake_secret)
    binary = tmp_path / "fakecli"
    binary.write_text(f"#!{sys.executable}\n" + dedent(f"""
        import os
        import sys

        key = os.environ["FAKECLI_API_KEY"]
        assert key == {fake_secret!r}
        assert [name for name, value in os.environ.items() if value == key] == ["FAKECLI_API_KEY"]
        assert all(key not in arg for arg in sys.argv)
        assert sys.argv[1:3] == ["ask", "fake-model"]
        assert "hello" in sys.argv[3]
        assert os.path.isdir(os.environ["FAKE_HOME"])
        print("DONE: ran with " + key)
        print(key, file=sys.stderr)
        """))
    binary.chmod(0o700)
    cfg = HarnessConfig(
        connections={"api": {
            "provider": profile.provider, "kind": "api_key", "data_policy": "no_training", "profile": profile.id, "secret": "fake-cli-key",
        }},
        routes={"api": {"driver": "cli.print", "profile": profile.id, "connection": "api", "model": "fake-model"}},
        models={"fake": {"route": "api", "agent": {"place": "host", "surface": "read"}}},
    )
    raw = cfg.model_dump()
    raw["models"]["fake"]["agent"]["place"] = "isolated"
    with pytest.raises(ValueError, match="api_key.*host placement only"):
        HarnessConfig.model_validate(raw)
    raw["models"]["fake"]["agent"]["place"] = "host"
    raw["connections"]["api"]["secret"] = "env:FAKECLI_API_KEY"
    with pytest.raises(ValueError, match="secret-store reference"):
        HarnessConfig.model_validate(raw)

    settings = SimpleNamespace(drivers={"cli.print": DriverInstall(profile_binaries={profile.id: binary})})
    resolved = resolve_declared_agent(
        "fake", cfg.preset_for("fake"), settings, cfg, resolve_preferences=False, resolve_prompts=False,
    )
    assert resolved.login_home is None
    job = PrintCliJob(
        profile_id=resolved.profile, cli_binary=resolved.cli_binary, model=resolved.model,
        workspace=tmp_path, login_home=resolved.login_home, surface="read", instructions="",
        prompt="hello", output_contract="text", timeout_seconds=3, grace_seconds=0.2,
        output_limit_bytes=4096, memory_bytes=128 * 1024 * 1024, pids_limit=32, cpus=1.0,
        api_key_secret=resolved.api_key_secret, connection_id=resolved.connection_id,
    )
    events = AsyncMock()
    worker = PrintCliWorker()
    result = await worker.execute(job, asyncio.Event(), on_process_started=AsyncMock(), on_event=events)
    assert result.status == "succeeded"
    assert result.content == b" ran with [REDACTED]"
    assert fake_secret not in repr(events.call_args_list) + repr(job) + caplog.text
    with pytest.raises(AgentStartRejected, match="host placement"):
        await worker.execute(replace(job, execution_environment="isolated"), asyncio.Event(),
                             on_process_started=AsyncMock(), on_event=events)

    from kotoyose.config import get_client
    from kotoyose.execution_agent import AgentExecutor
    from tests.test_data_policy import approve
    from tests.test_print_cli_admission_and_agent_execution import _setup_admission_Fixture

    request, admission, memory, native_cfg, harness_cfg, _, _ = _setup_admission_Fixture(
        tmp_path, monkeypatch, prompt_text="hello",
    )
    route_name = harness_cfg.models["agy-coder"].route
    harness_cfg.routes[route_name] = cfg.routes["api"]
    harness_cfg.connections["api"] = cfg.connections["api"]
    native_cfg.drivers["cli.print"].profile_binaries[profile.id] = binary
    approve(harness_cfg, "tools", "workspace", alias="agy-coder")
    plan = await admission.resolve("caller-a", get_client("caller-a"), request)
    assert plan["api_key_secret"] == "fake-cli-key"
    assert plan["login_home"] is None
    assert fake_secret not in repr(plan)
    attempt = {
        "id": "att-api-key", "principal": "caller-a", "status": "running", "generation": 0,
        "request": request.model_dump(mode="json"), "snapshot": plan,
    }
    memory.attempts[attempt["id"]] = attempt
    executor = AgentExecutor(native_cfg, memory, object())
    dispatched = await executor.execute(attempt, asyncio.Event())
    assert dispatched.status == "succeeded"
    assert dispatched.content == b" ran with [REDACTED]"
    assert fake_secret not in caplog.text


def test_agy_rejects_api_key_connections():
    with pytest.raises(ValueError, match="agy.*api_key.*api_key_env"):
        HarnessConfig(
            connections={"api": {"provider": "antigravity", "kind": "api_key", "data_policy": "no_training", "secret": "fake-cli-key"}},
            routes={"api": {"driver": "cli.print", "profile": "agy", "connection": "api", "model": "fake-model"}},
        )


async def test_browser_login_completion_refreshes_connection_state(fake_profile, login_setup, monkeypatch):
    """ブラウザで完了する login が終わったら、次の定期確認を待たずに接続の状態を更新する。"""
    profile = replace(fake_profile, id="browser_refresh", login_flow="browser_wait", login=replace(
        fake_profile.login, command=("browser",), url_pattern=r"https://\S+",
        status_command=("status",), success_json_field="signed_in", success_json_value=True,
    ))
    print_cli_profiles.register_print_cli_profile(profile)
    home, cfg, service = login_setup(profile)
    probed: list[str] = []

    async def fake_probe(conn_id, **kwargs):
        probed.append(conn_id)

    monkeypatch.setattr(cli_login, "probe_connection", fake_probe)
    try:
        started = await cli_login.start_cli_login_session("fake", cfg=cfg, execution_service=service, ttl_seconds=10)
        (home / "finish").touch()
        async with asyncio.timeout(3):
            while (await cli_login.get_cli_login_status("fake", started["session_id"]))["state"] == "waiting_for_browser":
                await asyncio.sleep(0.01)
        async with asyncio.timeout(3):
            while not probed:
                await asyncio.sleep(0.01)
        assert probed == ["fake"]
    finally:
        await cli_login.shutdown_cli_login_sessions()


def test_provider_model_discovery_finds_a_unique_profile():
    assert print_cli_profiles.get_print_cli_profile_for_provider("cursor").id == "cursor"
    assert print_cli_profiles.get_print_cli_profile_for_provider("no-such-provider") is None
