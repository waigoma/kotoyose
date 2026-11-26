from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import Response
from pydantic import ValidationError

from kotoyose import auth
from kotoyose.acp_agent import AcpAgent
from kotoyose.api_key_store import ApiKeyStore
from kotoyose.app import app
from kotoyose.config import Client, HarnessConfig, ModelPreset
from kotoyose.execution_admission import ExecutionAdmission
from kotoyose.execution_contract import AgentExecution
from kotoyose.execution_resources import CallerPolicy, ExecutionError, NativeExecutionConfig, caller_allows_isolation
from kotoyose.model_pools import ModelPools
from kotoyose.models import ChatCompletionRequest
from kotoyose.print_cli_profiles import use_data_profiles
from kotoyose.provider_data import parse_declaration
from kotoyose.routes import chat


def _config(client):
    connections = {
        "claude": {"provider": "claude", "kind": "subscription", "data_policy": "no_training", "secret": "claude-oauth"},
        "cursor": {"provider": "cursor", "kind": "api_key", "secret": "env:CURSOR_API_KEY", "data_policy": "no_training"},
    }
    routes = {
        "claude": {"driver": "claude.agent-sdk", "model": "claude", "connection": "claude"},
        "cursor": {"driver": "cursor.sdk", "model": "grok", "connection": "cursor"},
    }
    models = {
        "jail-agent": {"route": "claude", "agent": {"place": "isolated", "surface": "read"}},
        "host-agent": {"route": "cursor", "agent": {"place": "host", "surface": "write"}},
    }
    for name in ("dev", "other"):
        on = f"child:{name}"
        connections[name] = {
            "provider": "grok-build", "kind": "subscription", "profile": "grok-build",
            "data_policy": "no_training", "on": on,
        }
        routes[name] = {"driver": "cli.acp", "profile": "grok-build", "model": "grok-build", "connection": name}
        models[f"{name}-write"] = {"route": name, "on": on, "agent": {"place": "host", "surface": "write"}}
    models["dev-read"] = {"route": "dev", "on": "child:dev", "agent": {"place": "host", "surface": "read"}}
    models["child-chat"] = {"route": "dev", "on": "child:dev"}
    models["write-pool"] = {"candidates": ["other-write", "host-agent", "dev-write"], "policy": "first_available"}
    declaration = Path(__file__).resolve().parents[1] / "examples/provider-declarations/grok-build.json"
    with use_data_profiles(parse_declaration(declaration.read_bytes()).profile()):
        return HarnessConfig(connections=connections, routes=routes, models=models, clients={"test-client": client})


def _native_config():
    return NativeExecutionConfig(schema_version="1", image_id="sha256:" + "2" * 64)


class _PoolStore:
    selected = None

    async def select(self, pool_alias, names, choose):
        ordered = choose({}, {})
        self.selected = ordered[0]
        return ordered

    async def states(self, names):
        return {}

    async def last_selected(self, names):
        return {}


