"""
fixed_conversation_key のテスト。

- ModelPreset 拡張のユニットテスト
- chat ルートでのヘッダ vs preset 由来キーの解決ロジック
- /admin CRUD で fixed_conversation_key が保存・往復できること
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from kotoyose.app import app
from kotoyose.config import HarnessConfig, ModelPreset, reload_config
from kotoyose.session import session_cache_identities, session_last_used, sessions

from tests.fixtures.api_keys import TEST_API_HEADERS

pytestmark = pytest.mark.usefixtures("client_api_key")


# ── ユニットテスト: ModelPreset ────────────────────────────────────────


def test_model_preset_default_fixed_conversation_key_is_none():
    """fixed_conversation_key の既定値は None。"""
    p = ModelPreset()
    assert p.fixed_conversation_key is None


def test_model_preset_accepts_fixed_conversation_key():
    """fixed_conversation_key を文字列で受け取れる。"""
    p = ModelPreset(fixed_conversation_key="example-client:default")
    assert p.fixed_conversation_key == "example-client:default"


# ── chat ルート: キー解決ロジック ─────────────────────────────────────


def _patch_preset(
    *,
    memory_enabled: bool = False,
    memory_force: bool = False,
    fixed_conversation_key: str | None = None,
):
    """get_config().preset_for(...) を mock するヘルパ。"""
    fake_preset = ModelPreset(
        bare=True,
        cwd="/tmp",
        memory_enabled=memory_enabled,
        memory_force=memory_force,
        fixed_conversation_key=fixed_conversation_key,
    )
    fake_config = HarnessConfig(models={"kotoyose-chat": fake_preset})
    return patch("kotoyose.routes.chat.get_config", return_value=fake_config)


@pytest.mark.asyncio
async def test_no_header_with_fixed_key_uses_v1_path():
    """ヘッダなし + preset.fixed_conversation_key 設定 → 固定キーで get_or_resume_client が呼ばれる。"""
    with (
        patch("kotoyose.auth.API_KEY", None),
        _patch_preset(fixed_conversation_key="example-client:default"),
        patch("kotoyose.routes.chat.get_or_resume_client") as mock_resume,
        patch(
            "kotoyose.routes.chat.ask_claude",
            new=AsyncMock(return_value=("ok", None, None)),
        ),
    ):
        mock_resume.return_value = (AsyncMock(), False)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers=TEST_API_HEADERS
        ) as ac:
            resp = await ac.post(
                "/v1/chat/completions",
                json={
                    "model": "kotoyose-chat",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": False,
                },
            )

    assert resp.status_code == 200
    # preset 由来の固定キーで get_or_resume_client が呼ばれていること
    mock_resume.assert_awaited_once()
    args, _kwargs = mock_resume.call_args
    assert args[0] == "example-client:default"


@pytest.mark.asyncio
async def test_header_overrides_fixed_conversation_key():
    """ヘッダがあれば preset.fixed_conversation_key より優先される (ヘッダ値で get_or_resume_client)。"""
    with (
        patch("kotoyose.auth.API_KEY", None),
        _patch_preset(fixed_conversation_key="example-client:default"),
        patch("kotoyose.routes.chat.get_or_resume_client") as mock_resume,
        patch(
            "kotoyose.routes.chat.ask_claude",
            new=AsyncMock(return_value=("ok", None, None)),
        ),
    ):
        mock_resume.return_value = (AsyncMock(), False)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers=TEST_API_HEADERS
        ) as ac:
            resp = await ac.post(
                "/v1/chat/completions",
                json={
                    "model": "kotoyose-chat",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": False,
                },
                headers={"X-Conversation-Key": "explicit:override"},
            )

    assert resp.status_code == 200
    mock_resume.assert_awaited_once()
    args, _kwargs = mock_resume.call_args
    # ヘッダ由来の値が採用される (preset の固定キーは使われない)
    assert args[0] == "explicit:override"


@pytest.mark.asyncio
async def test_no_header_no_fixed_key_falls_back_to_v2_hash():
    """ヘッダなし + preset.fixed_conversation_key=None → messages ハッシュ由来 lookup_key で get_or_resume_client。"""
    with (
        patch("kotoyose.auth.API_KEY", None),
        _patch_preset(fixed_conversation_key=None),
        patch("kotoyose.routes.chat.get_or_resume_client") as mock_resume,
        patch(
            "kotoyose.routes.chat.ask_claude",
            new=AsyncMock(return_value=("ok", None, None)),
        ),
        patch("kotoyose.routes.chat._rotate_hash_key", new=AsyncMock()),
    ):
        mock_resume.return_value = (AsyncMock(), False)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers=TEST_API_HEADERS
        ) as ac:
            resp = await ac.post(
                "/v1/chat/completions",
                json={
                    "model": "kotoyose-chat",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": False,
                },
            )

    assert resp.status_code == 200
    mock_resume.assert_awaited_once()
    # 単一メッセージ (履歴なし) → lookup_key は None → get_or_resume_client(None, model) が呼ばれる
    args, _kwargs = mock_resume.call_args
    assert args[0] is None  # lookup_key=None (single message, no history)




@pytest.fixture
def clean_admin(tmp_path, monkeypatch):
    """admin ルート系テスト用: 隔離 config + 認証バイパス + sessions dict をクリア。"""
    path = tmp_path / "config.yml"
    path.write_text("models: {review: {}}\n")
    monkeypatch.setenv("KOTOYOSE_CONFIG", str(path))
    monkeypatch.setattr("kotoyose.auth.API_KEY", None)
    reload_config()
    sessions.clear()
    session_cache_identities.clear()
    session_last_used.clear()
    yield
    sessions.clear()
    session_cache_identities.clear()
    session_last_used.clear()
    reload_config()




# ── /admin CRUD: fixed_conversation_key の保存・取得 ───────────────────


def test_get_config_includes_fixed_conversation_key(clean_admin):
    """GET /admin/config が fixed_conversation_key を返す。"""
    client = TestClient(app)
    res = client.get("/admin/config")
    assert res.status_code == 200
    data = res.json()
    assert "fixed_conversation_key" in data["models"]["review"]
    assert data["models"]["review"]["fixed_conversation_key"] is None


def test_create_model_with_fixed_conversation_key(clean_admin):
    """POST /admin/models で fixed_conversation_key を保存できる。"""
    client = TestClient(app)
    res = client.post(
        "/admin/models",
        json={
            "model_id": "example-client-test",
            "preset": {
                "model": "claude-sonnet-5",
                "bare": True,
                "cwd": "/tmp",
                "deny": [],
                    "mcp_servers": {},
                "memory_enabled": True,
                "memory_force": True,
                "fixed_conversation_key": "example-client:default",
            },
        },
    )
    assert res.status_code == 200

    cfg = client.get("/admin/config").json()
    saved = cfg["models"]["example-client-test"]
    assert saved["fixed_conversation_key"] == "example-client:default"
    assert saved["memory_force"] is True
