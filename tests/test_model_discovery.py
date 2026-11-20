"""Provider model discovery の契約テスト。"""

import asyncio
import json
import urllib.error
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from kotoyose.app import app
from kotoyose.config import ModelPreset, reload_config
from kotoyose.model_discovery import (
    BUNDLED_CATALOGS,
    CAPTURE_LIMIT_BYTES,
    _read_limited,
    _run_command,
    clear_connection_discovery_cache,
    discover_models,
)

from tests.fixtures.api_keys import TEST_API_HEADERS


client = TestClient(app)


@pytest.mark.asyncio
async def test_run_command_returns_real_stdout_and_stderr():
    """mock では隠れる subprocess 成功経路の戻り値を実 process で検証する。"""
    return_code, stdout, stderr = await _run_command(
        "/bin/sh", "-c", "printf model-a; printf warning >&2"
    )

    assert return_code == 0
    assert stdout == b"model-a"
    assert stderr == b"warning"


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """認証状態と設定ファイルをテストごとに分離する。"""
    path = tmp_path / "config.yml"
    path.write_text("models: {}\n")
    monkeypatch.setenv("KOTOYOSE_CONFIG", str(path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    # Every SDK path is stubbed by default: cache tests must never start Codex or
    # consult a developer's account. Individual SDK-contract tests replace this.
    class UnavailableCodex:
        def __init__(self, config):
            raise FileNotFoundError("mocked unavailable Codex")

    monkeypatch.setattr("kotoyose.model_discovery.AsyncCodex", UnavailableCodex)
    monkeypatch.setattr("kotoyose.auth.API_KEY", None)
    clear_connection_discovery_cache()
    reload_config()
    yield path
    clear_connection_discovery_cache()
    reload_config()


@pytest.mark.asyncio
async def test_discover_cursor_models_from_json(monkeypatch):
    """Cursor SDK の SDKModel.id から候補と取得元を返す。"""

    class FakeClient:
        closed = False

        async def aclose(self):
            self.closed = True

    client = FakeClient()

    async def fake_launch_bridge():
        return client

    async def fake_list(*, client, api_key=None):
        assert api_key == "test"
        return [type("Model", (), {"id": "cursor-fast"})(), type("Model", (), {"id": "cursor-smart"})()]

    monkeypatch.setenv("CURSOR_API_KEY", "test")
    monkeypatch.setattr("kotoyose.model_discovery.AsyncClient.launch_bridge", fake_launch_bridge)
    monkeypatch.setattr("kotoyose.model_discovery.AsyncCursor.models.list", fake_list)

    result = await discover_models("cursor")

    assert result["models"] == ["cursor-fast", "cursor-smart"]
    assert result["source"] == "cursor-sdk"
    assert result["warning"] is None
    assert result["error_code"] is None
    assert client.closed is True
    datetime.fromisoformat(result["fetched_at"].replace("Z", "+00:00"))


@pytest.mark.asyncio
async def test_discover_cursor_models_uses_settings_key_over_environment(monkeypatch):
    """Cursor model discovery も Settings 優先で SDK に key を渡す。"""
    from kotoyose.config import HarnessConfig

    captured = {}

    class FakeClient:
        async def aclose(self):
            pass

    async def fake_launch_bridge():
        return FakeClient()

    async def fake_list(*, client, api_key=None):
        captured["api_key"] = api_key
        return []

    from kotoyose.config import Connection, HarnessConfig
    monkeypatch.setenv("CURSOR_API_KEY", "env-key")
    monkeypatch.setenv("FIXTURE_CURSOR_KEY", "settings-key")
    cfg = HarnessConfig(connections={"cursor": Connection(provider="cursor", kind="api_key", data_policy="no_training", secret="env:FIXTURE_CURSOR_KEY")})
    monkeypatch.setattr("kotoyose.model_discovery.get_config", lambda: cfg)
    monkeypatch.setattr("kotoyose.model_discovery.AsyncClient.launch_bridge", fake_launch_bridge)
    monkeypatch.setattr("kotoyose.model_discovery.AsyncCursor.models.list", fake_list)
    await discover_models("cursor")
    assert captured["api_key"] == "settings-key"


@pytest.mark.asyncio
async def test_discover_cursor_models_uses_settings_key_without_environment(monkeypatch):
    """CURSOR_API_KEY がなくても cursor connection の key だけでモデル一覧を取得できる。"""
    from kotoyose.config import Connection, HarnessConfig

    captured = {}

    class FakeClient:
        async def aclose(self):
            pass

    async def fake_launch_bridge():
        return FakeClient()

    async def fake_list(*, client, api_key=None):
        captured["api_key"] = api_key
        return []

    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    monkeypatch.setenv("FIXTURE_CURSOR_KEY", "settings-key")
    cfg = HarnessConfig(connections={"cursor": Connection(provider="cursor", kind="api_key", data_policy="no_training", secret="env:FIXTURE_CURSOR_KEY")})
    monkeypatch.setattr("kotoyose.model_discovery.get_config", lambda: cfg)
    monkeypatch.setattr("kotoyose.model_discovery.AsyncClient.launch_bridge", fake_launch_bridge)
    monkeypatch.setattr("kotoyose.model_discovery.AsyncCursor.models.list", fake_list)
    await discover_models("cursor")
    assert captured["api_key"] == "settings-key"


@pytest.mark.asyncio
async def test_discover_models_command_failure_is_warning(monkeypatch):
    """CLI 障害時も手入力を妨げない正常レスポンスを返す。"""

    async def missing_command(*command):
        raise FileNotFoundError(command[0])

    monkeypatch.setattr("kotoyose.model_discovery._run_command", missing_command)

    result = await discover_models("antigravity")

    assert result["models"] == []
    assert result["source"] == "agy models"
    assert result["warning"] == (
        "モデル一覧を取得できませんでした。Model ID を手入力してください。"
    )
    assert result["error_code"] == "command_not_found"


@pytest.mark.asyncio
async def test_discover_cursor_models_timeout_is_warning(monkeypatch):
    """SDK timeout を API error にせず手入力 fallback へ変換する。"""

    async def timed_out():
        raise TimeoutError

    monkeypatch.setenv("CURSOR_API_KEY", "test")
    monkeypatch.setattr("kotoyose.model_discovery.AsyncClient.launch_bridge", timed_out)

    result = await discover_models("cursor")

    assert result["models"] == []
    assert "timeout" in result["warning"]
    assert result["error_code"] == "timeout"


@pytest.mark.asyncio
async def test_discover_cursor_models_without_api_key_is_warning(monkeypatch):
    """Cursor SDK の認証キー未設定も手入力可能な degraded response にする。"""
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)

    result = await discover_models("cursor")

    assert result["models"] == []
    assert "CURSOR_API_KEY" in result["warning"]
    assert result["error_code"] == "authentication_missing"


@pytest.mark.asyncio
async def test_discover_models_nonzero_exit_is_warning(monkeypatch):
    """CLI の非 0 終了と標準エラーを警告として返す。"""

    async def unauthenticated(*command):
        return 1, b"", b"authentication required"

    monkeypatch.setattr("kotoyose.model_discovery._run_command", unauthenticated)

    result = await discover_models("antigravity")

    assert result["models"] == []
    assert result["warning"] == (
        "モデル一覧の取得に失敗しました。Model ID を手入力してください。"
    )
    assert "authentication required" not in result["warning"]
    assert result["error_code"] == "nonzero_exit"


def test_claude_bundled_catalog_includes_current_opus_model():
    """Claude の bundled catalog に公式の Opus 5 model ID を含める。"""
    assert "claude-opus-5" in BUNDLED_CATALOGS["claude"]


def test_provider_models_endpoint_forwards_runtime_endpoint(monkeypatch):
    """管理 API は Runtime Model の endpoint context を discovery 層へ渡す。"""
    captured: dict = {}

    async def fake_discover_models(
        provider, endpoint_base_url=None, endpoint_api_key_env=None
    ):
        captured.update(
            {
                "provider": provider,
                "endpoint_base_url": endpoint_base_url,
                "endpoint_api_key_env": endpoint_api_key_env,
            }
        )
        return {"provider": provider, "models": ["endpoint-model"]}

    monkeypatch.setattr("kotoyose.routes.admin.discover_models", fake_discover_models)

    response = client.get(
        "/admin/providers/codex/models",
        params={
            "endpoint_base_url": "https://router.example/v1",
            "endpoint_api_key_env": "ROUTER_TOKEN",
        },
    )

    assert response.status_code == 200
    assert captured == {
        "provider": "codex",
        "endpoint_base_url": "https://router.example/v1",
        "endpoint_api_key_env": "ROUTER_TOKEN",
    }


@pytest.fixture
def clear_claude_auth_env(monkeypatch):
    """テストごとに ANTHROPIC_* 環境変数を既知の状態 (未設定) にリセットする。

    .env に実運用の ANTHROPIC_* が定義されている
    環境で pytest を実行しても、このテストファイルが実ネットワークへ
    出ないようにするための隔離。
    """
    for var in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"):
        monkeypatch.delenv(var, raising=False)


@pytest.mark.asyncio
async def test_discover_claude_models_without_token_falls_back_to_bundled_catalog(
    clear_claude_auth_env, monkeypatch
):
    """認証情報が一切ない場合はネットワークへ出ずに bundled catalog を返す。"""
    # _build_auth_env() は kotoyose.config モジュール内で _get_oauth_token() を
    # 未修飾呼び出しするため、パッチ対象は kotoyose.config 側にする必要がある
    # (kotoyose.model_discovery 側には同名の名前は存在しない)。
    monkeypatch.setattr("kotoyose.config._get_oauth_token", lambda: None)

    result = await discover_models("claude")

    assert result["models"] == BUNDLED_CATALOGS["claude"]
    assert result["source"] == "bundled_catalog"
    assert "認証情報" in result["warning"]
    assert result["error_code"] is None


@pytest.mark.asyncio
async def test_discover_claude_models_queries_base_url_with_both_auth_headers(
    clear_claude_auth_env, monkeypatch
):
    """Runtime Model の endpoint と token env で /models を照会する。"""
    monkeypatch.setenv("RUNTIME_ROUTER_TOKEN", "test-token")
    captured: dict = {}

    def unexpected_default_auth():
        pytest.fail("endpoint 専用 token env がある場合は標準認証を解決しない")

    async def fake_fetch_url(url, headers):
        captured["url"] = url
        captured["headers"] = headers
        return b'{"data":[{"id":"router-model-a"},{"id":"router-model-b"}]}'

    monkeypatch.setattr("kotoyose.model_discovery._fetch_url", fake_fetch_url)
    monkeypatch.setattr("kotoyose.model_discovery._build_auth_env", unexpected_default_auth)

    result = await discover_models(
        "claude",
        endpoint_base_url="https://9router.example.net/v1",
        endpoint_api_key_env="RUNTIME_ROUTER_TOKEN",
    )

    assert captured["url"] == "https://9router.example.net/v1/models"
    assert captured["headers"]["Authorization"] == "Bearer test-token"
    assert captured["headers"]["x-api-key"] == "test-token"
    assert result["models"] == ["router-model-a", "router-model-b"]
    assert result["source"] == "https://9router.example.net/v1/models"
    assert result["warning"] is None


@pytest.mark.asyncio
async def test_discover_claude_models_queries_api_anthropic_com_without_base_url(
    clear_claude_auth_env, monkeypatch
):
    """明示 endpoint がなければ host の ANTHROPIC_BASE_URL を無視して公式 API を照会する。"""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://ignored.example/v1")
    captured: dict = {}

    async def fake_fetch_url(url, headers):
        captured["url"] = url
        captured["headers"] = headers
        return b'{"data":[{"id":"claude-sonnet-5"}]}'

    monkeypatch.setattr("kotoyose.model_discovery._fetch_url", fake_fetch_url)

    result = await discover_models("claude")

    assert captured["url"] == "https://api.anthropic.com/v1/models"
    assert captured["headers"]["x-api-key"] == "test-key"
    assert result["models"] == ["claude-sonnet-5"]
    assert result["source"] == "https://api.anthropic.com/v1/models"


@pytest.mark.asyncio
async def test_discover_claude_models_fetch_error_falls_back_to_bundled_catalog(
    clear_claude_auth_env, monkeypatch
):
    """接続失敗時は例外の詳細を warning に漏らさず bundled catalog へ fallback する。"""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    async def failing_fetch_url(url, headers):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("kotoyose.model_discovery._fetch_url", failing_fetch_url)

    result = await discover_models("claude")

    assert result["models"] == BUNDLED_CATALOGS["claude"]
    assert result["source"] == "bundled_catalog"
    assert "connection refused" not in result["warning"]


@pytest.mark.asyncio
async def test_discover_claude_models_malformed_response_falls_back_to_bundled_catalog(
    clear_claude_auth_env, monkeypatch
):
    """CAPTURE_LIMIT_BYTES 切り詰め等で JSON が壊れた場合、CLI 経路の行分割 fallback
    (ゴミ行を model ID として拾ってしまう) を使わず、素直に bundled catalog へ fallback する。
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    async def truncated_fetch_url(url, headers):
        # 途中で切れた JSON。行分割 fallback を通すとこの 1 行がそのまま
        # model ID 扱いされてしまうため、JSON parse 失敗として検出できるか検証する。
        return b'{"data":[{"id":"router-model-a"},{"id":"trunca'

    monkeypatch.setattr("kotoyose.model_discovery._fetch_url", truncated_fetch_url)

    result = await discover_models("claude")

    assert result["models"] == BUNDLED_CATALOGS["claude"]
    assert result["source"] == "bundled_catalog"
    assert "解析" in result["warning"]


@pytest.mark.asyncio
async def test_discover_claude_models_retries_with_v1_suffix_when_base_url_lacks_it(
    clear_claude_auth_env, monkeypatch
):
    """base_url が /v1 で終わらない場合、{base}/models が失敗したら {base}/v1/models を試す。"""
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "test-token")
    calls: list[str] = []

    async def fake_fetch_url(url, headers):
        calls.append(url)
        if url == "https://9router.example.net/models":
            raise urllib.error.URLError("404 not found")
        return b'{"data":[{"id":"router-model-a"}]}'

    monkeypatch.setattr("kotoyose.model_discovery._fetch_url", fake_fetch_url)

    result = await discover_models(
        "claude", endpoint_base_url="https://9router.example.net"
    )

    assert calls == [
        "https://9router.example.net/models",
        "https://9router.example.net/v1/models",
    ]
    assert result["models"] == ["router-model-a"]
    assert result["source"] == "https://9router.example.net/v1/models"


@pytest.mark.asyncio
async def test_discover_codex_models_queries_runtime_endpoint(monkeypatch):
    """Codex は Runtime Model の endpoint と token env から候補を取得する。"""
    monkeypatch.setenv("CODEX_ROUTER_TOKEN", "codex-token")
    captured: dict = {}

    async def fake_fetch_url(url, headers):
        captured["url"] = url
        captured["headers"] = headers
        return b'{"models":[{"id":"gpt-router-a"}]}'

    monkeypatch.setattr("kotoyose.model_discovery._fetch_url", fake_fetch_url)

    result = await discover_models(
        "codex",
        endpoint_base_url="https://router.example/v1",
        endpoint_api_key_env="CODEX_ROUTER_TOKEN",
    )

    assert captured["url"] == "https://router.example/v1/models"
    assert captured["headers"]["Authorization"] == "Bearer codex-token"
    assert result["models"] == ["gpt-router-a"]
    assert result["source"] == "https://router.example/v1/models"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["claude", "codex"])
async def test_runtime_endpoint_with_unknown_token_env_falls_back(
    provider, monkeypatch
):
    """未定義の token env は外部照会せず、安全な候補と警告へ fallback する。"""
    monkeypatch.delenv("MISSING_ROUTER_TOKEN", raising=False)

    async def unexpected_fetch_url(url, headers):
        pytest.fail(f"unexpected endpoint request: {url} {headers}")

    monkeypatch.setattr("kotoyose.model_discovery._fetch_url", unexpected_fetch_url)

    result = await discover_models(
        provider,
        endpoint_base_url="https://router.example/v1",
        endpoint_api_key_env="MISSING_ROUTER_TOKEN",
    )

    assert result["models"] == BUNDLED_CATALOGS[provider]
    assert result["source"] == "bundled_catalog"
    assert "MISSING_ROUTER_TOKEN" in result["warning"]


@pytest.mark.asyncio
async def test_discover_codex_models_filters_by_visibility(monkeypatch, tmp_path):
    """visibility が list 以外 (hide 等) の内部限定モデルは候補から除外する。"""
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    (codex_dir / "models_cache.json").write_text(
        json.dumps({
            "models": [
                {"slug": "gpt-5.5", "visibility": "list"},
                {"slug": "codex-auto-review", "visibility": "hide"},
                {"slug": "legacy-model"},  # visibility キー欠落 → list 扱い (旧形式互換)
            ]
        })
    )
    # Path.expanduser() は Path.home() ではなく HOME 環境変数を直接見るため、
    # monkeypatch.setattr(Path, "home", ...) では差し替わらない。
    monkeypatch.setenv("HOME", str(tmp_path))

    result = await discover_models("codex")

    assert result["models"] == ["gpt-5.5", "legacy-model"]
    assert result["source"] == "codex-cache"
    assert result["warning"] is not None
    assert result["freshness"] == "stale"
    assert result["fetched_at"] is None
    assert result["error_code"] == "command_not_found"


@pytest.mark.asyncio
async def test_discover_codex_models_missing_cache_falls_back_to_bundled_catalog(
    monkeypatch, tmp_path
):
    """cache file がない場合は bundled catalog へ fallback する。"""
    # Path.expanduser() は Path.home() ではなく HOME 環境変数を直接見るため、
    # monkeypatch.setattr(Path, "home", ...) では差し替わらない。
    monkeypatch.setenv("HOME", str(tmp_path))

    result = await discover_models("codex")

    assert result["models"] == BUNDLED_CATALOGS["codex"]
    assert result["source"] == "bundled_catalog"
    assert "cache" in result["warning"]


@pytest.mark.asyncio
async def test_discover_codex_models_invalid_utf8_cache_falls_back_to_bundled_catalog(
    monkeypatch, tmp_path
):
    """非 UTF-8 の cache file は UnicodeDecodeError を握りつぶし bundled catalog へ fallback する。

    UnicodeDecodeError は ValueError のサブクラスだが JSONDecodeError ではないため、
    except 節が (OSError, json.JSONDecodeError) のままだと未捕捉のまま discover_models()
    の外へ漏れてしまう回帰を防ぐ。
    """
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    (codex_dir / "models_cache.json").write_bytes(b"\xff\xfe\x00invalid-utf8")
    monkeypatch.setenv("HOME", str(tmp_path))

    result = await discover_models("codex")

    assert result["models"] == BUNDLED_CATALOGS["codex"]
    assert result["source"] == "bundled_catalog"
    assert "cache" in result["warning"]


def test_provider_models_endpoint_rejects_unknown_provider():
    """未対応 Provider は設定ミスを隠さず 404 にする。"""
    response = client.get("/admin/providers/unknown/models")

    assert response.status_code == 404


@pytest.mark.usefixtures("client_api_key")
def test_v1_models_still_returns_preset_ids(isolated_config):
    """互換 API は discovery 候補ではなく従来の preset ID を返す。"""
    isolated_config.write_text(
        "models:\n"
        "  coding:\n"
        "    provider: cursor\n"
        "    model: cursor-smart\n"
        "  review:\n"
        "    provider: claude\n"
        "    model: claude-review\n"
    )
    reload_config()

    response = client.get("/v1/models", headers=TEST_API_HEADERS)

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["data"]] == ["coding", "review"]


@pytest.mark.asyncio
async def test_stream_capture_is_bounded_while_input_is_drained():
    """巨大な CLI 出力を最後まで drain しても保持量は上限を超えない。"""
    reader = asyncio.StreamReader()
    reader.feed_data(b"a" * CAPTURE_LIMIT_BYTES)
    reader.feed_data(b"secret" * CAPTURE_LIMIT_BYTES)
    reader.feed_eof()

    captured = await _read_limited(reader)

    assert len(captured) == CAPTURE_LIMIT_BYTES
    assert await reader.read() == b""


def _codex_catalog(*, models=None, next_cursor=None):
    """Exercise the installed SDK's real generated response contract, not dict mocks."""
    from openai_codex.generated.v2_all import ModelListResponse

    if models is None:
        models = [dict(
            id="catalog-entry", model="provider-model", displayName="Provider model",
            description="Configured catalog", hidden=False, isDefault=True,
            defaultReasoningEffort="high",
            supportedReasoningEfforts=[{"reasoningEffort": "high", "description": "Thorough"}],
            inputModalities=["text", "image"],
            serviceTiers=[{"id": "priority", "name": "Priority", "description": "Faster"}],
            defaultServiceTier="priority",
        )]
    return ModelListResponse.model_validate({"data": models, "nextCursor": next_cursor})


def _mock_codex_catalog(monkeypatch, catalog=None, error=None):
    captured = {"closed": False}

    class FakeCodex:
        def __init__(self, config):
            captured["config"] = config

        async def models(self, *, include_hidden):
            captured["include_hidden"] = include_hidden
            if error is not None:
                raise error
            return catalog if catalog is not None else _codex_catalog()

        async def close(self):
            captured["closed"] = True

    monkeypatch.setattr("kotoyose.model_discovery.AsyncCodex", FakeCodex)
    return captured


@pytest.mark.asyncio
async def test_codex_sdk_catalog_preserves_metadata_and_filters_hidden(monkeypatch):
    catalog = _codex_catalog()
    visible = catalog.data[0]
    catalog.data.extend([
        visible.model_copy(update={"hidden": True, "model": "hidden-model"}),
        visible.model_copy(),
    ])
    captured = _mock_codex_catalog(monkeypatch, catalog)
    result = await discover_models("codex")

    assert captured["include_hidden"] is False
    assert captured["closed"] is True
    assert result["models"] == ["provider-model"]
    assert result["source"] == "codex-sdk"
    assert result["freshness"] == "sdk_reported"
    assert result["is_fallback"] is False
    assert result["cache_fetched_at"] is None
    assert result["complete"] is True
    assert result["warning"] is not None  # SDK does not establish live entitlement.
    assert result["model_details"] == [{
        "id": "provider-model", "catalog_id": "catalog-entry", "label": "Provider model",
        "description": "Configured catalog", "is_default": True,
        "default_reasoning_effort": "high",
        "supported_reasoning_efforts": [{"reasoning_effort": "high", "description": "Thorough"}],
        "input_modalities": ["text", "image"],
        "service_tiers": [{"id": "priority", "name": "Priority", "description": "Faster"}],
        "default_service_tier": "priority",
    }]
    json.dumps(result)


@pytest.mark.asyncio
async def test_codex_sdk_uses_effective_preset_config_command_home_and_provider(monkeypatch, tmp_path):
    from kotoyose.runtime.codex import CodexRuntimeSession

    captured = _mock_codex_catalog(monkeypatch)
    monkeypatch.setenv("CODEX_HOME", "/other/home")
    preset = ModelPreset(
        provider="codex", model="selected", cwd=str(tmp_path), codex_home="~/separate",
        codex={
            "codex_bin": "~/bin/codex", "profile": "custom",
            "config": {"model_catalog_json": "/catalog.json", "features": {"fast_mode": True}},
            "model_provider": {
                "id": "router", "base_url": "https://router.example/v1", "env_key": "ROUTER_KEY",
            },
        },
    )
    result = await discover_models("codex", preset=preset)
    sdk_config = captured["config"]
    assert sdk_config.cwd == str(tmp_path)
    assert sdk_config.codex_bin == str(tmp_path / "bin/codex")
    assert sdk_config.env == {"CODEX_HOME": str(tmp_path / "separate")}
    runtime = CodexRuntimeSession(model=preset.model, cwd=preset.cwd, config=dict(preset.codex))
    assert sdk_config.config_overrides == runtime._build_config_overrides()
    assert 'model_provider="router"' in sdk_config.config_overrides
    assert result["models"] == ["provider-model"]
    assert not set(BUNDLED_CATALOGS["codex"]) & set(result["models"])


@pytest.mark.asyncio
async def test_codex_sdk_authoritative_empty_catalog_does_not_fallback(monkeypatch, tmp_path):
    home = tmp_path / ".codex"
    home.mkdir()
    (home / "models_cache.json").write_text(json.dumps({"models": [{"slug": "unrelated"}]}))
    _mock_codex_catalog(monkeypatch, _codex_catalog(models=[]))
    result = await discover_models("codex")
    assert result["models"] == []
    assert result["is_fallback"] is False
    assert result["source"] == "codex-sdk"


@pytest.mark.asyncio
async def test_codex_sdk_reports_partial_catalog_without_claiming_completeness(monkeypatch):
    _mock_codex_catalog(monkeypatch, _codex_catalog(next_cursor="next-page"))
    result = await discover_models("codex")
    assert result["complete"] is False
    assert "一部" in result["warning"]


@pytest.mark.asyncio
async def test_codex_sdk_timeout_closes_client(monkeypatch):
    closed = False

    class StalledCodex:
        def __init__(self, config):
            pass

        async def models(self, *, include_hidden):
            await asyncio.Event().wait()

        async def close(self):
            nonlocal closed
            closed = True

    monkeypatch.setattr("kotoyose.model_discovery.AsyncCodex", StalledCodex)
    monkeypatch.setattr("kotoyose.model_discovery.DISCOVERY_TIMEOUT_SECONDS", 0.01)
    result = await discover_models("codex")
    assert closed
    assert result["error_code"] == "timeout"
    assert result["is_fallback"] is True
    assert result["fetched_at"] is None
    assert result["freshness"] == "unverified"


@pytest.mark.asyncio
async def test_codex_sdk_cancel_closes_client_and_preserves_cancellation(monkeypatch):
    started = asyncio.Event()
    closed = False

    class StalledCodex:
        def __init__(self, config):
            pass

        async def models(self, *, include_hidden):
            started.set()
            await asyncio.Event().wait()

        async def close(self):
            nonlocal closed
            closed = True

    monkeypatch.setattr("kotoyose.model_discovery.AsyncCodex", StalledCodex)
    task = asyncio.create_task(discover_models("codex"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed


@pytest.mark.asyncio
async def test_codex_sdk_cleanup_failure_is_bounded_and_visible(monkeypatch):
    class StalledClose:
        def __init__(self, config):
            pass

        async def models(self, *, include_hidden):
            return _codex_catalog()

        async def close(self):
            await asyncio.Event().wait()

    monkeypatch.setattr("kotoyose.model_discovery.AsyncCodex", StalledClose)
    monkeypatch.setattr("kotoyose.model_discovery.CODEX_CLEANUP_TIMEOUT_SECONDS", 0.01)
    result = await asyncio.wait_for(discover_models("codex"), timeout=1)
    assert result["models"] == ["provider-model"]
    assert "終了処理" in result["warning"]


@pytest.mark.asyncio
async def test_codex_sdk_auth_failure_is_redacted_and_cleanup_runs(monkeypatch):
    from openai_codex import JsonRpcError

    secret = "sensitive-token-not-for-logs"
    captured = _mock_codex_catalog(
        monkeypatch, error=JsonRpcError(-32000, f"authentication failed: Authorization Bearer {secret}"),
    )
    logs = []
    monkeypatch.setattr("kotoyose.model_discovery.log.warning", lambda *args, **kwargs: logs.append((args, kwargs)))
    result = await discover_models("codex")
    assert captured["closed"] is True
    assert result["error_code"] == "authentication_failed"
    assert result["fetched_at"] is None
    assert secret not in json.dumps([result, logs])


@pytest.mark.asyncio
@pytest.mark.parametrize("use_preset", [False, True])
async def test_codex_cache_failure_fallback_uses_matching_home_and_original_age(monkeypatch, tmp_path, use_preset):
    selected_home = tmp_path / "selected"
    selected_home.mkdir()
    timestamp = "2020-01-01T00:00:00Z"
    (selected_home / "models_cache.json").write_text(json.dumps({
        "fetched_at": timestamp, "models": [{"slug": "cached-model"}],
    }))
    default_home = tmp_path / ".codex"
    default_home.mkdir()
    (default_home / "models_cache.json").write_text(json.dumps({"models": [{"slug": "wrong-home-model"}]}))
    if use_preset:
        preset = ModelPreset(provider="codex", codex_home=str(selected_home))
        monkeypatch.setenv("CODEX_HOME", str(default_home))
    else:
        preset = None
        monkeypatch.setenv("CODEX_HOME", str(selected_home))
    result = await discover_models("codex", preset=preset)
    assert result["models"] == ["cached-model"]
    assert result["source"] == "codex-cache"
    assert result["freshness"] == "stale"
    assert result["fetched_at"] == timestamp
    assert result["cache_fetched_at"] == timestamp
    assert result["queried_at"] != timestamp
    assert result["is_fallback"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("codex", [
    {"model_provider": "custom"},
    {"profile": "other"},
    {"config": {"model_provider": "custom"}},
    {"config": {"model_catalog_json": "/some/catalog.json"}},
    {"config": {"model_providers.custom.base_url": "https://example.test"}},
])
async def test_codex_custom_catalog_failure_never_advertises_bundled_or_unscoped_cache(monkeypatch, tmp_path, codex):
    home = tmp_path / ".codex"
    home.mkdir()
    (home / "models_cache.json").write_text(json.dumps({"models": [{"slug": "wrong-provider"}]}))
    result = await discover_models("codex", preset=ModelPreset(provider="codex", codex=codex))
    assert result["models"] == []
    assert result["freshness"] == "unavailable"
    assert result["error_code"] == "command_not_found"


@pytest.mark.asyncio
async def test_codex_home_custom_provider_config_excludes_unscoped_fallback(monkeypatch, tmp_path):
    home = tmp_path / ".codex"
    home.mkdir()
    (home / "config.toml").write_text('model_provider = "custom"\n')
    (home / "models_cache.json").write_text(json.dumps({"models": [{"slug": "wrong-provider"}]}))
    result = await discover_models("codex")
    assert result["models"] == []
    assert result["freshness"] == "unavailable"


def test_codex_discovery_timeout_respects_shorter_explicit_preset_deadline():
    from kotoyose.model_discovery import _codex_discovery_timeout

    assert _codex_discovery_timeout(None) == 10.0
    assert _codex_discovery_timeout(ModelPreset(provider="codex", codex={"timeout": 1})) == 1.0
    assert _codex_discovery_timeout(ModelPreset(provider="codex", timeout_seconds=2)) == 2.0
    assert _codex_discovery_timeout(ModelPreset(provider="codex", codex={"timeout": 100})) == 10.0


def test_codex_bundled_fallback_includes_sdk_default_model():
    assert "gpt-6.1-sol" in BUNDLED_CATALOGS["codex"]


@pytest.mark.asyncio
async def test_codex_project_provider_config_excludes_unscoped_fallback(tmp_path):
    cwd = tmp_path / "project"
    config_home = cwd / ".codex"
    config_home.mkdir(parents=True)
    (config_home / "config.toml").write_text('model_provider = "project-provider"\n')
    result = await discover_models("codex", preset=ModelPreset(provider="codex", cwd=str(cwd)))
    assert result["models"] == []
    assert result["freshness"] == "unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("timestamp", ["not-a-date", "2099-01-01T00:00:00Z", "2020-01-01T00:00:00"])
async def test_codex_cache_invalid_timestamp_never_becomes_fresh(tmp_path, timestamp):
    home = tmp_path / ".codex"
    home.mkdir()
    (home / "models_cache.json").write_text(json.dumps({
        "models": [{"slug": "stale-model"}], "fetched_at": timestamp,
    }))
    result = await discover_models("codex")
    assert result["models"] == ["stale-model"]
    assert result["freshness"] == "stale"
    assert result["fetched_at"] is None
    assert result["cache_fetched_at"] is None


@pytest.mark.asyncio
async def test_codex_oversized_cache_is_not_parsed(tmp_path):
    home = tmp_path / ".codex"
    home.mkdir()
    (home / "models_cache.json").write_text(json.dumps({
        "models": [{"slug": "cached-model"}], "padding": "x" * CAPTURE_LIMIT_BYTES,
    }))
    result = await discover_models("codex")
    assert result["source"] == "bundled_catalog"
    assert result["fetched_at"] is None


@pytest.mark.asyncio
async def test_codex_invalid_provider_config_does_not_leak_config_or_read_fallback(monkeypatch):
    secret = "secret-endpoint-invalid-config"
    logs = []
    monkeypatch.setattr("kotoyose.model_discovery.log.warning", lambda *args, **kwargs: logs.append((args, kwargs)))
    result = await discover_models("codex", preset=ModelPreset(
        provider="codex", codex={"model_provider": {"id": secret + "/bad", "base_url": "https://router.test"}},
    ))
    assert result["error_code"] == "invalid_config_or_response"
    assert result["models"] == []
    assert secret not in json.dumps([result, logs])


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("failure", ["fetch", "parse"])
async def test_endpoint_discovery_never_logs_credential_urls_or_exception_text(monkeypatch, provider, failure):
    secret = "private-credential-marker"
    url = f"https://user:{secret}@router.example/v1?api_key={secret}#{secret}"
    logs = []
    monkeypatch.setenv("TEST_DISCOVERY_KEY", secret)
    monkeypatch.setattr("kotoyose.model_discovery.log.warning", lambda *args, **kwargs: logs.append((args, kwargs)))

    async def fake_fetch(request_url, headers):
        if failure == "fetch":
            raise urllib.error.URLError(f"{request_url} Authorization Bearer {secret}")
        return f"invalid JSON {secret}".encode()

    monkeypatch.setattr("kotoyose.model_discovery._fetch_url", fake_fetch)
    result = await discover_models(provider, endpoint_base_url=url, endpoint_api_key_env="TEST_DISCOVERY_KEY")
    assert result["source"] == "bundled_catalog"
    assert logs
    assert secret not in json.dumps([result, logs])
    assert "router.example" not in json.dumps(logs)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["claude", "codex"])
async def test_endpoint_discovery_redacts_source_credentials_and_preserves_request_query(monkeypatch, provider):
    secret = "private-credential-marker"
    url = f"https://user:{secret}@router.example:8443/v1?api_key={secret}#{secret}"
    captured = []
    monkeypatch.setenv("TEST_DISCOVERY_KEY", secret)

    async def fake_fetch(request_url, headers):
        captured.append(request_url)
        return b'{"data":[{"id":"endpoint-model"}]}'

    monkeypatch.setattr("kotoyose.model_discovery._fetch_url", fake_fetch)
    result = await discover_models(provider, endpoint_base_url=url, endpoint_api_key_env="TEST_DISCOVERY_KEY")
    assert captured == [f"https://user:{secret}@router.example:8443/v1/models?api_key={secret}"]
    assert result["source"] == "https://router.example:8443/v1/models"
    assert secret not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint_args", [
    {"endpoint_base_url": "https://different-provider.example/v1"},
    {"endpoint_api_key_env": "DIFFERENT_PROVIDER_KEY"},
])
async def test_codex_discovery_rejects_ambiguous_preset_and_endpoint(monkeypatch, endpoint_args):
    def unexpected_sdk(*args, **kwargs):
        pytest.fail("Ambiguous discovery must not start an SDK process")

    async def unexpected_fetch(*args, **kwargs):
        pytest.fail("Ambiguous discovery must not call an endpoint")

    monkeypatch.setattr("kotoyose.model_discovery.AsyncCodex", unexpected_sdk)
    monkeypatch.setattr("kotoyose.model_discovery._fetch_url", unexpected_fetch)
    with pytest.raises(ValueError, match="either a model alias or an explicit endpoint"):
        await discover_models("codex", preset=ModelPreset(provider="codex"), **endpoint_args)


def _mock_claude_sdk(monkeypatch, *, server_info=None, connect_error=None, stall=False):
    """Stub ClaudeSDKClient in model_discovery without launching any real CLI."""
    captured = {"calls": 0, "disconnected": 0, "options": []}

    class FakeClaudeClient:
        def __init__(self, options):
            captured["calls"] += 1
            captured["options"].append(options)
            captured["env_snapshot"] = dict(options.env)
            captured["config_dir_entries"] = (
                list(Path_iterdir(options.env["CLAUDE_CONFIG_DIR"]))
                if "CLAUDE_CONFIG_DIR" in options.env
                else None
            )

        async def connect(self):
            if stall:
                await asyncio.Event().wait()
            if connect_error is not None:
                raise connect_error

        async def get_server_info(self):
            return server_info

        async def disconnect(self):
            captured["disconnected"] += 1

    def Path_iterdir(raw):
        from pathlib import Path

        p = Path(raw)
        return p.iterdir() if p.is_dir() else []

    monkeypatch.setattr("kotoyose.model_discovery.ClaudeSDKClient", FakeClaudeClient)
    return captured


def _canned_claude_server_info():
    return {
        "models": [
            {
                "value": "default",
                "resolvedModel": "claude-sonnet-5-0",
                # 実物の CLI (2.1.323) と同じく default の表示名は "Default (recommended)"
                "displayName": "Default (recommended)",
                "description": "Sonnet 5.0 · Balanced",
                "supportsEffort": True,
                "supportedEffortLevels": ["low", "medium", "high"],
                "supportsFastMode": True,
            },
            {
                "value": "sonnet",
                "resolvedModel": "claude-sonnet-5-0",
                "displayName": "Sonnet 5.0",
                "description": "Balanced",
            },
            {
                "value": "opus",
                "resolvedModel": "claude-opus-5-5",
                "displayName": "Opus 5.5",
                "description": "Flagship",
                "supportsEffort": True,
                "supportedEffortLevels": ["low", "medium", "high", "xhigh", "max"],
                "supportsFastMode": True,
            },
            {
                "value": "claude-fable-5-1",
                "displayName": "Fable 5.1",
                "description": "Direct full ID",
                "supportedEffortLevels": ["high"],
                "supportsFastMode": False,
            },
            {
                "value": "haiku",
                "displayName": "Haiku alias without resolvedModel",
            },
        ]
    }


@pytest.mark.asyncio
async def test_claude_subscription_secret_connection_uses_sdk_initialize_and_strips_env(
    isolated_config, monkeypatch
):
    secret_token = "fake-setup-token-secret-xyz"
    inherited_api_key = "inherited-anthropic-api-key-should-not-leak"
    monkeypatch.setenv("ANTHROPIC_API_KEY", inherited_api_key)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://inherited.invalid")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "inherited-oauth-should-not-win")

    async def fake_resolve_secret(name):
        assert name == "claude-oauth-secret"
        return secret_token

    monkeypatch.setattr("kotoyose.model_discovery.resolve_secret_async", fake_resolve_secret)
    captured = _mock_claude_sdk(monkeypatch, server_info=_canned_claude_server_info())

    isolated_config.write_text(
        "connections:\n"
        "  claude-oauth:\n"
        "    provider: claude\n"
        "    kind: subscription\n"
        "    data_policy: no_training\n"
        "    secret: claude-oauth-secret\n"
        "models: {}\n"
    )
    reload_config()

    result = await discover_models("claude", connection="claude-oauth")

    assert captured["calls"] == 1
    assert captured["disconnected"] == 1
    opts = captured["options"][0]
    assert opts.setting_sources == []
    assert opts.tools == []
    assert opts.cwd
    env = captured["env_snapshot"]
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == secret_token
    assert env["ANTHROPIC_API_KEY"] == ""
    assert env["ANTHROPIC_BASE_URL"] == ""
    assert env["CLAUDE_CODE_USE_BEDROCK"] == ""
    assert env["CLAUDE_CONFIG_DIR"]
    assert captured["config_dir_entries"] == []

    assert result["source"] == "claude_cli_initialize"
    assert result["models"] == ["claude-sonnet-5-0", "claude-opus-5-5", "claude-fable-5-1"]
    assert result["warning"] is None
    assert result["error_code"] is None
    assert result["model_details"] == [
        {
            "id": "claude-sonnet-5-0",
            "displayName": "Sonnet 5.0",
            "label": "Sonnet 5.0",
            "description": "Balanced",
            "supportedEffortLevels": ["low", "medium", "high"],
            "supportsFastMode": True,
        },
        {
            "id": "claude-opus-5-5",
            "displayName": "Opus 5.5",
            "label": "Opus 5.5",
            "description": "Flagship",
            "supportedEffortLevels": ["low", "medium", "high", "xhigh", "max"],
            "supportsFastMode": True,
        },
        {
            "id": "claude-fable-5-1",
            "displayName": "Fable 5.1",
            "label": "Fable 5.1",
            "description": "Direct full ID",
            "supportedEffortLevels": ["high"],
            "supportsFastMode": False,
        },
    ]
    dumped = json.dumps(result)
    assert secret_token not in dumped
    assert inherited_api_key not in dumped


@pytest.mark.asyncio
async def test_claude_subscription_config_dir_connection_uses_config_dir_without_token_vars(
    isolated_config, monkeypatch, tmp_path
):
    cfg_dir = tmp_path / "claude-login-dir"
    cfg_dir.mkdir(mode=0o700)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "inherited-api-key")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "inherited-oauth")

    captured = _mock_claude_sdk(monkeypatch, server_info=_canned_claude_server_info())

    isolated_config.write_text(
        "connections:\n"
        "  claude-login:\n"
        "    provider: claude\n"
        "    kind: subscription\n"
        "    data_policy: no_training\n"
        f"    config_dir: {cfg_dir}\n"
        "models: {}\n"
    )
    reload_config()

    result = await discover_models("claude", connection="claude-login")

    assert captured["calls"] == 1
    assert captured["disconnected"] == 1
    env = captured["env_snapshot"]
    assert env["CLAUDE_CONFIG_DIR"] == str(cfg_dir)
    assert env.get("CLAUDE_CODE_OAUTH_TOKEN", "") == ""
    assert env.get("ANTHROPIC_API_KEY", "") == ""
    assert result["source"] == "claude_cli_initialize"
    assert result["models"] == ["claude-sonnet-5-0", "claude-opus-5-5", "claude-fable-5-1"]


