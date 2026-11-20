"""段階 1: route を判定して印を付けるだけ。既定では何も止めない。"""

from __future__ import annotations

import json
import subprocess
import sys
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import yaml
from pydantic import ValidationError

from kotoyose import config as config_module
from kotoyose.agent_sanction import (
    alias_route_sanction, reset_warned_routes, route_digest, route_sanction, sanction_reasons,
    with_sanction,
)
from kotoyose.config import AgentRoute, AliasAgent, Connection, HarnessConfig, ModelPreset, RiskAck
from kotoyose.execution_contract import ResumeRequest
from kotoyose.execution_resources import ExecutionError, content_digest
from kotoyose.runtime.resume import ResumeSupport
from tests.test_alias_execution_routing import routing as routing  # noqa: F401
from tests.test_codex_host_admission import admitted as admitted  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "sanction_candidates.py"
ROUTER_CONN = Connection(provider="claude", kind="endpoint", data_policy="no_training", endpoint="https://router.example/v1", secret="env:ROUTER_KEY")
OFFICIAL_CONN = Connection(provider="claude", kind="subscription", data_policy="no_training", secret="claude-oauth")
ROUTER = {"driver": "claude.agent-sdk", "model": "claude-sonnet-5", "connection": "router-conn"}


def _ack(name: str, route: AgentRoute, connection: Connection | None = None, **extra) -> dict:
    return {"route": name, "digest": route_digest(route, connection), "reasons": ["unofficial_endpoint"],
            "acknowledged_by": "owner", "at": "2026-11-14T00:00:00+00:00", **extra}


def _config(*, acks=None, router_conn=None) -> HarnessConfig:
    return HarnessConfig(
        connections={
            "router-conn": router_conn or ROUTER_CONN,
            "claude-oauth": OFFICIAL_CONN,
        },
        routes={"router": AgentRoute(**ROUTER),
                "official": AgentRoute(driver="claude.agent-sdk", model="claude-sonnet-5", connection="claude-oauth")},
        models={"routed": ModelPreset(cwd="/tmp", route="router", agent=AliasAgent(place="isolated", surface="write")),
                "plain": ModelPreset(cwd="/tmp", route="official", agent=AliasAgent(place="isolated", surface="write"))},
        risk_acks=acks or [],
    )


def test_custom_model_provider_is_a_reason():
    assert sanction_reasons("codex.app-server", "gpt-6-luna", None) == []
    assert sanction_reasons("codex.app-server", "gpt-6-luna", None, {"name": "x"}) == ["custom_model_provider"]
    assert sanction_reasons("codex.app-server", "gpt-6-luna", None, "x") == ["custom_model_provider"]


def test_digest_is_stable_and_changes_on_each_field():
    conn = Connection(provider="codex", kind="endpoint", data_policy="no_training", endpoint="https://e.example/v1", secret="env:A")
    base = AgentRoute(driver="codex.app-server", model="gpt-6-luna", connection="codex-ep", model_provider="p")
    assert len(route_digest(base, conn)) == 64
    assert route_digest(base, conn) == route_digest(base.model_copy(), conn)
    # connection は kind と endpoint だけ。env 変数名を変えても digest は動かない。
    assert route_digest(base, conn) == route_digest(base, conn.model_copy(update={"secret": "env:B"}))
    for change in ({"model": "gpt-6-sol"}, {"model_provider": "q"},
                   {"model_provider": {"k": 1}}, {"model_provider": None}):
        assert route_digest(base.model_copy(update=change), conn) != route_digest(base, conn), change
    for conn_change in (
        Connection(provider="codex", kind="endpoint", data_policy="no_training", endpoint="https://other.example/v1", secret="env:A"),
        Connection(provider="codex", kind="subscription", data_policy="no_training"),
        Connection(provider="codex", kind="api_key", data_policy="no_training", secret="env:A"),
    ):
        assert route_digest(base, conn_change) != route_digest(base, conn), conn_change
    claude = AgentRoute(**ROUTER)
    assert route_digest(claude, ROUTER_CONN) != route_digest(
        claude.model_copy(update={"driver": "claude.agent-sdk", "model": "x"}), ROUTER_CONN
    )
    assert route_digest(claude, ROUTER_CONN) != route_digest(
        AgentRoute(driver="cursor.sdk", model="claude-sonnet-5", connection="router-conn"), ROUTER_CONN
    )


