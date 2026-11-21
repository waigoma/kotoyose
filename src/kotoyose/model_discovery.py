"""Provider ごとの利用可能モデル候補を安全に収集する。"""

import asyncio
import contextlib
import copy
import json
import os
import tempfile
import time
import tomllib
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from cursor_sdk import (
    AsyncClient,
    AsyncCursor,
    AuthenticationError as CursorAuthenticationError,
    PermissionDeniedError as CursorPermissionDeniedError,
    RateLimitError as CursorRateLimitError,
)
from openai_codex import AsyncCodex, CodexConfig, JsonRpcError

from . import process_env
from .config import Connection, ModelPreset, _build_auth_env, get_config
from .claude_cli import bundled_claude_cli
from .connections_probe import codex_home_and_auth_path, resolve_connection_cli_binary
from .print_cli_events import _values_from_payload
from .print_cli_hooks import get_models_parser
from .print_cli_home import PrintCliHomeBusyError, use_login_home
from .print_cli_profiles import (
    PrintCliProfile,
    build_print_cli_env,
    get_print_cli_profile_for_provider,
    list_print_cli_profiles,
)
from .secret_store import SecretStoreError, resolve_secret_async

DISCOVERY_TIMEOUT_SECONDS = 10.0
CLAUDE_CLI_DISCOVERY_TIMEOUT_SECONDS = 30.0
CODEX_CLEANUP_TIMEOUT_SECONDS = 5.0
CONNECTION_DISCOVERY_CACHE_TTL_SECONDS = 600.0
CAPTURE_LIMIT_BYTES = 256 * 1024
READ_CHUNK_BYTES = 64 * 1024
ANTHROPIC_API_VERSION = "2023-06-01"
_CLAUDE_LOGIN_REQUIRED_MARKERS = (
    "not logged in",
    "please run /login",
    "please log in",
    "login expired",
    "session expired",
    "re-authenticate",
    "reauthenticate",
    "oauth token has expired",
    "requires login",
    "logged out",
    "authentication_failed",
    "unauthorized",
    "invalid_api_key",
    "401",
    "403",
)
CLI_COMMANDS = {
}
BUNDLED_CATALOGS = {
    # claude / codex は実データ源への照会に失敗した場合のみ使う fallback。利用可否は未検証。
    "claude": [
        "claude-fable-5",
        "claude-opus-5",
        "claude-opus-4-8",
        "claude-sonnet-5",
        "claude-haiku-4-5-20251001",
    ],
    "codex": [
        "gpt-6.1-sol",
        "gpt-5.5",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "gpt-5.4",
        "gpt-5.4-mini",
    ],
}
SUPPORTED_PROVIDERS = set(CLI_COMMANDS) | set(BUNDLED_CATALOGS) | {"cursor"}

log = structlog.get_logger()


async def _read_limited(reader: asyncio.StreamReader) -> bytes:
    """Stream を最後まで drain しつつ、保持する byte 数を制限する。

    Args:
        reader: subprocess の stdout または stderr。

    Returns:
        最大 CAPTURE_LIMIT_BYTES までの先頭 byte 列。
    """
    captured = bytearray()
    while chunk := await reader.read(READ_CHUNK_BYTES):
        remaining = CAPTURE_LIMIT_BYTES - len(captured)
        if remaining > 0:
            captured.extend(chunk[:remaining])
    return bytes(captured)


async def _run_command(*command: str, env: dict[str, str] | None = None) -> tuple[int, bytes, bytes]:
    """CLI を timeout 付きで実行し、終了コードと標準出力・標準エラーを返す。

    Args:
        *command: 実行ファイル名と引数。

    Returns:
        終了コード、標準出力、標準エラーの tuple。

    Raises:
        FileNotFoundError: 実行ファイルが存在しない場合。
        TimeoutError: 規定時間内に CLI が終了しない場合。
        OSError: subprocess を開始できない場合。
    """
    effective_env = env if env is not None else process_env.build_minimal_env()
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=READ_CHUNK_BYTES,
        env=effective_env,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    try:
        stdout, stderr, _ = await asyncio.wait_for(
            asyncio.gather(
                _read_limited(process.stdout),
                _read_limited(process.stderr),
                process.wait(),
            ),
            timeout=DISCOVERY_TIMEOUT_SECONDS,
        )
    except (TimeoutError, asyncio.CancelledError):
        # 解放した login home を子プロセスが使い続けないようにする。
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        raise
    return process.returncode, stdout, stderr


def _model_ids_from_values(values: list[Any]) -> list[str]:
    """値の一覧 (文字列、または id/name/model キーを持つ dict) から model ID を抽出する。

    Args:
        values: _values_from_payload などで取り出した生値一覧。

    Returns:
        出現順を保ち、重複と空文字を除いた model ID 一覧。
    """
    models: list[str] = []
    for value in values:
        if isinstance(value, str):
            model = value.strip()
        elif isinstance(value, dict):
            model = str(
                value.get("id") or value.get("name") or value.get("model") or ""
            ).strip()
        else:
            continue
        if model and model not in models:
            models.append(model)
    return models


def _extract_models(stdout: bytes) -> list[str]:
    """CLI の JSON または行形式の出力から model ID を抽出する。

    JSON として解釈できない場合は、改行区切りでモデル名を出す単純な CLI 実装にも
    対応するため行分割へ fallback する。この fallback は「JSON のつもりが壊れて
    いた」ケースではゴミ行を model ID として拾ってしまう副作用があるため、
    HTTP レスポンスの解析には使わない (_discover_claude_models は
    _values_from_payload/_model_ids_from_values を直接使い、JSON parse 失敗を
    bundled catalog へ明示的に fallback させる)。

    Args:
        stdout: CLI の標準出力。

    Returns:
        出現順を保ち、重複と空文字を除いた model ID 一覧。
    """
    text = stdout.decode(errors="replace").strip()
    if not text:
        return []

    try:
        values = _values_from_payload(json.loads(text))
    except json.JSONDecodeError:
        values = [line.strip() for line in text.splitlines()]

    return _model_ids_from_values(values)


