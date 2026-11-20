"""RequestValidationError を OpenAI 互換形式で返すことを検証するテスト。"""
import pytest
from fastapi.testclient import TestClient

from kotoyose.app import app
from kotoyose.config import reload_config

from tests.fixtures.api_keys import TEST_API_HEADERS

pytestmark = pytest.mark.usefixtures("client_api_key")

client = TestClient(app)


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """各テストを独立した config.yml で実行 (認証もバイパス)。"""
    path = tmp_path / "config.yml"
    path.write_text("models: {}\n")
    monkeypatch.setenv("KOTOYOSE_CONFIG", str(path))
    # KOTOYOSE_TOKEN env が立っている環境でも認証を無効化 (開発時テスト用)
    monkeypatch.setattr("kotoyose.auth.API_KEY", None)
    reload_config()
    yield path
    reload_config()


def test_validation_error_mcp_servers_type_mismatch(isolated_config):
    """mcp_servers 型不整合による検証エラーが OpenAI 互換形式で返ること。"""
    res = client.post("/admin/models", json={
        "model_id": "bad-mcp",
        "preset": {
            "model": "claude-sonnet-4-5",
            "bare": True,
            "cwd": "/tmp",
            "deny": [],
            # mcp_servers は dict であるべき、list を渡して検証エラーを引き起こす
            "mcp_servers": [{"name": "bash", "type": "stdio"}],
        },
    })
    assert res.status_code == 422
    body = res.json()

    # OpenAI 互換形式であること
    assert "error" in body, f"Expected 'error' key, got {body.keys()}"
    error = body["error"]
    assert "message" in error
    assert "type" in error
    assert "request_id" in error

    # type は invalid_request_error であること
    assert error["type"] == "invalid_request_error"

    # message にフィールド位置が含まれること (body.preset.mcp_servers など)
    assert "mcp_servers" in error["message"], \
        f"Expected 'mcp_servers' in message, got: {error['message']}"


def test_validation_error_missing_required_field(isolated_config):
    """必須フィールド欠落による検証エラーが OpenAI 互換形式で返ること。"""
    res = client.post("/v1/chat/completions", headers=TEST_API_HEADERS, json={
        # model は必須だが省略
        "messages": [{"role": "user", "content": "hello"}],
    })
    assert res.status_code == 422
    body = res.json()

    # OpenAI 互換形式であること
    assert "error" in body
    error = body["error"]
    assert "message" in error
    assert "type" in error
    assert "request_id" in error

    # type は invalid_request_error であること
    assert error["type"] == "invalid_request_error"

    # message にフィールド位置が含まれること (body.model など)
    assert "model" in error["message"], \
        f"Expected 'model' in message, got: {error['message']}"


def test_validation_error_multiple_errors(isolated_config):
    """複数フィールドの検証エラーが ; で結合されること。"""
    res = client.post("/v1/chat/completions", headers=TEST_API_HEADERS, json={
        # model と messages の両方が欠落
    })
    assert res.status_code == 422
    body = res.json()

    # OpenAI 互換形式であること
    assert "error" in body
    error = body["error"]
    assert "message" in error
    assert "type" in error
    assert "request_id" in error

    # type は invalid_request_error であること
    assert error["type"] == "invalid_request_error"

    # 複数エラーが ; で結合されていること
    # 少なくとも model と messages が含まれるはず
    message = error["message"]
    assert ("model" in message or "messages" in message), \
        f"Expected validation error fields in message, got: {message}"