@pytest.mark.asyncio
async def test_claude_subscription_missing_config_dir_falls_back_without_launching_sdk(
    isolated_config, monkeypatch, tmp_path
):
    missing_dir = tmp_path / "not-logged-in-dir"

    def unexpected_sdk(options):
        pytest.fail("missing config_dir must not launch ClaudeSDKClient")

    monkeypatch.setattr("kotoyose.model_discovery.ClaudeSDKClient", unexpected_sdk)

    isolated_config.write_text(
        "connections:\n"
        "  claude-login:\n"
        "    provider: claude\n"
        "    kind: subscription\n"
        "    data_policy: no_training\n"
        f"    config_dir: {missing_dir}\n"
        "models: {}\n"
    )
    reload_config()

    result = await discover_models("claude", connection="claude-login")
    assert not missing_dir.exists()
    assert result["source"] == "bundled_catalog"
    assert result["models"] == BUNDLED_CATALOGS["claude"]
    assert "Claude CLI にログインしていないため" in result["warning"]


@pytest.mark.asyncio
async def test_claude_subscription_timeout_falls_back_to_bundled_and_is_not_cached(
    isolated_config, monkeypatch
):
    monkeypatch.setenv("FAKE_CLAUDE_SUB_TOKEN", "fake-oauth-token")
    monkeypatch.setattr("kotoyose.model_discovery.CLAUDE_CLI_DISCOVERY_TIMEOUT_SECONDS", 0.01)
    captured = _mock_claude_sdk(monkeypatch, stall=True)

    isolated_config.write_text(
        "connections:\n"
        "  claude-oauth:\n"
        "    provider: claude\n"
        "    kind: subscription\n"
        "    data_policy: no_training\n"
        "    secret: env:FAKE_CLAUDE_SUB_TOKEN\n"
        "models: {}\n"
    )
    reload_config()

    first = await discover_models("claude", connection="claude-oauth")
    assert first["source"] == "bundled_catalog"
    assert first["models"] == BUNDLED_CATALOGS["claude"]
    assert "取得に失敗したため" in first["warning"]
    assert captured["calls"] == 1
    assert captured["disconnected"] == 1

    # Failures must not be cached: a subsequent recovery queries the SDK again.
    captured_ok = _mock_claude_sdk(monkeypatch, server_info=_canned_claude_server_info())
    second = await discover_models("claude", connection="claude-oauth")
    assert captured_ok["calls"] == 1
    assert second["source"] == "claude_cli_initialize"
    assert second["models"] == ["claude-sonnet-5-0", "claude-opus-5-5", "claude-fable-5-1"]