def _response(
    provider: str,
    models: list[str],
    source: str,
    warning: str | None = None,
    error_code: str | None = None,
) -> dict[str, Any]:
    """Model discovery API の共通レスポンスを組み立てる。

    Args:
        provider: Provider ID。
        models: 利用可能な model ID 候補。
        source: 候補の取得元。
        warning: 手入力 fallback を促す警告。正常時は None。
        error_code: 機械判定用の定型 error code。正常時は None。

    Returns:
        API がそのまま JSON 化できる辞書。
    """
    return {
        "provider": provider,
        "models": models,
        "source": source,
        "fetched_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "warning": warning,
        "error_code": error_code,
    }


def _bundled_fallback(provider: str, reason: str) -> dict[str, Any]:
    """実データ源への照会が使えない場合に、保守的な bundled catalog を返す。

    reason はカテゴリ化した理由文言のみを含め、生の例外文字列は含めない
    (既存 CLI 経路と同じく、内部エラーの詳細を admin UI へ漏らさないため)。

    Args:
        provider: 対象 Provider ID。
        reason: warning に含める理由。

    Returns:
        bundled catalog を source とする discovery レスポンス。
    """
    return _response(
        provider,
        list(BUNDLED_CATALOGS.get(provider, [])),
        "bundled_catalog",
        f"{reason} bundled catalog を返しました。Model ID を手入力してください。",
    )


async def _fetch_url(url: str, headers: dict[str, str]) -> bytes:
    """HTTP GET を worker thread で実行し、応答本文を上限付きで返す。

    urllib は同期 API のため asyncio.to_thread でイベントループ外に逃がす。
    timeout は urlopen 自身の timeout 引数で保証する (asyncio 側の
    wait_for を重ねると、thread 内の呼び出しは timeout 後も走り続けるため
    二重化しても安全性が上がらない)。

    Args:
        url: 照会先 URL。
        headers: リクエストヘッダ。

    Returns:
        最大 CAPTURE_LIMIT_BYTES までの応答本文。

    Raises:
        urllib.error.HTTPError: 4xx/5xx レスポンス。
        urllib.error.URLError: 接続失敗・DNS 解決失敗・timeout など。
    """
    def _request() -> bytes:
        req = urllib.request.Request(url, headers=headers)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=DISCOVERY_TIMEOUT_SECONDS) as resp:
            return resp.read(CAPTURE_LIMIT_BYTES)

    return await asyncio.to_thread(_request)


def _models_url_candidates(base_url: str) -> list[str]:
    """Endpoint の base URL から /models 照会先候補を組み立てる。

    base URL は SDK の慣例で末尾に /v1 を含む値 (例: https://router/v1) を
    想定しているが、/v1 なしの値 (例: https://router) が渡されると
    {base}/models は 404 になり気づかれないまま bundled catalog へ fallback して
    しまう。/v1 で終わらない場合は {base}/v1/models も候補に加えて試す。

    Args:
        base_url: route に指定された endpoint の base URL。

    Returns:
        試行順の候補 URL 一覧。
    """
    parsed = urllib.parse.urlsplit(base_url)
    path = parsed.path.rstrip("/")
    paths = [f"{path}/models"]
    if not path.endswith("/v1"):
        paths.append(f"{path}/v1/models")
    return [urllib.parse.urlunsplit(parsed._replace(path=value, fragment="")) for value in paths]


def _safe_endpoint_source(url: str) -> str:
    """Preserve ordinary sources while excluding credentials and URL parameters."""
    try:
        parsed = urllib.parse.urlsplit(url)
        return urllib.parse.urlunsplit(parsed._replace(
            netloc=parsed.netloc.rsplit("@", 1)[-1], query="", fragment="",
        ))
    except ValueError:
        return "configured_endpoint"


async def _discover_endpoint_models(
    provider: str,
    base_url: str,
    token: str | None,
) -> dict[str, Any]:
    """OpenAI 互換 endpoint の /models から model ID 候補を取得する。

    Args:
        provider: route の Provider ID。
        base_url: route に明示された endpoint の base URL。
        token: endpoint の認証 token。認証不要の場合は None。

    Returns:
        discovery レスポンス。取得失敗時は provider の bundled catalog。
    """
    urls = _models_url_candidates(base_url)
    headers = {"anthropic-version": ANTHROPIC_API_VERSION} if provider == "claude" else {}
    if token:
        # Router 実装差を吸収するため、OpenAI / Anthropic 両互換の認証 header を送る。
        headers.update({"Authorization": f"Bearer {token}", "x-api-key": token})

    for url in urls:
        try:
            body = await _fetch_url(url, headers)
        except Exception:
            # Exceptions may repeat Authorization headers or credential-bearing
            # URLs. No endpoint/error text belongs in logs or discovery responses.
            continue

        try:
            payload = json.loads(body.decode(errors="replace"))
        except json.JSONDecodeError:
            log.warning(
                "admin.model_discovery.endpoint_parse_failed",
                provider=provider,
                error_code="invalid_json",
            )
            return _bundled_fallback(provider, "取得結果を解析できなかったため")

        models = _model_ids_from_values(_values_from_payload(payload))
        if not models:
            return _bundled_fallback(provider, "取得結果が空だったため")
        return _response(provider, models, _safe_endpoint_source(url))

    log.warning(
        "admin.model_discovery.endpoint_fetch_failed",
        provider=provider,
        error_code="fetch_failed",
    )
    return _bundled_fallback(provider, "モデル一覧の取得に失敗したため")


