"""Training-risk policy is bound to an alias and checked before provider use."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from kotoyose.app import app
from kotoyose.config import Connection, HarnessConfig, TrainingAck
from kotoyose.data_policy import alias_data_policy, connection_data_policy, training_digest
from kotoyose.execution_resources import ExecutionError
from kotoyose.model_pools import ModelPools
from kotoyose.routes import chat

from tests.fixtures.api_keys import TEST_API_HEADERS

pytestmark = pytest.mark.usefixtures("client_api_key")


def config() -> HarnessConfig:
    return HarnessConfig.model_validate({
        "connections": {"gateway": {
            "provider": "claude", "kind": "endpoint", "data_policy": "unknown", "endpoint": "https://demo.invalid",
            "secret": "env:DEMO_GATEWAY_KEY",
        }},
        "routes": {"gateway": {"driver": "claude.agent-sdk", "model": "demo", "connection": "gateway"}},
        "models": {"demo": {"route": "gateway"}},
    })


def approve(cfg, *capabilities, alias="demo"):
    cfg.training_acks.append(TrainingAck(
        alias=alias, capabilities=list(capabilities), reasons=["unknown"],
        digest=training_digest(cfg, alias, capabilities),
        acknowledged_by="test-owner", at="2026-11-18T10:00:00Z", note="Public data only",
    ))


@pytest.mark.parametrize("policy", ["no_training", "trains", "unknown"])
def test_explicit_connection_policy_is_used(policy):
    connection = Connection(provider="codex", kind="subscription", data_policy=policy)
    assert connection_data_policy(connection) == policy


def test_missing_data_policy_is_rejected_at_load_and_save(tmp_path, monkeypatch):
    from kotoyose.config import _load_yaml_config, save_config

    path = tmp_path / "config.yml"
    path.write_text("connections: {codex-host: {provider: codex, kind: subscription}}\n")
    monkeypatch.setenv("KOTOYOSE_CONFIG", str(path))
    message = r"connection 'codex-host': set data_policy \(no_training, trains or unknown\)"
    with pytest.raises(ValidationError, match=message):
        _load_yaml_config()

    cfg = HarnessConfig(connections={"codex-host": Connection(
        provider="codex", kind="subscription", data_policy="no_training",
    )})
    del cfg.connections["codex-host"].data_policy
    with pytest.raises(ValidationError, match=message):
        save_config(cfg)


def test_unknown_route_does_not_grant_implicit_tools():
    cfg = config()
    assert alias_data_policy(cfg, "demo").view() == {"data_policy": "unknown", "training_ack": False}
    resolved = cfg.preset_for("demo")
    assert resolved.tools == resolved.setting_sources == []
    assert resolved.bare is True and resolved.strict_mcp_config is True


@pytest.mark.parametrize("mutation", ["rename", "allow", "tools", "route", "connection", "policy", "unlocks"])
def test_acknowledgement_binds_alias_route_connection_and_capabilities(mutation):
    cfg = config()
    cfg.models["demo"].tools = ["Read"]
    with pytest.raises(ValidationError, match="'demo'.*Acknowledge tools"):
        HarnessConfig.model_validate(cfg.model_dump())
    approve(cfg, "tools", "workspace")
    assert HarnessConfig.model_validate(cfg.model_dump()).preset_for("demo").tools == ["Read"]

    alias = "demo"
    if mutation == "rename":
        alias = "renamed"
        cfg.models[alias] = cfg.models.pop("demo")
    elif mutation == "allow":
        cfg.models[alias].place = "host"
    elif mutation == "tools":
        cfg.models[alias].tools = ["Read", "Grep"]
    elif mutation == "route":
        cfg.routes["gateway"].model = "new"
    elif mutation == "connection":
        cfg.connections["gateway"].endpoint = "https://different.invalid"
    elif mutation == "policy":
        cfg.connections["gateway"].data_policy = "trains"
    else:
        cfg.training_acks[0].capabilities.append("attachments")
    assert alias_data_policy(cfg, alias).view()["training_ack"] is False
    with pytest.raises(ValidationError, match=f"'{alias}'.*Acknowledge tools"):
        HarnessConfig.model_validate(cfg.model_dump())


@pytest.fixture
def http(monkeypatch):
    cfg = config()
    monkeypatch.setattr("kotoyose.config._config", cfg)
    monkeypatch.setattr("kotoyose.auth.API_KEY", None)
    monkeypatch.setattr(app.state, "execution_service", None, raising=False)
    monkeypatch.setattr(chat, "get_or_resume_client", AsyncMock(return_value=(AsyncMock(), False)))
    monkeypatch.setattr(chat, "ask_claude", AsyncMock(return_value=("answer", None, None)))
    client = TestClient(app, headers=TEST_API_HEADERS)
    return cfg, client


CHAT = {"model": "demo", "messages": [{"role": "user", "content": "hello"}]}


@pytest.mark.parametrize("fields", [
    {"tools": [{"type": "function", "function": {"name": "check"}}]},
    {"tool_choice": "auto"},
    {"attachments": [{"id": "fake"}]},
    {"files": [{"id": "fake"}]},
    {"images": ["https://example.invalid/image.jpg"]},
    {"messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "https://example.invalid/image.jpg"}},
    ]}]},
    {"messages": [{"role": "user", "content": [{"type": "file", "file": {"file_id": "fake"}}]}]},
])
def test_chat_rejects_unapproved_capabilities_before_provider(http, fields):
    _, client = http
    response = client.post("/v1/chat/completions", headers=TEST_API_HEADERS, json={**CHAT, **fields})
    assert response.status_code == 403
    assert response.json()["error"]["type"] == "permission_denied"
    assert "training_acks" in response.json()["error"]["message"]
    chat.get_or_resume_client.assert_not_awaited()


def test_chat_allows_plain_text_and_only_unlocks_selected_capabilities(http):
    cfg, client = http
    assert client.post("/v1/chat/completions", headers=TEST_API_HEADERS, json=CHAT).status_code == 200
    approve(cfg, "tools")
    assert client.post("/v1/chat/completions", headers=TEST_API_HEADERS, json={**CHAT, "tool_choice": "auto"}).status_code == 200
    assert client.post("/v1/chat/completions", headers=TEST_API_HEADERS, json={**CHAT, "files": ["example"]}).status_code == 403
    assert cfg.preset_for("demo").tools == []


def test_chat_allows_acknowledged_images_without_enabling_tools(http):
    cfg, client = http
    approve(cfg, "attachments")
    messages = [{"role": "user", "content": [{"type": "image_url", "image_url": {
        "url": "data:image/png;base64,QUJD",
    }}]}]
    assert client.post("/v1/chat/completions", headers=TEST_API_HEADERS, json={**CHAT, "messages": messages}).status_code == 200
    assert client.post("/v1/chat/completions", headers=TEST_API_HEADERS, json={**CHAT, "tool_choice": "auto"}).status_code == 403
    assert chat.ask_claude.await_count == 1


def test_models_publishes_effective_policy_and_live_ack(http):
    cfg, client = http
    cfg.connections["gateway"].data_policy = "trains"
    row = client.get("/v1/models").json()["data"][0]
    assert (row["data_policy"], row["training_ack"]) == ("trains", False)
    approve(cfg, "attachments")
    row = client.get("/v1/models").json()["data"][0]
    assert (row["data_policy"], row["training_ack"]) == ("trains", True)
    cfg.routes["gateway"].model = "new"
    assert client.get("/v1/models").json()["data"][0]["training_ack"] is False


@pytest.mark.asyncio
async def test_pool_skips_only_candidates_missing_requested_capability():
    cfg = config()
    raw = cfg.model_dump()
    raw["models"]["demo"]["agent"] = {"place": "isolated", "surface": "read"}
    raw["connections"]["safe"] = {
        "provider": "claude", "kind": "api_key", "secret": "env:SAFE_KEY", "data_policy": "no_training",
    }
    raw["routes"]["safe"] = {**raw["routes"]["gateway"], "connection": "safe"}
    raw["models"]["safe"] = {**raw["models"]["demo"], "route": "safe"}
    raw["models"]["pool"] = {"candidates": ["demo", "safe"], "policy": "first_available"}
    cfg = HarnessConfig.model_validate(raw)

    async def select(pool, names, choose):
        return choose({}, {})

    pools = ModelPools(SimpleNamespace(select=select))
    assert await pools.select(cfg, "pool", refuse_needs_ack=False) == ["demo", "safe"]
    assert await pools.select(cfg, "pool", refuse_needs_ack=False, training_capabilities={"tools"}) == ["safe"]
    cfg.connections["safe"].data_policy = "unknown"
    with pytest.raises(ExecutionError) as exc:
        await pools.select(cfg, "pool", refuse_needs_ack=False, training_capabilities={"tools"})
    assert (exc.value.status, exc.value.reason_code) == (403, "data_policy_restricted")
    approve(cfg, "tools")
    assert await pools.select(cfg, "pool", refuse_needs_ack=False, training_capabilities={"tools"}) == ["demo"]


@pytest.mark.asyncio
async def test_policy_change_invalidates_an_existing_host_chat_session(monkeypatch):
    from kotoyose import session

    trusted = config()
    trusted.connections["gateway"].data_policy = "no_training"
    restricted = config()
    monkeypatch.setenv("DEMO_GATEWAY_KEY", "synthetic-gateway-key")
    monkeypatch.setattr(session, "sessions", {})
    monkeypatch.setattr(session, "session_cache_identities", {})
    monkeypatch.setattr(session, "session_last_used", {})
    lookup = AsyncMock(return_value=None)
    monkeypatch.setattr(session, "lookup_session", lookup)
    sdk_sessions = []

    def make_sdk(*, options):
        sdk = SimpleNamespace(options=options, connect=AsyncMock(), disconnect=AsyncMock())
        sdk_sessions.append(sdk)
        return sdk

    monkeypatch.setattr(session, "ClaudeSDKClient", make_sdk)
    previous, _ = await session.get_or_resume_client("same-key", "demo", config_snapshot=trusted)
    latest, resumed = await session.get_or_resume_client("same-key", "demo", config_snapshot=restricted)
    assert previous is not latest and resumed is False
    sdk_sessions[0].disconnect.assert_awaited_once()
    assert lookup.await_count == 1
    assert sdk_sessions[1].options.tools == []
    assert sdk_sessions[1].options.setting_sources == []
    assert sdk_sessions[1].options.cwd == "/"