@pytest.mark.asyncio
async def test_claude_subscription_login_error_falls_back_with_login_reason_and_redacts_token(
    isolated_config, monkeypatch
):
    secret_token = "super-secret-oauth-token-123"
    monkeypatch.setenv("FAKE_CLAUDE_SUB_TOKEN", secret_token)
    logs = []
    monkeypatch.setattr("kotoyose.model_discovery.log.warning", lambda *a, **kw: logs.append((a, kw)))
    captured = _mock_claude_sdk(
        monkeypatch,
        connect_error=RuntimeError(f"Not logged in: invalid token {secret_token}"),
    )

    isolated_config.write_text(
        "connections:\n"
        "  claude-oauth:\n"
        "    provider: claude\n"
        "    kind: subscription\n"
        "    data_policy: no_training\n"
        "    secret: env:FAKE_CLAUDE_SUB_TOKEN\n"
        "models: {}\n"
    )
    reload_config()

    result = await discover_models("claude", connection="claude-oauth")
    assert captured["disconnected"] == 1
    assert result["source"] == "bundled_catalog"
    assert "Claude CLI にログインしていないため" in result["warning"]
    assert secret_token not in json.dumps([result, logs])


@pytest.mark.asyncio
async def test_connection_discovery_cache_hits_within_10_minutes_and_expires_after(
    isolated_config, monkeypatch
):
    monkeypatch.setenv("FAKE_CLAUDE_SUB_TOKEN", "fake-oauth-token")
    clock = {"now": 1000.0}
    monkeypatch.setattr("kotoyose.model_discovery.time.monotonic", lambda: clock["now"])
    captured = _mock_claude_sdk(monkeypatch, server_info=_canned_claude_server_info())

    isolated_config.write_text(
        "connections:\n"
        "  claude-oauth:\n"
        "    provider: claude\n"
        "    kind: subscription\n"
        "    data_policy: no_training\n"
        "    secret: env:FAKE_CLAUDE_SUB_TOKEN\n"
        "models: {}\n"
    )
    reload_config()

    r1 = await discover_models("claude", connection="claude-oauth")
    assert captured["calls"] == 1

    clock["now"] += 599.0
    r2 = await discover_models("claude", connection="claude-oauth")
    assert captured["calls"] == 1
    assert r2["models"] == r1["models"]

    clock["now"] += 2.0
    r3 = await discover_models("claude", connection="claude-oauth")
    assert captured["calls"] == 2
    assert r3["models"] == r1["models"]


