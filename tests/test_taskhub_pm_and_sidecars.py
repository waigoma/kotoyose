"""Task Hub の PM MCP 認証・usage と sidecar preset の回帰テスト。"""

from pathlib import Path
import textwrap
from unittest.mock import AsyncMock

import pytest
import yaml
from fastapi.testclient import TestClient

import kotoyose.session as session_module
from kotoyose.app import app
import kotoyose.config as config_module
from kotoyose.config import _inject_mcp_request_headers, _load_yaml_config, reload_config
from kotoyose.models import ChatMessage
from kotoyose.chat_session import rotate_hash_key
from kotoyose.chat_usage import build_response_usage

from tests.fixtures.api_keys import TEST_API_HEADERS

pytestmark = pytest.mark.usefixtures("client_api_key")

client = TestClient(app)


@pytest.fixture
def restore_global_config():
    """reload_config() による process-global 設定の汚染を test 後に戻す。"""
    original_config = config_module.get_config()
    yield
    config_module._config = original_config


def test_mcp_header_template_injects_request_secret_without_mutating_source():
    """PM token は接続時だけ差し込み、元の設定へ残さない。"""
    servers = {
        "pm": {
            "type": "http",
            "url": "http://localhost:8200/mcp/pm",
            "headers": {"Authorization": "Bearer {{header:X-Pm-Mcp-Token}}"},
        }
    }

    resolved = _inject_mcp_request_headers(servers, {"x-pm-mcp-token": "token-value"})

    assert resolved["pm"]["headers"]["Authorization"] == "Bearer token-value"
    assert servers["pm"]["headers"]["Authorization"] == "Bearer {{header:X-Pm-Mcp-Token}}"


def test_mcp_header_template_rejects_missing_secret():
    """必須 token が無い接続は MCP client を作る前に失敗する。"""
    with pytest.raises(ValueError, match="X-Pm-Mcp-Token"):
        _inject_mcp_request_headers(
            {"pm": {"headers": {"Authorization": "Bearer {{header:X-Pm-Mcp-Token}}"}}},
            {},
        )


def test_response_usage_normalizes_provider_and_cache_fields():
    """provider 固有 usage を Hub が読む OpenAI 互換の形へ揃える。"""
    usage = build_response_usage(
        {
            "input_tokens": 10,
            "output_tokens": 4,
            "cache_read_input_tokens": 3,
            "cache_creation_input_tokens": 2,
            "cost_usd": 0.0123,
        },
        tokens_in=1,
        tokens_out=1,
    )

    assert usage.model_dump() == {
        "prompt_tokens": 10,
        "completion_tokens": 4,
        "cache_read_tokens": 3,
        "cache_write_tokens": 2,
        "cost_usd": 0.0123,
    }


def test_response_usage_preserves_zero_reported_by_provider():
    """有効な 0 token を推定値へ置き換えない。"""
    usage = build_response_usage({"input_tokens": 0, "output_tokens": 0}, 10, 5)
    assert usage.prompt_tokens == 0
    assert usage.completion_tokens == 0


@pytest.mark.asyncio
async def test_v2_key_rotation_preserves_session_identity(monkeypatch):
    """header 無しの初回 turn でも回転後 key へ cache identity を移送する。"""
    session_module.sessions.clear()
    session_module.session_cache_identities.clear()
    client = object()
    identity = ("claude", "kotoyose-chat", "identity-digest")
    monkeypatch.setattr("kotoyose.chat_session.safe_upsert_session", AsyncMock())

    storage_key = await rotate_hash_key(
        lookup_key=None,
        messages=[ChatMessage(role="user", content="hello")],
        answer="world",
        model="kotoyose-chat",
        runtime_session_id=None,
        client=client,
        session_identity=identity,
    )

    assert session_module.sessions[storage_key] is client
    assert session_module.session_cache_identities[storage_key] == identity


def test_pm_request_without_mcp_token_is_rejected_before_runtime(
    tmp_path, monkeypatch, restore_global_config,
):
    """PM endpoint は token を欠く request を 400 で拒否する。"""
    config_path = tmp_path / "config.yml"
    config_path.write_text(textwrap.dedent("""
        models:
          taskhub-pm:
            required_request_headers: [X-Pm-Mcp-Token]
    """))
    monkeypatch.setenv("KOTOYOSE_CONFIG", str(config_path))
    monkeypatch.setattr("kotoyose.auth.API_KEY", None)
    reload_config()

    response = client.post("/v1/chat/completions", headers=TEST_API_HEADERS, json={
        "model": "taskhub-pm",
        "messages": [{"role": "user", "content": "status"}],
    })

    assert response.status_code == 400
    assert "X-Pm-Mcp-Token" in response.json()["error"]["message"]


