"""
/v1/chat/completions ルート。

ヘッダあり (n8n/Discord): X-Conversation-Key 固定キーでセッション取得
ヘッダなし (Open WebUI):  messages ハッシュ由来の lookup_key でセッション取得 (ターンごとキー回転)

どちらのパスも get_or_resume_client(lookup_key, model) に統合されている。
"""

import asyncio
import functools
import time
import uuid

import structlog
from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from .. import delegation
from ..activity import bind_event_queue, start_event_writer, stream_activity
from ..execution_http import authorize_execution, enforce_model_scope
from ..output_guard import CredentialLeakSuspected, LEAK_REASON, check_answer, guard_enabled, redact_event
from ..compatibility_http import native_chat_response, uses_native_execution
from ..execution_resources import ExecutionError, _profile_within
from ..capabilities import unsupported_capabilities
from ..chat_completion import (
    record_turn_failure,
    CompletionPorts,
    TurnContext,
    finalize_chat_turn,
)
from ..chat_policy import (
    ChatPolicyViolation,
    validate_cwd,
    validate_required_headers,
)
from ..chat_request import (
    RequestPreparationPorts,
    ensure_reasoning_effort,
    prepare_chat_request,
    prompt_for_resolved_session,
)
from ..data_policy import alias_data_policy, configured_capabilities, request_capabilities, training_binding
from ..chat_selection import pin_pm_saved_preset
from ..config import (
    ClaudeSubscriptionAuthError,
    resolve_preset_secrets_async,
    resolve_preset_secrets_with_versions_async,
)
from ..connections_probe import connection_for_model, is_auth_failure_error, record_connection_auth_failure
from ..secret_store import SecretStoreError, use_resolved_secrets
from ..chat_session import (
    is_session_cache_hit,
    rotate_hash_key as _rotate_hash_key,
    safe_disconnect_client as _safe_disconnect_client,
    safe_upsert_session as _safe_upsert_session,
)
from ..chat_streaming import StreamContext, StreamingPorts, stream_chat_response
from ..chat_usage import (
    build_response_usage as _build_response_usage,
    extract_tokens as _extract_tokens,
)
from ..config import get_config
from ..execution_response import unsupported_response
from ..interactions import (
    backfill_conversation_key,
    get_interaction_for_conversation,
    record_completed,
    record_failed,
    record_received,
)
from ..models import (
    FileContentPart,
    ChatCompletionChoice,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    ErrorDetail,
    ErrorResponse,
)
from ..runtime.acp import AcpPrompt, acp_binding
from ..runtime.base import RuntimeTurnError
from ..session import (
    SDKConnectionError,
    _cache_identity,
    ask_claude,
    delegation_identity,
    discard_runtime,
    get_or_resume_client,
    stream_claude,
)

router = APIRouter(dependencies=[Depends(authorize_execution)])
log = structlog.get_logger()

# session_event (cache_hit / resumed / created / first) を応答へ公開する header。
# 既存の鍵を送ったのに created が返ったことを、呼び出し側が「kotoyose 側の文脈が
# 失われた」と判ずるための唯一の signal である。生の was_resumed は正常な
# インメモリ cache hit でも False になるため、この 4 値だけが使える。
SESSION_EVENT_HEADER = "X-Session-Event"
# pool alias を名指した chat に、選ばれた候補 alias を返す header。
RESOLVED_MODEL_HEADER = "X-Resolved-Model"

def _invalid_request_error(message: str, request_id: str) -> JSONResponse:
    """Policy 違反を OpenAI 互換の 400 response へ変換する。"""
    return JSONResponse(
        status_code=400,
        content=ErrorResponse(
            error=ErrorDetail(
                message=message,
                type="invalid_request_error",
                request_id=request_id,
            )
        ).model_dump(),
    )
def _sanction_gate(request: Request, model: str, request_id: str) -> JSONResponse | None:
    """needs_ack の route を警告し、sanction_enforcement='all' のときだけ chat を断る。"""
    from ..agent_sanction import acknowledgement_error_message, alias_route_sanction, warn_needs_ack_once
    found = alias_route_sanction(get_config(), model)
    if found is None or found[1].status != "needs_ack":
        return None
    route_name, sanction = found
    service = getattr(request.app.state, "execution_service", None)
    if getattr(getattr(service, "config", None), "sanction_enforcement", "off") == "all":
        return JSONResponse(status_code=403, content=ErrorResponse(error=ErrorDetail(
            message=acknowledgement_error_message(route_name, sanction),
            type="route_needs_acknowledgement", request_id=request_id,
        )).model_dump())
    warn_needs_ack_once(route_name, sanction, via="chat")
    return None


