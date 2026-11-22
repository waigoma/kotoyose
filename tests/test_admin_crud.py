"""CRUD エンドポイントの統合テスト。"""
import re
from types import SimpleNamespace
import yaml
import pytest
from pydantic import ValidationError
from fastapi.testclient import TestClient

from kotoyose.app import app
from kotoyose.config import Client, HarnessSettings, get_config, reload_config, save_config

client = TestClient(app)


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """各テストを独立した config.yml で実行 (認証もバイパス)。"""
    path = tmp_path / "config.yml"
    path.write_text("models: {}\n")
    monkeypatch.setenv("KOTOYOSE_CONFIG", str(path))
    monkeypatch.delenv("KOTOYOSE_EXECUTION_CONFIG", raising=False)
    monkeypatch.setenv("KOTOYOSE_BROWSE_ROOTS", str(tmp_path))
    # KOTOYOSE_TOKEN env が立っている環境でも認証を無効化 (開発時テスト用)
    monkeypatch.setattr("kotoyose.auth.API_KEY", None)
    reload_config()
    yield path
    reload_config()


@pytest.mark.parametrize("method", ["POST", "PUT"])
def test_connection_save_requires_explicit_data_policy(isolated_config, method):
    original = isolated_config.read_text()
    response = client.request(method, "/admin/connections/codex-host", json={
        "provider": "codex",
        "kind": "subscription",
    })
    assert response.status_code == 422
    assert response.json()["error"]["message"] == (
        "connection 'codex-host': set data_policy (no_training, trains or unknown)"
    )
    assert isolated_config.read_text() == original


# ── POST /admin/models ──────────────────────────────────────────────────

def test_create_model(isolated_config):
    res = client.post("/admin/models", json={
        "model_id": "new-chat",
        "preset": {"model": "claude-sonnet-4-5", "bare": True, "cwd": "/tmp",
                   "deny": [], "mcp_servers": {}},
    })
    assert res.status_code == 200
    assert res.json()["model_id"] == "new-chat"

    with open(isolated_config) as f:
        data = yaml.safe_load(f)
    assert "new-chat" in data["models"]


def test_native_options_disabled(isolated_config, monkeypatch):
    monkeypatch.setattr(app.state, "execution_service", None, raising=False)
    response = client.get("/admin/native/options")
    assert response.status_code == 200
    assert response.json() == {
        "enabled": False, "clients": [], "routes": [], "drivers": [], "agents": [],
    }


def test_native_options_only_exposes_policy_choices(isolated_config, monkeypatch):
    secret = "never-return-this-token-or-hash"
    cfg = get_config().model_copy(deep=True)
    cfg.clients["reviewer"] = Client(
        allowed_env_vars=[secret],
        models=["review"],
        places=["isolated"],
        workspace_roots=[isolated_config.parent / "workspace"],
        max_run_seconds=300,
        max_memory_bytes=512 * 1024 * 1024,
        max_processes=128,
        max_cpus=2.0,
        max_concurrent_runs=1,
    )
    save_config(cfg)
    service = SimpleNamespace(config=SimpleNamespace(drivers=None))
    monkeypatch.setattr(app.state, "execution_service", service, raising=False)

    response = client.get("/admin/native/options")

    assert response.status_code == 200
    assert response.json() == {
        "enabled": True,
        "clients": [{
            "name": "reviewer", "models": ["review"], "places": ["isolated"],
            "max_surface": None, "allowed_agent_models": [],
            "workspace_roots": [str(isolated_config.parent / "workspace")],
            "max_run_seconds": 300, "max_memory_bytes": 512 * 1024 * 1024,
            "max_processes": 128, "max_cpus": 2.0,
        }],
        "routes": [],
        "drivers": [],
        "agents": [],
    }
    assert secret not in response.text


def test_model_alias_edit_rename_and_delete(isolated_config):
    """alias の編集は profile 目録を見ない。削除も名前変更もそのまま通る。"""
    for name, preset in (("review", {"provider": "claude", "model": "claude-sonnet-4-5"}),
                         ("coding", {"provider": "cursor", "model": "grok-test"})):
        assert client.post("/admin/models", json={"model_id": name, "preset": preset}).status_code == 200
    assert client.put("/admin/models/review", json={"provider": "claude", "model": "claude-opus-4-1"}).status_code == 200
    assert client.get("/admin/config").json()["models"]["review"]["model"] == "claude-opus-4-1"
    response = client.post("/admin/models/review/rename", json={
        "new_name": "review-renamed", "preset": {"provider": "claude", "model": "claude-opus-4-1"},
    })
    assert response.status_code == 200
    assert "review-renamed" in client.get("/admin/config").json()["models"]
    assert client.delete("/admin/models/coding").status_code == 200
    assert "coding" not in client.get("/admin/config").json()["models"]


def test_malformed_execution_config_does_not_block_unrelated_admin_saves(isolated_config, monkeypatch):
    """execution.json の参照照合は付随の確認なので、壊れた JSON で無関係な保存を止めない。"""
    path = isolated_config.parent / "execution.json"
    path.write_text('{"callers": {"caller": {')
    monkeypatch.setenv("KOTOYOSE_EXECUTION_CONFIG", str(path))
    response = client.post("/admin/models", json={
        "model_id": "unrelated", "preset": {"provider": "cursor", "model": "grok-test"},
    })
    assert response.status_code == 200
    assert client.delete("/admin/models/unrelated").status_code == 200