def test_ack_matches_only_the_same_route_and_digest():
    route = AgentRoute(**ROUTER)
    assert route_sanction(_config(), "router").status == "needs_ack"
    assert route_sanction(_config(), "official").status == "official"
    acked = _config(acks=[_ack("router", route, ROUTER_CONN)])
    result = route_sanction(acked, "router")
    assert (result.status, result.reasons) == ("acknowledged", ("unofficial_endpoint",))
    # connection の endpoint を編集すると ack は外れる。
    edited = _config(
        acks=[_ack("router", route, ROUTER_CONN)],
        router_conn=ROUTER_CONN.model_copy(update={"endpoint": "https://elsewhere.example/v1"}),
    )
    assert route_sanction(edited, "router").status == "needs_ack"
    # 別 route 名への流用は効かない。
    other = HarnessConfig(
        connections={"router-conn": ROUTER_CONN},
        routes={"a": route, "b": route},
        risk_acks=[_ack("a", route, ROUTER_CONN)],
    )
    assert route_sanction(other, "a").status == "acknowledged"
    assert route_sanction(other, "b").status == "needs_ack"


def test_risk_acks_shape_is_strict():
    route = AgentRoute(**ROUTER)
    with pytest.raises(ValidationError, match="不明な route"):
        _config(acks=[_ack("ghost", route, ROUTER_CONN)])
    for bad in ({"digest": "short"}, {"reasons": []}, {"reasons": ["nope"]}, {"at": "yesterday"},
                {"acknowledged_by": ""}, {"extra": 1}):
        with pytest.raises(ValidationError):
            _config(acks=[{**_ack("router", route, ROUTER_CONN), **bad}])
    ack = RiskAck.model_validate({**_ack("router", route, ROUTER_CONN), "at": datetime(2026, 11, 14, tzinfo=timezone.utc)})
    assert ack.at.startswith("2026-11-14")
    assert "risk_acks" not in _config().model_dump(mode="json")
    assert _config(acks=[_ack("router", route, ROUTER_CONN)]).model_dump(mode="json")["risk_acks"][0]["route"] == "router"


def test_alias_without_route_is_not_judged():
    config = _config()
    assert alias_route_sanction(config, "missing") is None
    assert with_sanction(None, config, "routed") is None
    assert with_sanction({"place": "host"}, config, "plain") == {"place": "host"}
    assert with_sanction({"place": "host"}, config, "routed") == {
        "place": "host", "sanction": "needs_ack", "sanction_reasons": ["unofficial_endpoint"]}


def _set_config(monkeypatch, config):
    monkeypatch.setattr(config_module, "_config", config)


def _codex_config(*, acks=None, model_provider="custom", auth_file=None) -> HarnessConfig:
    return HarnessConfig(
        connections={"codex-chatgpt": Connection(provider="codex", kind="subscription", data_policy="no_training", auth_file=auth_file)},
        routes={"coding": AgentRoute(driver="codex.app-server", model="gpt-6-luna", connection="codex-chatgpt",
                                     model_provider=model_provider)},
        models={"saved-coding": ModelPreset(effort="max", route="coding",
                                            agent=AliasAgent(place="isolated", surface="write"))},
        risk_acks=acks or [],
    )


async def test_attempt_snapshot_records_sanction(admitted, monkeypatch):  # noqa: F811
    auth_file = admitted.runtime.codex_auth_file
    _set_config(monkeypatch, _codex_config(auth_file=auth_file))
    snapshot = await admitted.admission.resolve("fixture", admitted.policy, admitted.request)
    assert snapshot["sanction"] == {"status": "needs_ack", "reasons": ["custom_model_provider"]}
    cfg = config_module.get_config()
    route = cfg.routes["coding"]
    conn = cfg.connections[route.connection]
    _set_config(monkeypatch, _codex_config(acks=[_ack("coding", route, conn, reasons=["custom_model_provider"])], auth_file=auth_file))
    assert (await admitted.admission.resolve("fixture", admitted.policy, admitted.request))["sanction"][
        "status"] == "acknowledged"
    _set_config(monkeypatch, _codex_config(model_provider=None, auth_file=auth_file))
    assert "sanction" not in await admitted.admission.resolve("fixture", admitted.policy, admitted.request)


async def test_resume_ignores_the_sanction_key(admitted, monkeypatch):  # noqa: F811
    """後から ack を足しても、resume の同一性比較 (execution_resume.py の key 一覧) は動かない。"""
    auth_file = admitted.runtime.codex_auth_file
    _set_config(monkeypatch, _codex_config(auth_file=auth_file))
    snapshot = await admitted.admission.resolve("fixture", admitted.policy, admitted.request)
    assert snapshot["sanction"]["status"] == "needs_ack"
    previous = {"id": "previous", "snapshot": snapshot, "request": admitted.request.model_dump(mode="json"),
                "workspace_id": snapshot["workspace_id"], "generation": snapshot["generation"], "sequence": 0,
                "status": "succeeded", "finished_at": datetime.now(timezone.utc),
                "stop_confirmation": "confirmed", "side_effect_resolution": "resolved"}
    request = admitted.request.model_copy(update={"workspace_ref": "workspace:fixture:1", "idempotency_key": "resume-1"})
    admitted.resources["grant"] = admitted.resources["grant"].model_copy(update={"workspace_ref": request.workspace_ref})
    admitted.admission.store.workspace.return_value = {**admitted.admission.store.workspace.return_value, "generation": 1}
    admitted.admission.store.operations = AsyncMock(return_value=[])
    cfg = config_module.get_config()
    route = cfg.routes["coding"]
    conn = cfg.connections[route.connection]
    _set_config(monkeypatch, _codex_config(acks=[_ack("coding", route, conn, reasons=["custom_model_provider"])], auth_file=auth_file))
    resume = ResumeRequest(schema_version="1", execution=request, predecessor_snapshot_digest=content_digest(snapshot))
    result = await admitted.admission.resolve_resume(
        "fixture", admitted.policy, previous, resume,
        ResumeSupport(modes=("durable_state",), reason="workspace_checkpoint_only"))
    assert result["sanction"]["status"] == "acknowledged"
    assert deepcopy(snapshot)["sanction"]["status"] == "needs_ack"