@pytest.mark.asyncio
async def test_endpoint_connection_uses_connection_secret_without_leaking_token(
    isolated_config, monkeypatch
):
    secret_token = "endpoint-secret-token-987"

    async def fake_resolve_secret(name):
        assert name == "router-secret"
        return secret_token

    captured = {}

    async def fake_fetch_url(url, headers):
        captured["url"] = url
        captured["headers"] = headers
        return b'{"data":[{"id":"router-claude-1"}]}'

    from kotoyose.secret_store import SecretStore

    store = SecretStore(master_key=b"\x07" * 32)
    await store.set("router-secret", secret_token)
    monkeypatch.setattr("kotoyose.config.get_secret_store", lambda: store)
    monkeypatch.setattr("kotoyose.model_discovery.resolve_secret_async", fake_resolve_secret)
    monkeypatch.setattr("kotoyose.model_discovery._fetch_url", fake_fetch_url)

    isolated_config.write_text(
        "connections:\n"
        "  my-router:\n"
        "    provider: claude\n"
        "    kind: endpoint\n"
        "    data_policy: no_training\n"
        "    endpoint: https://router.example/v1\n"
        "    secret: router-secret\n"
        "models: {}\n"
    )
    reload_config()

    result = await discover_models("claude", connection="my-router")
    assert captured["url"] == "https://router.example/v1/models"
    assert captured["headers"]["Authorization"] == f"Bearer {secret_token}"
    assert captured["headers"]["x-api-key"] == secret_token
    assert result["models"] == ["router-claude-1"]
    assert result["source"] == "https://router.example/v1/models"
    assert secret_token not in json.dumps(result)