@router.get("/v1/conversations/{conversation_key}/events/stream")
async def stream_conversation_events(conversation_key: str, request: Request, after_seq: int = -1):
    """進行中、または最新の interaction activity を SSE で返す。"""
    if conversation_key.startswith(delegation.KEY_PREFIX):
        raise ExecutionError("permission_denied", "Reserved conversation key", 403)
    interaction = await get_interaction_for_conversation(conversation_key, api_key_name=request.state.api_key_name)
    # interaction が無いときは、Native 無効時と同じく空の stream を返す (最初の要求前に
    # 購読する direct chat client を 501 にしない)。Native alias の会話だけ Attempt events へ誘導する。
    if (
        interaction is not None
        and getattr(request.app.state, "execution_service", None) is not None
        and uses_native_execution(request, interaction.get("model") or "")
    ):
        raise ExecutionError("unsupported_capability", "Use principal-scoped Native Attempt events", 501)
    if interaction is None:
        return StreamingResponse(iter(()), media_type="text/event-stream")
    return StreamingResponse(
        stream_activity(
            interaction["id"],
            after_seq,
            live=interaction.get("status") == "in_progress",
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post(
    "/v1/chat/completions",
    summary="チャット補完",
    description=(
        "OpenAI 互換のチャットエンドポイント。\n\n"
        "**会話キーの指定方法:**\n"
        "- `X-Conversation-Key` ヘッダ: n8n / Discord / curl から明示指定 "
        "(例: `discord:channel:9876`, `cron:morning-briefing:2026-04-10`)\n"
        "- ヘッダなし: messages 履歴のハッシュで自動生成 (Open WebUI 用)\n\n"
        "**ストリーミング:**\n"
        '`"stream": true` で SSE レスポンスを返します。\n\n'
        "**画像入力:**\n"
        "OpenAI 互換の `content` parts (`text` / `image_url`) を受理します。"
        "Claude / Codex / Cursor provider で利用でき、Codex / Cursor は base64 data URL "
        "のみ対応します。\n\n"
        "**cli.acp:** 設定済みの login home を使い、会話キーごとにセッションを再利用します。"
        "画像・添付は非対応です。\n\n"
        "**cli.print:** 設定済みの login home を使い、各ターンで messages の履歴を送ります。"
        "画像・添付とセッション再開は非対応です。\n\n"
        "**応答ヘッダ:**\n"
        "`X-Session-Event` にセッションの解決結果 "
        "(`cache_hit` / `resumed` / `created` / `first`) を返します。"
        "既存の `X-Conversation-Key` を送ったのに `created` が返った場合、"
        "kotoyose 側の会話履歴が失われています。"
    ),
    response_model=ChatCompletionResponse,
    responses={
        404: {"model": ErrorResponse, "description": "Model alias が未登録"},
        400: {"model": ErrorResponse, "description": "リクエスト不正 (messages 空など)"},
        401: {"description": "認証失敗"},
        500: {"model": ErrorResponse, "description": "Claude SDK 内部エラー"},
        503: {"model": ErrorResponse, "description": "Claude SDK 接続失敗"},
    },
)
async def chat_completions(
    req: ChatCompletionRequest,
    request: Request,
    response: Response,
    x_conversation_key: str | None = Header(default=None),
):
    if (x_conversation_key or "").startswith(delegation.KEY_PREFIX):
        raise ExecutionError("permission_denied", "Reserved conversation key", 403)
    enforce_model_scope(request, req.model)
    resolved = None
    cfg = get_config()
    if req.model not in cfg.models and req.model not in cfg.pools and not cfg.compatibility_config.has_model(req.model):
        raise ExecutionError("model_not_found", f"Model '{req.model}' does not exist", 404)
    if req.model in cfg.pools:
        req, resolved = await _resolve_pool_model(request, req)
    result = await _chat_completions(req, request, response, x_conversation_key)
    if resolved is not None:
        if isinstance(result, ChatCompletionResponse):
            result.resolved_model = resolved
            response.headers[RESOLVED_MODEL_HEADER] = resolved
        elif isinstance(result, Response):
            result.headers[RESOLVED_MODEL_HEADER] = resolved
    return result


async def _resolve_pool_model(request: Request, req: ChatCompletionRequest):
    """pool alias を候補 1 つへ。以後は、その候補が名指されたときと同じ経路を通る。"""
    store = getattr(getattr(request.app.state, "execution_service", None), "store", None)
    if getattr(store, "pool", None) is None or getattr(store, "model_pools", None) is None:
        raise ExecutionError("unsupported_capability", "Pool aliases require the native execution store", 501)
    service = request.app.state.execution_service
    cfg = get_config()
    refuse = getattr(service.config, "sanction_enforcement", "off") == "all"
    capabilities = request_capabilities(req)
    ordered = await store.model_pools.select(
        cfg, req.model, refuse_needs_ack=refuse,
        **({"training_capabilities": capabilities} if capabilities else {}),
    )
    policy = getattr(getattr(request, "state", None), "execution_policy", None)
    if policy is not None:
        ordered = [candidate for candidate in ordered if _profile_within(candidate, policy, service.config, cfg)]
        if not ordered:
            raise ExecutionError("permission_denied", f"Model '{req.model}' is outside this API key's client scope", 403)
    return req.model_copy(update={"model": ordered[0]}), ordered[0]


async def delegated_chat_turn(
    request: Request, alias: str, task: str, conversation_key: str, metadata: dict,
):
    enforce_model_scope(request, alias)
    preset = get_config().models.get(alias)
    if preset is None or not preset.is_direct_host_chat:
        raise ExecutionError("unsupported_capability", "Delegation requires a direct host chat alias", 501)
    req = ChatCompletionRequest(model=alias, messages=[ChatMessage(role="user", content=task)])
    return await _chat_completions(req, request, Response(), conversation_key, delegation_metadata=metadata)


async def _chat_completions(req, request, response, x_conversation_key, *, delegation_metadata=None):
    request_id = getattr(request.state, "request_id", "unknown")

    if (refused := _sanction_gate(request, req.model, request_id)) is not None:
        return refused

    cfg = get_config()
    pm_requirements = req.metadata.kotoyose.pm_saved_preset if req.metadata and req.metadata.kotoyose else None
    native_chat = pm_requirements is None and uses_native_execution(request, req.model)
    decision = alias_data_policy(cfg, req.model)
    if decision.restricted:
        declared = cfg.models[req.model]
        if message := decision.refusal(request_capabilities(req) | configured_capabilities(declared)):
            raise ExecutionError("permission_denied", message, 403, reason_code="data_policy_restricted")
    if any(isinstance(item.content, list) and any(isinstance(part, FileContentPart) for part in item.content)
           for item in req.messages):
        return _invalid_request_error("File content parts are unsupported", request_id)
    if decision.restricted and native_chat:
        raise ExecutionError(
            "permission_denied", f"Alias '{req.model}' requires a chat runtime with enforceable data policy", 403,
            reason_code="data_policy_restricted",
        )
    if native_chat:
        if delegation_metadata is not None:
            raise ExecutionError("unsupported_capability", "Delegation requires a direct host chat alias", 501)
        return await native_chat_response(request, req, x_conversation_key)

    if req.reasoning_effort is not None and req.model in cfg.models:
        ensure_reasoning_effort(cfg, req.model, req.reasoning_effort)

    if req.metadata is not None and req.metadata.kotoyose is not None:
        missing = unsupported_capabilities(req.metadata.kotoyose.required_capabilities)
        if missing:
            return unsupported_response(missing, request_id)

    # The strict PM path owns one config/preset/options snapshot.
    selection = None
    try:
        if pm_requirements is not None:
            try:
                preloaded_secrets = (
                    await resolve_preset_secrets_async(cfg, cfg.preset_for(req.model))
                    if req.model in cfg.models else {}
                )
            except SecretStoreError as exc:
                raise ExecutionError(
                    "unsupported_capability",
                    f"Saved PM preset secret unavailable: {exc}",
                    503,
                    reason_code="pm_credential_unavailable",
                ) from exc
            with use_resolved_secrets(preloaded_secrets):
                selection = pin_pm_saved_preset(cfg, req, dict(request.headers))
        preset = selection.preset if selection is not None else cfg.preset_for(req.model)
    except ChatPolicyViolation as exc:
        return _invalid_request_error(str(exc), request_id)
    provider = preset.provider
    delegation_client = None
    if delegation_metadata is None and delegation.MCP_NAME in preset.mcp_refs:
        delegation_client = getattr(request.state, "api_key_caller", None)

    if provider in {"print_cli", "cli_acp"} and "attachments" in request_capabilities(req):
        driver = "cli.acp" if provider == "cli_acp" else "cli.print"
        return _invalid_request_error(f"{driver} does not support images or attachments", request_id)

    # Provider 接続前に受付 policy を適用し、設定ミスを silent ignore しない。
    try:
        validate_required_headers(preset, request.headers)
        validate_cwd(preset, req.model, req.cwd)
    except ChatPolicyViolation as exc:
        return _invalid_request_error(str(exc), request_id)

    # fixed_conversation_key: ヘッダ未指定時のみ preset 由来の固定キーを採用し、
    # v1 ヘッダパスに合流させる (example-client など X-Conversation-Key を送れないクライアント用)
    if not x_conversation_key and preset.fixed_conversation_key:
        x_conversation_key = preset.fixed_conversation_key

    log.info(
        "request.received",
        model=req.model,
        provider=provider,
        conversation_key_source="header" if x_conversation_key else "hash",
        msg_length=sum(len(m.text_content()) for m in req.messages),
    )
    log.debug(
        "request.detail",
        msg_count=len(req.messages),
        msg_roles=[m.role for m in req.messages],
        msg_lengths=[len(m.text_content()) for m in req.messages],
        stream=req.stream,
    )

    secret_versions: dict[str, int] = {}
    if selection is None and provider != "print_cli":
        try:
            _, secret_versions = await resolve_preset_secrets_with_versions_async(cfg, preset)
        except ClaudeSubscriptionAuthError as exc:
            log.error("sdk.authentication_failed", error=str(exc))
            await record_connection_auth_failure(connection_for_model(get_config(), req.model), error=str(exc))
            return JSONResponse(
                status_code=401,
                content=ErrorResponse(
                    error=ErrorDetail(
                        message=str(exc),
                        type="authentication_failed",
                        request_id=request_id,
                    )
                ).model_dump(),
            )
        except SecretStoreError as exc:
            log.error("sdk.connection_error", error=str(exc))
            return JSONResponse(
                status_code=503,
                content=ErrorResponse(
                    error=ErrorDetail(
                        message=f"Claude SDK connection failed: {exc}",
                        type="service_unavailable",
                        request_id=request_id,
                    )
                ).model_dump(),
            )

    identity_kwargs = {}
    if req.reasoning_effort is not None:
        identity_kwargs["reasoning_effort"] = req.reasoning_effort
    resolve_identity = functools.partial(_cache_identity, **identity_kwargs)
    if decision.restricted:
        resolve_identity = functools.partial(
            _cache_identity, training_binding=training_binding(cfg, req.model), **identity_kwargs,
        )
    elif provider == "cli_acp":
        resolve_identity = functools.partial(
            _cache_identity, acp_scope=acp_binding(cfg, preset, req.cwd),
            execution_environment=preset.execution_environment,
            **identity_kwargs,
        )

    if delegation_client:
        base_identity = resolve_identity

        def resolve_identity(*args, **kwargs):
            return delegation_identity(base_identity(*args, **kwargs), delegation_client)

    try:
        prepared = prepare_chat_request(
            req,
            request.headers,
            preset,
            RequestPreparationPorts(
                resolve_session_identity=resolve_identity,
                is_cache_hit=is_session_cache_hit,
            ),
            conversation_key=x_conversation_key,
            **({"session_identity": (
                delegation_identity(selection.identity, delegation_client)
                if delegation_client else selection.identity
            )} if selection is not None else {}),
            **({"secret_versions": secret_versions} if secret_versions else {}),
        )
    except ChatPolicyViolation as exc:
        return _invalid_request_error(str(exc), request_id)

    base_prompt = prepared.prompt
    if delegation_metadata is not None and isinstance(base_prompt, AcpPrompt):
        base_prompt = AcpPrompt(prepared.latest_text, prepared.latest_text)
    lookup_key = prepared.lookup_key

    log.debug(
        "session.lookup",
        lookup_key=lookup_key,
        source="header" if x_conversation_key else "hash",
        history_count=len(req.messages) - 1 if len(req.messages) > 1 else 0,
        first_msg_preview=delegation.redact_token(req.messages[0].text_content())[:80] if req.messages else "",
    )
    # interactions 記録: 受付時 INSERT
    interaction_id = await record_received(
        request_id=request_id,
        conversation_key=prepared.effective_conversation_key,
        provider=provider,
        model=req.model,
        source=request.headers.get("x-memory-source"),
        prompt=delegation.redact_token(prepared.audit_text),
        streamed=req.stream,
        api_key_name=getattr(request.state, "api_key_name", None),
        **({"metadata": delegation_metadata} if delegation_metadata is not None else {}),
    )

    # ── セッション取得 ──────────────────────────────────────────
    try:
        client_kwargs: dict[str, object] = {"cwd_override": req.cwd}
        if selection is not None:
            client_kwargs["selection"] = selection
        elif decision.restricted or preset.route is not None or cfg is not get_config():
            client_kwargs["config_snapshot"] = cfg
        # 既存 preset の session 生成 API は変えず、MCP request header を宣言した
        # preset だけに接続設定の解決値を渡す。
        if preset.required_request_headers:
            client_kwargs["request_headers"] = dict(request.headers)
        if provider in {"print_cli", "cli_acp"}:
            client_kwargs["execution_settings"] = getattr(
                getattr(request.app.state, "execution_service", None), "config", None,
            )
        if req.reasoning_effort is not None:
            client_kwargs["reasoning_effort"] = req.reasoning_effort
        if delegation_metadata is not None:
            client_kwargs.update(delegated=True, config_snapshot=cfg)
        elif delegation_client:
            client_kwargs["config_snapshot"] = cfg
            models = request.state.execution_policy.models
            targets = frozenset(alias for alias in preset.delegates if "*" in models or alias in models)
            client_kwargs["delegation_parent"] = delegation.DelegationParent(
                delegation_client, request.state.api_key_name, targets,
            )
        async with delegation.serialize_conversation(lookup_key if delegation_client else None):
            client, was_resumed = await get_or_resume_client(lookup_key, req.model, **client_kwargs)
    except asyncio.CancelledError:
        if delegation_metadata is not None or delegation_client is not None:
            await record_failed(interaction_id, "Chat turn cancelled")
        raise
    except SDKConnectionError as e:
        log.error("sdk.connection_error", error=str(e))
        await record_failed(interaction_id, str(e))
        if getattr(e, "code", None) == "authentication_failed":
            await record_connection_auth_failure(connection_for_model(get_config(), req.model), error=str(e))
            return JSONResponse(
                status_code=401,
                content=ErrorResponse(
                    error=ErrorDetail(
                        message=str(e),
                        type="authentication_failed",
                        request_id=request_id,
                    )
                ).model_dump(),
            )
        return JSONResponse(
            status_code=503,
            content=ErrorResponse(
                error=ErrorDetail(
                    message=f"Claude SDK connection failed: {e}",
                    type="service_unavailable",
                    request_id=request_id,
                )
            ).model_dump(),
        )

    conversation_key = x_conversation_key or (lookup_key or "first")
    structlog.contextvars.bind_contextvars(conversation_key=conversation_key)

    # session_event の 4 値: cache_hit / resumed / created / first
    # session_store 導入で "replayed" は廃止 (replay_messages を削除したため)
    if prepared.cache_hit:
        session_event = "cache_hit"
    elif lookup_key is None:
        session_event = "first"
    else:
        # was_resumed は get_or_resume_client が DB から claude_session_id を
        # 引けたかを示す。lookup_session を 2 度呼ぶ TOCTOU を避けるため、
        # 戻り値の bool を信用する。
        session_event = "resumed" if was_resumed else "created"

    # 注入された response の header が効くのは pydantic を返す同期成功パスだけ。
    # Response を直接返す streaming / エラーパスには個別に headers= を渡す。
    response.headers[SESSION_EVENT_HEADER] = session_event
    # Prompt は session 解決の前に組まれる。新規 SDK session かどうかは
    # cache hit と was_resumed が決まってからでないと分からない。
    runtime_prompt = prompt_for_resolved_session(
        req.messages,
        provider,
        base_prompt,
        fresh=not prepared.cache_hit and not was_resumed,
    )

    turn_context = TurnContext(
        client=client,
        latest_text=prepared.latest_text,
        messages=req.messages,
        model=req.model,
        provider=provider,
        interaction_id=interaction_id,
        session_event=session_event,
        conversation_key=x_conversation_key,
        lookup_key=lookup_key,
        session_identity=prepared.session_identity,
    )
    completion_ports = CompletionPorts(
        record_completed=record_completed,
        backfill_conversation_key=backfill_conversation_key,
        rotate_hash_key=_rotate_hash_key,
        upsert_session=_safe_upsert_session,
        extract_tokens=_extract_tokens,
    )

    # ── ストリーミング ───────────────────────────────────────────
    if req.stream:
        return StreamingResponse(
            stream_chat_response(
                StreamContext(
                    turn=turn_context,
                    prompt=runtime_prompt,
                    inspect_output=guard_enabled() or delegation_client is not None,
                    include_events=request.headers.get("x-kotoyose-events") == "1",
                ),
                StreamingPorts(
                    stream_runtime=stream_claude,
                    start_event_writer=start_event_writer,
                    record_failed=record_failed,
                    disconnect_client=_safe_disconnect_client,
                    completion=completion_ports,
                ),
            ),
            media_type="text/event-stream",
            headers={SESSION_EVENT_HEADER: session_event},
        )

    # ── 同期レスポンス ──────────────────────────────────────────
    event_queue = start_event_writer(interaction_id)
    runtime_session_id: str | None = None
    try:
        with bind_event_queue(event_queue):
            answer, usage, runtime_session_id = await ask_claude(client, runtime_prompt)
        if guard_enabled() or delegation_client is not None:
            check_answer(answer)
    except asyncio.CancelledError:
        if delegation_metadata is not None or delegation_client is not None:
            discard_runtime(lookup_key, client)
            await _safe_disconnect_client(client)
            await record_failed(interaction_id, "Chat turn cancelled")
        raise
    except Exception as e:
        error_text = delegation.redact_token(str(e))
        message = redact_event(error_text) if guard_enabled() else error_text
        leak = isinstance(e, CredentialLeakSuspected)
        log.error("sdk.error", error=message)
        if is_auth_failure_error(e):
            await record_connection_auth_failure(
                connection_for_model(get_config(), req.model),
                error="authentication_failed",
            )
        await record_turn_failure(record_failed, interaction_id, e)
        if lookup_key is None:
            await _safe_disconnect_client(client)
        if provider in {"print_cli", "cli_acp"} and isinstance(e, RuntimeTurnError):
            code = str(e.terminal_reason or "provider_error")
            status = {
                "authentication_failed": 401,
                "provider_usage_limit": 429,
                "unsupported_input": 400,
                "timeout": 504,
                "capacity_exhausted": 409,
                "login_in_progress": 409,
            }.get(code, 502)
            raise ExecutionError(code, message, status, safe_to_retry=False) from None
        return JSONResponse(
            status_code=502 if leak else 500,
            content=ErrorResponse(
                error=ErrorDetail(
                    message=message if leak else f"Provider runtime error: {message}",
                    type=LEAK_REASON if leak else "internal_error",
                    request_id=request_id,
                )
            ).model_dump(),
            headers={SESSION_EVENT_HEADER: session_event},
        )
    finally:
        event_queue.put_nowait(None)

    completion = await finalize_chat_turn(
        turn_context,
        completion_ports,
        answer=answer,
        usage=usage,
        runtime_session_id=runtime_session_id,
    )

    log.info("response.completed", answer_length=len(answer), streamed=False)

    return ChatCompletionResponse(
        id=f"chatcmpl-{uuid.uuid4().hex[:12]}",
        created=int(time.time()),
        model=req.model,
        choices=[
            ChatCompletionChoice(
                message=ChatMessage(role="assistant", content=answer),
            )
        ],
        usage=_build_response_usage(
            usage,
            completion.tokens_in,
            completion.tokens_out,
        ),
    )
