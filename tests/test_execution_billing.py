"""No provider process or credential discovery is needed to check billing admission."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kotoyose.capabilities import CapabilitySupport, capability_manifest
from kotoyose.config import AgentRoute, AliasAgent, Connection, HarnessConfig
from kotoyose.execution_admission import ExecutionAdmission
from kotoyose.execution_billing import require_runtime_selection
from kotoyose.execution_contract import ExecutionEnvelope
from kotoyose.execution_resources import (
    AgentDefinition, CallerPolicy, ExecutionBudget, ExecutionError, ExecutionGrant,
    DriverInstall, InputText, NativeExecutionConfig, TimeoutPolicy,
)
from kotoyose.execution_service import ExecutionService
from kotoyose.native_claude import NativeAgentResult, _unknown_usage


def envelope(**updates):
    return {"execution": {
        "schema_version": "1", "kind": "agent", "model": "taskhub-pm",
        "idempotency_key": "fixture-key", "caller_run_ref": "fixture-run",
        "workspace_ref": "workspace:fixture:0", "execution_grant_ref": "grant",
        "budgets_ref": "budget", "agent_definition_ref": "definition",
        "inputs": [{"kind": "instructions", "provenance": "trusted", "content_ref": "input"}],
        **updates,
    }}


def test_selection_requirements_are_optional_and_do_not_change_old_wire():
    wire = ExecutionEnvelope.model_validate(envelope()).model_dump(mode="json")["execution"]
    assert "required_provider" not in wire and "required_billing_mode" not in wire
    selected = ExecutionEnvelope.model_validate(envelope(required_provider="claude", required_billing_mode="subscription"))
    assert selected.execution.required_provider == "claude"
    assert selected.execution.required_billing_mode == "subscription"
    assert "expected_runtime_binding_digest" not in selected.execution.model_dump(mode="json")


@pytest.mark.parametrize("extra", [
    {"required_billing_mode": "unknown"}, {"required_billing_mode": "metered"},
    {"required_provider": "unregistered-provider"}, {"metered_fallback": "allowed"},
])
def test_unknown_or_relaxed_constraints_are_not_silently_ignored(extra):
    with pytest.raises(ValueError):
        ExecutionEnvelope.model_validate(envelope(**extra))


def test_provider_mismatch_is_independent_of_billing_constraint():
    with pytest.raises(ExecutionError) as caught:
        require_runtime_selection({"backend": "direct", "provider": "cursor"}, provider="claude", billing_mode=None)
    assert (caught.value.status, caught.value.reason_code) == (403, "runtime_provider_mismatch")


@pytest.mark.parametrize("runtime", [
    {"backend": "direct", "provider": "claude", "model": "cc/sonnet"},
    {"backend": "native", "provider": "claude", "model": "cc/sonnet", "credential_name": "ANTHROPIC_AUTH_TOKEN"},
    {"backend": "native", "provider": "claude", "model": "sonnet", "credential_name": "CLAUDE_CODE_OAUTH_TOKEN"},
])
def test_alias_prefix_and_credential_name_do_not_establish_subscription(runtime):
    with pytest.raises(ExecutionError) as caught:
        require_runtime_selection(runtime, provider="claude", billing_mode="subscription")
    assert caught.value.reason_code == "billing_policy_unavailable"


def test_codex_existing_fixed_subscription_route_has_no_metered_fallback():
    runtime = {"backend": "native", "provider": "codex", "model": "fixed-model",
               "credential_mode": "chatgpt_subscription", "credential_name": "CODEX_CHATGPT_AUTH",
               "codex_auth_file": "/synthetic/not-read", "codex_auth_identity": "fixture-account-digest"}
    result = require_runtime_selection(runtime, provider="codex", billing_mode="subscription")
    assert result["mode"] == "subscription" and result["metered_fallback"] == "forbidden"
    assert len(result["policy_digest"]) == 64
    assert "/synthetic" not in str(result) and "fixture-account-digest" not in str(result)
    runtime["model"] = "changed-model"
    assert require_runtime_selection(runtime, provider="codex", billing_mode="subscription") != result
    runtime.update(credential_mode="environment", credential_name="OPENAI_API_KEY", credential_env="UNREAD")
    with pytest.raises(ExecutionError) as caught:
        require_runtime_selection(runtime, provider="codex", billing_mode="subscription")
    assert caught.value.reason_code == "billing_mode_mismatch"


@pytest.fixture
def admission_fixture(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    info = workspace.stat()
    from kotoyose.config import Client
    config = HarnessConfig(
        connections={"pm-conn": Connection(
            provider="claude", kind="endpoint", endpoint="http://127.0.0.1:19999/v1", secret="env:NEVER_READ_FIXTURE_TOKEN",
            data_policy="no_training",
        )},
        routes={"taskhub-pm": AgentRoute(
            driver="claude.agent-sdk", model="cc/claude-sonnet-5-5", connection="pm-conn",
        )},
        models={"taskhub-pm": {"route": "taskhub-pm", "agent": AliasAgent(place="isolated", surface="none")}},
        clients={"caller": Client(
            workspace_roots=[tmp_path], models=["taskhub-pm"],
            allowed_provenance=["trusted"], max_run_seconds=60, max_memory_bytes=128 * 1024 * 1024,
            max_processes=32, max_cpus=1, max_concurrent_runs=1,
        )},
    )
    monkeypatch.setattr("kotoyose.config._config", config)
    policy = CallerPolicy(workspace_roots=[tmp_path], agent_models=["taskhub-pm"],
        provenance=["trusted"], max_seconds=60, max_memory_bytes=128 * 1024 * 1024,
        max_pids=32, max_cpus=1, max_concurrent=1)
    settings = NativeExecutionConfig(schema_version="1", image_id="sha256:" + "a" * 64,
        drivers={"claude.agent-sdk": DriverInstall(cli_binary=Path("/usr/bin/true"))})
    now = datetime.now(timezone.utc)
    resources = {
        "input": InputText(schema_version="1", kind="text", text="Do not run this fixture.", provenance="trusted"),
        "timeout": TimeoutPolicy(schema_version="1", kind="timeout_policy", soft_seconds=1, hard_seconds=20, grace_seconds=1),
        "definition": AgentDefinition(schema_version="1", kind="agent_definition", instructions="fixture", max_turns=2,
                                      output_contract="text", timeout_policy_ref="timeout"),
        "budget": ExecutionBudget(schema_version="1", kind="budget", reservation_ref="fixture-reservation", mode="observed",
            deadline=now + timedelta(minutes=5), memory_bytes=64 * 1024 * 1024, pids_limit=16, cpus=1, output_limit_bytes=8192),
        "grant": ExecutionGrant(schema_version="1", kind="grant", grant_id="fixture-grant", version=1,
            policy_ref="policy:fixture", delegation="none",
            caller_run_ref="fixture-run", workspace_ref="workspace:fixture:0", isolation="sandbox", network="none",
            not_before=now - timedelta(seconds=1), expires_at=now + timedelta(minutes=5), max_seconds=30,
            agent_models=["taskhub-pm"], resource_refs=["definition", "budget", "input", "timeout"]),
    }
    async def resource(principal, ref):
        return resources[ref]
    store = SimpleNamespace(resource=resource, workspace=AsyncMock(return_value={"id": "fixture", "generation": 0,
        "path": str(workspace), "device": info.st_dev, "inode": info.st_ino, "caller_ref": "fixture"}))
    manifest = capability_manifest()
    for name in manifest.capabilities:
        manifest.capabilities[name] = CapabilitySupport(status="supported", reason="Admission-only fixture")
    admission = ExecutionAdmission(store, manifest, settings.image_id, settings=settings)
    return admission, policy, config


async def test_actual_registry_and_admission_resolve_saved_alias_once(admission_fixture, monkeypatch):
    admission, policy, config = admission_fixture
    # Change global registry only after _effective_request has resolved the alias.
    original_workspace = admission.store.workspace
    async def workspace(*args):
        config.routes["taskhub-pm"].model = "changed-after-resolution"
        return await original_workspace(*args)
    admission.store.workspace = workspace
    monkeypatch.setattr("asyncio.create_subprocess_exec", AsyncMock(side_effect=AssertionError("no process")))
    request = ExecutionEnvelope.model_validate(envelope(required_provider="claude")).execution
    plan = await admission.resolve("caller", policy, request)
    assert plan["alias"] == "taskhub-pm" and plan["model"] == "cc/claude-sonnet-5-5"
    assert plan["base_url"] == "http://127.0.0.1:19999/v1"
    assert plan["credential_env"] == "NEVER_READ_FIXTURE_TOKEN"
    assert "runtime_billing" not in plan


async def test_actual_admission_rejects_unestablished_subscription_before_attempt(admission_fixture):
    admission, policy, _ = admission_fixture
    request = ExecutionEnvelope.model_validate(envelope(required_provider="claude", required_billing_mode="subscription")).execution
    with pytest.raises(ExecutionError) as caught:
        await admission.resolve("caller", policy, request)
    assert caught.value.reason_code == "billing_policy_unavailable"


async def test_dispatch_billing_rejection_cannot_reach_durable_admit_or_schedule(admission_fixture):
    admission, policy, _ = admission_fixture
    service = object.__new__(ExecutionService)
    service._accepting = True
    service.admission = admission
    service.store = SimpleNamespace(replay=AsyncMock(return_value=None),
        admit=AsyncMock(side_effect=AssertionError("rejected request reached durable admission")))
    service._prepare_remote = AsyncMock(side_effect=AssertionError("rejected request prepared worker"))
    service._schedule = lambda _: (_ for _ in ()).throw(AssertionError("rejected request scheduled worker"))
    request = ExecutionEnvelope.model_validate(envelope(required_provider="claude", required_billing_mode="subscription")).execution
    with pytest.raises(ExecutionError) as caught:
        await service.dispatch("caller", policy, request)
    assert caught.value.reason_code == "billing_policy_unavailable"
    service.store.admit.assert_not_called()
    service._prepare_remote.assert_not_called()


async def test_actual_admission_rejects_resolved_provider_mismatch(admission_fixture):
    admission, policy, _ = admission_fixture
    request = ExecutionEnvelope.model_validate(envelope(required_provider="codex")).execution
    with pytest.raises(ExecutionError) as caught:
        await admission.resolve("caller", policy, request)
    assert caught.value.reason_code == "runtime_provider_mismatch"


async def test_existing_idempotent_attempt_is_returned_without_new_resolution(admission_fixture):
    admission, policy, _ = admission_fixture
    service = object.__new__(ExecutionService)
    service.store = SimpleNamespace(replay=AsyncMock(return_value={"id": "original-attempt"}),
                                    admit=AsyncMock(side_effect=AssertionError("no new Attempt")))
    service.admission = SimpleNamespace(resolve=AsyncMock(side_effect=AssertionError("no re-resolution")))
    request = ExecutionEnvelope.model_validate(envelope(required_provider="claude", required_billing_mode="subscription")).execution
    assert await service.dispatch("caller", policy, request) == "original-attempt"
    service.admission.resolve.assert_not_called()
    service.store.admit.assert_not_called()


def owner_route(**updates):
    route = {"driver": "claude.agent-sdk", "model": "opaque-model", "subscription_only": True, "connection": "owner-conn"}
    conn = {"provider": "claude", "kind": "endpoint", "data_policy": "no_training",
            "endpoint": "http://127.0.0.1:19999/v1", "secret": "env:NEVER_READ_FIXTURE_TOKEN"}
    for key, value in updates.items():
        if key == "provider":
            route["driver"] = "codex.app-server" if value == "codex" else value
        elif key == "anthropic_base_url":
            conn["endpoint"] = value
        elif key == "anthropic_auth_token_env":
            conn["secret"] = "env:" + value if value is not None else ""
        else:
            route[key] = value
    return {"connections": {"owner-conn": conn}, "routes": {"owner": route}}


@pytest.mark.parametrize("updates", [
    {"subscription_only": "true"}, {"subscription_only": 1}, {"provider": "codex"},
    {"anthropic_base_url": None}, {"anthropic_base_url": "file:///tmp/router"},
    {"anthropic_base_url": "http://token@127.0.0.1/v1"},
    {"anthropic_base_url": "http://127.0.0.1/v1?token=secret"},
    {"anthropic_base_url": "http://127.0.0.1/v1#fragment"},
    {"anthropic_base_url": "http://127.0.0.1/ v1"},
    {"anthropic_auth_token_env": None}, {"anthropic_auth_token_env": " "},
])
def test_owner_route_assertion_requires_explicit_valid_source(updates):
    with pytest.raises(ValueError):
        HarnessConfig.model_validate(owner_route(**updates))


def test_owner_route_false_is_omitted_from_existing_config_wire():
    from kotoyose.execution_resources import NativeRuntimeProfile
    assert "subscription_only" not in AgentRoute(driver="claude.agent-sdk", model="explicit-model", connection="claude-oauth").model_dump(mode="json")
    profile = NativeRuntimeProfile(provider="claude", model="explicit-model", cli_binary=Path("/usr/bin/true"),
                                   credential_mode="runtime_managed")
    assert "subscription_only" not in profile.model_dump(mode="json")


@pytest.mark.parametrize("updates", [
    {"subscription_only": "true"}, {"subscription_only": 1}, {"base_url": None},
    {"base_url": "http://secret@127.0.0.1/v1"}, {"base_url": "http://127.0.0.1/v1?secret=x"},
    {"credential_name": "ANTHROPIC_API_KEY"},
    {"credential_name": "CLAUDE_CODE_OAUTH_TOKEN"}, {"credential_env": " "},
    {"credential_mode": "runtime_managed", "credential_env": None},
])
def test_explicit_owner_profile_assertion_requires_same_strict_route(updates):
    from kotoyose.execution_resources import NativeRuntimeProfile
    body = {"provider": "claude", "model": "opaque-model", "cli_binary": "/usr/bin/true",
            "subscription_only": True, "base_url": "http://127.0.0.1:19999/v1",
            "credential_mode": "environment", "credential_name": "ANTHROPIC_AUTH_TOKEN",
            "credential_env": "NEVER_READ_FIXTURE_TOKEN", **updates}
    with pytest.raises(ValueError):
        NativeRuntimeProfile.model_validate(body)


@pytest.mark.parametrize("extra", [
    {"subscription_only": True}, {"runtime_billing": {"mode": "subscription"}},
])
def test_caller_cannot_assert_owner_route_trust(extra):
    from kotoyose.execution_resources import ResourceEnvelope
    with pytest.raises(ValueError):
        ExecutionEnvelope.model_validate(envelope(**extra))
    with pytest.raises(ValueError):
        ResourceEnvelope.model_validate({"resource": {"schema_version": "1", "kind": "text", "text": "fixture",
                                                       "provenance": "trusted", **extra}})


async def test_confirmed_route_admits_same_resolved_plan_and_public_receipt(admission_fixture, monkeypatch):
    import kotoyose.execution_admission as module
    from kotoyose.execution_resources import content_digest
    from kotoyose.execution_snapshot import resolved_attempt_snapshot
    admission, policy, config = admission_fixture
    config.routes["taskhub-pm"].subscription_only = True
    original_resolve = module.resolve_agent_model
    resolutions = []
    def resolve(*args, **kwargs):
        result = original_resolve(*args, **kwargs)
        resolutions.append(result)
        return result
    monkeypatch.setattr(module, "resolve_agent_model", resolve)
    original_workspace = admission.store.workspace
    async def workspace(*args):
        config.routes["taskhub-pm"].model = "changed-after-resolution"
        config.connections["pm-conn"].endpoint = "http://127.0.0.1:20000/v1"
        config.connections["pm-conn"].secret = "env:OTHER_SOURCE"
        config.routes["taskhub-pm"].subscription_only = False
        return await original_workspace(*args)
    admission.store.workspace = workspace
    monkeypatch.setattr("asyncio.create_subprocess_exec", AsyncMock(side_effect=AssertionError("no process")))
    request = ExecutionEnvelope.model_validate(envelope(required_provider="claude", required_billing_mode="subscription")).execution
    plan = await admission.resolve("caller", policy, request)
    assert len(resolutions) == 1
    assert plan["model"] == "cc/claude-sonnet-5-5"
    assert plan["base_url"] == "http://127.0.0.1:19999/v1"
    assert plan["credential_env"] == "NEVER_READ_FIXTURE_TOKEN"
    assert plan["subscription_only"] is True
    assert plan["runtime_billing"] == require_runtime_selection(plan, provider="claude", billing_mode="subscription")
    payload = request.model_dump(mode="json")
    receipt = resolved_attempt_snapshot({"id": "fixture-attempt", "sequence": 0, "request_digest": content_digest(payload),
                                         "request": payload, "snapshot": plan}).model_dump(mode="json")
    assert receipt["runtime_billing"] == plan["runtime_billing"]
    assert receipt["snapshot_digest"] == content_digest(plan)
    assert "NEVER_READ_FIXTURE_TOKEN" not in str(receipt)
    assert "subscription_only" not in receipt
    # A different idempotency key is a new admission and must use the now-unknown route.
    with pytest.raises(ExecutionError) as caught:
        await admission.resolve("caller", policy, request.model_copy(update={"idempotency_key": "new-key"}))
    assert caught.value.reason_code == "billing_policy_unavailable"


async def test_alias_base_url_override_does_not_inherit_route_trust(admission_fixture):
    admission, policy, config = admission_fixture
    config.routes["taskhub-pm"].subscription_only = True
    config.models["taskhub-pm"].anthropic_base_url = "http://127.0.0.1:20000/v1"
    request = ExecutionEnvelope.model_validate(envelope(required_provider="claude", required_billing_mode="subscription")).execution
    with pytest.raises(ExecutionError) as caught:
        await admission.resolve("caller", policy, request)
    assert caught.value.reason_code == "billing_policy_unavailable"


async def test_route_credential_replaces_alias_token_env(admission_fixture):
    admission, policy, config = admission_fixture
    config.routes["taskhub-pm"].subscription_only = True
    config.models["taskhub-pm"].anthropic_auth_token_env = "OTHER_SOURCE"
    request = ExecutionEnvelope.model_validate(envelope(required_provider="claude", required_billing_mode="subscription")).execution
    plan = await admission.resolve("caller", policy, request)
    assert plan["credential_env"] == "NEVER_READ_FIXTURE_TOKEN"


@pytest.mark.parametrize("scope", ["caller", "grant"])
async def test_confirmed_route_cannot_expand_alias_authority(admission_fixture, scope):
    admission, policy, config = admission_fixture
    config.routes["taskhub-pm"].subscription_only = True
    if scope == "caller":
        policy.agent_models = ["different-alias"]
    else:
        grant = await admission.store.resource("caller", "grant")
        grant.agent_models = ["different-alias"]
    request = ExecutionEnvelope.model_validate(envelope(required_provider="claude", required_billing_mode="subscription")).execution
    with pytest.raises(ExecutionError) as caught:
        await admission.resolve("caller", policy, request)
    assert caught.value.code == "permission_denied" and caught.value.status == 403


async def test_known_confirmed_receipt_replays_after_route_policy_removed(admission_fixture):
    from copy import deepcopy
    from kotoyose.execution_resources import content_digest
    from kotoyose.execution_snapshot import resolved_attempt_snapshot
    admission, policy, config = admission_fixture
    config.routes["taskhub-pm"].subscription_only = True
    request = ExecutionEnvelope.model_validate(envelope(required_provider="claude", required_billing_mode="subscription")).execution
    service = object.__new__(ExecutionService)
    service._accepting, service.admission = True, admission
    saved, scheduled = {}, []
    # In-memory persistence seam; the separate PostgreSQL helper tests real durable storage.
    async def replay(principal, key, digest):
        prior = saved.get(key)
        if prior and prior["request_digest"] != digest:
            raise ExecutionError("request_conflict", "Idempotency key has a different request", 409)
        return deepcopy(prior)
    async def admit(principal, payload, plan, max_concurrent):
        saved[payload["idempotency_key"]] = deepcopy({"id": "saved-attempt", "sequence": 0,
            "request_digest": content_digest(payload), "request": payload, "snapshot": plan})
        return saved[payload["idempotency_key"]], True
    service.store = SimpleNamespace(replay=AsyncMock(side_effect=replay), admit=AsyncMock(side_effect=admit))
    service._prepare_remote = AsyncMock()
    service._schedule = lambda attempt: scheduled.append(attempt["id"])
    assert await service.dispatch("caller", policy, request) == "saved-attempt"
    original = resolved_attempt_snapshot(saved[request.idempotency_key]).model_dump(mode="json")
    assert original["runtime_billing"]["mode"] == "subscription"
    config.routes["taskhub-pm"].subscription_only = False
    fresh = request.model_copy(update={"idempotency_key": "fresh-attempt"})
    with pytest.raises(ExecutionError) as caught:
        await service.dispatch("caller", policy, fresh)
    assert caught.value.reason_code == "billing_policy_unavailable"
    config.models.clear()
    config.routes.clear()
    service.admission.resolve = AsyncMock(side_effect=AssertionError("replay re-resolved removed registry"))
    service._accepting = False
    assert await service.dispatch("caller", policy, request) == "saved-attempt"
    assert resolved_attempt_snapshot(saved[request.idempotency_key]).model_dump(mode="json") == original
    assert scheduled == ["saved-attempt"]
    assert service.store.admit.await_count == 1
    service.admission.resolve.assert_not_called()
    with pytest.raises(ExecutionError) as caught:
        await service.dispatch("caller", policy, request.model_copy(update={"required_provider": "codex"}))
    assert caught.value.code == "request_conflict"


@pytest.mark.parametrize("field,value", [
    ("model", "another-model"), ("base_url", "http://127.0.0.1:20000/v1"),
    ("credential_env", "ANOTHER_SOURCE"),
])
def test_owner_policy_digest_is_bound_to_actual_plan(field, value):
    runtime = {"backend": "native", "provider": "claude", "model": "opaque-model", "subscription_only": True,
               "base_url": "http://127.0.0.1:19999/v1", "credential_mode": "environment",
               "credential_name": "ANTHROPIC_AUTH_TOKEN", "credential_env": "NEVER_READ_FIXTURE_TOKEN"}
    before = require_runtime_selection(runtime, provider="claude", billing_mode="subscription")
    assert "NEVER_READ_FIXTURE_TOKEN" not in str(before)
    runtime[field] = value
    assert require_runtime_selection(runtime, provider="claude", billing_mode="subscription") != before
    runtime["credential_name"] = "ANTHROPIC_API_KEY"
    with pytest.raises(ExecutionError) as caught:
        require_runtime_selection(runtime, provider="claude", billing_mode="subscription")
    assert caught.value.reason_code == "billing_policy_unavailable"


async def test_admitted_alias_job_uses_pinned_route_after_registry_removal(admission_fixture, monkeypatch, tmp_path):
    import asyncio
    from kotoyose.execution_agent import AgentExecutor
    from kotoyose.native_claude import _build_options
    from kotoyose.provider_jail import provider_environment
    admission, policy, config = admission_fixture
    config.routes["taskhub-pm"].subscription_only = True
    request = ExecutionEnvelope.model_validate(envelope(required_provider="claude", required_billing_mode="subscription")).execution
    plan = await admission.resolve("caller", policy, request)
    config.models.clear()
    config.routes.clear()
    monkeypatch.setenv("NEVER_READ_FIXTURE_TOKEN", "synthetic-route-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-forbidden-alternative")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "synthetic-forbidden-alternative")
    monkeypatch.setattr("kotoyose.execution_resources.resolve_agent_model", lambda *a, **k: pytest.fail("alias re-resolved"))
    worker = AsyncMock(return_value=NativeAgentResult(
        status="failed", reason="provider_error", content=b"", stop_confirmation="confirmed", usage=_unknown_usage(),
    ))
    executor = object.__new__(AgentExecutor)
    executor.config = admission.settings
    executor.runtime = SimpleNamespace(execute=worker)
    executor.worker = SimpleNamespace(execute=AsyncMock(side_effect=AssertionError("no tool worker")))
    executor.store = SimpleNamespace(prepare_operation=AsyncMock(return_value="synthetic-operation"),
        resolve_operation=AsyncMock(), append_event=AsyncMock())
    attempt = {"id": "synthetic-attempt", "principal": "caller", "request": request.model_dump(mode="json"),
        "snapshot": plan, "generation": plan["generation"]}
    await executor.execute(attempt, asyncio.Event())
    job = worker.call_args.args[0]
    assert (job.model, job.base_url, job.credential_name) == (plan["model"], plan["base_url"], plan["credential_name"])
    assert job.generation == plan["generation"]
    assert job.credential_value == "synthetic-route-token"
    options = _build_options(job, Path("/usr/bin/true"), tmp_path, None)
    assert options.fallback_model is None
    assert options.env["ANTHROPIC_API_KEY"] == options.env["CLAUDE_CODE_OAUTH_TOKEN"] == ""
    environment = provider_environment(options.env)
    assert environment["ANTHROPIC_AUTH_TOKEN"] == "synthetic-route-token"
    assert environment["ANTHROPIC_BASE_URL"] == plan["base_url"]
    assert "ANTHROPIC_API_KEY" not in environment and "CLAUDE_CODE_OAUTH_TOKEN" not in environment
    assert "synthetic-route-token" not in str(plan)
    assert worker.await_count == 1


@pytest.mark.parametrize("updates", [
    {"credential_env": "DIFFERENT_SOURCE"},
    {"base_url": "http://127.0.0.1:20000/v1"},
    {"credential_name": "ANTHROPIC_API_KEY", "subscription_only": False},
])
async def test_explicit_profile_cannot_mix_new_source_with_admitted_route(admission_fixture, updates):
    import asyncio
    from kotoyose.execution_agent import AgentExecutor
    admission, policy, config = admission_fixture
    config.routes["taskhub-pm"].subscription_only = True
    request = ExecutionEnvelope.model_validate(envelope(required_provider="claude", required_billing_mode="subscription")).execution
    plan = await admission.resolve("caller", policy, request)
    plan["runtime_profile"] = "owner-profile"
    assert updates
    executor = object.__new__(AgentExecutor)
    executor.config, executor.runtime, executor.worker = admission.settings, SimpleNamespace(), SimpleNamespace()
    executor.store = SimpleNamespace(prepare_operation=AsyncMock(side_effect=AssertionError("no provider operation")))
    with pytest.raises(ExecutionError) as caught:
        await executor.execute({"snapshot": plan}, asyncio.Event())
    assert caught.value.code == "request_conflict"
    executor.store.prepare_operation.assert_not_called()


def test_claude_oauth_subscription_route_validation_and_profile():
    from kotoyose.execution_resources import NativeRuntimeProfile
    cfg = HarnessConfig(
        connections={"claude-oauth": Connection(provider="claude", kind="subscription", data_policy="no_training", secret="claude-setup-token")},
        routes={"claude-sub": AgentRoute(
            driver="claude.agent-sdk", model="claude-sonnet-5-5", connection="claude-oauth", subscription_only=True,
        )},
    )
    assert cfg.routes["claude-sub"].subscription_only is True
    profile = NativeRuntimeProfile(
        provider="claude", model="claude-sonnet-5-5", cli_binary=Path("/usr/bin/true"),
        credential_mode="runtime_managed", credential_name="CLAUDE_CODE_OAUTH_TOKEN", subscription_only=True,
    )
    assert profile.base_url is None and profile.subscription_only is True
    with pytest.raises(ValueError):
        HarnessConfig(
            connections={"claude-host": Connection(provider="claude", kind="subscription", data_policy="no_training", config_dir="/tmp/claude-host")},
            routes={"claude-sub": AgentRoute(
                driver="claude.agent-sdk", model="claude-sonnet-5-5", connection="claude-host", subscription_only=True,
            )},
        )


async def test_claude_oauth_subscription_route_admits_subscription_billing(tmp_path, monkeypatch):
    from kotoyose.config import Client
    from kotoyose.execution_snapshot import resolved_attempt_snapshot
    from kotoyose.execution_resources import content_digest

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    info = workspace.stat()
    config = HarnessConfig(
        connections={"claude-oauth": Connection(provider="claude", kind="subscription", data_policy="no_training", secret="claude-setup-token")},
        routes={"claude-sub": AgentRoute(
            driver="claude.agent-sdk", model="claude-sonnet-5-5", connection="claude-oauth", subscription_only=True,
        )},
        models={"taskhub-review": {"route": "claude-sub", "agent": AliasAgent(place="isolated", surface="none")}},
        clients={"caller": Client(
            workspace_roots=[tmp_path], models=["taskhub-review"],
            allowed_provenance=["trusted"], max_run_seconds=60, max_memory_bytes=128 * 1024 * 1024,
            max_processes=32, max_cpus=1, max_concurrent_runs=1,
        )},
    )
    monkeypatch.setattr("kotoyose.config._config", config)
    policy = CallerPolicy(
        workspace_roots=[tmp_path], agent_models=["taskhub-review"],
        provenance=["trusted"], max_seconds=60, max_memory_bytes=128 * 1024 * 1024,
        max_pids=32, max_cpus=1, max_concurrent=1,
    )
    settings = NativeExecutionConfig(
        schema_version="1", image_id="sha256:" + "a" * 64,
        drivers={"claude.agent-sdk": DriverInstall(cli_binary=Path("/usr/bin/true"))},
    )
    now = datetime.now(timezone.utc)
    resources = {
        "input": InputText(schema_version="1", kind="text", text="Do not run.", provenance="trusted"),
        "timeout": TimeoutPolicy(schema_version="1", kind="timeout_policy", soft_seconds=1, hard_seconds=20, grace_seconds=1),
        "definition": AgentDefinition(
            schema_version="1", kind="agent_definition", instructions="fixture", max_turns=2,
            output_contract="text", timeout_policy_ref="timeout",
        ),
        "budget": ExecutionBudget(
            schema_version="1", kind="budget", reservation_ref="fixture-reservation", mode="observed",
            deadline=now + timedelta(minutes=5), memory_bytes=64 * 1024 * 1024, pids_limit=16, cpus=1,
            output_limit_bytes=8192,
        ),
        "grant": ExecutionGrant(
            schema_version="1", kind="grant", grant_id="fixture-grant", version=1,
            policy_ref="policy:fixture", delegation="none",
            caller_run_ref="fixture-run", workspace_ref="workspace:fixture:0", isolation="sandbox", network="none",
            not_before=now - timedelta(seconds=1), expires_at=now + timedelta(minutes=5), max_seconds=30,
            agent_models=["taskhub-review"], resource_refs=["definition", "budget", "input", "timeout"],
        ),
    }

    async def resource(principal, ref):
        return resources[ref]

    store = SimpleNamespace(
        resource=resource,
        workspace=AsyncMock(return_value={
            "id": "fixture", "generation": 0, "path": str(workspace),
            "device": info.st_dev, "inode": info.st_ino, "caller_ref": "fixture",
        }),
    )
    manifest = capability_manifest()
    for name in manifest.capabilities:
        manifest.capabilities[name] = CapabilitySupport(status="supported", reason="Admission-only fixture")
    admission = ExecutionAdmission(store, manifest, settings.image_id, settings=settings)

    request = ExecutionEnvelope.model_validate(
        envelope(model="taskhub-review", required_provider="claude", required_billing_mode="subscription"),
    ).execution
    plan = await admission.resolve("caller", policy, request)
    assert "base_url" not in plan
    assert plan["credential_mode"] == "runtime_managed"
    assert plan["credential_name"] == "CLAUDE_CODE_OAUTH_TOKEN"
    assert plan["subscription_only"] is True
    assert plan["runtime_billing"] == require_runtime_selection(plan, provider="claude", billing_mode="subscription")

    payload = request.model_dump(mode="json")
    receipt = resolved_attempt_snapshot({
        "id": "fixture-attempt", "sequence": 0, "request_digest": content_digest(payload),
        "request": payload, "snapshot": plan,
    }).model_dump(mode="json")
    assert receipt["runtime_billing"] == plan["runtime_billing"]

    config.routes["claude-sub"].subscription_only = False
    with pytest.raises(ExecutionError) as caught:
        await admission.resolve("caller", policy, request)
    assert caught.value.reason_code == "billing_policy_unavailable"
