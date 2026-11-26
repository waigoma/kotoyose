"""
config.yml の読み込みと ClaudeAgentOptions 生成テスト。
"""
import json
import os
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from claude_agent_sdk.types import ToolsPreset

from kotoyose.config import (
    _build_auth_env,
    _load_yaml_config,
    build_options,
    _get_oauth_token,
    HarnessConfig,
    ModelPreset,
    PromptFileDefinition,
    AgentRoute,
    Connection,
    SkillDefinition,
    resolve_prompt_texts,
    save_config,
)


def test_route_definition_validates_provider_specific_fields():
    with pytest.raises(ValueError, match="driver"):
        AgentRoute(driver="unknown", model="example", connection="x")
    with pytest.raises(ValueError, match="model"):
        AgentRoute(driver="claude.agent-sdk", model="  ", connection="x")
    with pytest.raises(ValueError, match="model_provider"):
        AgentRoute(driver="claude.agent-sdk", model="example", connection="x", model_provider="proxy")
    with pytest.raises(ValueError, match="anthropic_base_url"):
        AgentRoute(driver="codex.app-server", model="example", connection="x", anthropic_base_url="https://proxy.example/v1")


def test_route_resolves_definition_and_preserves_inline_priority():
    config = HarnessConfig(
        connections={
            "codex-host": Connection(provider="codex", kind="subscription", data_policy="no_training"),
            "claude-router": Connection(provider="claude", kind="endpoint", data_policy="no_training",
                                        endpoint="https://shared-claude.example/v1", secret="env:SHARED_CLAUDE_TOKEN"),
        },
        routes={
            "codex-daily": {"driver": "codex.app-server", "connection": "codex-host", "model": "gpt-runtime",
                            "model_provider": {"id": "shared", "base_url": "https://shared.example/v1"}},
            "claude-daily": {"driver": "claude.agent-sdk", "connection": "claude-router",
                             "model": "claude-runtime"},
        },
        models={"codex-shared": {"route": "codex-daily"},
                "codex-inline": {"route": "codex-daily", "codex": {"model_provider": "inline"}},
                "claude-shared": {"route": "claude-daily"}},
    )
    shared_codex = config.preset_for("codex-shared")
    assert (shared_codex.provider, shared_codex.model) == ("codex", "gpt-runtime")
    assert shared_codex.codex["model_provider"] == {"id": "shared", "base_url": "https://shared.example/v1"}
    assert config.preset_for("codex-inline").codex["model_provider"] == "inline"
    shared_claude = config.preset_for("claude-shared")
    assert shared_claude.anthropic_base_url == "https://shared-claude.example/v1"
    assert shared_claude.anthropic_auth_token_env == "SHARED_CLAUDE_TOKEN"
    assert "model_provider" not in config.models["codex-shared"].codex
    for field in ("provider", "model", "anthropic_base_url", "anthropic_auth_token_env"):
        with pytest.raises(ValueError, match=field + " comes from route"):
            HarnessConfig(connections=config.connections, routes=config.routes,
                          models={"conflicting": {"route": "claude-daily", field: "conflicting"}})


def test_route_validation_and_unreferenced_preset_behavior():
    with pytest.raises(ValueError, match="不明な route"):
        HarnessConfig(models={"broken": {"route": "missing"}})
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        HarnessConfig(defaults={"route": "missing"})
    default_config = HarnessConfig(
        connections={"host": Connection(provider="antigravity", kind="subscription", data_policy="no_training")},
        routes={"daily": {"driver": "antigravity.sdk", "model": "gemini-4-hydrogen-high", "connection": "host"}},
        models={"daily-alias": {"route": "daily"}},
    )
    assert default_config.preset_for("daily-alias").provider == "antigravity"
    assert default_config.preset_for("daily-alias").model == "gemini-4-hydrogen-high"
    config = HarnessConfig(models={"direct": {"provider": "cursor", "model": "cursor-model"}})
    assert config.preset_for("direct") is config.models["direct"]
    config.models["direct"].route = "missing"
    with pytest.raises(ValueError, match="不明な route"):
        config.preset_for("direct")


def test_build_auth_env_prioritizes_runtime_preset_values():
    """Claude の preset endpoint / token env は Settings と標準環境変数より優先する。"""
    config = HarnessConfig(models={"any-model": {}}, settings={
        "anthropic_auth_token": "settings-token",
        "anthropic_base_url": "https://settings.example/v1",
    })
    preset = ModelPreset(
        anthropic_base_url="https://preset.example/v1",
        anthropic_auth_token_env="RUNTIME_CLAUDE_TOKEN",
    )
    with patch("kotoyose.config._config", config), patch.dict(
        os.environ,
        {
            "RUNTIME_CLAUDE_TOKEN": "runtime-token",
            "ANTHROPIC_AUTH_TOKEN": "standard-token",
        },
        clear=False,
    ):
        assert _build_auth_env(preset) == {
            "ANTHROPIC_API_KEY": "runtime-token",
            "ANTHROPIC_BASE_URL": "https://preset.example/v1",
        }

    with patch("kotoyose.config._config", config), patch.dict(
        os.environ, {"RUNTIME_CLAUDE_TOKEN": ""}, clear=False
    ):
        del os.environ["RUNTIME_CLAUDE_TOKEN"]
        with pytest.raises(ValueError, match="RUNTIME_CLAUDE_TOKEN"):
            _build_auth_env(preset)


def test_defaults_when_no_config(tmp_path):
    """設定なしでは alias を公開せず、コード側の preset 既定値は保持する。"""
    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(tmp_path / "nonexistent.yml")}):
        config = _load_yaml_config()
    # bare のデフォルトは True (--bare: CLAUDE.md/MCP/skills/agents/commands の自動検出を無効化)
    assert config.models == {}
    preset = ModelPreset()
    assert preset.bare is True
    assert preset.deny == []
    assert preset.prompt_file_refs == []
    assert preset.provider == "claude"


def test_settings_roundtrip_and_resolution_priority(tmp_path):
    """Settings の値は YAML 保存後も保たれ、環境変数より優先される。"""
    yaml_file = tmp_path / "config.yml"
    config = HarnessConfig(models={"any-model": {}}, settings={"anthropic_auth_token": "settings-anthropic-key"})
    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        save_config(config)
        reloaded = _load_yaml_config()
    assert reloaded.settings.anthropic_auth_token == "settings-anthropic-key"
    assert reloaded.resolve_setting("anthropic_auth_token", "ANTHROPIC_AUTH_TOKEN") == "settings-anthropic-key"
    with patch.dict(os.environ, {"ANTHROPIC_AUTH_TOKEN": "env-anthropic-key"}):
        assert reloaded.resolve_setting("anthropic_auth_token", "ANTHROPIC_AUTH_TOKEN") == "settings-anthropic-key"


def test_build_auth_env_prioritizes_settings_then_env_then_oauth():
    """Claude 認証は Settings、環境変数、OAuth の順で解決する。"""
    settings_config = HarnessConfig(models={"any-model": {}}, settings={
        "anthropic_auth_token": "settings-token",
        "anthropic_base_url": "https://settings.example/v1",
    })
    with patch("kotoyose.config._config", settings_config), patch(
        "kotoyose.config._get_oauth_token", return_value="oauth-token"
    ), patch.dict(os.environ, {
        "ANTHROPIC_AUTH_TOKEN": "env-token",
        "ANTHROPIC_API_KEY": "env-api-key",
        "ANTHROPIC_BASE_URL": "https://env.example/v1",
    }, clear=False):
        assert build_options("any-model").env == {
            "ANTHROPIC_API_KEY": "settings-token",
            "ANTHROPIC_BASE_URL": "https://settings.example/v1",
        }

    env_config = HarnessConfig(models={"any-model": {}})
    with patch("kotoyose.config._config", env_config), patch(
        "kotoyose.config._get_oauth_token", return_value="oauth-token"
    ), patch.dict(os.environ, {"ANTHROPIC_AUTH_TOKEN": "env-token", "ANTHROPIC_API_KEY": "env-api-key"}, clear=False):
        assert build_options("any-model").env["ANTHROPIC_API_KEY"] == "env-token"
    with patch("kotoyose.config._config", env_config), patch(
        "kotoyose.config._get_oauth_token", return_value="oauth-token"
    ), patch.dict(os.environ, {"ANTHROPIC_AUTH_TOKEN": "", "ANTHROPIC_API_KEY": ""}, clear=False):
        assert build_options("any-model").env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-token"


def test_model_preset_override(tmp_path):
    """models.kotoyose-chat が定義されていれば使われる。"""
    yaml_content = textwrap.dedent("""
        models:
          kotoyose-chat:
            bare: false
            deny:
              - "Bash(rm -rf*)"
    """)
    yaml_file = tmp_path / "config.yml"
    yaml_file.write_text(yaml_content)
    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        config = _load_yaml_config()
    preset = config.preset_for("kotoyose-chat")
    assert preset.bare is False
    assert "Bash(rm -rf*)" in preset.deny