async def _discover_anthropic_api_models(token: str) -> dict[str, Any]:
    """Anthropic 公式 /v1/models から Claude model ID 候補を取得する。"""
    url = "https://api.anthropic.com/v1/models"
    headers = {"x-api-key": token, "anthropic-version": ANTHROPIC_API_VERSION}
    try:
        body = await _fetch_url(url, headers)
        payload = json.loads(body.decode(errors="replace"))
    except json.JSONDecodeError:
        log.warning("admin.model_discovery.claude_parse_failed", error_code="invalid_json")
        return _bundled_fallback("claude", "取得結果を解析できなかったため")
    except Exception:
        log.warning("admin.model_discovery.claude_fetch_failed", error_code="fetch_failed")
        return _bundled_fallback("claude", "モデル一覧の取得に失敗したため")

    models = _model_ids_from_values(_values_from_payload(payload))
    if not models:
        return _bundled_fallback("claude", "取得結果が空だったため")
    return _response("claude", models, url)


async def _discover_claude_models(
    endpoint_base_url: str | None = None,
    endpoint_api_key_env: str | None = None,
) -> dict[str, Any]:
    """claude provider の model 候補を、接続先 API から動的に取得する。

    認証情報は config._build_auth_env() (Claude サブプロセス起動時と同じ優先順位
    ロジック) を単一の情報源として使う。secret store 等へのアクセスを含み得るため、
    event loop を止めないよう to_thread で worker thread に逃がす。

    route の endpoint が明示されていればそこへ OpenAI 互換の /models を
    照会し、未指定なら Settings の endpoint、api.anthropic.com の順で解決する。
    ANTHROPIC_BASE_URL 環境変数は discovery の入力として参照しない。
    認証情報がない・全候補 URL の取得に失敗した場合は bundled catalog へ
    fallback する。応答が CAPTURE_LIMIT_BYTES で打ち切られ JSON として壊れて
    いた場合は、CLI 経路の行分割 fallback (ゴミ行を model ID として拾う恐れが
    ある) を使わず、素直に bundled catalog へ fallback する。

    Args:
        endpoint_base_url: route に明示された endpoint の base URL。
        endpoint_api_key_env: endpoint の token を読む環境変数名。

    Returns:
        discovery レスポンス。空リストを返すことはない。
    """
    if endpoint_api_key_env:
        token = process_env.get(endpoint_api_key_env)
        if token is None:
            return _bundled_fallback(
                "claude",
                f"認証用の環境変数 {endpoint_api_key_env} が未定義のため",
            )
    else:
        try:
            env = await asyncio.to_thread(_build_auth_env)
        except SecretStoreError:
            env = {}
        token = env.get("ANTHROPIC_API_KEY")
    if not token:
        return _bundled_fallback("claude", "認証情報が見つからないため")

    base_url = endpoint_base_url or get_config().settings.anthropic_base_url
    if base_url:
        return await _discover_endpoint_models("claude", base_url, token)

    return await _discover_anthropic_api_models(token)


def _codex_discovery_config(preset: ModelPreset | None) -> CodexConfig:
    """Use the runtime's exact config/provider serialization, without creating a thread.

    Discovery does not need prompt files, skills, MCP connections or agent role files.
    The caller must resolve the route before supplying a selected preset.
    """
    if preset is None:
        return CodexConfig()
    from .runtime.codex import CodexRuntimeSession, _resolve_codex_bin

    session = CodexRuntimeSession(
        model=preset.model,
        cwd=preset.cwd,
        config=dict(preset.codex),
    )
    return CodexConfig(
        codex_bin=_resolve_codex_bin(session.config),
        cwd=preset.cwd,
        config_overrides=session._build_config_overrides(),
        env={"CODEX_HOME": os.path.expanduser(preset.codex_home)} if preset.codex_home else None,
    )


def _codex_discovery_timeout(preset: ModelPreset | None) -> float:
    timeout = preset.codex.get("timeout", preset.timeout_seconds) if preset else None
    if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and timeout > 0:
        return min(float(timeout), DISCOVERY_TIMEOUT_SECONDS)
    return DISCOVERY_TIMEOUT_SECONDS


def _codex_model_details(sdk_models: list[Any]) -> list[dict[str, Any]]:
    """Keep only supported, JSON-safe catalog fields, never arbitrary SDK internals."""
    details: list[dict[str, Any]] = []
    seen: set[str] = set()
    for model in sdk_models:
        if model.hidden:
            continue
        # Model.model is the identifier accepted by thread/start; id is a catalog ID.
        model_id = model.model.strip()
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        payload = model.model_dump(mode="json")
        details.append({
            "id": model_id,
            "catalog_id": model.id,
            "label": model.display_name,
            "description": model.description,
            "is_default": model.is_default,
            "default_reasoning_effort": payload["default_reasoning_effort"],
            "supported_reasoning_efforts": payload["supported_reasoning_efforts"],
            "input_modalities": payload.get("input_modalities"),
            "service_tiers": payload.get("service_tiers") or [],
            "default_service_tier": payload.get("default_service_tier"),
        })
    return details


def _codex_error_code(exc: Exception) -> str:
    """Categorize errors without exposing URLs, config, credentials or stderr."""
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, FileNotFoundError):
        return "command_not_found"
    if isinstance(exc, JsonRpcError):
        message = exc.message.lower()
        if exc.code in (401, 403) or any(
            marker in message for marker in ("unauthorized", "authentication", "not logged in", "login required")
        ):
            return "authentication_failed"
    if isinstance(exc, (ValueError, TypeError)):
        return "invalid_config_or_response"
    return "sdk_error"


def _has_codex_catalog_overrides(config: dict[str, Any]) -> bool:
    # Be conservative: an unscoped disk cache cannot establish which profile/provider
    # produced it. Never merge bundled/default-account choices into a custom catalog.
    return any(
        key.split(".", 1)[0] in {"model_provider", "model_providers", "model_catalog_json", "profile", "profiles"}
        for key in config
    )