@pytest.mark.parametrize("expiry", [{"expires_at": "2030-01-01T12:00:00+09:00"}, {"expires_after_seconds": 300}])
def test_native_alias_admin_save_round_trip(isolated_config, expiry):
    native = {
        "callers": ["reviewer"], "runtime_profile": "claude-native",
        "workspace_path": str(isolated_config.parent / "workspace"),
        "caller_workspace_ref": "workspace:reviewer", "policy_ref": "policy:reviewer",
        "policy_version": 2, **expiry,
        "instructions": "Review the change.", "accept_system_instructions": True,
        "tool_refs": ["builtin:sandbox-exec@1"], "max_turns": 12,
        "timeout": {"schema_version": "1", "kind": "timeout_policy",
                    "soft_seconds": 120, "hard_seconds": 300, "grace_seconds": 10},
        "memory_bytes": 512 * 1024 * 1024, "pids_limit": 128,
        "cpus": 1.5, "output_limit_bytes": 1024 * 1024,
    }
    response = client.post("/admin/models", json={
        "model_id": "native-review", "preset": {"execution": "native", "native": native}
    })
    assert response.status_code == 200, response.text
    saved = client.get("/admin/config").json()["models"]["native-review"]
    assert saved["execution"] == "native"
    assert saved["native"] == native

    response = client.put("/admin/models/native-review", json={
        "execution": "direct", "native": saved["native"]
    })
    assert response.status_code == 200, response.text
    saved = client.get("/admin/config").json()["models"]["native-review"]
    assert saved["execution"] == "direct"
    assert saved["native"] == native


def test_provider_order_persists_in_settings(isolated_config):
    assert HarnessSettings().provider_order == []
    assert client.get("/admin/config").json()["settings"]["provider_order"] == []
    order = ["plugin-provider", "claude", "codex"]
    response = client.put("/admin/settings/provider-order", json={"provider_order": order})
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "provider_order": order}
    assert yaml.safe_load(isolated_config.read_text())["settings"]["provider_order"] == order
    assert reload_config().settings.provider_order == order
    assert client.get("/admin/config").json()["settings"]["provider_order"] == order

    assert client.put("/admin/settings", json={"anthropic_base_url": "https://proxy.example/v1"}).status_code == 200
    assert reload_config().settings.provider_order == order
    assert client.put("/admin/settings/provider-order", json={"provider_order": []}).status_code == 200
    assert reload_config().settings.provider_order == []
    assert get_config().settings.anthropic_base_url == "https://proxy.example/v1"


@pytest.mark.parametrize("order", [["claude", "claude"], [str(i) for i in range(129)], [""]])
def test_provider_order_rejects_invalid_values(isolated_config, order):
    original = isolated_config.read_text()
    with pytest.raises(ValidationError):
        HarnessSettings(provider_order=order)
    response = client.put("/admin/settings/provider-order", json={"provider_order": order})
    assert response.status_code == 422
    assert isolated_config.read_text() == original
    assert get_config().settings.provider_order == []


def test_settings_api_masks_secrets_and_updates_partially(isolated_config):
    """GET は秘密値を返さず、PUT は省略・削除・上書きを区別する。"""
    secret = "anthropic-secret-5678"
    response = client.put("/admin/settings", json={
        "anthropic_auth_token": secret,
        "anthropic_base_url": "https://proxy.example/v1",
    })
    assert response.status_code == 200
    settings = client.get("/admin/settings")
    config = client.get("/admin/config")
    for response in (settings, config):
        assert secret not in response.text
    assert settings.json()["anthropic_auth_token"] == {"set": True, "hint": "…5678"}
    assert settings.json()["anthropic_base_url"] == "https://proxy.example/v1"
    assert config.json()["settings"]["anthropic_auth_token"] == {"set": True, "hint": "…5678"}
    assert client.put("/admin/settings", json={"anthropic_base_url": "https://new.example/v1"}).status_code == 200
    assert client.get("/admin/settings").json()["anthropic_auth_token"] == {"set": True, "hint": "…5678"}
    assert client.put("/admin/settings", json={"anthropic_auth_token": ""}).status_code == 200
    assert client.get("/admin/settings").json()["anthropic_auth_token"] == {"set": False}


def test_create_model_with_provider_configs(isolated_config):
    """POST /admin/models で provider と nested config を保存できる。"""
    res = client.post("/admin/models", json={
        "model_id": "codex-coding",
        "preset": {
            "provider": "codex",
            "model": "gpt-5.1-codex",
            "cwd": "/tmp",
            "codex": {"sandbox": "workspace-write", "reasoning_effort": "high"},
            "cursor": {"runtime": "local"},
            "antigravity": {"command": "agy"},
        },
    })
    assert res.status_code == 200

    saved = client.get("/admin/config").json()["models"]["codex-coding"]
    assert saved["provider"] == "codex"
    assert saved["codex"]["sandbox"] == "workspace-write"
    assert saved["cursor"]["runtime"] == "local"
    assert saved["antigravity"]["command"] == "agy"


def test_create_model_duplicate_returns_409(isolated_config):
    payload = {"model_id": "dup", "preset": {"bare": True, "cwd": "/tmp",
               "deny": [], "mcp_servers": {}}}
    client.post("/admin/models", json=payload)
    res = client.post("/admin/models", json=payload)
    assert res.status_code == 409


def test_create_model_rejects_removed_system_prompt_files(isolated_config):
    """廃止済み raw path を API で無言に捨てない。"""
    response = client.post(
        "/admin/models",
        json={"model_id": "legacy", "preset": {"system_prompt_files": ["/tmp/prompt.md"]}},
    )
    assert response.status_code == 422
    assert "system_prompt_files" in response.text


# ── PUT /admin/models/{model_id} ────────────────────────────────────────

