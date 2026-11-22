"""
セッション管理モジュール。

会話キーに対応する Claude クライアントをインメモリ辞書で管理し、
Postgres にも session_id を永続化する。

v3 改修 (session_store 統合):
  get_or_resume_client — v1/v2 統合エントリ: DB lookup → SDK resume= 注入
  replay_messages 削除 — SDK の session_store が履歴を復元するため不要
  stream_claude        — ストリーミング用の非同期ジェネレータ
"""

import asyncio
import contextvars
import dataclasses
import functools
import hashlib
import json
import time
from collections.abc import Mapping
from contextlib import contextmanager
from typing import TYPE_CHECKING

from claude_agent_sdk import ClaudeSDKClient

from . import delegation
from .data_policy import alias_data_policy, configured_capabilities, training_binding
from .execution_resources import ExecutionError, NativeExecutionConfig
from .config import (
    SESSION_TTL_SECONDS,
    ClaudeSubscriptionAuthError,
    HarnessConfig,
    ModelPreset,
    build_options,
    get_config,
    resolve_preset_secrets_async,
    resolve_preset_secrets_with_versions_async,
)
from .connections_probe import connection_for_model
from .db import lookup_runtime_session, lookup_session
from .logging_setup import log
from .secret_store import SecretStoreError, use_resolved_secrets
from .runtime.antigravity_sdk import build_antigravity_sdk_session
from .registry import registry
from .runtime.acp import acp_binding, build_acp_session
from .runtime.print_cli import build_print_cli_session
from .runtime.base import (
    RuntimePrompt, RuntimeProviderSpec, RuntimeResult, RuntimeSession, prompt_text,
)
from .runtime.claude import ClaudeRuntimeSession
from .runtime.claude_auth import validate_auth_options
from .runtime.codex import build_codex_session
from .runtime.cursor import build_cursor_session

if TYPE_CHECKING:
    from .chat_selection import PinnedChatSelection

# { 会話キー: 接続済みの RuntimeSession }
sessions: dict[str, RuntimeSession] = {}

# { 会話キー: 最終使用の Unix タイムスタンプ }
session_last_used: dict[str, float] = {}

# { 会話キー: (provider, preset 名, runtime 設定の digest) }
# 認証 header の生値を保持せず、実 model を含む runtime 設定の違う接続を共有しない。
session_cache_identities: dict[str, tuple[str, str, str]] = {}