def _read_codex_fallback(preset: ModelPreset | None) -> tuple[list[str], str | None, bool]:
    """Read a bounded, matching-home cache only when its provider scope is safe.

    Returns models, original cache timestamp, and whether bundled fallback is safe.
    Cache age/account availability can never be established by its presence alone.
    """
    codex = preset.codex if preset else {}
    if _has_codex_catalog_overrides(codex) or (
        isinstance(codex.get("config"), dict) and _has_codex_catalog_overrides(codex["config"])
    ):
        return [], None, False
    home = Path(
        (preset.codex_home if preset else None) or process_env.get("CODEX_HOME") or "~/.codex"
    ).expanduser()
    # Startup also loads workspace/ancestor config. On failure we cannot ask the
    # SDK for the effective provider, so reject ambiguous fallback conservatively.
    cwd = Path(preset.cwd if preset else os.getcwd()).expanduser().absolute()
    config_paths = [home / "config.toml", *(directory / ".codex/config.toml" for directory in [cwd, *cwd.parents])]
    for config_path in config_paths:
        try:
            with config_path.open("rb") as stream:
                raw_config = stream.read(CAPTURE_LIMIT_BYTES + 1)
            if len(raw_config) > CAPTURE_LIMIT_BYTES or _has_codex_catalog_overrides(tomllib.loads(raw_config.decode())):
                return [], None, False
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            return [], None, False
    try:
        with (home / "models_cache.json").open("rb") as stream:
            raw = stream.read(CAPTURE_LIMIT_BYTES + 1)
        if len(raw) > CAPTURE_LIMIT_BYTES:
            return [], None, True
        payload = json.loads(raw.decode())
        if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
            return [], None, True
        models = list(dict.fromkeys(
            entry["slug"].strip()
            for entry in payload["models"]
            if isinstance(entry, dict) and entry.get("visibility", "list") == "list"
            and isinstance(entry.get("slug"), str) and entry["slug"].strip()
        ))
        fetched_at = None
        timestamp = payload.get("fetched_at")
        if isinstance(timestamp, str):
            try:
                parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                if parsed.tzinfo is not None and parsed <= datetime.now(UTC):
                    fetched_at = parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")
            except ValueError:
                pass
        return models, fetched_at, True
    except (OSError, ValueError):
        return [], None, True


async def _discover_codex_models(preset: ModelPreset | None = None) -> dict[str, Any]:
    """List the effective SDK catalog, with explicitly unverified failure fallbacks.

    SDK model/list can itself use cached/bundled provider data and does not report
    refresh age or account entitlement. A successful RPC is not proof of live access.
    """
    codex = None
    error_code = None
    cleanup_failed = False
    try:
        async with asyncio.timeout(_codex_discovery_timeout(preset)):
            codex = AsyncCodex(_codex_discovery_config(preset))
            catalog = await codex.models(include_hidden=False)
            details = _codex_model_details(catalog.data)
    except Exception as exc:
        error_code = _codex_error_code(exc)
        log.warning("admin.model_discovery.codex.failed", error_code=error_code)
    finally:
        if codex is not None:
            try:
                async with asyncio.timeout(CODEX_CLEANUP_TIMEOUT_SECONDS):
                    await codex.close()
            except Exception:
                cleanup_failed = True
                log.warning("admin.model_discovery.codex.cleanup_failed")

    if error_code is None:
        warning = "SDK が返した catalog です。更新時刻と現在の account での利用可否は未検証です。"
        if catalog.next_cursor:
            warning += " SDK の一覧に続きがあるため、候補は一部のみです。"
        if not details:
            warning += " モデル候補がありません。Model ID を手入力してください。"
        if cleanup_failed:
            warning += " SDK の終了処理を確認できませんでした。"
        result = _response("codex", [model["id"] for model in details], "codex-sdk", warning)
        result.update(
            model_details=details, freshness="sdk_reported", is_fallback=False,
            cache_fetched_at=None, queried_at=result["fetched_at"],
            complete=not bool(catalog.next_cursor),
        )
        return result

    models, cache_fetched_at, allow_bundled = await asyncio.to_thread(_read_codex_fallback, preset)
    if models:
        source, freshness = "codex-cache", "stale"
    elif allow_bundled:
        source, freshness = "bundled_catalog", "unverified"
        models = list(BUNDLED_CATALOGS["codex"])
    else:
        source, freshness = "codex-sdk", "unavailable"
    result = _response(
        "codex", models, source,
        "SDK のモデル一覧を確認できませんでした。cache / bundled catalog は現在の account での利用可否が未検証です。"
        " Model ID を手入力してください。",
        error_code,
    )
    result.update(
        model_details=[], freshness=freshness, is_fallback=True,
        cache_fetched_at=cache_fetched_at, queried_at=result["fetched_at"],
        fetched_at=cache_fetched_at, complete=False,
    )
    return result


def _cursor_model_details(sdk_models: list[Any]) -> list[dict[str, Any]]:
    """Cursor SDKModel の parameter/variant catalog を JSON-safe な形へ変換する。"""
    details: list[dict[str, Any]] = []
    for model in sdk_models:
        model_id = str(getattr(model, "id", "") or "").strip()
        if not model_id:
            continue
        parameters: list[dict[str, Any]] = []
        for parameter in getattr(model, "parameters", ()) or ():
            parameter_id = str(getattr(parameter, "id", "") or "").strip()
            if not parameter_id:
                continue
            values = [
                {
                    "value": str(getattr(entry, "value", "") or ""),
                    "label": str(
                        getattr(entry, "display_name", "")
                        or getattr(entry, "value", "")
                        or ""
                    ),
                }
                for entry in (getattr(parameter, "values", ()) or ())
            ]
            parameters.append(
                {
                    "id": parameter_id,
                    "label": str(getattr(parameter, "display_name", "") or parameter_id),
                    "values": values,
                }
            )
        variants: list[dict[str, Any]] = []
        for variant in getattr(model, "variants", ()) or ():
            variants.append(
                {
                    "label": str(getattr(variant, "display_name", "") or ""),
                    "description": str(getattr(variant, "description", "") or ""),
                    "is_default": bool(getattr(variant, "is_default", False)),
                    "params": [
                        {
                            "id": str(getattr(param, "id", "") or ""),
                            "value": str(getattr(param, "value", "") or ""),
                        }
                        for param in (getattr(variant, "params", ()) or ())
                        if getattr(param, "id", None)
                    ],
                }
            )
        details.append(
            {
                "id": model_id,
                "label": str(getattr(model, "display_name", "") or model_id),
                "description": str(getattr(model, "description", "") or ""),
                "parameters": parameters,
                "variants": variants,
            }
        )
    return details


