"""replay_messages が session.py から削除されたことを保証する。

session_store 導入後はこの関数は不要になり、N+1 倍コスト爆発の根源だった。
"""

import kotoyose.session as session_module


def test_replay_messages_is_removed():
    """replay_messages 関数が session module に存在しないこと。"""
    assert not hasattr(session_module, "replay_messages"), (
        "replay_messages must be removed after session_store integration. "
        "If you re-introduced it, the (N+1) Claude API cost explosion is back."
    )


def test_get_or_create_client_is_removed_or_aliased_to_get_or_resume_client():
    """v1/v2 統合で get_or_create_client は廃止されている。"""
    has_create = hasattr(session_module, "get_or_create_client")
    has_resume = hasattr(session_module, "get_or_resume_client")
    assert has_resume, "get_or_resume_client must exist as the unified entry point"
    if has_create:
        # 移行期に旧名を残す場合は同一実装にエイリアスされていることを確認
        assert session_module.get_or_create_client is session_module.get_or_resume_client