@pytest.mark.parametrize("mode,refused", [("off", False), ("agent", True), ("all", True)])
async def test_agent_enforcement(admitted, monkeypatch, mode, refused):  # noqa: F811
    _set_config(monkeypatch, _codex_config(auth_file=admitted.runtime.codex_auth_file))
    admitted.settings.sanction_enforcement = mode
    if not refused:
        assert (await admitted.admission.resolve("fixture", admitted.policy, admitted.request))["sanction"]
        return
    with pytest.raises(ExecutionError) as error:
        await admitted.admission.resolve("fixture", admitted.policy, admitted.request)
    assert (error.value.code, error.value.status) == ("route_needs_acknowledgement", 403)
    assert "coding" in str(error.value) and "custom_model_provider" in str(error.value)


async def test_acknowledged_route_passes_enforcement(admitted, monkeypatch):  # noqa: F811
    auth_file = admitted.runtime.codex_auth_file
    cfg = _codex_config(auth_file=auth_file)
    route = cfg.routes["coding"]
    conn = cfg.connections[route.connection]
    _set_config(monkeypatch, _codex_config(acks=[_ack("coding", route, conn, reasons=["custom_model_provider"])], auth_file=auth_file))
    admitted.settings.sanction_enforcement = "all"
    assert (await admitted.admission.resolve("fixture", admitted.policy, admitted.request))["sanction"][
        "status"] == "acknowledged"


def test_enforcement_default_is_off():
    from kotoyose.execution_resources import NativeExecutionConfig
    fields = NativeExecutionConfig.model_fields
    assert fields["sanction_enforcement"].default == "off"
    with pytest.raises(ValidationError):
        NativeExecutionConfig.model_validate({"schema_version": "1", "image_id": "sha256:" + "a" * 64,
                                              "sanction_enforcement": "some"})


def test_warning_is_logged_once_per_route(admitted, monkeypatch):  # noqa: F811
    from kotoyose import agent_sanction
    seen = []
    monkeypatch.setattr(agent_sanction.log, "warning", lambda event, **kw: seen.append((event, kw)))
    reset_warned_routes()
    sanction = route_sanction(_config(), "router")
    for _ in range(3):
        agent_sanction.warn_needs_ack_once("router", sanction, via="chat")
    agent_sanction.warn_needs_ack_once("plain", route_sanction(_config(), "official"), via="chat")
    assert seen == [("route.needs_acknowledgement",
                     {"route": "router", "sanction_reasons": ["unofficial_endpoint"], "via": "chat"})]
    reset_warned_routes()


def _chat_client(monkeypatch, config, enforcement):
    from types import SimpleNamespace
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from kotoyose.routes import chat as chat_routes
    monkeypatch.setattr(chat_routes, "get_config", lambda: config)
    app = FastAPI()
    app.state.execution_service = SimpleNamespace(config=SimpleNamespace(sanction_enforcement=enforcement))
    return app, chat_routes


@pytest.mark.parametrize("mode,status", [("off", None), ("agent", None), ("all", 403)])
def test_chat_enforcement_only_in_all_mode(monkeypatch, mode, status):
    from starlette.requests import Request
    app, chat_routes = _chat_client(monkeypatch, _config(), mode)
    request = Request({"type": "http", "app": app, "headers": [], "method": "POST", "path": "/"})
    reset_warned_routes()
    refused = chat_routes._sanction_gate(request, "routed", "req-1")
    assert (refused.status_code if refused is not None else None) == status
    if refused is not None:
        error = json.loads(refused.body)["error"]
        assert error["type"] == "route_needs_acknowledgement" and "router" in error["message"]
    assert chat_routes._sanction_gate(request, "plain", "req-1") is None
    assert chat_routes._sanction_gate(request, "unknown-alias", "req-1") is None
    reset_warned_routes()


