"""route / pool alias / risk 承諾の admin API。保存の経路は MCP などと同じ。"""

import pytest
import yaml
from fastapi.testclient import TestClient

from kotoyose.agent_sanction import route_digest
from kotoyose.app import app
from kotoyose.config import AgentRoute, Connection, get_config, reload_config

client = TestClient(app)

CONNECTIONS = {
    "main-env": {"provider": "claude", "kind": "api_key", "data_policy": "no_training", "secret": "env:ANTHROPIC_AUTH_TOKEN"},
    "router-conn": {"provider": "claude", "kind": "endpoint", "data_policy": "no_training", "endpoint": "https://9router.example/v1", "secret": "env:ROUTER_TOKEN"},
    "other-router-conn": {"provider": "claude", "kind": "endpoint", "data_policy": "no_training", "endpoint": "https://other-router.example/v1", "secret": "env:OTHER_NAME"},
    "codex-host": {"provider": "codex", "kind": "subscription", "data_policy": "no_training"},
    "cursor-conn": {"provider": "cursor", "kind": "api_key", "data_policy": "no_training", "secret": "env:CURSOR_API_KEY"},
}
OFFICIAL = {"driver": "claude.agent-sdk", "model": "claude-sonnet-5", "connection": "main-env"}
RISKY = {"driver": "claude.agent-sdk", "model": "claude-sonnet-5", "connection": "router-conn"}
CODEX = {"driver": "codex.app-server", "model": "gpt-5", "connection": "codex-host"}
AGENT = {"place": "isolated", "surface": "write"}


def _write(path, data):
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    reload_config()