@dataclass(frozen=True, slots=True)
class _CachedDiscovery:
    expires_at: float
    fingerprint: tuple[Any, ...]
    result: dict[str, Any]


_CONNECTION_DISCOVERY_CACHE: dict[str, _CachedDiscovery] = {}


def clear_connection_discovery_cache() -> None:
    """Clear in-memory per-connection discovery cache (intended for test isolation)."""
    _CONNECTION_DISCOVERY_CACHE.clear()


def _connection_cache_fingerprint(provider: str, conn: Connection) -> tuple[Any, ...]:
    return (
        provider,
        conn.provider,
        conn.kind,
        conn.endpoint,
        conn.secret,
        str(conn.config_dir) if conn.config_dir is not None else None,
        conn.home,
        str(conn.auth_file) if conn.auth_file is not None else None,
    )


def _is_cacheable_connection_result(result: dict[str, Any]) -> bool:
    return (
        result.get("error_code") is None
        and not result.get("is_fallback")
        and result.get("source") != "bundled_catalog"
    )


async def _resolve_connection_secret(conn: Connection) -> tuple[str | None, str | None]:
    """Resolve a Connection's `secret` (`env:` or secret store) without logging its value."""
    if conn.env_name:
        raw = process_env.get(conn.env_name)
        if raw is None:
            return None, f"認証用の環境変数 {conn.env_name} が未定義のため"
        token = raw.strip()
        if not token:
            return None, "認証情報が見つからないため"
        return token, None
    if conn.secret_name:
        try:
            token = (await resolve_secret_async(conn.secret_name)).strip()
        except SecretStoreError:
            return None, "認証情報を解決できないため"
        if not token:
            return None, "認証情報が見つからないため"
        return token, None
    return None, "認証情報が見つからないため"


def _claude_subscription_discovery_env(
    *,
    token: str | None,
    config_dir: str,
) -> dict[str, str]:
    """Build auth env for Claude CLI initialize discovery without inherited ANTHROPIC_*/CLAUDE_CODE_*."""
    env: dict[str, str] = {}
    for key in os.environ:
        if key.startswith("ANTHROPIC_") or key.startswith("CLAUDE_CODE_"):
            env[key] = ""
    env["CLAUDE_CONFIG_DIR"] = config_dir
    if token is not None:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    return env


def _claude_cli_error_code(exc: Exception) -> str:
    """Categorize Claude CLI initialization failures without exposing raw text or tokens."""
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, FileNotFoundError):
        return "command_not_found"
    message = str(exc).lower()
    if any(marker in message for marker in _CLAUDE_LOGIN_REQUIRED_MARKERS):
        return "not_logged_in"
    return "sdk_error"


def _claude_cli_model_details(info: Any) -> list[dict[str, Any]]:
    """Extract unique resolved model IDs and JSON-safe details from Claude initialize info."""
    if not isinstance(info, dict):
        return []
    raw_models = info.get("models")
    if not isinstance(raw_models, list):
        return []

    details: list[dict[str, Any]] = []
    seen: set[str] = set()
    # CLI は先頭に "default" (例: Opus 5.5 を指す) を置く。その項目が先に来ても、同じ
    # model の名前付きの項目 ("opus" → "Opus 5.5") の表示名と説明を後から採る。
    from_default: dict[str, dict[str, Any]] = {}
    for entry in raw_models:
        if not isinstance(entry, dict):
            continue
        resolved = entry.get("resolvedModel")
        value = entry.get("value")
        model_id = ""
        if isinstance(resolved, str) and resolved.strip():
            model_id = resolved.strip()
        elif isinstance(value, str) and value.strip().startswith("claude-"):
            model_id = value.strip()
        if not model_id:
            continue
        if model_id in seen:
            first = from_default.pop(model_id, None)
            named = entry.get("displayName")
            if first is not None and entry.get("value") != "default" and isinstance(named, str) and named.strip():
                first["displayName"] = first["label"] = named.strip()
                described = entry.get("description")
                if isinstance(described, str) and described.strip():
                    first["description"] = described.strip()
            continue
        seen.add(model_id)

        display_name = entry.get("displayName")
        label = display_name.strip() if isinstance(display_name, str) and display_name.strip() else model_id
        description = entry.get("description")
        effort_levels = entry.get("supportedEffortLevels")
        details.append(
            {
                "id": model_id,
                "displayName": label,
                "label": label,
                "description": description.strip() if isinstance(description, str) else "",
                "supportedEffortLevels": (
                    [level for level in effort_levels if isinstance(level, str)]
                    if isinstance(effort_levels, list)
                    else []
                ),
                "supportsFastMode": bool(entry.get("supportsFastMode", False)),
            }
        )
        if entry.get("value") == "default":
            from_default[model_id] = details[-1]
    return details