@pytest.mark.asyncio
async def test_api_key_connections_dispatch_by_provider(isolated_config, monkeypatch):
    claude_key = "fake-anthropic-api-key-111"
    cursor_key = "fake-cursor-api-key-222"
    monkeypatch.setenv("FAKE_ANTHROPIC_KEY", claude_key)
    monkeypatch.setenv("FAKE_CURSOR_KEY", cursor_key)

    captured_claude = {}

    async def fake_fetch_url(url, headers):
        captured_claude["url"] = url
        captured_claude["headers"] = headers
        return b'{"data":[{"id":"claude-opus-5"}]}'

    monkeypatch.setattr("kotoyose.model_discovery._fetch_url", fake_fetch_url)

    class FakeCursorClient:
        async def aclose(self):
            pass

    captured_cursor = {}

    async def fake_launch_bridge():
        return FakeCursorClient()

    async def fake_cursor_list(*, client, api_key=None):
        captured_cursor["api_key"] = api_key
        return [type("M", (), {"id": "cursor-model-x"})()]

    monkeypatch.setattr("kotoyose.model_discovery.AsyncClient.launch_bridge", fake_launch_bridge)
    monkeypatch.setattr("kotoyose.model_discovery.AsyncCursor.models.list", fake_cursor_list)

    isolated_config.write_text(
        "connections:\n"
        "  claude-api:\n"
        "    provider: claude\n"
        "    kind: api_key\n"
        "    data_policy: no_training\n"
        "    secret: env:FAKE_ANTHROPIC_KEY\n"
        "  codex-api:\n"
        "    provider: codex\n"
        "    kind: api_key\n"
        "    data_policy: no_training\n"
        "    secret: env:FAKE_ANTHROPIC_KEY\n"
        "  cursor-api:\n"
        "    provider: cursor\n"
        "    kind: api_key\n"
        "    data_policy: no_training\n"
        "    secret: env:FAKE_CURSOR_KEY\n"
        "models: {}\n"
    )
    reload_config()

    res_claude = await discover_models("claude", connection="claude-api")
    assert captured_claude["url"] == "https://api.anthropic.com/v1/models"
    assert captured_claude["headers"]["x-api-key"] == claude_key
    assert res_claude["models"] == ["claude-opus-5"]
    assert claude_key not in json.dumps(res_claude)

    res_codex = await discover_models("codex", connection="codex-api")
    assert res_codex["source"] == "bundled_catalog"
    assert res_codex["models"] == BUNDLED_CATALOGS["codex"]

    res_cursor = await discover_models("cursor", connection="cursor-api")
    assert captured_cursor["api_key"] == cursor_key
    assert res_cursor["models"] == ["cursor-model-x"]
    assert cursor_key not in json.dumps(res_cursor)