def test_update_model(isolated_config):
    client.post("/admin/models", json={
        "model_id": "edit-me",
        "preset": {"model": "old-id", "bare": True, "cwd": "/tmp",
                   "deny": [], "mcp_servers": {}},
    })
    res = client.put("/admin/models/edit-me", json={
        "model": "new-id", "bare": False, "cwd": "/tmp",
        "deny": ["Bash"], "mcp_servers": {},
    })
    assert res.status_code == 200

    with open(isolated_config) as f:
        data = yaml.safe_load(f)
    assert data["models"]["edit-me"]["model"] == "new-id"
    assert data["models"]["edit-me"]["bare"] is False
    assert "Bash" in data["models"]["edit-me"]["deny"]


def test_update_model_not_found_returns_404(isolated_config):
    res = client.put("/admin/models/ghost", json={
        "bare": True, "cwd": "/tmp", "deny": [], "mcp_servers": {}
    })
    assert res.status_code == 404


# ── DELETE /admin/models/{model_id} ─────────────────────────────────────

def test_delete_model(isolated_config):
    client.post("/admin/models", json={
        "model_id": "bye",
        "preset": {"bare": True, "cwd": "/tmp",
                   "deny": [], "mcp_servers": {}},
    })
    res = client.delete("/admin/models/bye")
    assert res.status_code == 200

    with open(isolated_config) as f:
        data = yaml.safe_load(f)
    assert "bye" not in data.get("models", {})


def test_delete_model_not_found_returns_404(isolated_config):
    res = client.delete("/admin/models/ghost")
    assert res.status_code == 404



# ── 個別リソース参照の妥当性検証 ──────────────────────────────────


def test_model_with_unknown_skill_ref_is_rejected_without_saving(isolated_config):
    """未定義 Skill への skill_refs 参照は 422 とし、不正な Model を保存しない。"""
    response = client.post(
        "/admin/models",
        json={
            "model_id": "broken",
            "preset": {"skill_refs": ["missing"]},
        },
    )

    assert response.status_code == 422
    assert "broken" not in client.get("/admin/config").json()["models"]


# ── GET /admin/ui ────────────────────────────────────────────────────────

def test_admin_config_includes_advanced_taskhub_preset_fields(isolated_config):
    """PM の認証・timeout・権限設定は API/UI round-trip で失われない。"""
    assert client.post("/admin/models", json={
        "model_id": "taskhub-pm",
        "preset": {
            "required_request_headers": ["X-Pm-Mcp-Token"],
            "timeout_seconds": 300,
            "permission_mode": "plan",
        },
    }).status_code == 200
    preset = client.get("/admin/config").json()["models"]["taskhub-pm"]
    assert preset["required_request_headers"] == ["X-Pm-Mcp-Token"]
    assert preset["timeout_seconds"] == 300
    assert preset["permission_mode"] == "plan"


def test_admin_roundtrip_preserves_claude_isolation_options(isolated_config):
    """管理画面が読む投影から再保存しても tool/isolation の制約を失わない。"""
    fields = {
        "tools": ["Bash", "Edit", "Read"],
        "require_isolation": True,
        "strict_mcp_config": True,
        "system_prompt_snapshot": False,
        "output_schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}},
    }
    preset = {"bare": False, "setting_sources": [], "skills": [], **fields}
    endpoint = "/admin/models/isolated-claude"
    assert client.post("/admin/models", json={"model_id": "isolated-claude", "preset": preset}).status_code == 200

    def projected():
        config = client.get("/admin/config").json()
        return config["models"]["isolated-claude"]

    returned = projected()
    assert {name: returned.get(name) for name in fields} == fields
    assert client.put(endpoint, json=returned).status_code == 200
    assert {name: projected().get(name) for name in fields} == fields
    saved = yaml.safe_load(isolated_config.read_text())
    persisted = saved["models"]["isolated-claude"]
    assert {name: persisted.get(name) for name in fields} == fields


def test_individual_mcp_and_prompt_file_resources_are_crud_and_model_selectable(isolated_config):
    """個別リソースは profile を作らずに保存・Model 選択できる。"""
    mcp = {"description": "memory", "server": {"type": "http", "url": "http://localhost:8101/mcp/"}}
    prompt = {"description": "assistant identity", "path": "/tmp/identity.md"}
    assert client.post("/admin/mcps/memory", json=mcp).status_code == 200
    assert client.post("/admin/prompt-files/identity", json=prompt).status_code == 200
    response = client.post("/admin/models", json={
        "model_id": "individual",
        "preset": {"mcp_refs": ["memory"], "prompt_file_refs": ["identity"]},
    })
    assert response.status_code == 200
    config = client.get("/admin/config").json()
    assert config["mcps"]["memory"] == mcp
    assert config["models"]["individual"]["prompt_file_refs"] == ["identity"]
    assert client.delete("/admin/mcps/memory").status_code == 409
    assert client.delete("/admin/prompt-files/identity").status_code == 409


def test_routes_are_listed_in_admin_config(isolated_config):
    """route の編集は /admin/routes に移った (tests/test_admin_routes.py)。一覧は /admin/config に残る。"""
    definition = {"driver": "codex.app-server", "model": "gpt-runtime", "connection": "codex-host",
                  "model_provider": {"id": "proxy", "base_url": "https://proxy.example/v1"}}
    isolated_config.write_text(
        yaml.safe_dump({
            "connections": {"codex-host": {"provider": "codex", "kind": "subscription", "data_policy": "no_training"}},
            "routes": {"daily": definition},
        }),
        encoding="utf-8",
    )
    reload_config()
    config = client.get("/admin/config")
    assert config.status_code == 200
    assert config.json()["routes"]["daily"] == definition