def test_unknown_model_has_no_preset():
    with pytest.raises(KeyError, match="unknown-model"):
        HarnessConfig(models={"known": {}}).preset_for("unknown-model")


def test_cwd_tilde_expanded(tmp_path):
    """cwd の ~ が展開される。"""
    yaml_content = textwrap.dedent("""
        models:
          sample:
            cwd: "~/my-workspace"
    """)
    yaml_file = tmp_path / "config.yml"
    yaml_file.write_text(yaml_content)
    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        config = _load_yaml_config()
    preset = config.preset_for("sample")
    assert not preset.cwd.startswith("~")
    assert preset.cwd.startswith("/")


def test_model_field_passed_to_options(tmp_path):
    """model フィールドが ClaudeAgentOptions.model に渡される。"""
    yaml_content = textwrap.dedent("""
        models:
          test-model:
            model: claude-sonnet-4-5-20241022
    """)
    yaml_file = tmp_path / "config.yml"
    yaml_file.write_text(yaml_content)
    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        config = _load_yaml_config()
    with patch("kotoyose.config._config", config):
        opts = build_options("test-model")
    assert opts.model == "claude-sonnet-4-5-20241022"


def test_model_field_default_none(tmp_path):
    """model 未指定時は None が渡される (SDK デフォルト)。"""
    yaml_content = textwrap.dedent("""
        models:
          test-model:
            bare: true
    """)
    yaml_file = tmp_path / "config.yml"
    yaml_file.write_text(yaml_content)
    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        config = _load_yaml_config()
    with patch("kotoyose.config._config", config):
        opts = build_options("test-model")
    assert opts.model is None


def test_deny_list_passed_to_disallowed_tools(tmp_path):
    """deny リストが ClaudeAgentOptions.disallowed_tools に渡される。"""
    yaml_content = textwrap.dedent("""
        models:
          test-model:
            deny:
              - "Bash(rm -rf*)"
              - "Write(/etc/*)"
    """)
    yaml_file = tmp_path / "config.yml"
    yaml_file.write_text(yaml_content)
    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        config = _load_yaml_config()
    with patch("kotoyose.config._config", config):
        opts = build_options("test-model")
    assert opts.disallowed_tools == ["Bash(rm -rf*)", "Write(/etc/*)", "Task", "Agent"]


def test_permission_mode_always_bypass(tmp_path):
    """permission_mode は常に bypassPermissions。"""
    yaml_file = tmp_path / "config.yml"
    yaml_file.write_text("models:\n  any-model: {}\n")
    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        config = _load_yaml_config()
    with patch("kotoyose.config._config", config):
        opts = build_options("any-model")
    assert opts.permission_mode == "bypassPermissions"


def test_bare_false_omits_flag(tmp_path):
    """bare: false のモデルは --bare を extra_args に含まない。"""
    yaml_content = textwrap.dedent("""
        models:
          kotoyose-dev:
            bare: false
    """)
    yaml_file = tmp_path / "config.yml"
    yaml_file.write_text(yaml_content)
    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        config = _load_yaml_config()
    with patch("kotoyose.config._config", config):
        opts = build_options("kotoyose-dev")
    # bare: false → --bare を渡さない → 個人設定 (CLAUDE.md/MCP/skills) が有効
    assert "bare" not in (opts.extra_args or {})


def test_bare_true_adds_flag(tmp_path):
    """bare: true (デフォルト) は --bare を extra_args に設定し、CLAUDE.md 自動検出を無効化する。"""
    yaml_file = tmp_path / "config.yml"
    yaml_file.write_text("models:\n  any-model:\n    cwd: ~/kotoyose-workspace\n")
    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        config = _load_yaml_config()
    # 認証環境変数とcredentialsファイルをモック (env 注入のテスト対象外)
    with patch("kotoyose.config._config", config), \
         patch("kotoyose.config._get_oauth_token", return_value=None), \
         patch.dict(os.environ, {"ANTHROPIC_AUTH_TOKEN": "", "ANTHROPIC_API_KEY": ""}, clear=False):
        opts = build_options("any-model")
    # bare: true (default) → extra_args に bare: None が入る (= --bare フラグ)
    assert opts.extra_args == {"bare": None}


# ── 認証 env 注入テスト ─────────────────────────────────────────────


def test_auth_token_injected_from_anthropic_auth_token(tmp_path):
    """ANTHROPIC_AUTH_TOKEN が設定されていれば ANTHROPIC_API_KEY として注入される。"""
    yaml_file = tmp_path / "config.yml"
    yaml_file.write_text("models:\n  any-model: {}\n")
    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        config = _load_yaml_config()
    with patch("kotoyose.config._config", config), \
         patch.dict(os.environ, {"ANTHROPIC_AUTH_TOKEN": "tok-auth", "ANTHROPIC_API_KEY": ""}):
        opts = build_options("any-model")
    assert opts.env.get("ANTHROPIC_API_KEY") == "tok-auth"


def test_auth_token_injected_from_anthropic_api_key(tmp_path):
    """ANTHROPIC_API_KEY が設定されていればそのまま注入される。"""
    yaml_file = tmp_path / "config.yml"
    yaml_file.write_text("models:\n  any-model: {}\n")
    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        config = _load_yaml_config()
    with patch("kotoyose.config._config", config), \
         patch.dict(os.environ, {"ANTHROPIC_AUTH_TOKEN": "", "ANTHROPIC_API_KEY": "sk-ant-xxx"}):
        opts = build_options("any-model")
    assert opts.env.get("ANTHROPIC_API_KEY") == "sk-ant-xxx"


def test_auth_token_from_oauth_credentials(tmp_path):
    """環境変数未設定時は credentials.json の OAuth トークンが注入される。"""
    yaml_file = tmp_path / "config.yml"
    yaml_file.write_text("models:\n  any-model: {}\n")
    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        config = _load_yaml_config()
    with patch("kotoyose.config._config", config), \
         patch("kotoyose.config._get_oauth_token", return_value="oauth-access-token"), \
         patch.dict(os.environ, {"ANTHROPIC_AUTH_TOKEN": "", "ANTHROPIC_API_KEY": ""}):
        opts = build_options("any-model")
    assert opts.env.get("CLAUDE_CODE_OAUTH_TOKEN") == "oauth-access-token"


def test_auth_env_injected_even_when_bare_false(tmp_path):
    """bare: false の preset でも認証・エンドポイント env が注入される (回帰ガード)。

    taskhub の bare:false 移行時、_build_auth_env() が if p.bare の内側にあったため
    ANTHROPIC_BASE_URL / 認証トークンが欠落するバグがあった。bare の有無に依らず注入
    されることを固定する。
    """
    yaml_file = tmp_path / "config.yml"
    yaml_file.write_text(textwrap.dedent("""
        settings:
          anthropic_base_url: https://proxy.example/v1
        models:
          taskhub-coding:
            bare: false
            setting_sources: []
            require_isolation: true
    """))
    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        config = _load_yaml_config()
    with patch("kotoyose.config._config", config), \
         patch("kotoyose.config._get_oauth_token", return_value=None), \
         patch.dict(os.environ, {
             "ANTHROPIC_AUTH_TOKEN": "tok-bare-false",
             "ANTHROPIC_API_KEY": "",
             "ANTHROPIC_BASE_URL": "https://proxy.example/v1",
         }):
        opts = build_options("taskhub-coding")
    assert opts.env.get("ANTHROPIC_API_KEY") == "tok-bare-false"
    assert opts.env.get("ANTHROPIC_BASE_URL") == "https://proxy.example/v1"
    # bare:false なので --bare フラグは付かないまま
    assert "bare" not in (opts.extra_args or {})


def test_host_anthropic_base_url_is_not_used_as_config_input(tmp_path):
    """host の ANTHROPIC_BASE_URL は管理対象 endpoint の代わりに使用しない。"""
    yaml_file = tmp_path / "config.yml"
    yaml_file.write_text("models:\n  any-model: {}\n")
    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        config = _load_yaml_config()
    with patch("kotoyose.config._config", config), \
         patch("kotoyose.config._get_oauth_token", return_value=None), \
         patch.dict(os.environ, {
             "ANTHROPIC_BASE_URL": "https://proxy.example.com",
             "ANTHROPIC_AUTH_TOKEN": "",
             "ANTHROPIC_API_KEY": "",
        }):
        opts = build_options("any-model")
        assert "ANTHROPIC_BASE_URL" not in os.environ
    assert "ANTHROPIC_BASE_URL" not in opts.env


