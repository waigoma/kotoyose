"""Local ACP session files: path, round trip, prune, truncation, and listing."""

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kotoyose.acp_sessions import (
    InvalidCursor,
    InvalidSessionId,
    SessionDataError,
    SessionStore,
    acp_state_dir,
)


UNDER_LIMIT = "a" * (7 * 1024 * 1024)
OVER_LIMIT = "b" * (8 * 1024 * 1024 + 100)


class Clock:
    def __init__(self, current: datetime):
        self.current = current

    def now(self) -> datetime:
        return self.current

    def advance(self, **delta) -> None:
        self.current += timedelta(**delta)


def store_at(root: Path, when: datetime) -> tuple[SessionStore, Clock]:
    clock = Clock(when)
    return SessionStore(root, now=clock.now), clock


def save(store: SessionStore, session_id: str, *, cwd: str = "/workspace", when_messages: str = "hello",
         model: str = "one", effort: str = "default", fast: str = "off") -> None:
    store.save(
        session_id=session_id, cwd=cwd, model=model, effort=effort, fast=fast,
        messages=[{"role": "user", "content": when_messages}],
    )


def test_state_dir_on_windows(monkeypatch):
    """Windows selects %LOCALAPPDATA%\\kotoyose-acp, not the XDG path."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("KOTOYOSE_ACP_STATE_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", "/tmp/xdg")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\ada\AppData\Local")
    assert acp_state_dir() == Path(r"C:\Users\ada\AppData\Local") / "kotoyose-acp"


def test_state_dir_on_windows_without_localappdata(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("KOTOYOSE_ACP_STATE_DIR", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/users/ada")))
    assert acp_state_dir() == Path("/users/ada/AppData/Local/kotoyose-acp")


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_state_dir_on_posix(monkeypatch, platform):
    """Linux and macOS use XDG_STATE_HOME, then ~/.local/state."""
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.delenv("KOTOYOSE_ACP_STATE_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", "/tmp/xdg")
    assert acp_state_dir() == Path("/tmp/xdg/kotoyose-acp")
    monkeypatch.delenv("XDG_STATE_HOME")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/users/ada")))
    assert acp_state_dir() == Path("/users/ada/.local/state/kotoyose-acp")


def test_state_dir_override_wins(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\ada\AppData\Local")
    monkeypatch.setenv("KOTOYOSE_ACP_STATE_DIR", "~/acp-state")
    monkeypatch.setenv("HOME", "/users/ada")
    monkeypatch.setenv("USERPROFILE", "/users/ada")
    assert acp_state_dir() == Path("/users/ada/acp-state")


def test_round_trip_permissions_and_secret_exclusion(tmp_path):
    """A saved session reloads, is private on POSIX, and contains no credentials."""
    store, clock = store_at(tmp_path, datetime(2026, 4, 2, 3, 4, 5, tzinfo=timezone.utc))
    store.save(
        session_id="chat-1", cwd="/workspace", model="one", effort="high", fast="on",
        messages=[
            {"role": "user", "content": "  こんにちは\n次の行"},
            {"role": "assistant", "content": "了解"},
            {"role": "user", "content": [
                {"type": "text", "text": "look"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,YQ=="}},
            ]},
        ],
    )
    loaded = store.load("chat-1")
    assert loaded is not None
    assert loaded.session_id == "chat-1"
    assert loaded.cwd == "/workspace"
    assert loaded.title == "こんにちは"
    assert loaded.model == "one"
    assert loaded.effort == "high"
    assert loaded.fast == "on"
    assert loaded.messages == [
        {"role": "user", "content": "  こんにちは\n次の行"},
        {"role": "assistant", "content": "了解"},
        {"role": "user", "content": [
            {"type": "text", "text": "look"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,YQ=="}},
        ]},
    ]
    assert loaded.created_at == "2026-04-02T03:04:05.000000Z"
    assert loaded.updated_at == "2026-04-02T03:04:05.000000Z"
    assert loaded.truncated is False
    path = tmp_path / "sessions" / "chat-1.json"
    text = path.read_text(encoding="utf-8")
    assert "Authorization" not in text
    assert "X-Conversation-Key" not in text
    if sys.platform != "win32":
        assert (path.stat().st_mode & 0o777) == 0o600
        assert (tmp_path.stat().st_mode & 0o777) == 0o700
        assert ((tmp_path / "sessions").stat().st_mode & 0o777) == 0o700
    assert list((tmp_path / "sessions").glob("*.tmp")) == []

    clock.advance(seconds=2)
    store.save(
        session_id="chat-1", cwd="/workspace", model="two", effort="low", fast="off",
        messages=[{"role": "user", "content": "続き"}],
    )
    again = store.load("chat-1")
    assert again is not None
    assert again.created_at == "2026-04-02T03:04:05.000000Z"
    assert again.updated_at == "2026-04-02T03:04:07.000000Z"
    assert again.model == "two"
    assert again.messages == [{"role": "user", "content": "続き"}]


def test_windows_save_does_not_chmod(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")

    def boom(*_args, **_kwargs):
        raise AssertionError("chmod")

    monkeypatch.setattr(os, "chmod", boom)
    monkeypatch.setattr(os, "fchmod", boom, raising=False)
    save(SessionStore(tmp_path), "abc")
    assert (tmp_path / "sessions" / "abc.json").is_file()


def test_failed_replace_preserves_previous_history(monkeypatch, tmp_path):
    store = SessionStore(tmp_path)
    save(store, "saved")

    def fail_replace(_source, _destination):
        raise OSError("replace failed")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        save(store, "saved", when_messages="new turn")

    assert store.load("saved").messages == [{"role": "user", "content": "hello"}]
    assert list((tmp_path / "sessions").iterdir()) == [tmp_path / "sessions" / "saved.json"]


def test_prune_keeps_two_hundred_newest(tmp_path):
    """The 201st save deletes the oldest session and leaves the newest 200."""
    store, clock = store_at(tmp_path, datetime(2026, 6, 1, tzinfo=timezone.utc))
    for index in range(201):
        save(store, f"s{index:03d}", when_messages=f"m{index}")
        clock.advance(seconds=1)
    assert store.load("s000") is None
    assert store.load("s001") is not None
    assert store.load("s200") is not None
    assert len(list((tmp_path / "sessions").glob("s*.json"))) == 200


def test_prune_keeps_a_session_aged_exactly_thirty_days(tmp_path):
    store, clock = store_at(tmp_path, datetime(2026, 1, 1, tzinfo=timezone.utc))
    save(store, "old", when_messages="old")
    clock.advance(days=30)
    save(store, "edge", when_messages="edge")
    assert store.load("old") is not None
    assert store.load("edge") is not None
    clock.advance(microseconds=1)
    save(store, "fresh", when_messages="fresh")
    assert store.load("old") is None
    assert store.load("edge") is not None
    assert store.load("fresh") is not None


def test_truncation_keeps_the_newest_fitting_messages(tmp_path):
    """A file over 8 MB drops the oldest messages, keeps the newest that fit, and stays truncated."""
    store = SessionStore(tmp_path)
    store.save(
        session_id="big", cwd="/workspace", model="one", effort="default", fast="off",
        messages=[
            {"role": "user", "content": UNDER_LIMIT},
            {"role": "assistant", "content": "tail"},
        ],
    )
    whole = store.load("big")
    assert whole is not None
    assert whole.truncated is False
    assert whole.title == "a" * 200
    assert whole.messages[0]["content"] == UNDER_LIMIT
    assert (tmp_path / "sessions" / "big.json").stat().st_size <= 8 * 1024 * 1024

    store.save(
        session_id="big", cwd="/workspace", model="one", effort="default", fast="off",
        messages=[
            {"role": "user", "content": OVER_LIMIT},
            {"role": "assistant", "content": "middle"},
            {"role": "user", "content": "newest"},
        ],
    )
    fitted = store.load("big")
    assert fitted is not None
    assert fitted.truncated is True
    assert fitted.messages == [
        {"role": "assistant", "content": "middle"},
        {"role": "user", "content": "newest"},
    ]
    assert fitted.title == whole.title
    assert (tmp_path / "sessions" / "big.json").stat().st_size <= 8 * 1024 * 1024

    store.save(
        session_id="big", cwd="/workspace", model="one", effort="default", fast="off",
        messages=[{"role": "user", "content": "later"}],
    )
    sticky = store.load("big")
    assert sticky is not None
    assert sticky.truncated is True
    assert sticky.messages == [{"role": "user", "content": "later"}]


def test_single_message_over_the_limit_is_dropped(tmp_path):
    store = SessionStore(tmp_path)
    store.save(
        session_id="huge", cwd="/workspace", model="one", effort="default", fast="off",
        messages=[{"role": "user", "content": OVER_LIMIT}],
    )
    loaded = store.load("huge")
    assert loaded is not None
    assert loaded.messages == []
    assert loaded.truncated is True
    assert loaded.title == "b" * 200


def test_session_id_rejects_traversal(tmp_path):
    """Ids outside [A-Za-z0-9_-]{1,128} never touch the filesystem."""
    store = SessionStore(tmp_path)
    outside = tmp_path.parent / "pwned.json"
    before = set(tmp_path.rglob("*"))
    for session_id in ("../x", "..", "a/b", "a\\b", "has.dot", "", "a" * 129, "id\n", "id ", ".", "a" * 128 + "!"):
        with pytest.raises(InvalidSessionId):
            save(store, session_id)
        with pytest.raises(InvalidSessionId):
            store.load(session_id)
    assert set(tmp_path.rglob("*")) == before
    assert not outside.exists()
    boundary = "A" + ("b" * 126) + "_"
    save(store, boundary, when_messages="ok")
    loaded = store.load(boundary)
    assert loaded is not None and loaded.messages == [{"role": "user", "content": "ok"}]
    assert store.load("missing-session") is None


def test_list_orders_filters_and_paginates(tmp_path):
    """Newest updatedAt is first, cwd matches after normalization, and the cursor is the next page."""
    store, clock = store_at(tmp_path, datetime(2026, 5, 1, tzinfo=timezone.utc))
    save(store, "zzz", cwd="/workspace", when_messages="  older\nrest")
    clock.advance(seconds=1)
    save(store, "aaa", cwd="/other", when_messages="newer")
    listed, cursor = store.list_sessions(cwd=None, cursor=None)
    assert [(item.session_id, item.title, item.cwd) for item in listed] == [
        ("aaa", "newer", "/other"),
        ("zzz", "older", "/workspace"),
    ]
    assert cursor is None
    same, _ = store.list_sessions(cwd="/workspace/", cursor=None)
    assert [item.session_id for item in same] == ["zzz"]
    other, _ = store.list_sessions(cwd="/workspace/sub", cursor=None)
    assert other == []

    for index in range(51):
        clock.advance(seconds=1)
        save(store, f"{50 - index:02d}x", cwd="/pages", when_messages=f"p{index}")
    first, next_cursor = store.list_sessions(cwd="/pages", cursor=None)
    assert [item.session_id for item in first] == [f"{index:02d}x" for index in range(50)]
    assert isinstance(next_cursor, str) and next_cursor not in {item.session_id for item in first}
    second, done = store.list_sessions(cwd="/pages", cursor=next_cursor)
    assert [item.session_id for item in second] == ["50x"]
    assert done is None
    with pytest.raises(InvalidCursor):
        store.list_sessions(cwd=None, cursor="not a cursor")


def test_unreadable_session_is_rejected_and_skipped(tmp_path):
    store = SessionStore(tmp_path)
    save(store, "good", when_messages="kept")
    path = tmp_path / "sessions" / "broken.json"
    path.write_text("{", encoding="utf-8")
    with pytest.raises(SessionDataError):
        store.load("broken")
    listed, _ = store.list_sessions(cwd=None, cursor=None)
    assert [item.session_id for item in listed] == ["good"]
    save(store, "good", when_messages="still")
    assert path.is_file()