def test_individual_skill_crud_and_model_selection(isolated_config, tmp_path):
    """個別 Skill は plugin 検証付きで CRUD でき、Model から参照できる。"""
    plugin = tmp_path / "plugin"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin" / "plugin.json").write_text('{"name":"demo"}')
    payload = {"description": "debug helper", "plugin": str(plugin), "skill": "debugging"}
    assert client.post("/admin/skills/debug", json=payload).status_code == 200
    assert client.put("/admin/skills/debug", json={**payload, "description": "updated"}).status_code == 200
    assert client.post("/admin/models", json={
        "model_id": "skill-model", "preset": {"skill_refs": ["debug"]},
    }).status_code == 200
    assert client.get("/admin/config").json()["skill_definitions"]["debug"]["description"] == "updated"
    assert client.delete("/admin/skills/debug").status_code == 409
    assert client.delete("/admin/models/skill-model").status_code == 200
    assert client.delete("/admin/skills/debug").status_code == 200


def test_prompt_file_content_is_created_only_below_managed_root(isolated_config, tmp_path):
    """新規 prompt は config と同階層の prompts directory に安全に作成する。"""
    managed = tmp_path / "prompts" / "identity.md"
    assert client.post("/admin/prompt-files/identity", json={
        "description": "assistant identity", "path": str(managed),
    }).status_code == 200
    response = client.put("/admin/prompt-files/identity/content", json={"content": "Be concise."})
    assert response.status_code == 200
    assert managed.read_text(encoding="utf-8") == "Be concise."
    content = client.get("/admin/prompt-files/identity/content").json()
    assert content["content"] == "Be concise."
    assert content["writable"] is True


def test_prompt_file_without_path_uses_managed_default(isolated_config, tmp_path):
    """Admin API の path 省略は config directory 配下の既定 path へ補完する。"""
    assert client.post("/admin/prompt-files/default", json={"description": "default"}).status_code == 200
    saved = client.get("/admin/config").json()["prompt_files"]["default"]
    assert saved["path"] == str(tmp_path / "prompts" / "default.md")


def test_rename_prompt_file_updates_definition_and_all_references_atomically(isolated_config, tmp_path):
    """Rename は定義と全 prompt file 参照を同時に保存する。"""
    path = tmp_path / "prompts" / "identity.md"
    updated_path = tmp_path / "prompts" / "persona.md"
    assert client.post("/admin/prompt-files/identity", json={"path": str(path)}).status_code == 200
    assert client.post("/admin/models", json={"model_id": "alternate", "preset": {"prompt_file_refs": ["identity"]}}).status_code == 200
    assert client.post(
        "/admin/models",
        json={"model_id": "assistant", "preset": {"prompt_file_refs": ["identity"]}},
    ).status_code == 200

    response = client.post(
        "/admin/prompt-files/identity/rename",
        json={"new_name": "persona", "description": "updated", "path": str(updated_path)},
    )

    assert response.status_code == 200
    assert response.json()["name"] == "persona"
    config = client.get("/admin/config").json()
    assert "identity" not in config["prompt_files"]
    assert config["prompt_files"]["persona"] == {
        "description": "updated",
        "path": str(updated_path),
    }
    assert config["models"]["alternate"]["prompt_file_refs"] == ["persona"]
    assert config["models"]["assistant"]["prompt_file_refs"] == ["persona"]
    with open(isolated_config) as file:
        saved = yaml.safe_load(file)
    assert saved["models"]["alternate"]["prompt_file_refs"] == ["persona"]
    assert saved["models"]["assistant"]["prompt_file_refs"] == ["persona"]


def test_rename_prompt_file_rejects_invalid_names_and_allows_noop(isolated_config):
    """Rename は存在・重複・空白を判定し、同名は成功として扱う。"""
    assert client.post("/admin/prompt-files/identity", json={}).status_code == 200
    assert client.post("/admin/prompt-files/other", json={}).status_code == 200

    assert client.post("/admin/prompt-files/missing/rename", json={"new_name": "new"}).status_code == 404
    assert client.post("/admin/prompt-files/identity/rename", json={"new_name": "other"}).status_code == 409
    assert client.post("/admin/prompt-files/identity/rename", json={"new_name": "  \t"}).status_code == 422
    assert client.post("/admin/prompt-files/identity/rename", json={"new_name": "nested/name"}).status_code == 422
    response = client.post("/admin/prompt-files/identity/rename", json={"new_name": "identity"})
    assert response.status_code == 200
    assert response.json()["name"] == "identity"


def test_rename_prompt_file_normalizes_name_and_keeps_omitted_definition(isolated_config, tmp_path):
    """省略した定義を維持しつつ、名前だけを正規化して変更する。"""
    path = tmp_path / "prompts" / "identity.md"
    definition = {"description": "identity", "path": str(path)}
    assert client.post("/admin/prompt-files/identity", json=definition).status_code == 200

    response = client.post("/admin/prompt-files/identity/rename", json={"new_name": "  persona  "})

    assert response.status_code == 200
    assert response.json()["name"] == "persona"
    config = client.get("/admin/config").json()
    assert config["prompt_files"] == {"persona": definition}


def test_rename_mcp_updates_definition_and_all_references_atomically(isolated_config):
    """Rename は MCP 定義と全 MCP 参照を同時に保存する。"""
    definition = {
        "description": "memory",
        "server": {"type": "http", "url": "http://localhost:8101/mcp/"},
    }
    updated_server = {"type": "stdio", "command": "memory-server", "args": ["serve"]}
    assert client.post("/admin/mcps/memory", json=definition).status_code == 200
    assert client.post("/admin/models", json={"model_id": "alternate", "preset": {"mcp_refs": ["memory"]}}).status_code == 200
    assert client.post(
        "/admin/models", json={"model_id": "assistant", "preset": {"mcp_refs": ["memory"]}}
    ).status_code == 200

    response = client.post(
        "/admin/mcps/memory/rename",
        json={"new_name": "memory-gateway", "description": "updated", "server": updated_server},
    )

    assert response.status_code == 200
    assert response.json()["name"] == "memory-gateway"
    config = client.get("/admin/config").json()
    assert "memory" not in config["mcps"]
    assert config["mcps"]["memory-gateway"] == {"description": "updated", "server": updated_server}
    assert config["models"]["alternate"]["mcp_refs"] == ["memory-gateway"]
    assert config["models"]["assistant"]["mcp_refs"] == ["memory-gateway"]
    with open(isolated_config) as file:
        saved = yaml.safe_load(file)
    assert saved["models"]["alternate"]["mcp_refs"] == ["memory-gateway"]
    assert saved["models"]["assistant"]["mcp_refs"] == ["memory-gateway"]


