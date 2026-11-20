"""Chat session lifecycle service の単体テスト。"""

from types import SimpleNamespace

from kotoyose.chat_session import is_session_cache_hit


def test_session_cache_hit_requires_matching_identity(monkeypatch):
    """Cache identity が request 境界と一致する session だけを再利用する。"""
    client = SimpleNamespace(invalidated=False)
    monkeypatch.setattr("kotoyose.chat_session.sessions", {"key": client})
    monkeypatch.setattr(
        "kotoyose.chat_session.session_cache_identities",
        {"key": ("claude", "model", "digest")},
    )

    assert is_session_cache_hit("key", ("claude", "model", "digest")) is True
    assert is_session_cache_hit("key", ("claude", "model", "other")) is False


def test_session_cache_hit_rejects_invalidated_client(monkeypatch):
    """Identity が一致しても invalidated session は再利用しない。"""
    client = SimpleNamespace(invalidated=True)
    monkeypatch.setattr("kotoyose.chat_session.sessions", {"key": client})
    monkeypatch.setattr(
        "kotoyose.chat_session.session_cache_identities",
        {"key": ("claude", "model", "digest")},
    )

    assert is_session_cache_hit("key", ("claude", "model", "digest")) is False