def test_bare_false_still_injects_auth_env(tmp_path):
    """bare: false のモデルでも認証・エンドポイント env は注入される。

    以前は if p.bare の内側で env 注入していたため bare: false だと env={} になる
    バグがあった。bare はあくまで
    --bare フラグ (設定自動検出の無効化) の制御であり、認証 env 注入とは独立した
    関心事のため、bare の値に関わらず注入されるべきが正しい挙動。
    """
    yaml_content = textwrap.dedent("""
        models:
          kotoyose-dev:
            bare: false
    """)
    yaml_file = tmp_path / "config.yml"
    yaml_file.write_text(yaml_content)
    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        config = _load_yaml_config()
    with patch("kotoyose.config._config", config), \
         patch("kotoyose.config._get_oauth_token", return_value=None), \
         patch.dict(os.environ, {"ANTHROPIC_AUTH_TOKEN": "tok-kotoyose-dev", "ANTHROPIC_API_KEY": ""}):
        opts = build_options("kotoyose-dev")
    assert opts.env.get("ANTHROPIC_API_KEY") == "tok-kotoyose-dev"
    assert "bare" not in (opts.extra_args or {})


# ── _get_oauth_token テスト ────────────────────────────────────────


def test_get_oauth_token_from_secret_store(monkeypatch):
    """Claude subscription connection の secret から OAuth token を解決する。"""
    import asyncio
    from kotoyose.config import AgentRoute, ClaudeSubscriptionAuthError, Connection
    from kotoyose.secret_store import SecretStore, set_secret_store

    for key in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    store = SecretStore(master_key=b"\x01" * 32)
    asyncio.run(store.set("claude-oauth", "oauth-secret-value"))
    set_secret_store(store)
    cfg = HarnessConfig(
        connections={"claude-oauth": Connection(provider="claude", kind="subscription", data_policy="no_training", secret="claude-oauth")},
        routes={"r": AgentRoute(driver="claude.agent-sdk", model="claude-sonnet-5", connection="claude-oauth")},
        models={"chat": ModelPreset(route="r", bare=False)},
    )
    try:
        with patch("kotoyose.config._config", cfg):
            assert _get_oauth_token() == "oauth-secret-value"
            assert _build_auth_env(cfg.preset_for("chat")) == {"CLAUDE_CODE_OAUTH_TOKEN": "oauth-secret-value"}
            asyncio.run(store.delete("claude-oauth"))
            with pytest.raises(ClaudeSubscriptionAuthError, match="claude setup-token"):
                _build_auth_env(cfg.preset_for("chat"))
    finally:
        set_secret_store(None)


def test_get_oauth_token_none_when_no_subscription_connection():
    """Claude subscription connection が無ければ None を返す。"""
    with patch("kotoyose.config._config", HarnessConfig()):
        assert _get_oauth_token() is None


def test_get_oauth_token_env_override_wins_over_secret_store(monkeypatch):
    """明示的な CLAUDE_CODE_OAUTH_TOKEN 環境変数が secret store より優先される。"""
    from kotoyose.config import AgentRoute, Connection
    from kotoyose.secret_store import SecretStore, set_secret_store

    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "env-oauth-override")
    set_secret_store(SecretStore(None))
    cfg = HarnessConfig(
        connections={"claude-oauth": Connection(provider="claude", kind="subscription", data_policy="no_training", secret="missing-secret")},
        routes={"r": AgentRoute(driver="claude.agent-sdk", model="claude-sonnet-5", connection="claude-oauth")},
        models={"chat": ModelPreset(route="r", bare=False)},
    )
    try:
        with patch("kotoyose.config._config", cfg):
            assert _build_auth_env(cfg.preset_for("chat")) == {"CLAUDE_CODE_OAUTH_TOKEN": "env-oauth-override"}
    finally:
        set_secret_store(None)




def test_system_prompt_preset_always_set():
    """prompt file が空でも SystemPromptPreset が設定される。"""
    config = HarnessConfig(models={"any-model": {}})
    with patch("kotoyose.config._config", config):
        opts = build_options("any-model")
    assert opts.system_prompt == {"type": "preset", "preset": "claude_code"}




def test_resolve_prompt_texts_reads_files_in_order(tmp_path):
    """provider 非依存の resolver: 指定順に本文を読み込む。"""
    a = tmp_path / "a.md"
    a.write_text("A", encoding="utf-8")
    b = tmp_path / "b.md"
    b.write_text("B", encoding="utf-8")
    assert resolve_prompt_texts([str(a), str(b)]) == ["A", "B"]


def test_resolve_prompt_texts_skips_missing_files():
    """_build_system_prompt と同じ寛容さ: 存在しない file は黙ってスキップする。"""
    assert resolve_prompt_texts(["/nonexistent/path/file.md"]) == []


def test_resolve_prompt_texts_raises_on_non_utf8(tmp_path):
    """UTF-8 でない file は ValueError で loud に失敗する。"""
    bad = tmp_path / "bad.md"
    bad.write_bytes(b"\xff\xfe\x00\x01")
    with pytest.raises(ValueError, match="UTF-8"):
        resolve_prompt_texts([str(bad)])






# ── MCP サーバー設定テスト ─────────────────────────────────────────────


def test_mcp_servers_default_empty():
    """mcp_servers のデフォルトは空 dict。"""
    preset = ModelPreset()
    assert preset.mcp_servers == {}


def test_mcp_servers_http_config(tmp_path):
    """HTTP 型の MCP サーバーが config から読み込まれる。"""
    yaml_content = textwrap.dedent("""
        models:
          test-model:
            mcp_servers:
              mem0:
                type: http
                url: "http://localhost:3001/mcp"
    """)
    yaml_file = tmp_path / "config.yml"
    yaml_file.write_text(yaml_content)
    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        config = _load_yaml_config()
    preset = config.preset_for("test-model")
    assert "mem0" in preset.mcp_servers
    assert preset.mcp_servers["mem0"]["type"] == "http"
    assert preset.mcp_servers["mem0"]["url"] == "http://localhost:3001/mcp"


def test_mcp_servers_stdio_config(tmp_path):
    """stdio 型の MCP サーバーが config から読み込まれる。"""
    yaml_content = textwrap.dedent("""
        models:
          test-model:
            mcp_servers:
              local-tool:
                command: "npx"
                args: ["-y", "some-mcp-server"]
                env:
                  API_KEY: "test-key"
    """)
    yaml_file = tmp_path / "config.yml"
    yaml_file.write_text(yaml_content)
    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        config = _load_yaml_config()
    preset = config.preset_for("test-model")
    assert preset.mcp_servers["local-tool"]["command"] == "npx"
    assert preset.mcp_servers["local-tool"]["args"] == ["-y", "some-mcp-server"]
    assert preset.mcp_servers["local-tool"]["env"]["API_KEY"] == "test-key"


def test_mcp_servers_passed_to_options(tmp_path):
    """mcp_servers が ClaudeAgentOptions に正しく渡される。"""
    yaml_content = textwrap.dedent("""
        models:
          test-model:
            mcp_servers:
              mem0:
                type: http
                url: "http://localhost:3001/mcp"
    """)
    yaml_file = tmp_path / "config.yml"
    yaml_file.write_text(yaml_content)
    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        config = _load_yaml_config()
    with patch("kotoyose.config._config", config):
        opts = build_options("test-model")
    assert opts.mcp_servers == {"mem0": {"type": "http", "url": "http://localhost:3001/mcp"}}


def test_mcp_servers_empty_when_not_configured(tmp_path):
    """mcp_servers 未設定のモデルは空 dict が渡される。"""
    yaml_file = tmp_path / "config.yml"
    yaml_file.write_text("models:\n  any-model: {}\n")
    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        config = _load_yaml_config()
    with patch("kotoyose.config._config", config), \
         patch("kotoyose.config._get_oauth_token", return_value=None), \
         patch.dict(os.environ, {"ANTHROPIC_AUTH_TOKEN": "", "ANTHROPIC_API_KEY": ""}, clear=False):
        opts = build_options("any-model")
    assert opts.mcp_servers == {}


# ── memory_enabled / memory_force テスト ───────────────────────────────


def test_memory_enabled_default_false():
    """memory_enabled の既定値は False (明示 opt-in)。"""
    preset = ModelPreset()
    assert preset.memory_enabled is False


def test_memory_force_default_false():
    """memory_force の既定値は False (ヘッダ必須)。"""
    preset = ModelPreset()
    assert preset.memory_force is False


def test_memory_enabled_loaded_from_yaml(tmp_path):
    """memory_enabled / memory_force が YAML から読み込まれる。"""
    yaml_content = textwrap.dedent("""
        models:
          test-model:
            memory_enabled: true
            memory_force: true
    """)
    yaml_file = tmp_path / "config.yml"
    yaml_file.write_text(yaml_content)
    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        config = _load_yaml_config()
    preset = config.preset_for("test-model")
    assert preset.memory_enabled is True
    assert preset.memory_force is True


def test_memory_fields_default_when_omitted_in_yaml(tmp_path):
    """YAML で省略された preset は memory_enabled=False, memory_force=False。"""
    yaml_content = textwrap.dedent("""
        models:
          test-model:
            bare: true
    """)
    yaml_file = tmp_path / "config.yml"
    yaml_file.write_text(yaml_content)
    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        config = _load_yaml_config()
    preset = config.preset_for("test-model")
    assert preset.memory_enabled is False
    assert preset.memory_force is False


# ── thinking / effort 設定 ───────────────────────────────────────