async def _discover_claude_subscription_models(conn: Connection, binary: Path | None) -> dict[str, Any]:
    """Query the official Claude Code CLI initialize handshake for subscription models."""
    if conn.config_dir is not None:
        if not Path(conn.config_dir).is_dir():
            log.warning("admin.model_discovery.claude_cli.failed", error_code="not_logged_in")
            return _bundled_fallback("claude", "Claude CLI にログインしていないため")
        token = None
    else:
        token, _ = await _resolve_connection_secret(conn)
        if not token:
            log.warning("admin.model_discovery.claude_cli.failed", error_code="authentication_missing")
            return _bundled_fallback("claude", "Claude CLI にログインしていないため")

    with tempfile.TemporaryDirectory(prefix="kotoyose-claude-discovery-") as tmp_root:
        work_dir = Path(tmp_root) / "cwd"
        work_dir.mkdir(mode=0o700)
        if conn.config_dir is not None:
            auth_env = _claude_subscription_discovery_env(token=None, config_dir=str(conn.config_dir))
        else:
            empty_cfg_dir = Path(tmp_root) / "config"
            empty_cfg_dir.mkdir(mode=0o700)
            auth_env = _claude_subscription_discovery_env(token=token, config_dir=str(empty_cfg_dir))

        try:
            cli = binary if binary is not None else bundled_claude_cli()
        except ValueError as exc:
            return _bundled_fallback("claude", str(exc))
        options = ClaudeAgentOptions(
            cli_path=cli,
            cwd=str(work_dir),
            env=auth_env,
            setting_sources=[],
            tools=[],
        )
        client = ClaudeSDKClient(options)
        info: dict[str, Any] | None = None
        try:
            async with asyncio.timeout(CLAUDE_CLI_DISCOVERY_TIMEOUT_SECONDS):
                await client.connect()
                info = await client.get_server_info()
        except TimeoutError:
            log.warning("admin.model_discovery.claude_cli.failed", error_code="timeout")
            return _bundled_fallback("claude", "モデル一覧の取得に失敗したため")
        except Exception as exc:
            error_code = _claude_cli_error_code(exc)
            log.warning("admin.model_discovery.claude_cli.failed", error_code=error_code)
            if error_code == "not_logged_in":
                return _bundled_fallback("claude", "Claude CLI にログインしていないため")
            return _bundled_fallback("claude", "モデル一覧の取得に失敗したため")
        finally:
            with contextlib.suppress(Exception):
                async with asyncio.timeout(CODEX_CLEANUP_TIMEOUT_SECONDS):
                    await client.disconnect()

    details = _claude_cli_model_details(info)
    if not details:
        if isinstance(info, dict) and (
            info.get("loggedIn") is False
            or info.get("authenticated") is False
            or any(
                marker in str(info.get("error") or "").lower()
                for marker in _CLAUDE_LOGIN_REQUIRED_MARKERS
            )
        ):
            return _bundled_fallback("claude", "Claude CLI にログインしていないため")
        return _bundled_fallback("claude", "モデル一覧の取得に失敗したため")

    result = _response("claude", [item["id"] for item in details], "claude_cli_initialize")
    result["model_details"] = details
    return result


_CURSOR_AUTH_ERROR_MARKERS = (
    "unauthorized",
    "unauthenticated",
    "authentication_failed",
    "authentication failed",
    "invalid api key",
    "invalid_api_key",
    "api key rejected",
    "permission denied",
    "forbidden",
    "401",
    "403",
)


def _is_cursor_auth_error(exc: BaseException) -> bool:
    """Return True if a Cursor SDK exception represents an API key / auth rejection."""
    if isinstance(exc, (CursorAuthenticationError, CursorPermissionDeniedError)):
        return True
    for attr in ("status_code", "http_status_code", "code"):
        val = getattr(exc, attr, None)
        if val in (401, 403, "401", "403", "authentication_failed", "unauthorized", "unauthenticated", "invalid_api_key"):
            return True
    message = str(exc).lower()
    return any(marker in message for marker in _CURSOR_AUTH_ERROR_MARKERS)


def _is_cursor_rate_limit_error(exc: BaseException) -> bool:
    """Return True if a Cursor SDK exception represents rate limiting (HTTP 429)."""
    if isinstance(exc, CursorRateLimitError):
        return True
    for attr in ("status_code", "http_status_code", "code"):
        if getattr(exc, attr, None) in (429, "429", "rate_limited"):
            return True
    return "rate limit" in str(exc).lower() or "429" in str(exc)


async def _discover_cursor_models(conn: Connection | None = None) -> dict[str, Any]:
    """Cursor SDK bridge 経由で利用可能な model ID を取得する。"""
    source = "cursor-sdk"
    if conn is None:
        conn = get_config().cursor_connection()
    if conn is not None and conn.secret_name:
        try:
            api_key = await resolve_secret_async(conn.secret_name)
        except SecretStoreError:
            log.warning("admin.model_discovery.cursor.failed", error_code="authentication_missing")
            return _response(
                "cursor", [], source,
                f"Cursor secret '{conn.secret_name}' を解決できません。Model ID を手入力してください。",
                "authentication_missing",
            )
    elif conn is not None and conn.env_name:
        api_key = process_env.get(conn.env_name) or None
    else:
        api_key = process_env.get("CURSOR_API_KEY") or None
    if not api_key:
        log.warning("admin.model_discovery.cursor.failed", error_code="authentication_missing")
        return _response(
            "cursor", [], source,
            "CURSOR_API_KEY が未設定です。Model ID を手入力してください。",
            "authentication_missing",
        )

    client = None
    try:
        async with asyncio.timeout(DISCOVERY_TIMEOUT_SECONDS):
            client = await AsyncClient.launch_bridge()
            sdk_models = await AsyncCursor.models.list(client=client, api_key=api_key)
    except TimeoutError:
        log.warning("admin.model_discovery.cursor.failed", error_code="timeout")
        return _response(
            "cursor", [], source,
            "モデル一覧の取得が timeout しました。Model ID を手入力してください。",
            "timeout",
        )
    except Exception as exc:
        if _is_cursor_auth_error(exc):
            log.warning("admin.model_discovery.cursor.failed", error_code="authentication_failed")
            return _response(
                "cursor", [], source,
                "Cursor API key の認証に失敗しました。Model ID を手入力してください。",
                "authentication_failed",
            )
        if _is_cursor_rate_limit_error(exc):
            log.warning("admin.model_discovery.cursor.failed", error_code="rate_limited")
            return _response(
                "cursor", [], source,
                "Cursor API がレート制限中です。Model ID を手入力してください。",
                "rate_limited",
            )
        log.warning("admin.model_discovery.cursor.failed", error_code="spawn_failed")
        return _response(
            "cursor", [], source,
            "モデル一覧を取得できませんでした。Model ID を手入力してください。",
            "spawn_failed",
        )
    finally:
        if client is not None:
            with contextlib.suppress(Exception):
                await client.aclose()

    models = [model.id for model in sdk_models if model.id]
    warning = None if models else "モデル候補がありません。Model ID を手入力してください。"
    result = _response("cursor", models, source, warning, None if models else "empty_result")
    result["model_details"] = _cursor_model_details(sdk_models)
    return result