@pytest.mark.asyncio
async def test_codex_subscription_connection_uses_connection_codex_home(
    isolated_config, monkeypatch, tmp_path
):
    codex_home = tmp_path / "codex-work"
    codex_home.mkdir()
    captured = _mock_codex_catalog(monkeypatch)

    isolated_config.write_text(
        "connections:\n"
        "  codex-work:\n"
        "    provider: codex\n"
        "    kind: subscription\n"
        "    data_policy: no_training\n"
        f"    home: {codex_home}\n"
        "models: {}\n"
    )
    reload_config()

    from pathlib import Path

    from kotoyose import model_discovery
    from kotoyose.config import ModelPreset

    # 本番の機械には ModelPreset の既定 cwd が無く、app-server の起動が落ちていた。
    seen_cwd = {}
    original = model_discovery._discover_codex_models

    async def spy(preset=None):
        seen_cwd["cwd"] = preset.cwd if preset is not None else None
        seen_cwd["exists"] = preset is not None and Path(preset.cwd).is_dir()
        return await original(preset)

    monkeypatch.setattr(model_discovery, "_discover_codex_models", spy)

    result = await discover_models("codex", connection="codex-work")
    assert seen_cwd["exists"], "Codex discovery must run in an existing directory"
    assert seen_cwd["cwd"] != ModelPreset(provider="codex").cwd
    assert captured["config"].env == {"CODEX_HOME": str(codex_home)}
    assert result["models"] == ["provider-model"]
    assert result["source"] == "codex-sdk"