def test_thinking_default_none():
    """ModelPreset の thinking フィールドはデフォルト None。"""
    preset = ModelPreset()
    assert preset.thinking is None
    assert preset.thinking_budget_tokens is None
    assert preset.effort is None


def test_thinking_disabled_loaded_from_yaml(tmp_path):
    """thinking: disabled が YAML から読み込まれる (haiku 用途)。"""
    yaml_content = textwrap.dedent("""
        models:
          haiku-model:
            model: claude-haiku-4-5
            thinking: disabled
    """)
    yaml_file = tmp_path / "config.yml"
    yaml_file.write_text(yaml_content)
    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        config = _load_yaml_config()
    preset = config.preset_for("haiku-model")
    assert preset.thinking == "disabled"


def test_thinking_adaptive_loaded_from_yaml(tmp_path):
    """thinking: adaptive + effort: high が YAML から読み込まれる。"""
    yaml_content = textwrap.dedent("""
        models:
          opus-model:
            model: claude-opus-4-7
            thinking: adaptive
            effort: high
    """)
    yaml_file = tmp_path / "config.yml"
    yaml_file.write_text(yaml_content)
    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        config = _load_yaml_config()
    preset = config.preset_for("opus-model")
    assert preset.thinking == "adaptive"
    assert preset.effort == "high"


def test_thinking_enabled_requires_budget():
    """thinking='enabled' のとき thinking_budget_tokens 必須。"""
    with pytest.raises(ValueError, match="thinking_budget_tokens"):
        ModelPreset(thinking="enabled")


def test_thinking_enabled_with_budget_ok():
    """thinking='enabled' + budget があれば作成可能。"""
    preset = ModelPreset(thinking="enabled", thinking_budget_tokens=8000)
    assert preset.thinking == "enabled"
    assert preset.thinking_budget_tokens == 8000


def test_effort_xhigh_accepted():
    """effort='xhigh' (Opus 4.7 専用) を受け入れる。"""
    preset = ModelPreset(effort="xhigh")
    assert preset.effort == "xhigh"


def test_effort_invalid_rejected():
    """無効な effort 値はバリデーションエラー。"""
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        ModelPreset(effort="ultra")


def test_thinking_disabled_passed_to_options(tmp_path):
    """thinking: disabled が ClaudeAgentOptions.thinking に dict 形式で渡る。"""
    yaml_content = textwrap.dedent("""
        models:
          haiku-model:
            model: claude-haiku-4-5
            thinking: disabled
    """)
    yaml_file = tmp_path / "config.yml"
    yaml_file.write_text(yaml_content)
    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        from kotoyose import config as config_module
        config_module._config = config_module._load_yaml_config()
        options = build_options("haiku-model")
    assert options.thinking == {"type": "disabled"}


def test_thinking_adaptive_passed_to_options(tmp_path):
    """thinking: adaptive + effort が ClaudeAgentOptions に渡る。"""
    yaml_content = textwrap.dedent("""
        models:
          opus-model:
            model: claude-opus-4-7
            thinking: adaptive
            effort: xhigh
    """)
    yaml_file = tmp_path / "config.yml"
    yaml_file.write_text(yaml_content)
    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        from kotoyose import config as config_module
        config_module._config = config_module._load_yaml_config()
        options = build_options("opus-model")
    assert options.thinking == {"type": "adaptive"}
    assert options.effort == "xhigh"


def test_thinking_enabled_passed_to_options(tmp_path):
    """thinking: enabled + budget が enabled dict として渡る。"""
    yaml_content = textwrap.dedent("""
        models:
          legacy-model:
            model: claude-sonnet-4-5
            thinking: enabled
            thinking_budget_tokens: 8192
    """)
    yaml_file = tmp_path / "config.yml"
    yaml_file.write_text(yaml_content)
    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        from kotoyose import config as config_module
        config_module._config = config_module._load_yaml_config()
        options = build_options("legacy-model")
    assert options.thinking == {"type": "enabled", "budget_tokens": 8192}


def test_thinking_none_omits_options(tmp_path):
    """thinking 未指定なら ClaudeAgentOptions.thinking は None (SDK デフォルト)。"""
    yaml_content = textwrap.dedent("""
        models:
          plain-model:
            model: claude-sonnet-5
    """)
    yaml_file = tmp_path / "config.yml"
    yaml_file.write_text(yaml_content)
    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        from kotoyose import config as config_module
        config_module._config = config_module._load_yaml_config()
        options = build_options("plain-model")
    assert options.thinking is None
    assert options.effort is None


# ── MCP server schema 検証 ─────────────────────────────────────────────


def test_mcp_servers_accepts_http_with_type_and_url():
    """type:http + url がそろっていれば valid。"""
    p = ModelPreset(
        mcp_servers={"mem0": {"type": "http", "url": "http://example/mcp"}}
    )
    assert p.mcp_servers["mem0"]["type"] == "http"


def test_mcp_servers_accepts_sse_with_type_and_url():
    """type:sse も同様に valid。"""
    p = ModelPreset(
        mcp_servers={"sse-srv": {"type": "sse", "url": "http://example/sse"}}
    )
    assert p.mcp_servers["sse-srv"]["type"] == "sse"


def test_mcp_servers_accepts_stdio_with_command():
    """type:stdio + command が valid (type 省略時の stdio も OK)。"""
    p = ModelPreset(
        mcp_servers={
            "explicit": {"type": "stdio", "command": "npx"},
            "implicit": {"command": "/bin/cmd"},
        }
    )
    assert p.mcp_servers["explicit"]["command"] == "npx"


def test_mcp_servers_rejects_url_without_type():
    """url 指定 + type 未指定は schema 違反として ValidationError。"""
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as exc:
        ModelPreset(
            mcp_servers={"broken": {"url": "http://example/mcp"}}
        )
    assert "type は 'http' または 'sse' を明示" in str(exc.value)


def test_mcp_servers_rejects_invalid_type():
    """type に 'http' / 'sse' / 'stdio' 以外の値は拒否。"""
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as exc:
        ModelPreset(mcp_servers={"weird": {"type": "websocket", "url": "ws://x"}})
    assert "type は" in str(exc.value)


def test_mcp_servers_rejects_http_without_url():
    """type:http で url が無いのは拒否。"""
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as exc:
        ModelPreset(mcp_servers={"http-no-url": {"type": "http"}})
    assert "url が必須" in str(exc.value)


def test_mcp_servers_rejects_stdio_without_command():
    """type:stdio で command が無いのは拒否。"""
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as exc:
        ModelPreset(mcp_servers={"stdio-no-cmd": {"type": "stdio"}})
    assert "command が必須" in str(exc.value)


def test_mcp_servers_empty_dict_passes():
    """空 dict は valid。"""
    p = ModelPreset(mcp_servers={})
    assert p.mcp_servers == {}




# ── memory_source override ─────────────────────────────────────────────
# memory_source は任意文字列 (Literal にせず memory-service 側の判定に委ねる)。
# ハーネスは中継地点なので、契約を二重化しないことで memory-service の VALID_SOURCES
# 拡張に追従する必要がなくなる。値の妥当性は memory-service が 422 で返し warn ログに残る。


def test_memory_source_default_is_none():
    p = ModelPreset()
    assert p.memory_source is None


def test_memory_source_accepts_arbitrary_string():
    """memory_source は任意文字列を受容する。"""
    for src in ("discord", "openwebui", "n8n", "manual", "slack", "custom-tag"):
        p = ModelPreset(memory_source=src)
        assert p.memory_source == src


# ── taskhub-task-agent preset ─────────────────────────────────────────────


def test_task_hub_task_agent_preset(tmp_path):
    """taskhub-task-agent preset が正しく読み込まれる。"""
    yaml_file = tmp_path / "config.yml"
    yaml_file.write_text(textwrap.dedent("""
        models:
          taskhub-task-agent:
            bare: true
            cwd: ~/kotoyose/taskhub-workspace
            model: cc/claude-sonnet-5
            allow_cwd_override: false
            mcp_servers:
              ai-task-hub:
                type: http
                url: http://localhost:8200/mcp
    """))
    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        config = _load_yaml_config()
    preset = config.preset_for("taskhub-task-agent")
    assert preset.model == "cc/claude-sonnet-5"
    assert "ai-task-hub" in preset.mcp_servers
    assert preset.allow_cwd_override is False
    assert preset.bare is True


def test_taskhub_bare_false_requires_empty_setting_sources(tmp_path):
    """require_isolation: true の preset は bare:false のとき setting_sources: [] だけを許可する。

    判定は preset 名ではなく require_isolation フラグで行われる (ModelPreset レベルの
    不変条件) ため、preset 名は taskhub-coding である必然性はない。
    """
    from pydantic import ValidationError

    yaml_file = tmp_path / "config.yml"
    yaml_file.write_text(textwrap.dedent("""
        models:
          taskhub-coding:
            require_isolation: true
            bare: false
            setting_sources: null
            allow_cwd_override: true
            cwd_allowed_roots:
              - ~/kotoyose-workspace
    """))
    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        with pytest.raises(ValidationError, match="setting_sources: \\[\\]"):
            _load_yaml_config()


