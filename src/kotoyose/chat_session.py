"""Chat conversation key と runtime session の lifecycle service。"""

from __future__ import annotations

import time

import structlog

from .conversation_key import derive_storage_key
from .db import upsert_session
from .models import ChatMessage
from .runtime.base import RuntimeSession
from .session import session_cache_identities, session_last_used, sessions


log = structlog.get_logger()


def is_session_cache_hit(
    lookup_key: str | None,
    requested_identity: tuple[str, str, str],
) -> bool:
    """Lookup key の session が同じ権限境界で再利用可能か判定する。

    Args:
        lookup_key: Conversation key または履歴 hash。初回は None。
        requested_identity: Provider、preset、認証境界を含む identity。

    Returns:
        In-memory session が存在し、identity 一致かつ有効なら True。
    """
    cached_client = sessions.get(lookup_key) if lookup_key else None
    return (
        cached_client is not None
        and session_cache_identities.get(lookup_key) == requested_identity
        and getattr(cached_client, "invalidated", False) is not True
    )


async def rotate_hash_key(
    lookup_key: str | None,
    messages: list[ChatMessage],
    answer: str,
    model: str,
    runtime_session_id: str | None,
    client: RuntimeSession | None = None,
    provider: str = "claude",
    session_identity: tuple[str, str, str] | None = None,
) -> str:
    """応答を含む storage key へ in-memory / DB session を回転する。

    Args:
        lookup_key: 今回の session 検索に使用した履歴 hash。
        messages: 今回受信した OpenAI 互換 message 履歴。
        answer: Runtime が返した完全な応答本文。
        model: 公開 model 名。
        runtime_session_id: Provider 固有の resume 用 session ID。
        client: 回転後の key へ登録する runtime session。
        provider: Runtime provider 名。
        session_identity: Cache の権限境界を表す identity tuple。

    Returns:
        次 turn の lookup に使用する storage key。
    """
    storage_key = derive_storage_key(messages, answer)
    log.debug(
        "v2.rotate",
        lookup_key=lookup_key,
        storage_key=storage_key,
        answer_length=len(answer),
    )
    if client is None and lookup_key:
        client = sessions.get(lookup_key)
    if client:
        sessions[storage_key] = client
        if session_identity is not None:
            session_cache_identities[storage_key] = session_identity
        elif lookup_key and lookup_key in session_cache_identities:
            session_cache_identities[storage_key] = session_cache_identities[lookup_key]
    session_last_used[storage_key] = time.time()

    if lookup_key and lookup_key != storage_key and lookup_key in sessions:
        sessions.pop(lookup_key)
        session_last_used.pop(lookup_key, None)
        session_cache_identities.pop(lookup_key, None)

    session_id = runtime_session_id or storage_key
    await safe_upsert_session(
        storage_key,
        session_id,
        model,
        provider=provider,
        runtime_session_id=session_id,
        session_identity=(
            session_identity[2] if session_identity is not None else None
        ),
    )
    return storage_key


async def safe_upsert_session(
    conversation_key: str,
    claude_session_id: str,
    model: str,
    provider: str = "claude",
    runtime_session_id: str | None = None,
    session_identity: str | None = None,
) -> None:
    """Session 永続化の失敗を response 成否から切り離す。

    DB 障害は warning に記録し、runtime が生成済みの応答は失敗させない。
    """
    try:
        await upsert_session(
            conversation_key,
            claude_session_id,
            model,
            provider=provider,
            runtime_session_id=runtime_session_id,
            session_identity=session_identity,
        )
    except Exception as exc:
        log.warning(
            "session.upsert.failed",
            conversation_key=conversation_key,
            provider=provider,
            model=model,
            error=str(exc),
        )


async def safe_disconnect_client(client: RuntimeSession) -> None:
    """Cache 登録前の runtime session を元の error を隠さず解放する。"""
    disconnect = getattr(client, "disconnect", None)
    if not callable(disconnect):
        return
    try:
        await disconnect()
    except Exception as exc:
        log.warning("session.uncached_disconnect_failed", error=str(exc))