def test_rename_mcp_validates_name_and_preserves_omitted_definition(isolated_config):
    """MCP rename は名前を正規化し、省略した定義を維持する。"""
    definition = {
        "description": "memory",
        "server": {"type": "http", "url": "http://localhost:8101/mcp/"},
    }
    assert client.post("/admin/mcps/memory", json=definition).status_code == 200
    assert client.post("/admin/mcps/other", json=definition).status_code == 200

    assert client.post("/admin/mcps/missing/rename", json={"new_name": "new"}).status_code == 404
    assert client.post("/admin/mcps/memory/rename", json={"new_name": "other"}).status_code == 409
    assert client.post("/admin/mcps/memory/rename", json={"new_name": "  \t"}).status_code == 422
    assert client.post("/admin/mcps/memory/rename", json={"new_name": "nested/name"}).status_code == 422
    response = client.post("/admin/mcps/memory/rename", json={"new_name": "  memory-gateway  "})
    assert response.status_code == 200
    assert response.json()["name"] == "memory-gateway"
    assert client.get("/admin/config").json()["mcps"] == {"other": definition, "memory-gateway": definition}

    response = client.post(
        "/admin/mcps/memory-gateway/rename",
        json={"new_name": "memory-gateway", "description": "ignored"},
    )
    assert response.status_code == 200
    assert client.get("/admin/config").json()["mcps"]["memory-gateway"] == definition


def test_rename_mcp_validates_server_before_saving(isolated_config):
    """Rename に同乗した server は MCP 定義の validator を通過する。"""
    definition = {
        "description": "memory",
        "server": {"type": "http", "url": "http://localhost:8101/mcp/"},
    }
    assert client.post("/admin/mcps/memory", json=definition).status_code == 200
    assert client.post("/admin/models", json={"model_id": "alternate", "preset": {"mcp_refs": ["memory"]}}).status_code == 200

    response = client.post(
        "/admin/mcps/memory/rename",
        json={"new_name": "memory-gateway", "server": {"url": "http://localhost:8101/mcp/"}},
    )

    assert response.status_code == 422
    config = client.get("/admin/config").json()
    assert config["mcps"] == {"memory": definition}
    assert config["models"]["alternate"]["mcp_refs"] == ["memory"]


def test_prompt_file_content_previews_external_path_as_read_only(isolated_config, tmp_path):
    """外部 path は本文を preview できるが、書き込みは拒否する。"""
    external = tmp_path / "external.md"
    external.write_text("Preview only.", encoding="utf-8")
    assert client.post("/admin/prompt-files/external", json={"description": "external", "path": str(external)}).status_code == 200
    assert client.put("/admin/prompt-files/external/content", json={"content": "blocked"}).status_code == 403
    assert client.get("/admin/prompt-files/external/content").json() == {
        "content": "Preview only.", "path": str(external), "writable": False,
    }


def test_prompt_file_content_preview_handles_missing_external_file_and_directory(isolated_config, tmp_path):
    """外部 preview は未作成 file を空で返し、directory は明示的に拒否する。"""
    missing = tmp_path / "missing.md"
    directory = tmp_path / "directory"
    directory.mkdir()
    assert client.post("/admin/prompt-files/missing", json={"path": str(missing)}).status_code == 200
    assert client.post("/admin/prompt-files/directory", json={"path": str(directory)}).status_code == 200

    assert client.get("/admin/prompt-files/missing/content").json() == {
        "content": "", "path": str(missing), "writable": False,
    }
    assert client.get("/admin/prompt-files/directory/content").status_code == 422


def test_prompt_file_content_rejects_symlinked_components_and_target(isolated_config, tmp_path):
    """descriptor ベースの保存は root 内の symlink component / file を辿らない。"""
    root = tmp_path / "prompts"
    outside = tmp_path / "outside"
    outside.mkdir()
    root.mkdir()
    (root / "linked-directory").symlink_to(outside, target_is_directory=True)
    (root / "linked-file.md").symlink_to(outside / "escaped.md")

    for name, path in {
        "directory": root / "linked-directory" / "prompt.md",
        "file": root / "linked-file.md",
    }.items():
        assert client.post("/admin/prompt-files/" + name, json={"path": str(path)}).status_code == 200
        response = client.put("/admin/prompt-files/" + name + "/content", json={"content": "blocked"})
        assert response.status_code == 403

    assert not (outside / "prompt.md").exists()
    assert not (outside / "escaped.md").exists()


def test_prompt_file_content_rejects_symlinked_prompt_root(isolated_config, monkeypatch, tmp_path):
    """設定された prompt root 自体も symlink なら保存先として受け入れない。"""
    outside = tmp_path / "outside"
    outside.mkdir()
    root_link = tmp_path / "prompts-link"
    root_link.symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("KOTOYOSE_PROMPT_ROOT", str(root_link))
    target = root_link / "identity.md"
    assert client.post("/admin/prompt-files/identity", json={"path": str(target)}).status_code == 200
    response = client.put("/admin/prompt-files/identity/content", json={"content": "blocked"})
    assert response.status_code == 403
    assert not (outside / "identity.md").exists()