def test_taskhub_example_pm_is_approval_gated_and_coding_mcp_is_authenticated():
    """PM の filesystem 操作は deny で全面禁止し (plan は MCP tool call も塞ぐため
    不採用)、coding MCP の認証例も task-agent と揃える。"""
    recipes_path = Path(__file__).parents[1] / "tests/fixtures/public-recipes.yml"
    models = yaml.safe_load(recipes_path.read_text())["models"]

    pm = models["orchestrator-pm"]
    # plan は「no tool execution」— PM 自身の /mcp/pm ツール呼び出しまで塞いでしまうため使わない。
    assert pm["permission_mode"] == "bypassPermissions"
    # built-in tools は allowlist (空リスト) で全面封鎖。deny は二重防壁として維持。
    assert pm["tools"] == []
    assert "Bash(*)" in pm["deny"]
    assert "Write(*)" in pm["deny"]
    assert "Edit(*)" in pm["deny"]
    assert models["orchestrator-coding"]["mcp_servers"]["orchestrator"]["headers"] == {
        "Authorization": "Bearer <orchestrator-mcp-token>",
    }

    # decomposer は純テキスト (goal→JSON) 職。built-in tools は allowlist で
    # 全面封鎖し、deny は二重防壁として維持する (orchestrator-pm と同じ縛り方)。
    decomposer = models["orchestrator-decomposer"]
    assert decomposer["tools"] == []
    assert "Bash(*)" in decomposer["deny"]
    assert "Write(*)" in decomposer["deny"]
    assert "Edit(*)" in decomposer["deny"]


def test_sidecar_presets_load_with_static_cwd_contract(tmp_path):
    """各 sidecar は cwd override と許可ルートを明示し、consult は短時間 read-only。"""
    config_path = tmp_path / "config.yml"
    config_path.write_text(textwrap.dedent("""
        models:
          research:
            provider: antigravity
            allow_cwd_override: true
            cwd_allowed_roots: [/workspace]
          consult:
            provider: codex
            allow_cwd_override: true
            cwd_allowed_roots: [/workspace]
            codex: {sandbox: read-only, timeout: 300}
    """))
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv("KOTOYOSE_CONFIG", str(config_path))
        config = _load_yaml_config()

    assert config.preset_for("research").allow_cwd_override is True
    consult = config.preset_for("consult")
    assert consult.codex == {"sandbox": "read-only", "timeout": 300}


def test_taskhub_review_judge_preset_loads_and_pins_prompt_contract():
    """review-judge preset は tests/fixtures/public-recipes.yml から読み込め、参照する prompt は実在し、受け入れ条件の
    当否を論じず unconditionalBlocker を repair とする旨を含む。"""
    repo_root = Path(__file__).parents[1]
    recipes_path = repo_root / "tests/fixtures/public-recipes.yml"
    recipes = yaml.safe_load(recipes_path.read_text())

    judge = recipes["models"]["orchestrator-review-judge"]
    sibling = recipes["models"]["orchestrator-reviewer-quality-max"]
    # 読み取り専用 — permission_mode: plan がツール実行そのものを止める書き込みの
    # 防壁で、deny (taskhub_deny anchor) はツール面を閉じるだけ。両方とも判定するのは
    # 「judge が兄弟 preset と同じ縛り方をされている」ことを固定するため。
    assert judge["permission_mode"] == "plan"
    assert sibling["permission_mode"] == "plan"
    assert judge["deny"] == sibling["deny"]
    assert judge["prompt_file_refs"] == ["orchestrator-review-judge"]

    prompt_ref = recipes["prompt_files"]["orchestrator-review-judge"]
    prompt_path = repo_root / prompt_ref["path"]
    assert prompt_path.exists()

    prompt_text = prompt_path.read_text(encoding="utf-8")
    # 受け入れ条件そのものの当否は judge の裁く事柄ではない旨。
    assert "の当否は論じない" in prompt_text
    # unconditionalBlocker が立った finding は outcome だけを無条件に repair とする旨。
    assert "を無条件に `repair`" in prompt_text
