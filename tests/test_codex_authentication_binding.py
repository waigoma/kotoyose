"""モデルを起動しない事前 pin の権限・wire・token 更新契約を確認する。"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from kotoyose import config as config_module
from kotoyose.app import execution_exception_handler
from kotoyose.config import AgentRoute, AliasAgent, Client, Connection, HarnessConfig, ModelPreset
from kotoyose.execution_contract import ExecutionEnvelope, RuntimeAuthenticationResolution
from kotoyose.execution_resources import (
    CallerPolicy, DriverInstall, ExecutionError, NativeExecutionConfig, codex_authentication_binding, content_digest,
)
from kotoyose.execution_service import ExecutionService
from kotoyose.routes.executions import router
from tests.codex_subscription_fixture import subscription_auth
from tests.test_execution_contract import _agent_execution


def test_api_key_binding_pins_the_configured_source():
    bindings = [
        ({"credential_mode": "environment", "credential_env": "FIXTURE_KEY"}, "env:FIXTURE_KEY"),
        ({"credential_mode": "environment", "credential_env": "OTHER_KEY"}, "env:OTHER_KEY"),
        ({"credential_mode": "secret", "credential_secret": "FIXTURE_KEY"}, "secret:FIXTURE_KEY"),
        ({"credential_mode": "secret", "credential_secret": "OTHER_KEY"}, "secret:OTHER_KEY"),
    ]
    digests = set()
    for source, label in bindings:
        snapshot = {"credential_name": "OPENAI_API_KEY", **source}
        binding = codex_authentication_binding(snapshot)
        assert binding == {"mode": "api_key", "binding_digest": content_digest({"mode": "api_key", "source": label})}
        digests.add(binding["binding_digest"])
    assert len(digests) == len(bindings)


@pytest.fixture
def binding_service(tmp_path, monkeypatch):
    path, token = subscription_auth(tmp_path)
    monkeypatch.setattr(config_module, "_config", HarnessConfig(
        connections={"codex-chatgpt": Connection(provider="codex", kind="subscription", data_policy="no_training", auth_file=path)},
        routes={"reviewed": AgentRoute(driver="codex.app-server", model="gpt-6-luna", connection="codex-chatgpt")},
        models={"reviewed-alias": ModelPreset(
            route="reviewed",
            agent=AliasAgent(place="isolated", surface="exec"),
        )},
        clients={"binding-caller": Client(
            workspace_roots=[tmp_path],
            models=["reviewed-alias"], allowed_provenance=["trusted"], max_run_seconds=30,
            max_memory_bytes=64 * 1024 * 1024, max_processes=16, max_cpus=1, max_concurrent_runs=1,
        )},
    ))
    policy = CallerPolicy(workspace_roots=[tmp_path],
        agent_models=["reviewed-alias"], provenance=["trusted"], max_seconds=30,
        max_memory_bytes=64 * 1024 * 1024, max_pids=16, max_cpus=1, max_concurrent=1)
    service = object.__new__(ExecutionService)
    service.config = NativeExecutionConfig(schema_version="1", image_id="sha256:" + "a" * 64,
        drivers={"codex.app-server": DriverInstall(
            cli_binary=Path("/unavailable/codex"),
        )})
    service.store = SimpleNamespace(runtime_store_id=None)
    service.dispatch = AsyncMock(side_effect=AssertionError("readback must not dispatch"))
    return service, policy, path, token


async def test_authenticated_resolution_is_hash_only_without_process_or_provider(binding_service, monkeypatch):
    service, policy, path, token = binding_service
    monkeypatch.setattr("asyncio.create_subprocess_exec", AsyncMock(side_effect=AssertionError("no process")))
    monkeypatch.setattr("kotoyose.codex_egress.connect_subscription_endpoint", AsyncMock(side_effect=AssertionError("no network")))
    app = FastAPI()
    app.state.execution_service = service
    from tests.fixtures.api_keys import fixture_key_store

    app.state.api_key_store = await fixture_key_store(**{"binding-caller": "false-caller-token"})
    app.add_exception_handler(ExecutionError, execution_exception_handler)
    app.include_router(router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://fixture") as client:
        unauthenticated = await client.post("/v1/runtime-authentication/resolve", json={"schema_version": "1", "model": "reviewed-alias"})
        assert unauthenticated.status_code == 401
        headers = {"Authorization": "Bearer kys_false-caller-token"}
        response = await client.post("/v1/runtime-authentication/resolve", headers=headers,
            json={"schema_version": "1", "model": "reviewed-alias"})
        denied = await client.post("/v1/runtime-authentication/resolve", headers=headers,
            json={"schema_version": "1", "model": "other-alias"})
    assert denied.status_code == 403 and response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    result = response.json()
    assert set(result) == {"schema_version", "runtime_authentication"}
    assert set(result["runtime_authentication"]) == {"mode", "binding_digest"}
    assert result["runtime_authentication"]["mode"] == "chatgpt_subscription"
    assert len(result["runtime_authentication"]["binding_digest"]) == 64
    assert all(value not in response.text for value in (token, str(path), "fixture-account", "false-refresh-secret"))
    resolution = RuntimeAuthenticationResolution(schema_version="1", model="reviewed-alias")
    subscription_auth(path.parent, token_label="rotated")
    assert service.resolve_runtime_authentication(policy, resolution) == result
    relocated = path.parent / "relocated"
    relocated.mkdir()
    new_path, _ = subscription_auth(relocated)
    config = config_module.get_config()
    monkeypatch.setattr(config.connections["codex-chatgpt"], "auth_file", new_path)
    assert service.resolve_runtime_authentication(policy, resolution) == result
    subscription_auth(relocated, account="changed-account")
    assert service.resolve_runtime_authentication(policy, resolution) != result
    service.dispatch.assert_not_called()


@pytest.mark.parametrize("extra", [{}, {"model": "alias", "runtime_profile": "profile"},
    {"runtime_profile": "profile", "codex_auth_file": "/arbitrary/auth.json"}])
def test_resolution_cannot_select_arbitrary_credential_sources(extra):
    with pytest.raises(ValueError):
        RuntimeAuthenticationResolution.model_validate({"schema_version": "1", **extra})


@pytest.mark.parametrize("digest", ["0" * 63, "A" * 64, "sha256:" + "0" * 64, "0" * 65])
def test_expected_binding_digest_has_one_canonical_wire_format(digest):
    payload = _agent_execution()
    payload["execution"]["expected_authentication_binding_digest"] = digest
    with pytest.raises(ValueError):
        ExecutionEnvelope.model_validate(payload)


def test_old_explicit_profiles_keep_their_request_shape():
    parsed = ExecutionEnvelope.model_validate(_agent_execution())
    wire = parsed.model_dump(mode="json")["execution"]
    assert "required_authentication_mode" not in wire and "expected_authentication_binding_digest" not in wire