def _read(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    path = tmp_path / "config.yml"
    monkeypatch.setenv("KOTOYOSE_CONFIG", str(path))
    monkeypatch.delenv("KOTOYOSE_EXECUTION_CONFIG", raising=False)
    monkeypatch.setattr("kotoyose.auth.API_KEY", None)
    _write(path, {
        "connections": CONNECTIONS,
        "routes": {"main": OFFICIAL, "router": RISKY, "codex": CODEX},
        "models": {
            "chat-a": {"route": "main", "agent": AGENT, "effort": "high"},
            "chat-b": {"route": "router", "agent": AGENT},
            "chat-codex": {"route": "codex", "agent": {"place": "isolated", "surface": "exec"}},
            "plain": {"route": "main"},
            "pool-x": {"candidates": ["chat-a", "chat-b"], "policy": "first_available"},
        },
        "mcps": {"memory": {"description": "m", "server": {"type": "http", "url": "https://mcp.example/"}}},
        "settings": {"anthropic_base_url": "https://settings.example"},
    })
    yield path
    reload_config()


def _unrelated(_data=None):
    """保存は設定を全体として書き直す (既定値も書く) ので、意味で比べる。"""
    dumped = get_config().model_dump(mode="json")
    return {key: dumped[key] for key in ("mcps", "settings", "skill_definitions", "agent_definitions",
                                         "prompt_files")}


# ── routes CRUD ─────────────────────────────────────────────────────────

def test_create_update_route_preserves_unrelated_content(config_path):
    before = _unrelated()
    created = client.post("/admin/routes/second", json=OFFICIAL)
    assert created.status_code == 200 and created.json()["sanction"] == "official"
    data = _read(config_path)
    assert data["routes"]["second"] == OFFICIAL
    assert data["models"]["chat-a"]["route"] == "main" and data["models"]["chat-a"]["agent"] == AGENT
    assert data["models"]["chat-a"]["effort"] == "high"
    assert "provider" not in data["models"]["chat-a"] and "model" not in data["models"]["chat-a"]
    assert data["models"]["pool-x"]["candidates"] == ["chat-a", "chat-b"]
    assert _unrelated() == before

    updated = client.put("/admin/routes/second", json={**OFFICIAL, "model": "claude-opus-5"})
    assert updated.status_code == 200
    assert _read(config_path)["routes"]["second"]["model"] == "claude-opus-5"
    assert _unrelated() == before


def test_route_validation_errors_and_conflicts(config_path):
    assert client.post("/admin/routes/main", json=OFFICIAL).status_code == 409
    assert client.put("/admin/routes/missing", json=OFFICIAL).status_code == 404
    assert client.post("/admin/routes/bad", json={**OFFICIAL, "connection": "missing-conn"}).status_code == 422
    assert client.post("/admin/routes/bad", json={**OFFICIAL, "credential": "env:ANTHROPIC_AUTH_TOKEN"}).status_code == 422
    assert client.post("/admin/routes/bad", json={**OFFICIAL, "driver": "nope.sdk"}).status_code == 422
    assert client.post("/admin/routes/bad", json={**OFFICIAL, "endpoint": "https://u:p@h/"}).status_code == 422
    assert client.post("/admin/routes/bad", json={**OFFICIAL, "model_provider": "x"}).status_code == 422
    assert "bad" not in _read(config_path)["routes"]


def test_update_route_to_driver_that_rejects_alias_place_is_refused(config_path):
    # chat-a は isolated を宣言する。cursor.sdk は host だけ。
    response = client.put("/admin/routes/main", json={"driver": "cursor.sdk", "model": "grok", "connection": "cursor-conn"})
    assert response.status_code == 422
    assert _read(config_path)["routes"]["main"] == OFFICIAL


def test_delete_route_is_refused_while_referenced(config_path):
    response = client.delete("/admin/routes/main")
    assert response.status_code == 409
    detail = response.json()["error"]["message"] if "error" in response.json() else response.json()["detail"]
    assert "chat-a" in detail and "plain" in detail and "pool:pool-x" in detail
    assert "main" in _read(config_path)["routes"]
    assert client.delete("/admin/routes/missing").status_code == 404


def test_delete_unreferenced_route_removes_its_acks(config_path):
    assert client.post("/admin/routes/spare", json=RISKY).status_code == 200
    assert client.post("/admin/routes/spare/ack", json={}).status_code == 200
    assert any(ack["route"] == "spare" for ack in _read(config_path)["risk_acks"])
    response = client.delete("/admin/routes/spare")
    assert response.status_code == 200 and response.json()["removed_acks"] == 1
    data = _read(config_path)
    assert "spare" not in data["routes"]
    assert all(ack["route"] != "spare" for ack in data.get("risk_acks", []))


def test_rename_route_updates_aliases_and_acks_and_keeps_the_ack_valid(config_path):
    assert client.post("/admin/routes/router/ack", json={"note": "ok"}).status_code == 200
    before_digest = route_digest(AgentRoute(**RISKY), Connection(**CONNECTIONS["router-conn"]))

    response = client.post("/admin/routes/router/rename", json={"new_name": "renamed"})
    assert response.status_code == 200 and response.json()["name"] == "renamed"
    data = _read(config_path)
    assert "router" not in data["routes"] and data["routes"]["renamed"] == RISKY
    assert data["models"]["chat-b"]["route"] == "renamed"
    assert [(ack["route"], ack["digest"]) for ack in data["risk_acks"]] == [("renamed", before_digest)]
    # digest は route の名前を含まないので、改名しても承諾は有効。
    cfg = client.get("/admin/config").json()
    assert cfg["route_sanctions"]["renamed"]["sanction"] == "acknowledged"
    assert cfg["route_sanctions"]["renamed"]["ack_stale"] is False
    assert data["models"]["pool-x"]["candidates"] == ["chat-a", "chat-b"]


def test_rename_route_conflict_and_missing(config_path):
    assert client.post("/admin/routes/router/rename", json={"new_name": "main"}).status_code == 409
    assert client.post("/admin/routes/missing/rename", json={"new_name": "x"}).status_code == 404
    assert client.post("/admin/routes/router/rename", json={"new_name": ""}).status_code == 422
    assert client.post("/admin/routes/router/rename", json={"new_name": "router"}).status_code == 200


# ── risk 承諾 ───────────────────────────────────────────────────────────

def test_acknowledge_and_revoke(config_path):
    sanction = client.get("/admin/config").json()["route_sanctions"]["router"]
    assert sanction["sanction"] == "needs_ack" and sanction["sanction_reasons"] == ["unofficial_endpoint"]
    assert sanction["ack"] is None and sanction["ack_stale"] is False

    response = client.post("/admin/routes/router/ack", json={"note": " reviewed "})
    assert response.status_code == 200
    body = response.json()
    assert body["sanction"] == "acknowledged" and body["ack_stale"] is False
    ack = _read(config_path)["risk_acks"][0]
    assert ack["route"] == "router" and ack["digest"] == route_digest(AgentRoute(**RISKY), Connection(**CONNECTIONS["router-conn"]))
    assert ack["reasons"] == ["unofficial_endpoint"] and ack["note"] == "reviewed"
    assert ack["acknowledged_by"] == "auth-disabled"
    assert ack["at"].endswith("Z")

    assert client.post("/admin/routes/router/ack", json={}).status_code == 409  # already acknowledged
    revoked = client.delete("/admin/routes/router/ack")
    assert revoked.status_code == 200 and revoked.json()["sanction"] == "needs_ack"
    assert "risk_acks" not in _read(config_path)
    assert client.delete("/admin/routes/router/ack").status_code == 404


def test_acknowledging_an_official_or_unknown_route_is_refused(config_path):
    assert client.post("/admin/routes/main/ack", json={}).status_code == 409
    assert client.post("/admin/routes/missing/ack", json={}).status_code == 404
    assert client.delete("/admin/routes/missing/ack").status_code == 404
    assert client.post("/admin/routes/router/ack", json={"note": "x" * 501}).status_code == 422


def test_ack_goes_stale_when_the_route_changes_and_can_be_reacknowledged(config_path):
    assert client.post("/admin/routes/router/ack", json={}).status_code == 200
    # connection の kind は endpoint のまま、endpoint URL の異なる connection に変えると digest が変わる。
    changed = {**RISKY, "connection": "other-router-conn"}
    assert client.put("/admin/routes/router", json=changed).status_code == 200
    view = client.get("/admin/config").json()["route_sanctions"]["router"]
    assert view["sanction"] == "needs_ack" and view["ack_stale"] is True and view["ack"] is not None
    # 再承諾で有効になる。
    reacked = client.post("/admin/routes/router/ack", json={"note": "again"})
    assert reacked.status_code == 200 and reacked.json()["ack_stale"] is False
    acks = _read(config_path)["risk_acks"]
    assert len(acks) == 1 and acks[0]["digest"] == route_digest(
        AgentRoute(**changed), Connection(**CONNECTIONS["other-router-conn"]),
    )


def test_env_name_change_does_not_stale_an_ack(config_path):
    assert client.post("/admin/routes/router/ack", json={}).status_code == 200
    assert client.put(
        "/admin/connections/router-conn",
        json={**CONNECTIONS["router-conn"], "secret": "env:OTHER_NAME"},
    ).status_code == 200
    assert client.get("/admin/config").json()["route_sanctions"]["router"]["sanction"] == "acknowledged"


def test_ack_endpoints_need_admin_basic_auth_and_record_the_admin_name(config_path, monkeypatch):
    monkeypatch.setattr("kotoyose.auth.API_KEY", "owner-secret")
    monkeypatch.setattr("kotoyose.auth.BASIC_AUTH_USER", "owner-name")
    bearer = {"Authorization": "Bearer owner-secret"}
    for method, path, body in [
        ("post", "/admin/routes/router/ack", {}),
        ("delete", "/admin/routes/router/ack", None),
        ("post", "/admin/routes/x", OFFICIAL),
        ("put", "/admin/routes/main", OFFICIAL),
        ("delete", "/admin/routes/main", None),
        ("post", "/admin/routes/main/rename", {"new_name": "y"}),
        ("post", "/admin/pools/p", {"candidates": ["chat-a"], "policy": "first_available"}),
    ]:
        kwargs = {"json": body} if body is not None else {}
        assert getattr(client, method)(path, **kwargs).status_code == 401, path
        # agent / caller の Bearer token は admin の経路に届かない。
        assert getattr(client, method)(path, headers=bearer, **kwargs).status_code == 401, path
        assert getattr(client, method)(path, auth=("owner-name", "wrong"), **kwargs).status_code == 401, path
    assert "risk_acks" not in _read(config_path)

    response = client.post("/admin/routes/router/ack", json={}, auth=("owner-name", "owner-secret"))
    assert response.status_code == 200
    assert _read(config_path)["risk_acks"][0]["acknowledged_by"] == "owner-name"


# ── pool alias ──────────────────────────────────────────────────────────

def test_pool_alias_crud_round_trip(config_path):
    before = _unrelated()
    body = {"candidates": ["chat-b", "chat-a"], "policy": "balanced", "requires": ["images"]}
    assert client.post("/admin/pools/pool-y", json=body).status_code == 200
    data = _read(config_path)
    assert data["models"]["pool-y"] == body
    assert data["models"]["pool-x"]["candidates"] == ["chat-a", "chat-b"]
    assert client.get("/admin/config").json()["pools"]["pool-y"] == body

    assert client.put("/admin/pools/pool-y", json={"candidates": ["chat-a"], "policy": "first_available"}).status_code == 200
    assert _read(config_path)["models"]["pool-y"] == {"candidates": ["chat-a"], "policy": "first_available"}
    assert client.delete("/admin/pools/pool-y").status_code == 200
    data = _read(config_path)
    assert "pool-y" not in data["models"] and "pool-x" in data["models"]
    assert _unrelated() == before
    assert client.delete("/admin/pools/pool-y").status_code == 404
    assert client.put("/admin/pools/pool-y", json=body).status_code == 404


@pytest.mark.parametrize("body", [
    {"candidates": [], "policy": "first_available"},
    {"candidates": ["chat-a"], "policy": "no-such-policy"},
    {"candidates": ["chat-a"], "policy": "first_available", "requires": ["telepathy"]},
    {"candidates": ["missing"], "policy": "first_available"},
    {"candidates": ["plain"], "policy": "first_available"},                      # agent が無い
    {"candidates": ["chat-a", "chat-codex"], "policy": "first_available"},      # surface が揃わない
    {"candidates": ["pool-x"], "policy": "first_available"},                    # 入れ子
])
def test_pool_alias_validation_errors(config_path, body):
    response = client.post("/admin/pools/bad", json=body)
    assert response.status_code == 422
    assert "bad" not in _read(config_path)["models"]


def test_pool_name_cannot_shadow_an_alias_and_alias_names_cannot_shadow_a_pool(config_path):
    body = {"candidates": ["chat-a"], "policy": "first_available"}
    assert client.post("/admin/pools/chat-a", json=body).status_code == 409
    assert client.post("/admin/pools/pool-x", json=body).status_code == 409
    assert client.post("/admin/models", json={"model_id": "pool-x", "preset": {"route": "main"}}).status_code == 409


def test_model_alias_rename_follows_pool_candidates_and_delete_is_protected(config_path):
    chat_a = client.get("/admin/config").json()["models"]["chat-a"]
    response = client.post("/admin/models/chat-a/rename", json={"new_name": "chat-a2", "preset": chat_a})
    assert response.status_code == 200
    assert _read(config_path)["models"]["pool-x"]["candidates"] == ["chat-a2", "chat-b"]
    refused = client.delete("/admin/models/chat-b")
    assert refused.status_code == 409 and "pool-x" in str(refused.json())
    assert "chat-b" in _read(config_path)["models"]


# ── alias の route / agent 編集と catalog ──────────────────────────────

def test_alias_route_and_agent_can_be_edited_through_the_model_api(config_path):
    preset = client.get("/admin/config").json()["models"]["plain"]
    assert preset["route"] == "main" and "provider" not in preset
    updated = {**preset, "route": "codex", "agent": {"place": "host", "surface": "read"}}
    assert client.put("/admin/models/plain", json=updated).status_code == 200
    saved = _read(config_path)["models"]["plain"]
    assert saved["route"] == "codex" and saved["agent"] == {"place": "host", "surface": "read"}
    assert "provider" not in saved and "model" not in saved and "anthropic_base_url" not in saved
    # route を持つ alias に provider / model を書くのは、読込時と同じく拒否される。
    assert client.put("/admin/models/plain", json={"route": "main", "model": "x"}).status_code == 422
    # driver が受け付けない place は拒否される。
    bad = {**updated, "route": "main", "agent": {"place": "host", "surface": "read"}}
    assert client.put("/admin/models/plain", json=bad).status_code == 422


def test_config_exposes_driver_and_pool_catalog_from_the_tables(config_path):
    catalog = client.get("/admin/config").json()["catalog"]
    claude = catalog["drivers"]["claude.agent-sdk"]
    assert claude["provider"] == "claude" and list(claude["places"]) == ["isolated"]
    assert "read" not in claude["places"]["isolated"] and "write" in claude["places"]["isolated"]
    assert set(catalog["drivers"]["codex.app-server"]["places"]) == {"host", "isolated"}
    assert list(catalog["drivers"]["cursor.sdk"]["places"]) == ["host"]
    assert catalog["pool"]["strategies"] == ["balanced", "first_available"]
    assert catalog["pool"]["requires"] == ["images"]
