"""Claude runtime の OAuth token と process 認証環境を解決する adapter。"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING

from .. import process_env
from ..secret_store import resolve_secret_sync

if TYPE_CHECKING:
    from claude_agent_sdk import ClaudeAgentOptions

    from ..config import HarnessConfig, ModelPreset


def build_auth_env(
    preset: ModelPreset | None,
    config: HarnessConfig,
    oauth_token_provider: Callable[[], str | None],
    *,
    resolved_secrets: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Claude process へ渡す endpoint と認証 token を優先順位どおり解決する。

    Args:
        preset: 適用する Claude preset。Model discovery では None。
        config: Settings の正本となる現在の HarnessConfig。
        oauth_token_provider: 最終 fallback の OAuth token resolver。

    Returns:
        Claude Agent SDK の process へ渡す環境変数。

    Raises:
        ValueError: Preset 指定の token 環境変数が未定義の場合。

    OAuth は CLAUDE_CODE_OAUTH_TOKEN へ渡す。bare mode の OAuth 非対応を
    回避するための設定変更や API key への変換は行わない。
    """
    # SDK は options.env にない host env も継承するため、管理対象外 endpoint を除く。
    os.environ.pop("ANTHROPIC_BASE_URL", None)
    env: dict[str, str] = {}

    from ..config import _find_claude_subscription_connection

    sub_pair = _find_claude_subscription_connection(config, preset)
    if (
        sub_pair is not None
        and sub_pair[1].config_dir is not None
        and (
            preset is None
            or (preset.anthropic_auth_token_secret is None and preset.anthropic_auth_token_env is None)
        )
    ):
        # Owner's rule: config_dir subscription runs use Claude CLI's own login inside
        # CLAUDE_CONFIG_DIR and must never inject or inherit token/API-key env vars.
        env["CLAUDE_CONFIG_DIR"] = str(sub_pair[1].config_dir)
        for token_key in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            if process_env.get(token_key) is not None:
                env[token_key] = ""
        return env

    base_url = (
        preset.anthropic_base_url
        if preset is not None and preset.anthropic_base_url is not None
        else config.settings.anthropic_base_url
    )
    if base_url:
        env["ANTHROPIC_BASE_URL"] = base_url

    secret_name = getattr(preset, "anthropic_auth_token_secret", None) if preset is not None else None
    if secret_name is not None:
        token = (
            resolved_secrets[secret_name]
            if resolved_secrets is not None and secret_name in resolved_secrets
            else resolve_secret_sync(secret_name)
        )
    elif preset is not None and preset.anthropic_auth_token_env is not None:
        token = process_env.get(preset.anthropic_auth_token_env)
        if token is None:
            raise ValueError(
                "anthropic_auth_token_env が参照する環境変数 "
                f"'{preset.anthropic_auth_token_env}' は未定義です"
            )
    else:
        token = (
            config.resolve_setting(
                "anthropic_auth_token",
                "ANTHROPIC_AUTH_TOKEN",
                resolved_secrets=resolved_secrets,
            )
            or process_env.get("ANTHROPIC_API_KEY")
        )
    if token:
        env["ANTHROPIC_API_KEY"] = token
        # 明示 API 認証へ host の OAuth が混入するのを防ぐ。
        if process_env.get("CLAUDE_CODE_OAUTH_TOKEN"):
            env["CLAUDE_CODE_OAUTH_TOKEN"] = ""
    else:
        oauth_token = process_env.get("CLAUDE_CODE_OAUTH_TOKEN") or oauth_token_provider()
        if oauth_token:
            # CLI 2.1.286 以降は API key と OAuth を区別する。bare mode は
            # OAuth 非対応なので、API key に偽装せず CLI の認証拒否を維持する。
            env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token
    return env


def validate_auth_options(options: ClaudeAgentOptions) -> None:
    """Reject resolved OAuth-only bare mode before starting a Claude subprocess.

    Args:
        options: Resolved options, including the authentication environment.

    Raises:
        ValueError: Bare mode is selected with OAuth and without an API key.

    No authentication or isolation setting is changed by this check.
    """
    if (
        "bare" in options.extra_args
        and options.env.get("CLAUDE_CODE_OAUTH_TOKEN")
        and not options.env.get("ANTHROPIC_API_KEY")
    ):
        raise ValueError(
            "Claude bare=true does not support subscription OAuth. "
            "An administrator must explicitly select bare=false with reviewed "
            "isolation/resource settings, or configure an existing API key for bare mode."
        )