def test_script_prints_candidates_without_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("ROUTER_KEY", "sk\x2dsuper-secret-value")
    path = tmp_path / "config.yml"
    path.write_text(yaml.safe_dump({
        "connections": {
            "router-conn": ROUTER_CONN.model_dump(mode="json"),
            "claude-oauth": OFFICIAL_CONN.model_dump(mode="json"),
        },
        "routes": {"router": ROUTER, "official": {"driver": "claude.agent-sdk", "model": "claude-sonnet-5",
                                                     "connection": "claude-oauth"}},
        "models": {"routed": {"cwd": "/tmp", "route": "router", "agent": {"place": "isolated", "surface": "write"}},
                   "routed2": {"cwd": "/tmp", "route": "router", "agent": {"place": "isolated", "surface": "write"}},
                   "plain": {"cwd": "/tmp", "route": "official", "agent": {"place": "isolated", "surface": "write"}}},
    }))
    done = subprocess.run([sys.executable, str(SCRIPT), str(path)], capture_output=True, text=True, check=True,
                          cwd=tmp_path, env={**__import__("os").environ, "PYTHONPATH": str(ROOT / "src")})
    assert "sk\x2dsuper-secret-value" not in done.stdout + done.stderr
    assert "routed, routed2" in done.stdout and "official" not in done.stdout.replace("unofficial_endpoint", "")
    entries = yaml.safe_load(done.stdout)["risk_acks"]
    assert len(entries) == 1
    expected = route_digest(AgentRoute(**ROUTER), ROUTER_CONN)
    assert entries[0]["route"] == "router" and entries[0]["digest"] == expected
    assert entries[0]["reasons"] == ["unofficial_endpoint"]
    assert entries[0]["acknowledged_by"].startswith("REPLACE") and entries[0]["at"].startswith("REPLACE")
    # 貼ったままでは at が通らない。埋めれば通る。
    config = _config()
    filled = {**entries[0], "acknowledged_by": "owner", "at": "2026-11-14T00:00:00Z"}
    assert route_sanction(HarnessConfig(**{**config.model_dump(), "risk_acks": [filled]}), "router").status == "acknowledged"
    with pytest.raises(ValidationError):
        RiskAck.model_validate(entries[0])


def test_models_api_publishes_sanction(monkeypatch):
    from types import SimpleNamespace
    from kotoyose.routes import models_api
    config = _config()
    monkeypatch.setattr(models_api, "published_agent", lambda alias, policy, settings, config: {"place": "isolated"})
    service = SimpleNamespace(config=None)
    routed = models_api._agent_with_sanction("routed", None, service, config)
    assert routed == {"place": "isolated", "sanction": "needs_ack", "sanction_reasons": ["unofficial_endpoint"]}
    assert models_api._agent_with_sanction("plain", None, service, config) == {"place": "isolated"}


def test_v1_models_and_admin_publish_sanction(routing):
    from kotoyose.config import get_config, save_config
    from kotoyose.execution_resources import DriverInstall
    cfg = get_config().model_copy(deep=True)
    cfg.connections["router-conn"] = ROUTER_CONN
    cfg.connections["claude-oauth"] = OFFICIAL_CONN
    cfg.routes["claude-route"] = AgentRoute(**ROUTER)
    cfg.routes["safe-route"] = AgentRoute(driver="claude.agent-sdk", model="claude-sonnet-5", connection="claude-oauth")
    cfg.models["agent-routed"] = ModelPreset(cwd="/tmp", route="claude-route", agent=AliasAgent(place="isolated", surface="write"))
    cfg.models["agent-safe"] = ModelPreset(cwd="/tmp", route="safe-route", agent=AliasAgent(place="isolated", surface="write"))
    cfg.clients["caller-a"] = cfg.clients["caller-a"].model_copy(update={
        "models": ["agent-routed", "agent-safe"], "places": ["isolated"]})
    save_config(cfg)
    settings = routing.service.config
    settings.drivers = {"claude.agent-sdk": DriverInstall(cli_binary=Path("/bin/sh"))}
    agents = {i["id"]: i["agent"] for i in routing.client.get("/v1/models").json()["data"]}
    assert agents["agent-routed"]["sanction"] == "needs_ack"
    assert agents["agent-routed"]["sanction_reasons"] == ["unofficial_endpoint"]
    assert "sanction" not in agents["agent-safe"]
    admin = routing.client.get("/admin/config").json()
    assert admin["route_sanctions"]["claude-route"]["sanction"] == "needs_ack"
    assert admin["route_sanctions"]["safe-route"]["sanction"] == "official"
    assert "sanction" not in admin["routes"]["claude-route"]
    listed = {i["id"]: i["agent"] for i in routing.client.get("/admin/models?client=caller-a").json()["data"]}
    assert listed["agent-routed"]["sanction"] == "needs_ack"