def test_prompt_file_content_keeps_utf8_behavior(isolated_config, tmp_path):
    """安全な descriptor 書き込みでも UTF-8 の本文を round-trip できる。"""
    path = tmp_path / "prompts" / "日本語.md"
    assert client.post("/admin/prompt-files/japanese", json={"path": str(path)}).status_code == 200
    text = "こんにちは、世界 🌏"
    assert client.put("/admin/prompt-files/japanese/content", json={"content": text}).status_code == 200
    assert path.read_text(encoding="utf-8") == text
    assert client.get("/admin/prompt-files/japanese/content").json()["content"] == text


# ── memory_enabled / memory_force CRUD ─────────────────────────────────


def test_get_config_includes_memory_fields(isolated_config):
    """GET /admin/config が memory_enabled / memory_force を返す。"""
    assert client.post("/admin/models", json={"model_id": "review", "preset": {}}).status_code == 200
    res = client.get("/admin/config")
    assert res.status_code == 200
    data = res.json()
    assert "memory_enabled" in data["models"]["review"]
    assert "memory_force" in data["models"]["review"]


def test_create_model_with_memory_fields(isolated_config):
    """POST /admin/models で memory_enabled / memory_force を保存できる。"""
    res = client.post("/admin/models", json={
        "model_id": "example-client-preset",
        "preset": {
            "model": "claude-haiku-4-5",
            "bare": True,
            "cwd": "/tmp",
            "deny": [],
            "mcp_servers": {},
            "memory_enabled": True,
            "memory_force": True,
        },
    })
    assert res.status_code == 200

    cfg = client.get("/admin/config").json()
    saved = cfg["models"]["example-client-preset"]
    assert saved["memory_enabled"] is True
    assert saved["memory_force"] is True


def test_create_model_rejects_removed_agent_refs(isolated_config):
    """alias の agent_refs は保存せず、委譲への置き換えを返す。"""
    res = client.post("/admin/models", json={
        "model_id": "broken",
        "preset": {"agent_refs": ["researcher"]},
    })
    assert res.status_code == 422
    assert "agent_refs was removed" in res.json()["error"]["message"]
    assert "mcp_refs: [kotoyose]" in res.json()["error"]["message"]
    assert "broken" not in client.get("/admin/config").json()["models"]


def test_create_model_rejects_url_without_type(isolated_config):
    """admin POST: mcp_servers に url-only エントリがあれば 422 で拒否される。"""
    res = client.post("/admin/models", json={
        "model_id": "broken",
        "preset": {
            "model": "claude-sonnet-5",
            "bare": True,
            "cwd": "/tmp",
            "mcp_servers": {
                "ai-task-hub": {"url": "http://localhost:8200/mcp"},
            },
        },
    })
    assert res.status_code == 422
    body = res.json()
    # RequestValidationError を OpenAI 互換形式で返すハンドラが適用
    # body.error.message に検証エラー内容が含まれる
    assert "error" in body
    assert "type は 'http' または 'sse' を明示" in body["error"]["message"]


def test_create_model_with_memory_source(isolated_config):
    """admin POST: memory_source に valid な Literal を保存できる。"""
    res = client.post("/admin/models", json={
        "model_id": "example-client-preset",
        "preset": {
            "model": "claude-sonnet-5",
            "bare": True,
            "cwd": "/tmp",
            "memory_enabled": True,
            "memory_force": True,
            "memory_source": "openwebui",
        },
    })
    assert res.status_code == 200
    saved = client.get("/admin/config").json()["models"]["example-client-preset"]
    assert saved["memory_source"] == "openwebui"


def test_create_model_accepts_arbitrary_memory_source(isolated_config):
    """admin POST: memory_source は任意文字列 (Literal 制約なし)。妥当性は memory-service に委ねる。"""
    res = client.post("/admin/models", json={
        "model_id": "custom-source",
        "preset": {
            "model": "claude-sonnet-5",
            "memory_source": "preset:example-client",
        },
    })
    assert res.status_code == 200
    saved = client.get("/admin/config").json()["models"]["custom-source"]
    assert saved["memory_source"] == "preset:example-client"


def test_create_model_with_thinking_disabled(isolated_config):
    """POST /admin/models で thinking: disabled を保存できる (haiku 用途)。"""
    res = client.post("/admin/models", json={
        "model_id": "haiku-route",
        "preset": {
            "model": "claude-haiku-4-5",
            "bare": True,
            "cwd": "/tmp",
            "thinking": "disabled",
        },
    })
    assert res.status_code == 200
    cfg = client.get("/admin/config").json()
    saved = cfg["models"]["haiku-route"]
    assert saved["thinking"] == "disabled"
    assert saved["thinking_budget_tokens"] is None


def test_create_model_with_thinking_adaptive_and_xhigh(isolated_config):
    """POST /admin/models で thinking: adaptive + effort: xhigh を保存できる (Opus 4.7)。"""
    res = client.post("/admin/models", json={
        "model_id": "opus-route",
        "preset": {
            "model": "claude-opus-4-7",
            "bare": True,
            "cwd": "/tmp",
            "thinking": "adaptive",
            "effort": "xhigh",
        },
    })
    assert res.status_code == 200
    cfg = client.get("/admin/config").json()
    saved = cfg["models"]["opus-route"]
    assert saved["thinking"] == "adaptive"
    assert saved["effort"] == "xhigh"


def test_create_model_with_thinking_enabled_requires_budget(isolated_config):
    """thinking: enabled で budget 未指定はバリデーションエラー (422)。"""
    res = client.post("/admin/models", json={
        "model_id": "legacy-route",
        "preset": {
            "model": "claude-sonnet-4-5",
            "bare": True,
            "cwd": "/tmp",
            "thinking": "enabled",
        },
    })
    assert res.status_code == 422