def test_taskhub_bare_false_rejects_omitted_setting_sources(tmp_path):
    """setting_sources 省略は None と同義なので require_isolation:true では拒否する。"""
    from pydantic import ValidationError

    yaml_file = tmp_path / "config.yml"
    yaml_file.write_text(textwrap.dedent("""
        models:
          taskhub-task-agent:
            require_isolation: true
            bare: false
    """))
    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        with pytest.raises(ValidationError, match="setting_sources: \\[\\]"):
            _load_yaml_config()


def test_taskhub_bare_false_allows_empty_setting_sources(tmp_path):
    """setting_sources: [] 明示なら require_isolation:true + bare:false を受け入れる。"""
    yaml_file = tmp_path / "config.yml"
    yaml_file.write_text(textwrap.dedent("""
        models:
          taskhub-task-agent:
            require_isolation: true
            bare: false
            setting_sources: []
    """))
    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        config = _load_yaml_config()
    preset = config.preset_for("taskhub-task-agent")
    assert preset.bare is False
    assert preset.setting_sources == []


def test_non_taskhub_bare_false_can_keep_existing_setting_sources():
    """require_isolation を付けない (既定 False) preset はこのガードの対象外。"""
    config = HarnessConfig(models={
        "kotoyose-dev": ModelPreset(bare=False, setting_sources=None),
    })
    assert config.preset_for("kotoyose-dev").setting_sources is None


def test_require_isolation_bare_true_is_exempt():
    """require_isolation: true でも bare: true なら setting_sources: null を許容する。

    --bare は自動検出そのものを無効化するため、setting_sources の値に関わらず
    project/user 設定は読み込まれない。
    """
    preset = ModelPreset(require_isolation=True, bare=True, setting_sources=None)
    assert preset.bare is True
    assert preset.setting_sources is None


def test_save_config_revalidates_isolation_invariant(tmp_path, monkeypatch):
    """admin 経路 (model_copy + dict 代入) を模し、save_config の関所が発火することを確認する。

    ModelPreset の @model_validator は属性への直接代入では再実行されないため、
    admin の update_model 的な経路 (model_copy(deep=True) → dict 代入 → save_config) を
    経由すると、一度妥当だった preset を不変条件違反な状態に改変できてしまう。
    save_config 側の再検証がその永続化を阻止することを確認する。
    """
    from kotoyose.config import save_config

    yaml_file = tmp_path / "config.yml"
    monkeypatch.setenv("KOTOYOSE_CONFIG", str(yaml_file))

    cfg = HarnessConfig(models={
        "taskhub-coding": ModelPreset(
            require_isolation=True,
            bare=False,
            setting_sources=[],
        ),
    })

    # admin API 経路の模倣: model_copy + 属性直接代入 (バリデータは再実行されない)
    broken_cfg = cfg.model_copy(deep=True)
    broken_cfg.models["taskhub-coding"].setting_sources = None

    from pydantic import ValidationError
    with pytest.raises(ValidationError, match="setting_sources: \\[\\]"):
        save_config(broken_cfg)

    # 不正な内容がファイルに書き込まれていないこと (存在しない、または空のまま)
    assert not yaml_file.exists()


# ── plugins / skills フィールド ─────────────────────────────────────────


def _make_bundle(base, name="example-bundle", with_manifest=True):
    """テスト用の最小プラグイン束を作る。"""
    bundle = base / name
    (bundle / "skills" / "hello").mkdir(parents=True)
    (bundle / "skills" / "hello" / "SKILL.md").write_text(
        "---\nname: hello\ndescription: test skill\n---\nhi"
    )
    if with_manifest:
        (bundle / ".claude-plugin").mkdir(parents=True)
        (bundle / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": name, "version": "0.0.1"})
        )
    return bundle


def test_plugins_default_empty():
    """plugins の既定は空リスト。"""
    assert ModelPreset().plugins == []


def test_skills_default_none():
    """skills の既定は None (SDK 自動構成なし)。"""
    assert ModelPreset().skills is None


def test_skills_accepts_all_list_and_empty():
    """skills は "all" / [names] / [] を受容する。"""
    assert ModelPreset(skills="all").skills == "all"
    assert ModelPreset(skills=["a", "b"]).skills == ["a", "b"]
    assert ModelPreset(skills=[]).skills == []


def test_plugins_valid_bundle_accepted(tmp_path):
    """存在し plugin.json を持つ束は受容され、パスは expanduser 済み。"""
    bundle = _make_bundle(tmp_path)
    preset = ModelPreset(plugins=[str(bundle)])
    assert preset.plugins == [str(bundle)]


def test_plugins_missing_dir_rejected():
    """存在しないパスは ValueError。"""
    from pydantic import ValidationError
    with pytest.raises(ValidationError) as exc:
        ModelPreset(plugins=["/nonexistent/bundle"])
    assert "存在しません" in str(exc.value)


def test_plugins_missing_manifest_rejected(tmp_path):
    """.claude-plugin/plugin.json が無い束は ValueError。"""
    from pydantic import ValidationError
    bundle = _make_bundle(tmp_path, with_manifest=False)
    with pytest.raises(ValidationError) as exc:
        ModelPreset(plugins=[str(bundle)])
    assert "plugin.json" in str(exc.value)


def test_plugins_passed_to_options(tmp_path):
    """plugins が ClaudeAgentOptions.plugins に local 形式で渡る。"""
    bundle = _make_bundle(tmp_path)
    yaml_content = textwrap.dedent(f"""
        models:
          test-model:
            plugins:
              - "{bundle}"
    """)
    yaml_file = tmp_path / "config.yml"
    yaml_file.write_text(yaml_content)
    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        config = _load_yaml_config()
    with patch("kotoyose.config._config", config):
        opts = build_options("test-model")
    assert opts.plugins == [{"type": "local", "path": str(bundle)}]


def test_plugins_empty_when_not_configured(tmp_path):
    """plugins 未設定なら ClaudeAgentOptions.plugins は空リスト。"""
    yaml_file = tmp_path / "config.yml"
    yaml_file.write_text("models:\n  any-model: {}\n")
    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        config = _load_yaml_config()
    with patch("kotoyose.config._config", config), \
         patch("kotoyose.config._get_oauth_token", return_value=None), \
         patch.dict(os.environ, {"ANTHROPIC_AUTH_TOKEN": "", "ANTHROPIC_API_KEY": ""}, clear=False):
        opts = build_options("any-model")
    assert opts.plugins == []


def test_skills_passed_to_options(tmp_path):
    """skills (list / all) が ClaudeAgentOptions.skills へそのまま渡る。"""
    yaml_content = textwrap.dedent("""
        models:
          list-model:
            skills: ["systematic-debugging"]
          all-model:
            skills: "all"
    """)
    yaml_file = tmp_path / "config.yml"
    yaml_file.write_text(yaml_content)
    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        config = _load_yaml_config()
    with patch("kotoyose.config._config", config):
        assert build_options("list-model").skills == ["systematic-debugging"]
        assert build_options("all-model").skills == "all"


def test_skills_none_when_not_configured(tmp_path):
    """skills 未設定なら ClaudeAgentOptions.skills は None。"""
    yaml_file = tmp_path / "config.yml"
    yaml_file.write_text("models:\n  any-model: {}\n")
    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        config = _load_yaml_config()
    with patch("kotoyose.config._config", config), \
         patch("kotoyose.config._get_oauth_token", return_value=None), \
         patch.dict(os.environ, {"ANTHROPIC_AUTH_TOKEN": "", "ANTHROPIC_API_KEY": ""}, clear=False):
        opts = build_options("any-model")
    assert opts.skills is None


# ── 個別リソース (skill_refs / mcp_refs) の合成 ───────────────────────


def test_individual_refs_resolve_in_order_and_inline_wins(tmp_path):
    """skill_refs/mcp_refs を記載順で合成し、同名 MCP と inline skills は inline を優先する。"""
    first_bundle = _make_bundle(tmp_path, "first")
    second_bundle = _make_bundle(tmp_path, "second")
    inline_bundle = _make_bundle(tmp_path, "inline")
    yaml_file = tmp_path / "config.yml"
    yaml_file.write_text(textwrap.dedent(f"""
        skill_definitions:
          base:
            plugin: "{first_bundle}"
            skill: base-skill
          extra:
            plugin: "{second_bundle}"
            skill: extra-skill
        mcps:
          shared:
            server:
              type: http
              url: http://resource-shared/mcp
          extra:
            server:
              command: extra-mcp
        models:
          referenced:
            skill_refs: [base, extra]
            mcp_refs: [shared, extra]
            plugins: ["{inline_bundle}"]
            skills: ["inline-skill"]
            mcp_servers:
              shared:
                type: http
                url: http://inline/mcp
    """))

    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        config = _load_yaml_config()
    with patch("kotoyose.config._config", config):
        opts = build_options("referenced")

    assert opts.plugins == [
        {"type": "local", "path": str(first_bundle)},
        {"type": "local", "path": str(second_bundle)},
        {"type": "local", "path": str(inline_bundle)},
    ]
    assert opts.skills == ["base-skill", "extra-skill", "inline-skill"]
    assert opts.mcp_servers == {
        "shared": {"type": "http", "url": "http://inline/mcp"},
        "extra": {"command": "extra-mcp"},
    }