async def _discover_print_cli_models(
    provider: str, conn: Connection, profile: PrintCliProfile, binary: Path | None,
) -> dict[str, Any]:
    """Discover models using the command and output hook declared by the profile."""
    if not profile.models_command or profile.parse_models is None:
        return _bundled_fallback(provider, "モデル一覧コマンドがないため")
    label = profile.display_name or profile.id
    if conn.config_dir is not None and not conn.config_dir.is_dir():
        log.warning("admin.model_discovery.print_cli.failed", error_code="not_logged_in")
        return _bundled_fallback(provider, f"{label} CLI にログインしていないため")
    try:
        if binary is None:
            raise FileNotFoundError(profile.binary)
        with use_login_home(conn.config_dir or Path.home()):
            command = (str(binary), *profile.models_command)
            if conn.config_dir is None and not profile.fixed_env:
                return_code, stdout, _stderr = await _run_command(*command)
            else:
                env = build_print_cli_env(profile, conn.config_dir or Path.home())
                return_code, stdout, _stderr = await _run_command(*command, env=env)
    except PrintCliHomeBusyError as exc:
        return _bundled_fallback(provider, str(exc))
    except FileNotFoundError:
        log.warning(
            "admin.model_discovery.print_cli.failed",
            error_code="command_not_found",
        )
        return _bundled_fallback(provider, f"{label} CLI が見つからないため")
    except TimeoutError:
        log.warning(
            "admin.model_discovery.print_cli.failed",
            error_code="timeout",
        )
        return _bundled_fallback(provider, "モデル一覧の取得がタイムアウトしたため")
    except OSError:
        log.warning(
            "admin.model_discovery.print_cli.failed",
            error_code="spawn_failed",
        )
        return _bundled_fallback(provider, "モデル一覧の取得に失敗したため")

    if return_code != 0:
        log.warning(
            "admin.model_discovery.print_cli.failed",
            exit_code=return_code,
            error_code="nonzero_exit",
        )
        return _bundled_fallback(provider, "モデル一覧の取得に失敗したため")

    details = get_models_parser(profile.parse_models)(stdout)
    if not details:
        return _bundled_fallback(provider, "取得結果が空だったため")

    result = _response(provider, [item["id"] for item in details], profile.models_source or f"{profile.id}_cli_models")
    result["model_details"] = details
    return result


async def _discover_connection_models_uncached(
    provider: str, conn: Connection, profile: PrintCliProfile | None, binary: Path | None,
) -> dict[str, Any]:
    """Dispatch model discovery for a validated Connection based on its kind and provider."""
    if conn.kind == "endpoint":
        token, reason = await _resolve_connection_secret(conn)
        if not token or not conn.endpoint:
            return _bundled_fallback(provider, reason or "認証情報が見つからないため")
        return await _discover_endpoint_models(provider, conn.endpoint, token)

    if conn.kind == "api_key":
        if provider == "cursor":
            return await _discover_cursor_models(conn)
        token, reason = await _resolve_connection_secret(conn)
        if not token:
            return _bundled_fallback(provider, reason or "認証情報が見つからないため")
        if provider == "claude":
            return await _discover_anthropic_api_models(token)
        if provider == "codex":
            return _response(
                "codex",
                list(BUNDLED_CATALOGS["codex"]),
                "bundled_catalog",
                "この候補は bundled catalog です。現在の account での利用可否は未検証です。",
            )
        return await _discover_provider_default_models(provider)

    if conn.kind == "subscription":
        if profile is not None:
            return await _discover_print_cli_models(provider, conn, profile, binary)
        if provider == "claude":
            return await _discover_claude_subscription_models(conn, binary)
        if provider == "codex":
            codex_home, _ = codex_home_and_auth_path(conn)
            # ModelPreset の既定 cwd (~/kotoyose-workspace) は機械に無いことがあり、無いと
            # app-server の起動が command_not_found で落ちる。一覧の取得に作業場は要らない。
            with tempfile.TemporaryDirectory(prefix="kotoyose-codex-discovery-") as cwd:
                preset = ModelPreset(provider="codex", codex_home=codex_home, cwd=cwd)
                return await _discover_codex_models(preset)
        return await _discover_provider_default_models(provider)

    return await _discover_provider_default_models(provider)