def test_create_model_with_thinking_enabled_and_budget(isolated_config):
    """thinking: enabled + budget は OK (旧形式)。"""
    res = client.post("/admin/models", json={
        "model_id": "legacy-route",
        "preset": {
            "model": "claude-sonnet-4-5",
            "bare": True,
            "cwd": "/tmp",
            "thinking": "enabled",
            "thinking_budget_tokens": 8192,
        },
    })
    assert res.status_code == 200
    cfg = client.get("/admin/config").json()
    saved = cfg["models"]["legacy-route"]
    assert saved["thinking"] == "enabled"
    assert saved["thinking_budget_tokens"] == 8192


# ── HTTP Basic 認証 ─────────────────────────────────────────────────────


def test_admin_basic_auth_rejects_no_credentials(tmp_path, monkeypatch):
    """KOTOYOSE_TOKEN 設定時、認証情報なしの /admin アクセスは 401 + Basic challenge。"""
    path = tmp_path / "config.yml"
    path.write_text("models: {}\n")
    monkeypatch.setenv("KOTOYOSE_CONFIG", str(path))
    monkeypatch.setattr("kotoyose.auth.API_KEY", "secret-token")
    monkeypatch.setattr("kotoyose.auth.BASIC_AUTH_USER", "kotoyose-service")
    reload_config()
    try:
        res = client.get("/admin/config")
        assert res.status_code == 401
        assert res.headers.get("WWW-Authenticate", "").lower().startswith("basic")
    finally:
        reload_config()


def test_admin_basic_auth_rejects_wrong_password(tmp_path, monkeypatch):
    """誤ったパスワードは 401。"""
    path = tmp_path / "config.yml"
    path.write_text("models: {}\n")
    monkeypatch.setenv("KOTOYOSE_CONFIG", str(path))
    monkeypatch.setattr("kotoyose.auth.API_KEY", "secret-token")
    monkeypatch.setattr("kotoyose.auth.BASIC_AUTH_USER", "kotoyose-service")
    reload_config()
    try:
        res = client.get(
            "/admin/config",
            auth=("kotoyose-service", "wrong-password"),
        )
        assert res.status_code == 401
    finally:
        reload_config()


def test_admin_basic_auth_rejects_wrong_user(tmp_path, monkeypatch):
    """誤ったユーザー名は 401。"""
    path = tmp_path / "config.yml"
    path.write_text("models: {}\n")
    monkeypatch.setenv("KOTOYOSE_CONFIG", str(path))
    monkeypatch.setattr("kotoyose.auth.API_KEY", "secret-token")
    monkeypatch.setattr("kotoyose.auth.BASIC_AUTH_USER", "kotoyose-service")
    reload_config()
    try:
        res = client.get(
            "/admin/config",
            auth=("attacker", "secret-token"),
        )
        assert res.status_code == 401
    finally:
        reload_config()


def test_admin_basic_auth_accepts_correct_credentials(tmp_path, monkeypatch):
    """正しい user/password で /admin/config が 200 を返す。"""
    path = tmp_path / "config.yml"
    path.write_text("models: {}\n")
    monkeypatch.setenv("KOTOYOSE_CONFIG", str(path))
    monkeypatch.setattr("kotoyose.auth.API_KEY", "secret-token")
    monkeypatch.setattr("kotoyose.auth.BASIC_AUTH_USER", "kotoyose-service")
    reload_config()
    try:
        res = client.get(
            "/admin/config",
            auth=("kotoyose-service", "secret-token"),
        )
        assert res.status_code == 200
    finally:
        reload_config()


# ── GET /admin/browse ───────────────────────────────────────────────────