def test_removed_subagent_config_keys_fail_with_delegation_hint(tmp_path):
    """agent_definitions と alias の agent_refs / agents は委譲先を示して拒否する。"""
    message = r"removed; delegate with mcp_refs: \[kotoyose\] and delegates"
    root = tmp_path / "root.yml"
    root.write_text("agent_definitions: {}\nmodels: {}\n")
    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(root)}):
        with pytest.raises(ValueError, match=message):
            _load_yaml_config()

    alias = tmp_path / "alias.yml"
    alias.write_text("models:\n  chat:\n    agent_refs: [researcher]\n")
    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(alias)}):
        with pytest.raises(ValueError, match=message) as refs:
            _load_yaml_config()
    assert "agent_refs" in str(refs.value)

    inline = tmp_path / "inline.yml"
    inline.write_text("models:\n  chat:\n    agents: {}\n")
    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(inline)}):
        with pytest.raises(ValueError, match=message) as agents:
            _load_yaml_config()
    assert "agents" in str(agents.value)


@pytest.mark.parametrize(
    ("mcp_configuration", "model_configuration", "expected_header"),
    [
        (
            """
            mcps:
              remote:
                server:
                  type: http
                  url: http://resource/mcp
                  headers:
                    Authorization: "Bearer {{header:X-Resource-Token}}"
            """,
            "mcp_refs: [remote]",
            "X-Resource-Token",
        ),
        (
            "",
            """
            mcp_servers:
              remote:
                type: http
                url: http://inline/mcp
                headers:
                  Authorization: "Bearer {{header:X-Inline-Token}}"
            """,
            "X-Inline-Token",
        ),
    ],
)
def test_mcp_header_template_requires_declared_request_header_at_config_load(
    tmp_path, mcp_configuration, model_configuration, expected_header
):
    """解決後の MCP template は preset ごとの request header 契約を必須にする。"""
    yaml_file = tmp_path / "config.yml"
    yaml_file.write_text(
        f"{textwrap.dedent(mcp_configuration).strip()}\n"
        "models:\n"
        "  taskhub-pm:\n"
        f"{textwrap.indent(textwrap.dedent(model_configuration).strip(), '    ')}\n"
    )

    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        with pytest.raises(ValueError, match=r"taskhub-pm.*required_request_headers") as exc_info:
            _load_yaml_config()

    assert expected_header in str(exc_info.value)


def test_mcp_header_template_accepts_case_insensitive_declared_request_header(tmp_path):
    """HTTP header 名は大文字小文字を区別せず、設定時にも同じ規則で照合する。"""
    yaml_file = tmp_path / "config.yml"
    yaml_file.write_text(textwrap.dedent("""
        models:
          taskhub-pm:
            required_request_headers: [x-pm-mcp-token]
            mcp_servers:
              pm:
                type: http
                url: http://localhost:8200/mcp/pm
                headers:
                  Authorization: "Bearer {{header:X-Pm-Mcp-Token}}"
    """))

    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        config = _load_yaml_config()

    assert config.models["taskhub-pm"].required_request_headers == ["x-pm-mcp-token"]


@pytest.mark.parametrize(
    ("resource_field", "resource_section"),
    [
        ("skill_refs", "skill_definitions: {}"),
        ("mcp_refs", "mcps: {}"),
    ],
)
def test_unknown_resource_reference_rejected_at_config_load(
    tmp_path, resource_field, resource_section
):
    """Model が不明な個別リソースを参照した場合は config load 時に失敗する。"""
    yaml_file = tmp_path / "config.yml"
    yaml_file.write_text(textwrap.dedent(f"""
        {resource_section}
        models:
          broken:
            {resource_field}: [missing]
    """))

    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        with pytest.raises(ValueError, match="missing"):
            _load_yaml_config()


@pytest.mark.parametrize("field", ["skill_profiles", "mcp_profiles"])
def test_legacy_profile_top_level_key_rejected_at_config_load(tmp_path, field):
    """撤去済みの skill_profiles / mcp_profiles が非空でトップレベルに残っていれば拒否される。"""
    yaml_file = tmp_path / "config.yml"
    yaml_file.write_text(textwrap.dedent(f"""
        {field}:
          example: {{}}
    """))

    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        with pytest.raises(ValueError, match="Extra inputs are not permitted"):
            _load_yaml_config()


@pytest.mark.parametrize("field", ["skill_profiles", "mcp_profiles"])
def test_legacy_profile_preset_key_rejected_at_config_load(tmp_path, field):
    """撤去済みの skill_profiles / mcp_profiles が非空で defaults/model に残っていれば拒否される。"""
    yaml_file = tmp_path / "config.yml"
    yaml_file.write_text(textwrap.dedent(f"""
        models:
          broken:
            {field}: [something]
    """))

    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        with pytest.raises(ValueError, match="Extra inputs are not permitted"):
            _load_yaml_config()




def test_inline_skills_and_mcp_remain_backward_compatible(tmp_path):
    """named profile が無い旧 YAML の inline 設定をそのまま runtime へ渡す。"""
    bundle = _make_bundle(tmp_path, "legacy")
    yaml_file = tmp_path / "config.yml"
    yaml_file.write_text(textwrap.dedent(f"""
        models:
          legacy:
            plugins: ["{bundle}"]
            skills: [legacy-skill]
            mcp_servers:
              legacy:
                command: legacy-mcp
    """))

    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        config = _load_yaml_config()
    with patch("kotoyose.config._config", config):
        opts = build_options("legacy")

    assert opts.plugins == [{"type": "local", "path": str(bundle)}]
    assert opts.skills == ["legacy-skill"]
    assert opts.mcp_servers == {"legacy": {"command": "legacy-mcp"}}


def test_individual_resources_resolve_for_model_and_keep_legacy_paths(tmp_path):
    """個別 Skill / MCP / prompt file が Model の参照順で runtime へ注入される。"""
    bundle = _make_bundle(tmp_path, "individual")
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Individual prompt", encoding="utf-8")
    yaml_file = tmp_path / "config.yml"
    yaml_file.write_text(textwrap.dedent(f"""
        skill_definitions:
          debugging:
            description: Debug assistance
            plugin: "{bundle}"
            skill: systematic-debugging
        mcps:
          memory:
            description: Long-term memory
            server:
              type: http
              url: http://localhost:8101/mcp/
        prompt_files:
          identity:
            description: Assistant identity
            path: "{prompt}"
        models:
          individual:
            skill_refs: [debugging]
            mcp_refs: [memory]
            prompt_file_refs: [identity]
    """))
    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        config = _load_yaml_config()
    with patch("kotoyose.config._config", config):
        opts = build_options("individual")

    assert opts.plugins == [{"type": "local", "path": str(bundle)}]
    assert opts.skills == ["systematic-debugging"]
    assert opts.mcp_servers == {"memory": {"type": "http", "url": "http://localhost:8101/mcp/"}}
    assert "Individual prompt" in opts.system_prompt["append"]


@pytest.mark.parametrize(
    ("definition", "field"),
    [
        (lambda tmp_path: SkillDefinition(plugin=str(_make_bundle(tmp_path, "blank-skill")), skill=" \t "), "skill"),
        (lambda _tmp_path: PromptFileDefinition(path=" \n "), "path"),
        (lambda _tmp_path: PromptFileDefinition(), "path"),
    ],
)
def test_individual_resource_definitions_reject_blank_required_values(definition, field, tmp_path):
    """個別 Skill 名と prompt path は空白だけでは保存できない。"""
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match=field):
        definition(tmp_path)


def test_individual_skill_refs_cannot_be_combined_with_disabled_skill_filter():
    """個別 Skill を選択した Model で skills: [] による無効化を拒否する。"""
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match=r"skill_refs と skills: \[\]"):
        ModelPreset(skill_refs=["debugging"], skills=[])


@pytest.mark.parametrize("field", ["skill_refs", "mcp_refs", "prompt_file_refs"])
def test_duplicate_resource_references_rejected(field):
    """同じ個別リソースの二重参照は設定時に拒否する。"""
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="重複指定"):
        ModelPreset(**{field: ["base", "base"]})


def test_claude_options_always_disallow_builtin_subagent_tools(tmp_path):
    """deny が空でも Task と Agent は disallowed_tools に入る。既存の deny は残す。"""
    yaml_file = tmp_path / "config.yml"
    yaml_file.write_text("models:\n  any-model: {}\n")
    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        config = _load_yaml_config()
    with patch("kotoyose.config._config", config), \
         patch("kotoyose.config._get_oauth_token", return_value=None), \
         patch.dict(os.environ, {"ANTHROPIC_AUTH_TOKEN": "", "ANTHROPIC_API_KEY": ""}, clear=False):
        opts = build_options("any-model")
    assert opts.agents is None
    assert opts.disallowed_tools == ["Task", "Agent"]


