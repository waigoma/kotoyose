"""Saved Codex preferences, with no real provider, credentials or runtime artifacts."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from kotoyose.config import AgentRoute, AliasAgent, Connection, HarnessConfig, ModelPreset
from kotoyose.codex_model_policy import model_catalog
from kotoyose.codex_preferences import resolve_codex_preferences, CodexModelPreferences
from kotoyose.execution_resources import (
    DriverCell, DriverInstall, NativeRuntimeProfile, resolve_agent_model, ExecutionError, content_digest,
)
from kotoyose.execution_snapshot import resolved_attempt_snapshot
from kotoyose.native_claude import NativeAgentJob, NativeAgentResult
from kotoyose.native_codex import NativeCodexWorker
from kotoyose.codex_egress import CodexEgress
from tests.test_execution_resource_pins import _admission


def setup_alias(effort="max", codex=None):
    profile = NativeRuntimeProfile(provider="codex", model="gpt-6-luna", cli_binary=Path("/bin/sh"),
        credential_mode="environment", credential_env="FIXTURE_ONLY", credential_name="OPENAI_API_KEY",
        codex_rootfs=Path("/fixture-rootfs-not-created"), codex_rootfs_digest="0" * 64)
    config = HarnessConfig(
        connections={"fixture-only": Connection(provider="codex", kind="api_key", data_policy="no_training", secret="env:FIXTURE_ONLY")},
        routes={"coding": AgentRoute(driver="codex.app-server", model="gpt-6-luna", connection="fixture-only")},
        models={"coding": ModelPreset(
            effort=effort, codex=codex or {}, route="coding",
            agent=AliasAgent(place="isolated", surface="exec"),
        )},
    )
    settings = SimpleNamespace(
        drivers={"codex.app-server": DriverInstall(
            cli_binary=profile.cli_binary,
            cells={"standard": DriverCell(rootfs=profile.codex_rootfs, rootfs_digest=profile.codex_rootfs_digest)},
        )},
        agent_compression_mode="runtime_managed",
    )
    return profile, config, settings


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
def test_saved_alias_effort_is_exact_and_profile_is_not_mutated(effort):
    profile, config, settings = setup_alias(effort)
    before = profile.model_dump()
    resolved = resolve_agent_model("coding", settings, config=config)
    assert resolved.codex_preferences.requested_effort == effort
    assert resolved.codex_preferences.effort == effort
    assert profile.model_dump() == before
    assert "codex_preferences" not in before
    assert resolved.cli_binary == profile.cli_binary
    assert resolved.credential_mode == profile.credential_mode
    assert resolved.codex_rootfs == profile.codex_rootfs


def test_alias_none_means_explicit_native_default_while_direct_stays_low():
    profile, config, settings = setup_alias(None)
    resolved = resolve_agent_model("coding", settings, config=config)
    assert resolved.codex_preferences.model_dump() == {
        "requested_effort": None, "effort": "low", "timeout_seconds": None,
    }
    assert not hasattr(profile, "codex_preferences")
    direct = model_catalog("gpt-6-luna")["models"][0]
    assert direct["default_reasoning_level"] == "low"
    assert direct["supported_reasoning_levels"] == [{"effort": "low", "description": "Bounded native execution"}]
    # Unknown direct model IDs retain the historical low-only contract too.
    assert model_catalog("reviewed-custom-model")["models"][0]["default_reasoning_level"] == "low"


@pytest.mark.parametrize("model,effort", [("gpt-6-luna", "ultra"), ("gpt-6-luna", "unknown"),
    ("gpt-6-luna", ""), ("other", "max"), ("other", None)])
def test_unsupported_model_effort_never_falls_back(model, effort):
    with pytest.raises(ValueError, match="unsupported_codex_reasoning_effort"):
        resolve_codex_preferences(model, effort)


def test_snapshot_preferences_cannot_claim_an_effort_downgrade():
    with pytest.raises(ValidationError):
        CodexModelPreferences(requested_effort="max", effort="low")


def test_incompatible_sandbox_is_rejected_and_other_security_options_are_not_copied():
    profile, config, settings = setup_alias(codex={
        "approval_mode": "never", "sandbox": "danger-full-access",
        "config": {"model_provider": "unreviewed", "features.shell_tool": True},
    })
    with pytest.raises(ValueError, match="requires read-only"):
        resolve_agent_model("coding", settings, config=config)
    config.models["coding"].codex["sandbox"] = "read-only"
    resolved = resolve_agent_model("coding", settings, config=config)
    assert "codex_sandbox" not in resolved.model_dump()
    assert set(resolved.model_dump()) - set(profile.model_dump()) <= {"codex_preferences"}
    assert resolved.codex_preferences.effort == "max"
    assert "danger-full-access" not in json.dumps(resolved.model_dump(mode="json"))


def test_security_revalidation_does_not_reinterpret_later_preset_preferences():
    profile, config, settings = setup_alias()
    saved = resolve_agent_model("coding", settings, config=config).codex_preferences
    config.models["coding"].effort = "unsupported-later-edit"
    frozen = resolve_agent_model("coding", settings, config=config, resolve_preferences=False)
    assert frozen.model_dump() == profile.model_dump()
    assert saved.effort == "max"
    with pytest.raises(ValueError):
        resolve_agent_model("coding", settings, config=config)


async def admitted_snapshot(tmp_path, monkeypatch):
    from kotoyose import config as config_module, codex_rootfs
    (tmp_path / "workspace").mkdir()
    resources = {}
    admission, policy, request = _admission(resources, tmp_path,
        grant_refs=["timeout", "budget", "definition"])
    profile, config, settings = setup_alias()
    monkeypatch.setattr(config_module, "get_config", lambda: config)
    def legacy_rootfs(root, digest, *, coding=False):
        assert root == profile.codex_rootfs and digest == profile.codex_rootfs_digest
        assert coding is False
        return Path("/fixture-rootfs-not-created")
    monkeypatch.setattr(codex_rootfs, "validate_rootfs", legacy_rootfs)
    admission.settings = settings
    policy = policy.model_copy(update={"agent_models": ["coding"]})
    resources[("caller-a", "grant")] = resources[("caller-a", "grant")].model_copy(update={"agent_models": ["coding"]})
    request = request.model_copy(update={"model": "coding", "runtime_profile": None, "inputs": [], "skill_refs": []})
    return await admission.resolve("caller-a", policy, request), request, config, settings


async def test_admission_and_public_snapshot_pin_preferences_once(tmp_path, monkeypatch):
    snapshot, request, config, _ = await admitted_snapshot(tmp_path, monkeypatch)
    expected = {"requested_effort": "max", "effort": "max", "timeout_seconds": None}
    assert snapshot["codex_preferences"] == expected
    config.models["coding"].effort = "low"
    attempt = {"id": "attempt", "sequence": 0, "request_digest": content_digest(request.model_dump(mode="json")),
        "request": request.model_dump(mode="json"), "snapshot": snapshot}
    public = resolved_attempt_snapshot(attempt)
    assert public.codex_preferences.model_dump() == expected
    assert public.snapshot_digest == content_digest(snapshot)
    changed = deepcopy(snapshot)
    changed["codex_preferences"] = {"requested_effort": "low", "effort": "low", "timeout_seconds": None}
    assert content_digest(changed) != public.snapshot_digest
    # Old receipt snapshots remain readable and are not retroactively assigned max.
    del snapshot["codex_preferences"]
    assert "codex_preferences" not in resolved_attempt_snapshot(attempt).model_dump(mode="json")


async def test_unsupported_alias_fails_before_workspace_or_provider_access(tmp_path):
    from kotoyose.execution_admission import ExecutionAdmission
    from kotoyose.execution_contract import AgentExecution
    from kotoyose.capabilities import capability_manifest
    from kotoyose import config as config_module
    profile, config, settings = setup_alias()
    config.models["coding"].effort = "unsupported"
    store = SimpleNamespace(workspace=AsyncMock())
    admission = ExecutionAdmission(store, capability_manifest(), "fixture", settings=settings)
    request = AgentExecution(schema_version="1", kind="agent", idempotency_key="key", caller_run_ref="run",
        workspace_ref="workspace", execution_grant_ref="grant", budgets_ref="budget", agent_definition_ref="definition", model="coding", inputs=[{"kind": "task_request", "provenance": "project", "content_ref": "prompt"}])
    from unittest.mock import patch
    with patch.object(config_module, "get_config", return_value=config):
        with pytest.raises(ExecutionError, match="unsupported_codex_reasoning_effort"):
            # chat 専用 caller の門 (workspace_roots が空なら 403) を越えるため root を 1 つ持たせる。
            await admission.resolve("caller", SimpleNamespace(workspace_roots=[tmp_path]), request)
    store.workspace.assert_not_awaited()


async def test_existing_dispatch_key_replays_without_re_resolving_preferences():
    from kotoyose.execution_service import ExecutionService
    request = SimpleNamespace(idempotency_key="same", model_dump=lambda **kwargs: {"idempotency_key": "same"})
    service = SimpleNamespace(store=SimpleNamespace(replay=AsyncMock(return_value={"id": "original-attempt"})),
        admission=SimpleNamespace(resolve=AsyncMock(side_effect=AssertionError("must not resolve"))))
    assert await ExecutionService.dispatch(service, "caller", SimpleNamespace(), request) == "original-attempt"
    service.admission.resolve.assert_not_awaited()


def make_job(effort="max"):
    return NativeAgentJob(attempt_id="fixture", model="gpt-6-luna", cli_binary=Path("/bin/sh"),
        credential_name="OPENAI_API_KEY", credential_value="fixture-not-a-credential", instructions="fixture", prompt="hello",
        output_contract="text", max_turns=2, timeout_seconds=10, grace_seconds=.1, output_limit_bytes=1024,
        tool_refs=(), reasoning_effort=effort)


class FakeRPC:
    def __init__(self, effort="max"):
        self.effort = effort
        self.calls = []
        self.image_uris = ()
        self.turn_id = None
        self.events = iter([
            {"method": "item/completed", "params": {"threadId": "thread", "turnId": "turn", "item": {
                "id": "answer", "type": "agentMessage", "phase": "final_answer", "text": "done"}}},
            {"method": "turn/completed", "params": {"threadId": "thread", "turn": {"id": "turn", "status": "completed"}}},
        ])

    async def request(self, method, params):
        self.calls.append((method, params))
        return {"initialize": {}, "thread/start": {"thread": {"id": "thread"}, "model": "gpt-6-luna",
            "modelProvider": "kotoyose_native", "reasoningEffort": self.effort}, "turn/start": {"turn": {"id": "turn"}}}[method]

    async def send(self, message):
        pass

    async def next(self):
        return next(self.events)


async def test_exact_effort_reaches_turn_without_widening_tools_or_permissions():
    rpc = FakeRPC()
    result = await NativeCodexWorker()._run(rpc, make_job(), asyncio.Event(), AsyncMock(), {}, {}, {})
    assert result == b"done"
    thread = next(params for method, params in rpc.calls if method == "thread/start")
    turn = next(params for method, params in rpc.calls if method == "turn/start")
    assert turn["effort"] == "max"
    assert thread["sandbox"] == "read-only" and thread["approvalPolicy"] == "never"
    assert thread["dynamicTools"] == [] and thread["environments"] == []


async def test_cli_effort_mismatch_stops_before_turn():
    rpc = FakeRPC("low")
    with pytest.raises(ValueError, match="provider_error"):
        await NativeCodexWorker()._run(rpc, make_job(), asyncio.Event(), AsyncMock(), {}, {}, {})
    assert not any(method == "turn/start" for method, _ in rpc.calls)


async def test_direct_job_retains_legacy_no_explicit_turn_effort():
    rpc = FakeRPC("low")
    await NativeCodexWorker()._run(rpc, make_job(None), asyncio.Event(), AsyncMock(), {}, {}, {})
    assert "effort" not in next(params for method, params in rpc.calls if method == "turn/start")


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
def test_catalog_keeps_security_metadata_fixed_and_exact_preference(effort):
    direct = model_catalog("gpt-6-luna")["models"][0]
    selected = model_catalog("gpt-6-luna", reasoning_effort=effort)["models"][0]
    assert selected["default_reasoning_level"] == effort
    assert [item["effort"] for item in selected["supported_reasoning_levels"]] == ["low", "medium", "high", "xhigh", "max"]
    for key in direct.keys() - {"default_reasoning_level", "supported_reasoning_levels"}:
        assert selected[key] == direct[key]


@pytest.mark.parametrize("reasoning", [None, {}, {"effort": "low"}, {"effort": "ultra"}, "max"])
def test_broker_rejects_effort_change_or_omission_before_upstream(reasoning):
    broker = CodexEgress(1, 4096, "gpt-6-luna", "fixture", None, reasoning_effort="max")
    request = b"POST /v1/responses HTTP/1.1\r\nauthorization: Bearer fixture\r\n\r\n"
    body = {"model": "gpt-6-luna", "stream": True, "store": False, "input": [], "reasoning": reasoning}
    try:
        with pytest.raises(ValueError, match="reasoning_effort_mismatch"):
            broker.validate_request(request, json.dumps(body).encode())
    finally:
        broker.directory.cleanup()


def test_broker_forwards_exact_effort():
    broker = CodexEgress(1, 4096, "gpt-6-luna", "fixture", None, reasoning_effort="max")
    request = b"POST /v1/responses HTTP/1.1\r\nauthorization: Bearer fixture\r\n\r\n"
    body = {"model": "gpt-6-luna", "stream": True, "store": False, "input": [], "reasoning": {"effort": "max"}}
    try:
        assert json.loads(broker.validate_request(request, json.dumps(body).encode()))["reasoning"] == {"effort": "max"}
    finally:
        broker.directory.cleanup()


@pytest.mark.parametrize("value", [False, True, 0, -1, float("nan"), float("inf"), "600"])
def test_invalid_preset_timeout_is_rejected_without_fallback(value):
    _, config, settings = setup_alias(codex={"timeout": value})
    with pytest.raises(ValueError):
        resolve_agent_model("coding", settings, config=config)


def test_timeout_uses_exact_host_adapter_key_and_precedence():
    _, config, settings = setup_alias(codex={"timeout": 600, "sandbox": "read-only", "approval_mode": "never"})
    config.models["coding"].timeout_seconds = 1200
    assert resolve_agent_model("coding", settings, config=config).codex_preferences.timeout_seconds == 600
    config.models["coding"].codex = {}
    assert resolve_agent_model("coding", settings, config=config).codex_preferences.timeout_seconds == 1200
    config.models["coding"].timeout_seconds = None
    # Raw CLI config is never treated as a source for native timeout or permissions.
    config.models["coding"].codex = {"config": {"timeout": 1, "sandbox": "danger-full-access"}}
    assert resolve_agent_model("coding", settings, config=config).codex_preferences.timeout_seconds is None


@pytest.mark.parametrize("preset_timeout,hard,absolute,expected", [(600, 10, 30, 10), (600, 1000, 800, 600),
    (600, 1000, 4, 4), (2, 10, 30, 2), (None, 10, 30, 10)])
async def test_pinned_job_and_workspace_tools_only_use_shorter_deadline(tmp_path, monkeypatch, preset_timeout, hard, absolute, expected):
    from kotoyose.execution_agent import AgentExecutor
    from kotoyose import execution_agent
    from kotoyose.execution_jobs import WorkerResult
    snapshot, request, config, settings = await admitted_snapshot(tmp_path, monkeypatch)
    snapshot["codex_preferences"]["timeout_seconds"] = preset_timeout
    snapshot["timeout"]["hard_seconds"] = hard
    snapshot["deadline"] = (datetime.now(timezone.utc) + timedelta(seconds=absolute)).isoformat()
    # Subsequent settings edits cannot alter the already-admitted values.
    config.models["coding"].effort = "low"
    config.models["coding"].codex = {"timeout": 99999}
    # Avoid reading any process environment or real credential source.
    # credential は process_env 経由で読む (env の scrub、ADR-0010 段 1)。実際の秘密には触れない。
    monkeypatch.setattr(execution_agent.process_env, "get", lambda name, default=None: {"FIXTURE_ONLY": "fixture-only"}.get(name, default))
    store = SimpleNamespace(prepare_operation=AsyncMock(return_value="operation"), resolve_operation=AsyncMock(),
        append_event=AsyncMock(), client_tools=SimpleNamespace())
    command_worker = SimpleNamespace(execute=AsyncMock(return_value=WorkerResult("succeeded", 0, b"", b"", "confirmed", "done")))
    captured = []

    async def runtime_execute(job, cancel, execute_tool, **kwargs):
        captured.append(job)
        await execute_tool(("true",), ".")
        return NativeAgentResult("succeeded", "completed", b"fixture", "confirmed", {})

    executor = object.__new__(AgentExecutor)
    executor.config = settings
    executor.store = store
    executor.worker = command_worker
    executor.codex_runtime = SimpleNamespace(execute=runtime_execute)
    executor._render_inputs = AsyncMock(return_value=("fixture", "fixture", []))
    attempt = {"id": "attempt", "principal": "caller-a", "generation": 0, "snapshot": snapshot,
        "request": request.model_dump(mode="json")}
    result = await executor.execute(attempt, asyncio.Event())
    assert result.status == "succeeded"
    assert captured[0].reasoning_effort == "max"
    assert expected - .5 < captured[0].timeout_seconds <= expected
    assert captured[0].deadline_monotonic <= asyncio.get_running_loop().time() + expected
    tool_job = command_worker.execute.call_args.args[0]
    assert 0 < tool_job.timeout_seconds <= captured[0].timeout_seconds
    assert snapshot["codex_preferences"]["timeout_seconds"] == preset_timeout


async def test_expired_absolute_deadline_rejects_before_any_model_or_launch():
    worker = NativeCodexWorker()
    job = replace(make_job(), deadline_monotonic=asyncio.get_running_loop().time() - 1)
    started, tools = AsyncMock(), AsyncMock()
    result = await worker.execute(job, asyncio.Event(), tools, on_process_started=started)
    assert (result.status, result.reason) == ("timeout", "timeout")
    started.assert_not_awaited()
    tools.assert_not_awaited()


@pytest.mark.parametrize("changed", [
    {"requested_effort": "low", "effort": "low", "timeout_seconds": 600},
    {"requested_effort": "max", "effort": "max", "timeout_seconds": 1200},
    None,
])
def test_resume_does_not_silently_change_pinned_preferences(changed):
    from kotoyose.execution_resume import build_resume_manifest
    from kotoyose.runtime.resume import ResumeSupport
    old = {"kind": "agent", "provider": "codex", "workspace_id": "workspace", "generation": 0,
        "codex_preferences": {"requested_effort": "max", "effort": "max", "timeout_seconds": 600}}
    previous = {"snapshot": old, "request": {"caller_run_ref": "run"}, "workspace_id": "workspace", "generation": 0}
    current = {**old, "generation": 1, "codex_preferences": changed}
    request = SimpleNamespace(execution=SimpleNamespace(kind="agent", caller_run_ref="run"),
        predecessor_snapshot_digest=content_digest(old))
    with pytest.raises(ExecutionError) as error:
        build_resume_manifest("caller", previous, [], request, current, ResumeSupport(("durable_state",), "fixture"))
    assert error.value.reason_code == "runtime_mismatch"


async def test_worker_catalog_config_turn_and_broker_all_use_one_pinned_effort(tmp_path, monkeypatch):
    import tempfile
    from kotoyose import native_codex
    captured = {}
    rpc = FakeRPC("max")

    async def blocked_stderr():
        await asyncio.Event().wait()

    rpc.drain_stderr = blocked_stderr

    class FakeBroker:
        def __init__(self, timeout, byte_limit, model, credential, tool_schema, **kwargs):
            self.directory = tempfile.TemporaryDirectory(dir=tmp_path)
            self.failure_reason = None
            self.failure_event = asyncio.Event()
            captured["broker_effort"] = kwargs["reasoning_effort"]
            captured["broker_timeout"] = timeout

        async def start(self):
            return Path(self.directory.name) / "never-opened.sock"

        async def close(self, grace):
            self.directory.cleanup()
            return "confirmed"

        def redact(self, value):
            return value

    process = SimpleNamespace(pid=123456, returncode=0, stdout=None, stderr=None, wait=AsyncMock(return_value=0))

    async def spawn(*args, **kwargs):
        captured["catalog"] = json.loads(Path(kwargs["env"]["KOTOYOSE_NATIVE_CODEX_MODEL_CATALOG"]).read_text())
        captured["argv"] = args
        return process

    monkeypatch.setattr(native_codex, "CodexEgress", FakeBroker)
    def legacy_rootfs(root, digest, *, coding=False):
        assert root is None and digest is None
        assert coding is False
        return Path("/fixture-never-created")
    monkeypatch.setattr(native_codex, "validate_rootfs", legacy_rootfs)
    monkeypatch.setattr(native_codex, "capture_process_identity", lambda *args: SimpleNamespace(pid=123456))
    monkeypatch.setattr(native_codex, "stop_codex_process", AsyncMock(return_value="confirmed"))
    monkeypatch.setattr(native_codex, "_Protocol", lambda *args: rpc)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    result = await NativeCodexWorker(Path("/fixture-not-executed")).execute(make_job(), asyncio.Event(), AsyncMock(),
        on_process_started=AsyncMock())
    assert result.status == "succeeded"
    assert captured["catalog"]["models"][0]["default_reasoning_level"] == "max"
    assert captured["broker_effort"] == "max"
    thread = next(params for method, params in rpc.calls if method == "thread/start")
    turn = next(params for method, params in rpc.calls if method == "turn/start")
    assert thread["config"]["model_reasoning_effort"] == turn["effort"] == "max"
    assert thread["config"]["features.shell_tool"] is False
    assert thread["config"]["agents.enabled"] is False
    assert thread["sandbox"] == "read-only" and thread["approvalPolicy"] == "never"
    assert captured["broker_timeout"] <= 10


@pytest.mark.parametrize("effort,mutation,accepted", [(None, None, True), ("max", None, True),
    ("max", {"shell_type": "shell_command"}, False), ("max", {"tool_mode": "code_mode_only"}, False),
    ("max", {"default_reasoning_level": "ultra"}, False)])
def test_launcher_accepts_only_canonical_restricted_effort_catalog(tmp_path, monkeypatch, effort, mutation, accepted):
    from kotoyose import codex_provider_jail
    catalog = model_catalog("gpt-6-luna", reasoning_effort=effort)
    if mutation:
        catalog["models"][0].update(mutation)
    path = tmp_path / "models.json"
    path.write_text(json.dumps(catalog))
    execute = Mock(side_effect=RuntimeError("fixture-accepted-no-execution"))
    monkeypatch.setattr(codex_provider_jail, "os", SimpleNamespace(environ={
        "KOTOYOSE_NATIVE_CODEX_BINARY": "/fixture", "KOTOYOSE_NATIVE_CODEX_ROOTFS": "/fixture", "KOTOYOSE_NATIVE_CODEX_ROOTFS_DIGEST": "fixture",
        "KOTOYOSE_NATIVE_CODEX_EGRESS_SOCKET": "/fixture", "KOTOYOSE_NATIVE_CODEX_MODEL_CATALOG": str(path),
    }, execve=execute))
    monkeypatch.setattr(codex_provider_jail, "_trusted_binary", lambda *args: Path("/fixture"))
    def legacy_rootfs(root, digest, *, coding=False):
        assert root == Path("/fixture") and digest == "fixture"
        assert coding is False
        return Path("/fixture")
    monkeypatch.setattr(codex_provider_jail, "validate_rootfs", legacy_rootfs)
    monkeypatch.setattr(codex_provider_jail.shutil, "which", lambda *args: "/fixture")
    monkeypatch.setattr(codex_provider_jail, "build_jail_command", lambda *args, **kwargs: ["fixture"])
    monkeypatch.setattr(codex_provider_jail, "provider_environment", lambda *args, **kwargs: {})
    with pytest.raises(RuntimeError if accepted else ValueError):
        codex_provider_jail.main()
    assert execute.call_count == int(accepted)


def test_system_python_launcher_boundary_has_no_site_package_dependencies():
    """The real launcher uses sanitized PATH, not the controller's virtualenv."""
    import subprocess
    import sys
    source = str(Path(__file__).resolve().parents[1] / "src")
    script = '''
import json
import sys
sys.path.insert(0, sys.argv[1])
import kotoyose.codex_provider_jail
from kotoyose.codex_model_policy import model_catalog, validate_reasoning_effort
assert not any(name == "pydantic" or name.startswith("pydantic.") for name in sys.modules)
assert "kotoyose.codex_preferences" not in sys.modules
assert model_catalog("gpt-6-luna")["models"][0]["default_reasoning_level"] == "low"
assert model_catalog("gpt-6-luna", reasoning_effort="max")["models"][0]["default_reasoning_level"] == "max"
try:
    validate_reasoning_effort("gpt-6-luna", "ultra")
except ValueError as error:
    assert str(error) == "unsupported_codex_reasoning_effort"
else:
    raise AssertionError("Unsupported effort was accepted")
print("stdlib-only launcher/catalog import and effort validation passed")
'''
    result = subprocess.run([sys.executable, "-I", "-S", "-c", script, source],
        capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "stdlib-only launcher/catalog import and effort validation passed"