def test_provider_models_api_connection_validation_and_response(isolated_config, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_SUB_TOKEN", "fake-api-token-secret")
    _mock_claude_sdk(monkeypatch, server_info=_canned_claude_server_info())

    isolated_config.write_text(
        "connections:\n"
        "  claude-oauth:\n"
        "    provider: claude\n"
        "    kind: subscription\n"
        "    data_policy: no_training\n"
        "    secret: env:FAKE_CLAUDE_SUB_TOKEN\n"
        "  codex-main:\n"
        "    provider: codex\n"
        "    kind: subscription\n"
        "    data_policy: no_training\n"
        "models:\n"
        "  chat:\n"
        "    provider: claude\n"
        "    model: claude-sonnet-5\n"
    )
    reload_config()

    # Exclusive params -> 400
    assert client.get(
        "/admin/providers/claude/models",
        params={"connection": "claude-oauth", "endpoint_base_url": "https://x.test"},
    ).status_code == 400
    assert client.get(
        "/admin/providers/claude/models",
        params={"connection": "claude-oauth", "endpoint_api_key_env": "TOK"},
    ).status_code == 400
    assert client.get(
        "/admin/providers/claude/models",
        params={"connection": "claude-oauth", "model_id": "chat"},
    ).status_code == 400

    # Unknown connection -> 404
    assert client.get(
        "/admin/providers/claude/models",
        params={"connection": "does-not-exist"},
    ).status_code == 404

    # Provider mismatch -> 404
    assert client.get(
        "/admin/providers/claude/models",
        params={"connection": "codex-main"},
    ).status_code == 404

    # Valid connection -> 200, never leaks token
    ok = client.get(
        "/admin/providers/claude/models",
        params={"connection": "claude-oauth"},
    )
    assert ok.status_code == 200
    assert "fake-api-token-secret" not in ok.text
    body = ok.json()
    assert body["provider"] == "claude"
    assert body["source"] == "claude_cli_initialize"
    assert body["models"] == ["claude-sonnet-5-0", "claude-opus-5-5", "claude-fable-5-1"]
    assert len(body["model_details"]) == 3
