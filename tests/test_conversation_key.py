"""会話キー導出ロジックのユニットテスト。"""

from kotoyose.conversation_key import derive_conversation_key
from kotoyose.models import ChatMessage


def _msgs(*pairs):
    """(role, content) のタプルリストから ChatMessage リストを作る。"""
    return [ChatMessage(role=r, content=c) for r, c in pairs]


def test_header_takes_priority():
    """X-Conversation-Key ヘッダが設定されていれば、それをそのまま返す。"""
    msgs = _msgs(("user", "Hello"))
    key = derive_conversation_key(
        header_key="discord:channel:9876",
        messages=msgs,
    )
    assert key == "discord:channel:9876"


def test_webui_fallback_uses_first_message():
    """ヘッダなし → 最初のメッセージ内容の SHA-256 から webui: キーを作る。"""
    msgs = _msgs(("user", "今日のタスクを整理して"))
    key = derive_conversation_key(header_key=None, messages=msgs)
    assert key.startswith("webui:")
    # 同じメッセージ → 同じキー
    key2 = derive_conversation_key(header_key=None, messages=msgs)
    assert key == key2


def test_webui_different_first_message_gives_different_key():
    """最初のメッセージが違えば異なるキーになる。"""
    msgs_a = _msgs(("user", "タスクA"))
    msgs_b = _msgs(("user", "タスクB"))
    key_a = derive_conversation_key(header_key=None, messages=msgs_a)
    key_b = derive_conversation_key(header_key=None, messages=msgs_b)
    assert key_a != key_b


def test_webui_key_stable_across_conversation_growth():
    """会話が伸びても最初のメッセージが同じならキーは変わらない。"""
    msgs_first = _msgs(("user", "最初のメッセージ"))
    msgs_later = _msgs(
        ("user", "最初のメッセージ"),
        ("assistant", "はい"),
        ("user", "続きを"),
    )
    key1 = derive_conversation_key(header_key=None, messages=msgs_first)
    key2 = derive_conversation_key(header_key=None, messages=msgs_later)
    assert key1 == key2


def test_no_messages_and_no_header_returns_stateless():
    """メッセージなし・ヘッダなし → stateless キーを返す。"""
    key = derive_conversation_key(header_key=None, messages=[])
    assert key == "stateless"


from kotoyose.conversation_key import (
    derive_lookup_key,
    derive_storage_key,
)


def _msg(role: str, content: str) -> ChatMessage:
    return ChatMessage(role=role, content=content)


# ── derive_lookup_key ──────────────────────────────────────────────

def test_lookup_key_none_for_single_message():
    messages = [_msg("user", "hello")]
    assert derive_lookup_key(messages) is None


def test_lookup_key_none_for_empty():
    assert derive_lookup_key([]) is None


def test_lookup_key_hashes_all_but_last():
    messages = [
        _msg("user", "hello"),
        _msg("assistant", "hi!"),
        _msg("user", "how are you?"),
    ]
    # Key must equal the result of hashing only the first 2 messages (all-but-last)
    two_msgs = messages[:2]
    key_3 = derive_lookup_key(messages)
    key_2 = derive_lookup_key(two_msgs + [_msg("user", "ignored")])
    assert key_3 is not None
    assert key_3.startswith("v2:")
    # Keys computed from different [:-1] slices must differ
    different_history = [_msg("user", "different"), _msg("assistant", "hi!"), _msg("user", "q")]
    assert key_3 != derive_lookup_key(different_history)
    # Appending a new message does not change the lookup key for the prior turn
    assert derive_lookup_key(messages[:2] + [_msg("user", "x")]) == derive_lookup_key(messages[:2] + [_msg("user", "y")])


def test_lookup_key_equals_storage_key_of_previous_turn():
    """Core invariant: turn N's lookup_key == turn N-1's storage_key."""
    turn1_messages = [_msg("user", "hello")]
    turn1_response = "hi!"
    storage_key = derive_storage_key(turn1_messages, turn1_response)

    turn2_messages = [
        _msg("user", "hello"),
        _msg("assistant", "hi!"),
        _msg("user", "how are you?"),
    ]
    lookup_key = derive_lookup_key(turn2_messages)

    assert lookup_key == storage_key


def test_lookup_key_changes_when_history_differs():
    messages_a = [_msg("user", "hello"), _msg("assistant", "hi!"), _msg("user", "q")]
    messages_b = [_msg("user", "hey"),   _msg("assistant", "yo!"), _msg("user", "q")]
    assert derive_lookup_key(messages_a) != derive_lookup_key(messages_b)


# ── derive_storage_key ────────────────────────────────────────────

def test_storage_key_has_v2_prefix():
    messages = [_msg("user", "hello")]
    key = derive_storage_key(messages, "hi!")
    assert key.startswith("v2:")


def test_storage_key_changes_with_different_response():
    messages = [_msg("user", "hello")]
    key1 = derive_storage_key(messages, "hi!")
    key2 = derive_storage_key(messages, "hey there!")
    assert key1 != key2
