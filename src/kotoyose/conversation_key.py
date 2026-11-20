"""
会話キー導出モジュール。

X-Conversation-Key ヘッダを最優先で使い、
なければ最初のメッセージ内容をハッシュしてキーにする。
これにより Open WebUI のような履歴付きクライアントで
会話の連続性を保てる。

v2 追加:
  derive_lookup_key   — 今ターンのセッション検索キー (messages[:-1] のハッシュ)
  derive_storage_key  — 今ターン処理後に保存するキー (messages + assistant のハッシュ)
"""

import hashlib
import json

from .models import ChatMessage


def derive_conversation_key(
    header_key: str | None,
    messages: list[ChatMessage],
) -> str:
    """
    会話キーを決定して返す (v1 ヘッダ優先パス用)。

    優先順位:
    1. X-Conversation-Key ヘッダ (n8n / Discord / curl 明示)
    2. 最初のメッセージの SHA-256 先頭12文字 (Open WebUI フォールバック)
    3. "stateless" (メッセージも無い場合)
    """
    if header_key:
        return header_key

    if not messages:
        return "stateless"

    first = {"role": messages[0].role, "content": messages[0].canonical_content()}
    digest = hashlib.sha256(json.dumps(first, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return f"webui:{digest[:12]}"


def _hash_messages(messages: list[dict]) -> str:
    """list[dict] を JSON にして SHA-256 先頭16文字を返す。"""
    digest = hashlib.sha256(
        json.dumps(messages, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    return f"v2:{digest[:16]}"


def derive_lookup_key(messages: list[ChatMessage]) -> str | None:
    """
    今ターンのセッション検索キーを返す。

    messages[:-1] (最新ユーザー発言を除いた履歴) をハッシュ化する。
    前ターンの derive_storage_key と等しくなるよう設計されている。
    messages が 1 件以下 (初回メッセージ) は None を返す。
    """
    if len(messages) <= 1:
        return None
    prior = [
        {"role": m.role, "content": m.canonical_content()}
        for m in messages[:-1]
    ]
    return _hash_messages(prior)


def derive_storage_key(messages: list[ChatMessage], assistant_response: str) -> str:
    """
    今ターン処理後に DB/メモリに保存するキーを返す。

    messages 全件 + assistant の応答をハッシュ化する。
    次ターンの derive_lookup_key がこのキーと一致する。
    """
    full = [
        {"role": m.role, "content": m.canonical_content()}
        for m in messages
    ]
    full.append({"role": "assistant", "content": assistant_response})
    return _hash_messages(full)