@pytest.mark.parametrize(
    ("places", "surface", "visible_agents", "pool_candidates"),
    [
        (["host"], "write", {"host-agent"}, ["host-agent"]),
        (["isolated"], "write", {"jail-agent"}, []),
        (["child"], "write", {"dev-read", "dev-write", "other-write"}, ["other-write", "dev-write"]),
        (["child:dev"], "write", {"dev-read", "dev-write"}, ["dev-write"]),
        (["child:other"], "write", {"other-write"}, ["other-write"]),
        (["child:missing"], "write", set(), []),
        (["isolated", "child:dev"], "read", {"jail-agent", "dev-read"}, []),
    ],
)
async def test_models_acp_catalog_and_run_admission_agree(monkeypatch, places, surface, visible_agents, pool_candidates):
    config = _config(Client(models=["*"], places=places, max_surface=surface))
    keys = ApiKeyStore()
    _, key = await keys.create("test-key", "test-client")
    pool_store = _PoolStore()
    service = SimpleNamespace(
        config=_native_config(),
        store=SimpleNamespace(pool=object(), model_pools=ModelPools(pool_store)),
    )
    monkeypatch.setattr("kotoyose.config._config", config)
    monkeypatch.setattr(app.state, "api_key_store", keys, raising=False)
    monkeypatch.setattr(app.state, "execution_service", service, raising=False)
    monkeypatch.setattr(app.state, "compatibility_adapter", None, raising=False)
    monkeypatch.setattr(chat, "_chat_completions", AsyncMock(side_effect=lambda req, *_: Response(req.model)))

    headers = {"Authorization": f"Bearer {key}"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        response = await http.get("/v1/models", headers=headers)
        assert response.status_code == 200
        models = {row["id"]: row for row in response.json()["data"]}
        expected = visible_agents | {"child-chat"}
        if pool_candidates:
            expected.add("write-pool")
            assert models["write-pool"]["candidates"] == pool_candidates
        assert set(models) == expected

        acp = AcpAgent(http, key, lambda _: None)
        assert set((await acp._model_catalog({}))[0]) == expected

        response = await http.get("/v1/pools", headers=headers)
        assert response.status_code == 200
        if pool_candidates:
            (pool,) = response.json()["data"]
            assert pool["first_choice"] == pool_candidates[0]
            assert [item["alias"] for item in pool["candidates"]] == pool_candidates
        else:
            assert response.json()["data"] == []

        for alias in config.models.keys() | config.pools.keys():
            response = await http.post("/v1/chat/completions", headers=headers, json={
                "model": alias, "messages": [{"role": "user", "content": "hello"}],
            })
            assert response.status_code == (200 if alias in expected else 403), (alias, response.text)
            if alias == "write-pool" and pool_candidates:
                assert response.headers["x-resolved-model"] == pool_candidates[0] == pool_store.selected
                assert response.text == pool_candidates[0]


async def test_child_alias_does_not_resolve_as_a_local_native_run(monkeypatch):
    policy = Client(models=["dev-write"], places=["child:dev", "host"])
    monkeypatch.setattr("kotoyose.config._config", _config(policy))
    admission = ExecutionAdmission(None, None, "", settings=_native_config())
    request = AgentExecution.model_construct(model="dev-write")

    with pytest.raises(ExecutionError, match="Child aliases run through the chat API"):
        await admission.resolve("test-client", policy, request)
    assert not caller_allows_isolation(Client(places=["child:dev"]), "native")
    assert not caller_allows_isolation(Client(places=["child"]), "sandbox")


async def test_pool_admission_rechecks_child_place_after_config_reload(monkeypatch):
    policy = Client(models=["write-pool"], places=["child:dev"])
    config = _config(policy)
    updated = config.model_copy(deep=True)
    updated.models["dev-write"] = updated.models["other-write"]
    monkeypatch.setattr("kotoyose.config._config", config)

    async def select(*args, **kwargs):
        monkeypatch.setattr("kotoyose.config._config", updated)
        return ["dev-write"]

    service = SimpleNamespace(config=_native_config(), store=SimpleNamespace(
        pool=object(), model_pools=SimpleNamespace(select=select),
    ))
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(execution_service=service)),
        state=SimpleNamespace(execution_policy=policy),
    )
    body = ChatCompletionRequest(model="write-pool", messages=[{"role": "user", "content": "hello"}])
    with pytest.raises(ExecutionError) as error:
        await chat._resolve_pool_model(request, body)
    assert error.value.status == 403


def _caller_policy(places):
    return CallerPolicy(places=places, max_seconds=1, max_memory_bytes=16 * 1024 * 1024, max_pids=1,
                        max_cpus=1, max_concurrent=1)


@pytest.mark.parametrize("place", ["child:A-1._z", "child:" + "a" * 64])
def test_child_place_uses_on_name_validation(place):
    assert ModelPreset(on=place).on == place
    assert Client(places=["child", place]).places == ["child", place]
    assert _caller_policy(["child", place]).places == ["child", place]


@pytest.mark.parametrize("place", ["remote", "child:", "child:.hidden", "child:dev/extra", "child:dev ", "child:" + "a" * 65])
def test_invalid_places_are_rejected(place):
    with pytest.raises(ValidationError, match="places"):
        Client(places=[place])
    with pytest.raises(ValidationError, match="places"):
        _caller_policy([place])
    with pytest.raises(ValidationError, match="on"):
        ModelPreset(on=place)


async def test_admin_clients_reports_place_gate_for_selected_models(monkeypatch):
    policy = Client(models=["*"], places=["isolated"])
    config = _config(policy)
    monkeypatch.setattr("kotoyose.config._config", config)
    monkeypatch.setattr(auth, "API_KEY", "admin-test")
    monkeypatch.setattr(auth, "BASIC_AUTH_USER", "admin")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        response = await http.get("/admin/clients", auth=("admin", "admin-test"))
    assert response.status_code == 200
    (row,) = response.json()["clients"]
    assert row["place_blocked_models"] == {
        "dev-read": ["child:dev"], "dev-write": ["child:dev"], "other-write": ["child:other"],
        "host-agent": ["host"], "write-pool": ["child:other", "host", "child:dev"],
    }