def test_omitted_cwd_is_expanded_like_explicit_cwd(tmp_path):
    """cwd 省略時も ~ を展開して SDK へ渡す。

    Pydantic は既定値にバリデータを走らせないため、以前は cwd を省いた preset だけ
    expand_cwd が動かず、文字列 '~/kotoyose-workspace' がそのまま SDK に渡っていた。
    SDK は起動前に Path(cwd).exists() を検査するため
    'Working directory does not exist: ~/kotoyose-workspace' で実行時に失敗する。
    CLI provider は各 adapter が Path(cwd).expanduser() を施しているため
    claude 経路だけが露出していた。
    """
    expected = os.path.expanduser("~/kotoyose-workspace")

    # モデル単体: 明示時と省略時で同じ結果になること
    assert ModelPreset().cwd == expected
    assert ModelPreset(cwd="~/kotoyose-workspace").cwd == expected

    # build_options 経由: SDK へ渡る値が展開済みであること
    yaml_file = tmp_path / "config.yml"
    yaml_file.write_text("models:\n  omitted-cwd:\n    model: claude-sonnet-5\n")
    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        config = _load_yaml_config()
    assert config.models["omitted-cwd"].cwd == expected
    with patch("kotoyose.config._config", config), \
         patch("kotoyose.config._get_oauth_token", return_value=None), \
         patch.dict(os.environ, {"ANTHROPIC_AUTH_TOKEN": "", "ANTHROPIC_API_KEY": ""}, clear=False):
        opts = build_options("omitted-cwd")
    assert opts.cwd == expected


def test_config_example_loads_and_validates():
    """コミット済み config.example.yml (最小スターター) が検証を通り、導線を保つ。"""
    repo_root = Path(__file__).resolve().parents[1]
    example = repo_root / "config.example.yml"
    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(example)}):
        config = _load_yaml_config()

    # provider ごとの最小形が 1 つずつ揃っていること (README がここへ導いている)
    assert {preset.provider for preset in config.models.values()} == {
        "claude", "codex", "cursor", "antigravity",
    }

    # 参照機構の実演 preset が skill / mcp / prompt の *_refs を実際に解決できること
    refs_demo = config.models["kotoyose-with-refs"]
    plugins, skills, mcp_servers = config.resolve_resources(refs_demo)
    assert plugins and skills and mcp_servers
    assert config.resolve_prompt_files(refs_demo)

    # 読み手がエージェントの作業場所を推測せずに済むよう、全 preset が cwd を明示する。
    # (省略しても既定値で動くが、例示としては明示していることに意味がある)
    raw_models = yaml.safe_load(example.read_text())["models"]
    for name, raw in raw_models.items():
        assert "cwd" in raw, f"{name} が cwd を明示していない"


def test_config_recipes_loads_and_validates():
    """設定例を含む tests/fixtures/public-recipes.yml が検証を通り、隔離契約を保つ。"""
    repo_root = Path(__file__).resolve().parents[1]
    recipes = repo_root / "tests/fixtures/public-recipes.yml"
    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(recipes)}):
        config = _load_yaml_config()
    assert "orchestrator-coding" in config.models
    coding = config.models["orchestrator-coding"]
    assert coding.bare is False
    assert coding.setting_sources == []
    assert coding.require_isolation is True
    assert "CronCreate" in coding.deny
    assert "RemoteTrigger" in coding.deny
    assert "WebFetch(*)" not in coding.deny
    assert "WebSearch(*)" not in coding.deny
    assert "orchestrator-task-agent" in config.models
    task_agent = config.models["orchestrator-task-agent"]
    assert task_agent.bare is False
    assert task_agent.setting_sources == []
    assert task_agent.require_isolation is True
    assert "CronCreate" in task_agent.deny
    assert "RemoteTrigger" in task_agent.deny
    assert "WebFetch(*)" not in task_agent.deny
    assert "WebSearch(*)" not in task_agent.deny


def test_committed_example_bundle_is_valid_plugin():
    """repo にコミットした skills/example-bundle が plugin 検証を通る。"""
    repo_root = Path(__file__).resolve().parents[1]
    bundle = repo_root / "skills" / "example-bundle"
    preset = ModelPreset(plugins=[str(bundle)])
    assert preset.plugins == [str(bundle)]


def test_plugins_null_yaml_becomes_empty(tmp_path):
    """plugins: (null) は空リスト扱い。"""
    yaml_content = textwrap.dedent("""
        models:
          test-model:
            plugins:
    """)
    yaml_file = tmp_path / "config.yml"
    yaml_file.write_text(yaml_content)
    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        config = _load_yaml_config()
    assert config.preset_for("test-model").plugins == []


def test_plugins_non_list_rejected():
    """plugins に非リスト (文字列) を渡すと明確な ValueError。"""
    from pydantic import ValidationError
    with pytest.raises(ValidationError) as exc:
        ModelPreset(plugins="some-path")
    assert "文字列リスト" in str(exc.value)


# ── runtime provider フィールド ───────────────────────────────────────


def test_provider_defaults_to_claude():
    """provider 未指定の既存 preset は claude として扱う。"""
    assert ModelPreset().provider == "claude"


def test_provider_and_nested_configs_round_trip_from_yaml(tmp_path):
    """provider 固有 config は YAML ロードで保持される。"""
    yaml_content = textwrap.dedent("""
        models:
          codex-coding:
            provider: codex
            model: gpt-5.1-codex
            codex:
              sandbox: workspace-write
              reasoning_effort: high
            cursor:
              runtime: local
            antigravity:
              sdk:
                vertex: true
                timeout_seconds: 30
    """)
    yaml_file = tmp_path / "config.yml"
    yaml_file.write_text(yaml_content)
    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        config = _load_yaml_config()
    preset = config.preset_for("codex-coding")
    assert preset.provider == "codex"
    assert preset.codex["sandbox"] == "workspace-write"
    assert preset.cursor["runtime"] == "local"
    assert preset.antigravity["sdk"] == {"vertex": True, "timeout_seconds": 30}


@pytest.mark.parametrize("provider", ["codex", "cursor", "antigravity"])
def test_build_options_rejects_non_claude_provider(tmp_path, provider):
    """ClaudeAgentOptions factory は Claude provider 専用。"""
    yaml_content = textwrap.dedent("""
        models:
          runtime-model:
            provider: {provider}
            model: test-model
    """).format(provider=provider)
    yaml_file = tmp_path / "config.yml"
    yaml_file.write_text(yaml_content)
    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        config = _load_yaml_config()
    with patch("kotoyose.config._config", config):
        with pytest.raises(ValueError) as exc:
            build_options("runtime-model")
    assert f"provider='{provider}'" in str(exc.value)


# ── cwd_allowed_roots フィールド と検証 ─────────────────────────────────


def test_cwd_allowed_roots_default_none():
    """cwd_allowed_roots の既定値は None。"""
    preset = ModelPreset()
    assert preset.cwd_allowed_roots is None


def test_cwd_allowed_roots_accepts_list():
    """cwd_allowed_roots にリストを受け入れる。"""
    preset = ModelPreset(cwd_allowed_roots=["~/kotoyose-workspace", "/tmp"])
    assert preset.cwd_allowed_roots == ["~/kotoyose-workspace", "/tmp"]


def test_allow_cwd_override_true_without_roots_raises_valueerror():
    """allow_cwd_override=True かつ cwd_allowed_roots が None または空リストの場合は ValueError。"""
    from pydantic import ValidationError
    with pytest.raises(ValidationError) as exc:
        ModelPreset(allow_cwd_override=True, cwd_allowed_roots=None)
    assert "cwd_allowed_roots" in str(exc.value)


def test_allow_cwd_override_true_with_empty_roots_raises_valueerror():
    """allow_cwd_override=True + cwd_allowed_roots=[] でも ValueError。"""
    from pydantic import ValidationError
    with pytest.raises(ValidationError) as exc:
        ModelPreset(allow_cwd_override=True, cwd_allowed_roots=[])
    assert "cwd_allowed_roots" in str(exc.value)


def test_allow_cwd_override_true_with_roots_ok():
    """allow_cwd_override=True + cwd_allowed_roots=['path'] は valid。"""
    preset = ModelPreset(allow_cwd_override=True, cwd_allowed_roots=["~/kotoyose-workspace"])
    assert preset.allow_cwd_override is True
    assert preset.cwd_allowed_roots == ["~/kotoyose-workspace"]