class SDKConnectionError(Exception):
    """Claude SDK の接続失敗を示す例外。503 または認証失敗時の 401 を返すために使用する。"""

    def __init__(self, message: str = "", *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


# host runtime の provider 表は registry が持つ。組み込み runtime はここで登録する。
RUNTIME_PROVIDER_REGISTRY: dict[str, RuntimeProviderSpec] = registry.runtime_providers
for _name, _builder in (
    ("cli_acp", build_acp_session),
    ("codex", build_codex_session), ("cursor", build_cursor_session),
    ("antigravity", build_antigravity_sdk_session),
):
    registry.register_builtin_runtime(_name, RuntimeProviderSpec(_builder, _name))
del _name, _builder
registry.register_builtin_runtime(
    "print_cli", RuntimeProviderSpec(build_print_cli_session, "print_cli", stateless=True),
)


# ── 共通ヘルパー ───────────────────────────────────────────────────

def _touch(key: str) -> None:
    """session_last_used を現在時刻で更新する。"""
    session_last_used[key] = time.time()


def discard_runtime(key: str | None, client: RuntimeSession) -> None:
    if key is not None and sessions.get(key) is client:
        sessions.pop(key)
        session_last_used.pop(key, None)
        session_cache_identities.pop(key, None)


# ── TTL 管理 ───────────────────────────────────────────────────────

async def evict_expired_sessions(ttl_seconds: int) -> None:
    """
    TTL を超えたセッションを切断してメモリから削除する。
    app.py の TTL バックグラウンドタスクから定期的に呼ばれる。
    """
    delegation.prune_expired(time.monotonic())
    now = time.time()
    expired = [
        key for key, last in session_last_used.items()
        if now - last > ttl_seconds
    ]
    for key in expired:
        client = sessions.pop(key, None)
        session_last_used.pop(key, None)
        session_cache_identities.pop(key, None)
        if client:
            try:
                await client.disconnect()
            except Exception as e:
                log.warning(
                    "session.ttl.disconnect.failed",
                    conversation_key=key,
                    error=str(e),
                )
        log.info("session.expired", conversation_key=key)


# ── 統合エントリ (v1 ヘッダパス / v2 ハッシュパス共通) ────────────


async def get_or_resume_client(
    lookup_key: str | None,
    model: str,
    cwd_override: str | None = None,
    request_headers: dict[str, str] | None = None,
    *, selection: "PinnedChatSelection | None" = None, config_snapshot: HarnessConfig | None = None,
    execution_settings: NativeExecutionConfig | None = None,
    reasoning_effort: str | None = None,
    delegation_parent: delegation.DelegationParent | None = None,
    delegated: bool = False,
) -> tuple[RuntimeSession, bool]:
    """会話キーに対応する runtime session を返す。

    v1 ヘッダパス (X-Conversation-Key 由来の固定キー) と
    v2 ハッシュパス (messages[:-1] のハッシュ) の両方の入口がこの関数を使う。
    呼び出し側は lookup_key の導出方法だけ責務を持つ。

    Args:
        lookup_key: セッション検索キー。None の場合は v2 ハッシュパスの初回リクエスト。
        model: preset キー。build_options に転送する。
        cwd_override: request body の cwd フィールド。クライアント生成時のみ適用される。
            インメモリキャッシュヒット時は無視される (TaskHub の conversation key は
            run 単位で一意 = "taskhub:task:<id>:run:<id>" なので問題ない)。

    Returns:
        (client, was_resumed):
            was_resumed=True なら DB に既存 runtime session id があり provider の
            resume 相当へ渡された (= chat.py の session_event="resumed")。
            False なら新規セッション (= "created" or "first")。

    フロー:
        1. インメモリキャッシュにあり、provider が一致すればそれを返す (高速パス,
           was_resumed=False)
        2. キャッシュはあるが provider が異なる場合 (preset 切替で同じ会話キーに
           別 provider が飛んできたケース) は、旧セッションを disconnect した上で
           sessions / session_last_used から破棄し、「キャッシュなし」として
           以下の新規生成/resume パスへ合流させる (誤って旧 provider のセッションを
           返す・リークさせるのを防ぐため)
        3. なければ DB を照会して claude_session_id があれば SDK の resume= に渡す
        4. SDK は session_store (Postgres) から履歴を load し、最新発言だけを Claude へ
        5. それもなければ新規セッションを作成

    なお、build_options() で session_store が ClaudeAgentOptions に注入されるため、
    過去の replay_messages による N 倍 Claude API 呼び出しは不要になっている。
    """
    if selection is not None and selection.preset_key != model:
        raise SDKConnectionError("Pinned PM preset does not match the requested alias")
    config = None if selection is not None else (config_snapshot or get_config())
    preset = selection.preset if selection is not None else config.preset_for(model)
    binding = None
    if config is not None:
        decision = alias_data_policy(config, model)
        if decision.restricted:
            declared = config.models[model]
            if message := decision.refusal(configured_capabilities(declared)):
                raise ExecutionError("permission_denied", message, 403, reason_code="data_policy_restricted")
            if preset.provider != "claude":
                raise ExecutionError(
                    "permission_denied", f"Alias '{model}' requires a chat runtime with enforceable data policy", 403,
                    reason_code="data_policy_restricted",
                )
            binding = training_binding(config, model)
    # This session path has no admitted outer jail. Reject before cache lookup,
    # database resume, auth construction, or any provider launch; never fall back.
    if preset.execution_environment == "isolated":
        raise SDKConnectionError("Isolated execution requires the admitted Agent runtime; ordinary chat cannot run this preset")
    try:
        preset = preset.with_execution_environment()
    except ValueError as exc:
        raise SDKConnectionError(str(exc)) from exc
    provider = preset.provider
    spec = RUNTIME_PROVIDER_REGISTRY.get(provider)
    if spec is not None and spec.stateless:
        return await _build_registered_runtime_session(
            provider=provider,
            lookup_key=None,
            model=model,
            preset=preset,
            cwd_override=cwd_override,
            request_headers=request_headers,
            allow_persisted_resume=False,
            session_identity=None,
            preloaded_secrets={},
            **({"builder_options": {"config": config, "settings": execution_settings}}
               if provider == "print_cli" else {}),
            effort_override=reasoning_effort,
        )
    preloaded_secrets: dict[str, str] = {}
    secret_versions: dict[str, int] = {}
    if selection is None:
        try:
            preloaded_secrets, secret_versions = await resolve_preset_secrets_with_versions_async(
                config, preset
            )
        except ClaudeSubscriptionAuthError as exc:
            raise SDKConnectionError(str(exc), code="authentication_failed") from exc
        except SecretStoreError as exc:
            raise SDKConnectionError(str(exc)) from exc
    if delegated or delegation.MCP_NAME not in preset.mcp_refs:
        delegation_parent = None
    requested_identity = selection.identity if selection is not None else _cache_identity(
        provider, model, preset.model, preset.required_request_headers, request_headers,
        output_schema=preset.output_schema,
        execution_environment=preset.execution_environment,
        codex_sandbox=preset.codex.get("sandbox") if provider == "codex" else None,
        **({"secret_versions": secret_versions} if secret_versions else {}),
        training_binding=binding,
        acp_scope=acp_binding(config, preset, cwd_override) if provider == "cli_acp" else None,
        reasoning_effort=reasoning_effort,
    )
    if delegation_parent:
        requested_identity = delegation_identity(requested_identity, delegation_parent.client_id)
    identity_mismatch = False

    if lookup_key and lookup_key in sessions:
        cached = sessions[lookup_key]
        cached_provider = getattr(cached, "provider", "claude")
        cached_identity = session_cache_identities.get(lookup_key)
        if (
            cached_provider != provider
            or cached_identity != requested_identity
            or getattr(cached, "invalidated", False) is True
        ):
            identity_mismatch = True
            log.warning(
                "session.cache_identity_mismatch",
                conversation_key=lookup_key,
                cached_provider=cached_provider,
                requested_provider=provider,
            )
            # 旧 provider のセッションをそのまま上書き/返却すると接続リークや
            # 誤セッション返却につながるため、後始末してから「キャッシュなし」
            # として通常の新規生成/resume パスへ合流させる。
            disconnect = getattr(cached, "disconnect", None)
            if disconnect is not None:
                try:
                    await disconnect()
                except Exception as e:
                    log.warning(
                        "session.mismatch.disconnect.failed",
                        conversation_key=lookup_key,
                        cached_provider=cached_provider,
                        error=str(e),
                    )
            discard_runtime(lookup_key, cached)
        else:
            _touch(lookup_key)
            log.info("session.cache_hit", conversation_key=lookup_key, provider=provider)
            return cached, False

    if provider in RUNTIME_PROVIDER_REGISTRY:  # registry.runtime_providers と同じ dict (tests が差し替える)
        client, was_resumed = await _build_registered_runtime_session(
            provider=provider,
            lookup_key=lookup_key,
            model=model,
            preset=preset,
            cwd_override=cwd_override,
            request_headers=request_headers,
            allow_persisted_resume=not identity_mismatch,
            session_identity=requested_identity[2],
            preloaded_secrets=preloaded_secrets,
            delegation_parent=delegation_parent,
            **({"builder_options": {"config": config, "settings": execution_settings}}
               if provider == "cli_acp" else {}),
            effort_override=reasoning_effort,
        )
        if lookup_key:
            session_cache_identities[lookup_key] = requested_identity
        return client, was_resumed

    if provider != "claude":
        raise SDKConnectionError(f"provider '{provider}' はまだ実装されていません")

    existing_session_id: str | None = None
    # identity mismatch 後は同じ conversation key でも別の権限境界なので resume しない。
    # token digest は DB metadata に保存され、再起動後も一致した project だけを復帰できる。
    can_resume_persisted_session = not identity_mismatch
    if lookup_key and can_resume_persisted_session:
        existing_session_id = await lookup_session(
            lookup_key,
            provider=provider,
            model=model,
            session_identity=requested_identity[2],
        )

    with _new_delegation_grant(delegation_parent, model, lookup_key) as (token, grant):
        runtime_client = await _build_claude_runtime_session(
            model, preset, selection, config, config_snapshot, cwd_override, request_headers,
            preloaded_secrets, existing_session_id, lookup_key, token, reasoning_effort,
        )
        runtime_client = _bind_delegation_runtime(runtime_client, grant)
    if lookup_key:
        sessions[lookup_key] = runtime_client
        _touch(lookup_key)
        session_cache_identities[lookup_key] = requested_identity
    return runtime_client, bool(existing_session_id)


@contextmanager
def _new_delegation_grant(parent: delegation.DelegationParent | None, alias: str, key: str | None):
    if parent is None:
        yield None, None
        return
    token, grant = delegation.issue(
        parent.client_id, alias, key, parent.allowed_aliases,
        parent_depth=0, ttl=SESSION_TTL_SECONDS, api_key_name=parent.api_key_name,
    )
    try:
        yield token, grant
    except BaseException:
        delegation.revoke(grant)
        raise


def _bind_delegation_runtime(runtime: RuntimeSession, grant: delegation.DelegationGrant | None) -> RuntimeSession:
    if grant is None:
        return runtime
    grant.runtime = runtime
    return delegation.DelegationRuntime(runtime, grant)


def delegation_identity(identity: tuple[str, str, str], client_id: str) -> tuple[str, str, str]:
    provider, model, digest = identity
    scoped = delegation.fingerprint(f"delegation:{digest}:{client_id}").hex()
    return provider, model, scoped


async def _build_claude_runtime_session(
    model: str, preset: ModelPreset, selection: "PinnedChatSelection | None",
    config: HarnessConfig | None, config_snapshot: HarnessConfig | None,
    cwd_override: str | None, request_headers: dict[str, str] | None,
    preloaded_secrets: dict[str, str], existing_session_id: str | None,
    lookup_key: str | None, token: str | None, reasoning_effort: str | None = None,
) -> RuntimeSession:
    # build_options は _build_auth_env 経由で secret store 解決等を行うため、
    # run_in_executor で worker thread に退避してイベントループのブロックを避ける。
    if selection is not None:
        # Use the exact options whose route/policy identity was checked before
        # cache lookup. Config reloads cannot change this request's execution.
        options = selection.options
        if token:
            servers = dict(options.mcp_servers)
            with delegation.runtime_credentials(token):
                delegation.inject_delegation_server(servers)
            options = dataclasses.replace(options, mcp_servers=servers)
    else:
        loop = asyncio.get_running_loop()

        def _build() -> object:
            kwargs: dict[str, object] = {
                "cwd_override": cwd_override,
                "request_headers": request_headers,
            }
            if preloaded_secrets:
                kwargs["resolved_secrets"] = preloaded_secrets
            if config_snapshot is not None or preset.route is not None:
                kwargs["config"] = config
            if reasoning_effort is not None:
                kwargs["effort_override"] = reasoning_effort
            with use_resolved_secrets(preloaded_secrets), delegation.runtime_credentials(token):
                return build_options(model, **kwargs)

        ctx = contextvars.copy_context()
        try:
            options = await loop.run_in_executor(None, functools.partial(ctx.run, _build))
        except ClaudeSubscriptionAuthError as exc:
            raise SDKConnectionError(str(exc), code="authentication_failed") from exc
        except SecretStoreError as exc:
            raise SDKConnectionError(str(exc)) from exc
    try:
        validate_auth_options(options)
    except ValueError as exc:
        raise SDKConnectionError(str(exc)) from exc
    if existing_session_id:
        options = dataclasses.replace(options, resume=existing_session_id)
        log.info(
            "session.resumed",
            conversation_key=lookup_key,
            claude_session_id=existing_session_id,
        )
    else:
        log.info("session.created", conversation_key=lookup_key or "first")

    client = ClaudeSDKClient(options=options)
    try:
        await client.connect()
    except asyncio.CancelledError:
        await client.disconnect()
        raise
    except Exception as e:
        raise SDKConnectionError(delegation.redact_token(str(e))) from None
    runtime_client = ClaudeRuntimeSession(
        client=client,
        timeout_seconds=preset.timeout_seconds,
        connection_id=(selection.connection_id if selection is not None
                       else connection_for_model(config, model)),
    )
    if preset.output_schema is not None:
        from .structured_output import StructuredOutputSession
        runtime_client = StructuredOutputSession(runtime_client, preset.output_schema)

    return runtime_client


def _cache_identity(
    provider: str,
    preset_key: str,
    runtime_model: str | None,
    required_headers: list[str],
    request_headers: dict[str, str] | None,
    *, output_schema: dict | None = None,
    execution_environment: str | None = None,
    codex_sandbox: object = None,
    secret_versions: Mapping[str, int] | None = None,
    training_binding: str | None = None,
    acp_scope: str | None = None,
    reasoning_effort: str | None = None,
) -> tuple[str, str, str]:
    """session cache が安全に再利用できる runtime 設定の同一性を返す。"""
    normalized = {key.lower(): value for key, value in (request_headers or {}).items()}
    digest_source = json.dumps(
        {
            "provider": provider,
            "preset_key": preset_key,
            "runtime_model": runtime_model or "",
            **({"acp_scope": acp_scope} if acp_scope else {}),
            **({"training_binding": training_binding} if training_binding else {}),
            **({"output_schema": output_schema} if output_schema is not None else {}),
            **({"execution_environment": execution_environment,
                "codex_sandbox": codex_sandbox} if execution_environment is not None else {}),
            **({"secret_versions": sorted((name, int(ver)) for name, ver in secret_versions.items())}
               if secret_versions else {}),
            **({"reasoning_effort": reasoning_effort} if reasoning_effort is not None else {}),
            "required_headers": [
                (name.lower(), normalized.get(name.lower(), ""))
                for name in sorted(required_headers, key=str.lower)
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return provider, preset_key, hashlib.sha256(digest_source.encode("utf-8")).hexdigest()


async def _build_registered_runtime_session(
    provider: str,
    lookup_key: str | None,
    model: str,
    preset: ModelPreset,
    cwd_override: str | None,
    request_headers: dict[str, str] | None,
    allow_persisted_resume: bool,
    session_identity: str | None,
    preloaded_secrets: Mapping[str, str] | None = None,
    builder_options: dict | None = None,
    effort_override: str | None = None,
    delegation_parent: delegation.DelegationParent | None = None,
) -> tuple[RuntimeSession, bool]:
    """registry に登録済みの subprocess provider session を構築する。"""
    spec = RUNTIME_PROVIDER_REGISTRY[provider]
    existing_session_id: str | None = None
    if lookup_key and allow_persisted_resume:
        existing_session_id = await lookup_runtime_session(
            lookup_key,
            provider=spec.session_provider,
            model=model,
            session_identity=session_identity,
        )
    if provider == "cli_acp" and lookup_key:
        # DB lookup の間に同じ会話が初期化されていれば、同じ runtime を使う。
        cached = sessions.get(lookup_key)
        if (cached is not None and not getattr(cached, "invalidated", False)
                and session_cache_identities.get(lookup_key) == (provider, model, session_identity)):
            _touch(lookup_key)
            return cached, False
    try:
        secrets_map = (
            preloaded_secrets
            if preloaded_secrets is not None
            else await resolve_preset_secrets_async(get_config(), preset)
        )
        builder_kwargs = dict(builder_options or {})
        if effort_override is not None:
            builder_kwargs["effort_override"] = effort_override
        with _new_delegation_grant(delegation_parent, model, lookup_key) as (token, grant):
            with use_resolved_secrets(secrets_map), delegation.runtime_credentials(token):
                client = spec.builder(preset, cwd_override, existing_session_id, request_headers, **builder_kwargs)
            if preset.output_schema is not None:
                from .structured_output import StructuredOutputSession
                client = StructuredOutputSession(client, preset.output_schema)
            client = _bind_delegation_runtime(client, grant)
    except SecretStoreError as exc:
        raise SDKConnectionError(delegation.redact_token(str(exc))) from None
    if lookup_key:
        sessions[lookup_key] = client
        _touch(lookup_key)
    log.info(
        f"session.{provider}.created",
        conversation_key=lookup_key or "first",
        resumed=bool(existing_session_id),
    )
    return client, bool(existing_session_id)


# ── runtime 呼び出し ────────────────────────────────────────────────


async def ask_claude(client: RuntimeSession, prompt: RuntimePrompt) -> tuple[str, dict | None, str | None]:
    """runtime に質問して (応答テキスト, usage_dict_or_None, session_id) を返す。"""
    _ensure_runtime_session(client)
    log.debug("runtime.query.start", provider=client.provider, prompt_length=len(prompt_text(prompt)))
    result = await client.ask(prompt)
    return result.text, result.usage, result.runtime_session_id


async def stream_claude(client: RuntimeSession, prompt: RuntimePrompt):
    """
    runtime の応答をテキストチャンクとして逐次 yield する非同期ジェネレータ。
    最後に (None, usage_dict_or_None, session_id) のタプルを 1 回だけ yield する。
    """
    _ensure_runtime_session(client)
    log.debug("runtime.query.start", provider=client.provider, prompt_length=len(prompt_text(prompt)))
    iterator = client.stream(prompt)
    try:
        async for item in iterator:
            if isinstance(item, RuntimeResult):
                yield (item.text if item.provider == "print_cli" else None, item.usage, item.runtime_session_id)
            else:
                yield item
    finally:
        close = getattr(iterator, "aclose", None)
        if close is not None:
            await close()


def _ensure_runtime_session(client: object) -> None:
    """Protocol 外の object が route 層へ漏れた場合に早期失敗させる。"""
    if not isinstance(client, RuntimeSession):
        raise TypeError("client must implement RuntimeSession Protocol")
