"""Chat 応答後の interaction と session 永続化を統括する use case。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .delegation import redact_token
from .conversation_key import derive_storage_key
from .models import ChatMessage
from .output_guard import guard_enabled, redact_event
from .runtime.base import RuntimeSession, RuntimeTurnError


@dataclass(frozen=True, slots=True)
class TurnContext:
    """同期 / streaming に共通する解決済み chat turn context。"""

    client: RuntimeSession
    latest_text: str
    messages: list[ChatMessage]
    model: str
    provider: str
    interaction_id: int | None
    session_event: str
    conversation_key: str | None
    lookup_key: str | None
    session_identity: tuple[str, str, str]


@dataclass(frozen=True, slots=True)
class CompletionPorts:
    """Completion use case が interaction / session 永続化へ接続する port。"""

    record_completed: Callable[..., Awaitable[object]]
    backfill_conversation_key: Callable[[int | None, str], Awaitable[object]]
    rotate_hash_key: Callable[..., Awaitable[str]]
    upsert_session: Callable[..., Awaitable[object]]
    extract_tokens: Callable[
        [dict | None, str, str], tuple[int, int, str]
    ]


@dataclass(frozen=True, slots=True)
class CompletionResult:
    """永続化後に response 組立と cleanup 判断へ渡す結果。"""

    tokens_in: int
    tokens_out: int
    session_registered: bool


async def finalize_chat_turn(
    context: TurnContext,
    ports: CompletionPorts,
    *,
    answer: str,
    usage: dict | None,
    runtime_session_id: str | None,
) -> CompletionResult:
    """完了した chat turn の帳簿を閉じ、次回 resume 用 session を保存する。

    Args:
        context: Route で解決済みの chat turn context。
        ports: Interaction と session 永続化への port。
        answer: Runtime が返した完全な応答本文。
        usage: Runtime 由来の token / cost 情報。未提供時は None。
        runtime_session_id: Provider 固有の resume 用 session ID。

    Returns:
        Response token 数と session 登録状態。
    """
    tokens_in, tokens_out, tokens_source = ports.extract_tokens(
        usage,
        context.latest_text,
        answer,
    )
    cost = (usage or {}).get("cost_usd") if context.provider == "openai_compat" else None
    await ports.record_completed(
        interaction_id=context.interaction_id,
        response=answer,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        tokens_source=tokens_source,
        session_event=context.session_event,
        claude_session_id=(
            runtime_session_id if context.provider == "claude" else None
        ),
        provider=context.provider,
        runtime_session_id=runtime_session_id,
        **({"metadata": {"cost_usd": cost}} if cost is not None else {}),
    )

    if context.provider in {"print_cli", "openai_compat"}:
        if context.lookup_key is None and not context.conversation_key:
            await ports.backfill_conversation_key(
                context.interaction_id, derive_storage_key(context.messages, answer),
            )
        return CompletionResult(tokens_in=tokens_in, tokens_out=tokens_out, session_registered=False)

    session_registered = context.lookup_key is not None
    if context.conversation_key and runtime_session_id:
        await ports.upsert_session(
            context.conversation_key,
            runtime_session_id,
            context.model,
            provider=context.provider,
            runtime_session_id=runtime_session_id,
            session_identity=context.session_identity[2],
        )
    if not context.conversation_key:
        storage_key = await ports.rotate_hash_key(
            context.lookup_key,
            context.messages,
            answer,
            context.model,
            runtime_session_id,
            context.client,
            provider=context.provider,
            session_identity=context.session_identity,
        )
        session_registered = True
        if context.lookup_key is None:
            await ports.backfill_conversation_key(
                context.interaction_id,
                storage_key,
            )

    return CompletionResult(
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        session_registered=session_registered,
    )



async def record_turn_failure(record_failed, interaction_id, error: Exception) -> None:
    """Keep SDK usage on failed turns without completing or registering a session."""
    error_text = redact_token(str(error))
    message = redact_event(error_text) if guard_enabled() else error_text
    if not isinstance(error, RuntimeTurnError):
        await record_failed(interaction_id, message)
        return
    usage = error.partial_result.usage or {}
    def token(name):
        value = usage.get(name)
        return max(0, int(value)) if isinstance(value, int) and not isinstance(value, bool) else None
    await record_failed(
        interaction_id, message, tokens_in=token("input_tokens"),
        tokens_out=token("output_tokens"),
        tokens_source="sdk" if any(key in usage for key in ("input_tokens", "output_tokens")) else None,
        metadata={"provider_failure": redact_event(error.diagnostics) if guard_enabled() else error.diagnostics,
                  "partial_response_chars": len(error.partial_result.text)},
    )