def test_browse_home(isolated_config, tmp_path, monkeypatch):
    """/admin/browse はホームディレクトリの内容を返す。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    res = client.get("/admin/browse")
    assert res.status_code == 200
    data = res.json()
    assert "current" in data
    assert "entries" in data
    assert isinstance(data["entries"], list)


def test_browse_specific_path(isolated_config, tmp_path):
    """/admin/browse?path=<dir> は指定ディレクトリの内容を返す。"""
    subdir = tmp_path / "prompts"
    subdir.mkdir()
    (subdir / "identity.md").write_text("# identity")
    (subdir / "sub").mkdir()
    res = client.get(f"/admin/browse?path={subdir}")
    assert res.status_code == 200
    data = res.json()
    assert data["current"] == str(subdir)
    names = [e["name"] for e in data["entries"]]
    assert "sub" in names
    assert "identity.md" in names
    # dirs come first
    dir_indices = [i for i, e in enumerate(data["entries"]) if e["is_dir"]]
    file_indices = [i for i, e in enumerate(data["entries"]) if not e["is_dir"]]
    assert all(d < f for d in dir_indices for f in file_indices)


def test_browse_non_directory_returns_400(isolated_config, tmp_path):
    """/admin/browse にファイルパスを渡すと 400 を返す。"""
    f = tmp_path / "file.txt"
    f.write_text("hello")
    res = client.get(f"/admin/browse?path={f}")
    assert res.status_code == 400


def test_browse_allowed_root_has_no_parent(isolated_config, tmp_path):
    """許可 root 自体を開いた場合は上位へ移動できない。"""
    response = client.get("/admin/browse", params={"path": str(tmp_path)})

    assert response.status_code == 200
    assert response.json()["current"] == str(tmp_path.resolve())
    assert response.json()["parent"] is None


def test_browse_rejects_path_outside_allowed_roots(isolated_config, tmp_path):
    """許可 root 外のディレクトリは 403 で拒否する。"""
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()

    response = client.get("/admin/browse", params={"path": str(outside)})

    assert response.status_code == 403


def test_browse_rejects_symlink_escape(isolated_config, tmp_path):
    """許可 root 内の symlink が root 外を指す場合も 403 で拒否する。"""
    outside = tmp_path.parent / f"{tmp_path.name}-symlink-target"
    outside.mkdir()
    escape = tmp_path / "escape"
    escape.symlink_to(outside, target_is_directory=True)

    response = client.get("/admin/browse", params={"path": str(escape)})

    assert response.status_code == 403


# ── allow_cwd_override / setting_sources CRUD round-trip ───────────────────


def test_create_model_with_allow_cwd_override(isolated_config):
    """POST /admin/models で allow_cwd_override=True を保存・取得できる。"""
    res = client.post("/admin/models", json={
        "model_id": "coding",
        "preset": {
            "model": "cc/claude-sonnet-5",
            "bare": False,
            "cwd": "/tmp",
            "allow_cwd_override": True,
            "cwd_allowed_roots": ["/tmp"],
            "setting_sources": ["user", "project"],
        },
    })
    assert res.status_code == 200
    saved = client.get("/admin/config").json()["models"]["coding"]
    assert saved["allow_cwd_override"] is True
    assert saved["cwd_allowed_roots"] == ["/tmp"]
    assert saved["setting_sources"] == ["user", "project"]


def test_create_model_allow_cwd_override_defaults_false(isolated_config):
    """allow_cwd_override を省略すると False になる (既定値)。"""
    res = client.post("/admin/models", json={
        "model_id": "plain",
        "preset": {
            "bare": True,
            "cwd": "/tmp",
        },
    })
    assert res.status_code == 200
    saved = client.get("/admin/config").json()["models"]["plain"]
    assert saved["allow_cwd_override"] is False


def test_create_model_setting_sources_none_round_trips(isolated_config):
    """setting_sources=None が round-trip で null として保持される。"""
    res = client.post("/admin/models", json={
        "model_id": "default-sources",
        "preset": {
            "bare": True,
            "cwd": "/tmp",
            "setting_sources": None,
        },
    })
    assert res.status_code == 200
    saved = client.get("/admin/config").json()["models"]["default-sources"]
    assert saved["setting_sources"] is None


def test_create_model_setting_sources_empty_list_round_trips(isolated_config):
    """setting_sources=[] (隔離モード) が round-trip で空リストとして保持される。"""
    res = client.post("/admin/models", json={
        "model_id": "isolated",
        "preset": {
            "bare": True,
            "cwd": "/tmp",
            "setting_sources": [],
        },
    })
    assert res.status_code == 200
    saved = client.get("/admin/config").json()["models"]["isolated"]
    assert saved["setting_sources"] == []


def test_put_model_updates_cwd_override_fields(isolated_config):
    """PUT /admin/models で allow_cwd_override / setting_sources が更新される。"""
    # まず作成
    client.post("/admin/models", json={
        "model_id": "update-me",
        "preset": {
            "bare": True,
            "cwd": "/tmp",
            "allow_cwd_override": False,
            "setting_sources": None,
        },
    })
    # 更新
    res = client.put("/admin/models/update-me", json={
        "bare": False,
        "cwd": "/tmp",
        "allow_cwd_override": True,
        "cwd_allowed_roots": ["/tmp"],
        "setting_sources": ["user", "project"],
    })
    assert res.status_code == 200
    saved = client.get("/admin/config").json()["models"]["update-me"]
    assert saved["allow_cwd_override"] is True
    assert saved["cwd_allowed_roots"] == ["/tmp"]
    assert saved["setting_sources"] == ["user", "project"]


# ── cwd_allowed_roots round-trip テスト ────────────────────────────────


def test_create_model_with_cwd_allowed_roots_round_trip(isolated_config):
    """cwd_allowed_roots を含む preset の POST → GET でフィールドが保持される。"""
    res = client.post("/admin/models", json={
        "model_id": "coding-with-roots",
        "preset": {
            "model": "claude-sonnet-5",
            "bare": False,
            "cwd": "~/kotoyose-workspace",
            "allow_cwd_override": True,
            "cwd_allowed_roots": [
                "~/kotoyose-workspace",
                "/tmp/worktrees",
            ],
        },
    })
    assert res.status_code == 200

    # GET で同じ値が返ってくることを確認
    cfg = client.get("/admin/config").json()
    saved = cfg["models"]["coding-with-roots"]
    assert saved["allow_cwd_override"] is True
    assert saved["cwd_allowed_roots"] == ["~/kotoyose-workspace", "/tmp/worktrees"]


def test_update_model_with_cwd_allowed_roots_preserved(isolated_config):
    """PUT で cwd_allowed_roots が保持される (silent reset 防止)。"""
    # 先に作成
    client.post("/admin/models", json={
        "model_id": "edit-roots",
        "preset": {
            "model": "claude-sonnet-5",
            "bare": True,
            "cwd": "~/ws",
            "allow_cwd_override": True,
            "cwd_allowed_roots": ["~/ws", "/var/tmp"],
        },
    })

    # 他のフィールドを更新しても cwd_allowed_roots が消えないことを確認
    client.put("/admin/models/edit-roots", json={
        "model": "claude-opus-4-8",
        "bare": False,
        "cwd": "~/ws",
        "allow_cwd_override": True,
        "cwd_allowed_roots": ["~/ws", "/var/tmp"],
    })

    cfg = client.get("/admin/config").json()
    saved = cfg["models"]["edit-roots"]
    assert saved["model"] == "claude-opus-4-8"
    assert saved["bare"] is False
    assert saved["cwd_allowed_roots"] == ["~/ws", "/var/tmp"]