def test_cwd_allowed_roots_loaded_from_yaml(tmp_path):
    """cwd_allowed_roots が YAML から読み込まれる。"""
    yaml_content = textwrap.dedent("""
        models:
          coding:
            bare: false
            allow_cwd_override: true
            cwd_allowed_roots:
              - ~/kotoyose-workspace
              - /tmp/worktrees
    """)
    yaml_file = tmp_path / "config.yml"
    yaml_file.write_text(yaml_content)
    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        config = _load_yaml_config()
    preset = config.preset_for("coding")
    assert preset.cwd_allowed_roots == ["~/kotoyose-workspace", "/tmp/worktrees"]


def test_allow_cwd_override_true_without_roots_in_yaml_raises_valueerror(tmp_path):
    """YAML で allow_cwd_override=True + cwd_allowed_roots なし → ロード時 ValueError。"""
    from pydantic import ValidationError
    yaml_content = textwrap.dedent("""
        models:
          coding:
            bare: false
            allow_cwd_override: true
    """)
    yaml_file = tmp_path / "config.yml"
    yaml_file.write_text(yaml_content)
    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        with pytest.raises(ValidationError) as exc:
            _load_yaml_config()
    assert "cwd_allowed_roots" in str(exc.value)


# ── Per-element validation tests (Fix 2) ────────────────────────────────────

def test_cwd_allowed_roots_rejects_empty_string():
    """cwd_allowed_roots に空文字列要素があると ValueError。"""
    from pydantic import ValidationError
    with pytest.raises(ValidationError) as exc:
        ModelPreset(
            allow_cwd_override=True,
            cwd_allowed_roots=[""],
        )
    assert "cwd_allowed_roots" in str(exc.value)


def test_cwd_allowed_roots_rejects_whitespace_only():
    """cwd_allowed_roots に空白のみ要素があると ValueError。"""
    from pydantic import ValidationError
    with pytest.raises(ValidationError) as exc:
        ModelPreset(
            allow_cwd_override=True,
            cwd_allowed_roots=["   "],
        )
    assert "cwd_allowed_roots" in str(exc.value)


def test_cwd_allowed_roots_rejects_mixed_empty_and_valid():
    """cwd_allowed_roots に空要素と有効要素が混在していると ValueError。"""
    from pydantic import ValidationError
    with pytest.raises(ValidationError) as exc:
        ModelPreset(
            allow_cwd_override=True,
            cwd_allowed_roots=["", "/tmp/ok"],
        )
    assert "cwd_allowed_roots" in str(exc.value)


# ── tools (per-preset built-in tools allowlist) ─────────────────────────


def test_tools_default_none():
    """ModelPreset の tools フィールドはデフォルト None (preset 全体を使う従来挙動)。"""
    assert ModelPreset().tools is None


def test_tools_roundtrip_omitted(tmp_path):
    """tools 未指定の preset は YAML round-trip 後も None のまま。"""
    yaml_file = tmp_path / "config.yml"
    yaml_file.write_text(textwrap.dedent("""
        models:
          test-model:
            bare: true
    """))
    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        config = _load_yaml_config()
    assert config.preset_for("test-model").tools is None


def test_tools_roundtrip_empty_list(tmp_path):
    """tools: [] は「built-in tool なし」を意味する有効値として保存・復元される。"""
    yaml_file = tmp_path / "config.yml"
    yaml_file.write_text(textwrap.dedent("""
        models:
          test-model:
            tools: []
    """))
    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        config = _load_yaml_config()
    assert config.preset_for("test-model").tools == []


def test_tools_roundtrip_enumerated_list(tmp_path):
    """tools に列挙した built-in tool 名がそのまま保存・復元される。"""
    yaml_file = tmp_path / "config.yml"
    yaml_file.write_text(textwrap.dedent("""
        models:
          test-model:
            tools: ["Read", "Grep"]
    """))
    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        config = _load_yaml_config()
    assert config.preset_for("test-model").tools == ["Read", "Grep"]


def test_tools_none_passes_claude_code_preset_to_options(tmp_path):
    """tools 未指定 (None) のときは従来通り ToolsPreset(claude_code) が渡る。"""
    yaml_file = tmp_path / "config.yml"
    yaml_file.write_text(textwrap.dedent("""
        models:
          test-model:
            bare: true
    """))
    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        config = _load_yaml_config()
    with patch("kotoyose.config._config", config):
        opts = build_options("test-model")
    assert opts.tools == ToolsPreset(type="preset", preset="claude_code")


def test_tools_empty_list_passes_empty_list_to_options(tmp_path):
    """tools: [] は SDK にそのまま空リストとして渡り、built-in tool を全面封鎖する。"""
    yaml_file = tmp_path / "config.yml"
    yaml_file.write_text(textwrap.dedent("""
        models:
          test-model:
            tools: []
    """))
    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        config = _load_yaml_config()
    with patch("kotoyose.config._config", config):
        opts = build_options("test-model")
    assert opts.tools == []


def test_tools_enumerated_list_passed_to_options(tmp_path):
    """tools に列挙した built-in tool 名がそのまま ClaudeAgentOptions.tools へ渡る。"""
    yaml_file = tmp_path / "config.yml"
    yaml_file.write_text(textwrap.dedent("""
        models:
          test-model:
            tools: ["Read", "Grep"]
    """))
    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        config = _load_yaml_config()
    with patch("kotoyose.config._config", config):
        opts = build_options("test-model")
    assert opts.tools == ["Read", "Grep"]


def test_tools_independent_of_deny(tmp_path):
    """tools (allowlist) と deny (disallowed_tools) は独立に併用できる (defense in depth)。"""
    yaml_file = tmp_path / "config.yml"
    yaml_file.write_text(textwrap.dedent("""
        models:
          test-model:
            tools: []
            deny: ["Bash(*)"]
    """))
    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        config = _load_yaml_config()
    with patch("kotoyose.config._config", config):
        opts = build_options("test-model")
    assert opts.tools == []
    assert opts.disallowed_tools == ["Bash(*)", "Task", "Agent"]


def test_replaced_configuration_keys_are_rejected():
    with pytest.raises(ValueError, match="runtime_models.*routes"):
        HarnessConfig.model_validate({"runtime_models": {}})
    with pytest.raises(ValueError, match="runtime_ref.*route"):
        HarnessConfig.model_validate({"models": {"alias": {"runtime_ref": "old"}}})


def test_route_preset_round_trip_does_not_write_derived_fields():
    config = HarnessConfig.model_validate({
        "connections": {"router-conn": {"provider": "claude", "kind": "endpoint", "data_policy": "no_training",
                                        "endpoint": "https://example.invalid/v1", "secret": "env:ROUTE_TOKEN"}},
        "routes": {"router": {"driver": "claude.agent-sdk", "model": "fixture",
                              "connection": "router-conn", "subscription_only": True}},
        "models": {"alias": {"route": "router"}},
    })
    encoded = config.model_dump(mode="json")
    alias = encoded["models"]["alias"]
    assert all(key not in alias for key in ("provider", "model", "anthropic_base_url",
                                             "anthropic_auth_token_env", "subscription_only"))
    restored = HarnessConfig.model_validate(encoded).preset_for("alias")
    assert (restored.provider, restored.model, restored.anthropic_base_url, restored.anthropic_auth_token_env,
            restored.subscription_only) == ("claude", "fixture", "https://example.invalid/v1", "ROUTE_TOKEN", True)


@pytest.mark.parametrize(("data", "field"), [
    ({"defaults": {}}, "defaults"),
    ({"skill_profiles": {}}, "skill_profiles"),
    ({"mcp_profiles": {}}, "mcp_profiles"),
    ({"runtime_profiles": {}}, "runtime_profiles"),
    ({"clients": {"fixture": {"token_env": "FIXTURE_TOKEN_ENV"}}}, "token_env"),
    ({"models": {"fixture": {"system_prompt_files": []}}}, "system_prompt_files"),
])
def test_removed_config_keys_are_forbidden(data, field):
    with pytest.raises(ValueError, match=field) as exc:
        HarnessConfig.model_validate(data)
    assert exc.value.errors()[0]["type"] == "extra_forbidden"


def test_legacy_execution_value_is_rejected():
    with pytest.raises(ValueError, match="'direct' or 'native'"):
        ModelPreset.model_validate({"execution": "legacy"})


def test_hand_written_on_key_is_read_as_a_string():
    # PyYAML's YAML 1.1 default would turn the key `on` into True and the config would fail to load.
    from kotoyose.config import HarnessConfig, load_config_yaml

    data = load_config_yaml("""
connections:
  c:
    provider: claude
    kind: subscription
    data_policy: no_training
    on: child:dev
routes:
  r: {driver: claude.agent-sdk, connection: c, model: claude-haiku-5-5}
models:
  a:
    route: r
    on: child:dev
    bare: yes
    strict_mcp_config: off
""")
    assert data["models"]["a"]["on"] == "child:dev" and data["models"]["a"]["bare"] == "yes"
    cfg = HarnessConfig.model_validate(data)
    assert cfg.models["a"].on == "child:dev" and cfg.connections["c"].on == "child:dev"
    assert cfg.models["a"].bare is True and cfg.models["a"].strict_mcp_config is False
    assert load_config_yaml("x: true\ny: False\n") == {"x": True, "y": False}