async def _discover_connection_models(
    provider: str,
    connection_id: str,
    conn: Connection,
    profile: PrintCliProfile | None,
    binary: Path | None,
) -> dict[str, Any]:
    """Resolve model candidates for a Connection with a 10-minute success cache."""
    fingerprint = _connection_cache_fingerprint(provider, conn)
    if profile is not None:
        fingerprint += (profile.profile_digest(), binary)
    elif provider == "claude":
        fingerprint += (binary,)
    now = time.monotonic()
    cached = _CONNECTION_DISCOVERY_CACHE.get(connection_id)
    if cached is not None and cached.fingerprint == fingerprint and now < cached.expires_at:
        return copy.deepcopy(cached.result)

    result = await _discover_connection_models_uncached(provider, conn, profile, binary)
    if _is_cacheable_connection_result(result):
        _CONNECTION_DISCOVERY_CACHE[connection_id] = _CachedDiscovery(
            expires_at=time.monotonic() + CONNECTION_DISCOVERY_CACHE_TTL_SECONDS,
            fingerprint=fingerprint,
            result=copy.deepcopy(result),
        )
    return result


async def _discover_provider_default_models(provider: str) -> dict[str, Any]:
    """Run CLI/bundled discovery for providers without connection-specific handling."""
    profile = get_print_cli_profile_for_provider(provider)
    command = (
        (profile.binary, *profile.models_command)
        if profile is not None and profile.models_command else CLI_COMMANDS.get(provider)
    )
    if command is None:
        return _response(
            provider,
            list(BUNDLED_CATALOGS.get(provider, [])),
            "bundled_catalog",
            "この候補は bundled catalog です。現在の account での利用可否は未検証です。",
        )

    source = " ".join(command)
    try:
        return_code, stdout, stderr = await _run_command(*command)
    except FileNotFoundError:
        log.warning(
            "admin.model_discovery.failed",
            provider=provider,
            error_code="command_not_found",
        )
        return _response(
            provider,
            [],
            source,
            "モデル一覧を取得できませんでした。Model ID を手入力してください。",
            "command_not_found",
        )
    except TimeoutError:
        log.warning("admin.model_discovery.failed", provider=provider, error_code="timeout")
        return _response(
            provider,
            [],
            source,
            "モデル一覧の取得が timeout しました。Model ID を手入力してください。",
            "timeout",
        )
    except OSError:
        log.warning(
            "admin.model_discovery.failed",
            provider=provider,
            error_code="spawn_failed",
        )
        return _response(
            provider,
            [],
            source,
            "モデル一覧を取得できませんでした。Model ID を手入力してください。",
            "spawn_failed",
        )

    if return_code != 0:
        log.warning(
            "admin.model_discovery.failed",
            provider=provider,
            exit_code=return_code,
            error_code="nonzero_exit",
        )
        return _response(
            provider,
            [],
            source,
            "モデル一覧の取得に失敗しました。Model ID を手入力してください。",
            "nonzero_exit",
        )

    models = _extract_models(stdout)
    warning = None if models else "モデル候補がありません。Model ID を手入力してください。"
    error_code = None if models else "empty_result"
    return _response(provider, models, source, warning, error_code)


async def discover_models(
    provider: str,
    endpoint_base_url: str | None = None,
    endpoint_api_key_env: str | None = None,
    preset: ModelPreset | None = None,
    connection: str | None = None,
    *,
    execution_service: Any = None,
) -> dict[str, Any]:
    """Provider の model 候補を返し、取得障害は警告へ変換する。

    Args:
        provider: 対象 Provider ID。
        endpoint_base_url: route に明示された endpoint の base URL。
        endpoint_api_key_env: endpoint の token を読む環境変数名。
        preset: 選択 alias の route を解決した設定。Codex SDK の実効 catalog に使う。
        connection: 接続 ID。指定時はその connection の kind に応じて候補を取得する。

    Returns:
        provider、models、source、fetched_at、warning を含む辞書。

    Raises:
        ValueError: 未対応 Provider や不正な接続指定が渡された場合。
    """
    if provider not in SUPPORTED_PROVIDERS and not any(p.provider == provider for p in list_print_cli_profiles()):
        raise ValueError(f"Unsupported provider: {provider}")

    if preset is not None and preset.route is not None:
        cfg = get_config()
        route = cfg.routes.get(preset.route)
        if route is not None and route.profile is not None:
            if connection is not None or endpoint_base_url is not None or endpoint_api_key_env is not None:
                raise ValueError("print CLI preset cannot be combined with endpoint overrides or connection")
            connection = route.connection
            preset = None

    if connection is not None:
        if endpoint_base_url is not None or endpoint_api_key_env is not None or preset is not None:
            raise ValueError("connection cannot be combined with endpoint overrides or preset")
        cfg = get_config()
        conn = cfg.connections.get(connection)
        if conn is None:
            raise ValueError(f"Unknown connection: {connection}")
        if conn.provider.lower() != provider:
            raise ValueError("Connection belongs to another provider")
        profile = cfg.print_cli_profile_for_connection(connection)
        binary = None
        if profile is not None or (provider == "claude" and conn.kind == "subscription"):
            binary = resolve_connection_cli_binary(cfg, connection, conn, execution_service)
        return await _discover_connection_models(provider, connection, conn, profile, binary)

    if provider == "claude":
        return await _discover_claude_models(endpoint_base_url, endpoint_api_key_env)
    if provider == "codex":
        if preset is not None:
            if endpoint_base_url or endpoint_api_key_env:
                raise ValueError("Select either a model alias or an explicit endpoint for Codex discovery")
            if preset.provider != "codex":
                raise ValueError("Codex discovery requires a codex preset")
            return await _discover_codex_models(preset)
        if endpoint_base_url:
            token = process_env.get(endpoint_api_key_env) if endpoint_api_key_env else None
            if endpoint_api_key_env and token is None:
                return _bundled_fallback(
                    "codex",
                    f"認証用の環境変数 {endpoint_api_key_env} が未定義のため",
                )
            return await _discover_endpoint_models("codex", endpoint_base_url, token)
        return await _discover_codex_models()
    if provider == "cursor":
        return await _discover_cursor_models()

    return await _discover_provider_default_models(provider)
