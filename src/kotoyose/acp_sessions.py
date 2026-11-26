"""Local history for the stdio ACP agent."""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path


_SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_MAX_SESSIONS = 200
_MAX_AGE = timedelta(days=30)
_MAX_BYTES = 8 * 1024 * 1024
_PAGE_SIZE = 50


class InvalidSessionId(ValueError):
    pass


class InvalidCursor(ValueError):
    pass


class SessionDataError(ValueError):
    pass


def acp_state_dir() -> Path:
    if override := os.environ.get("KOTOYOSE_ACP_STATE_DIR"):
        return Path(override).expanduser()
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        return (Path(local) if local else Path.home() / "AppData" / "Local") / "kotoyose-acp"
    xdg = os.environ.get("XDG_STATE_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "state") / "kotoyose-acp"


def same_cwd(left: str, right: str) -> bool:
    return os.path.normcase(os.path.normpath(left)) == os.path.normcase(os.path.normpath(right))


def _session_id(value: object) -> str:
    if not isinstance(value, str) or not _SESSION_ID.fullmatch(value):
        raise InvalidSessionId("Invalid sessionId")
    return value


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _title(messages: list[dict]) -> str:
    for message in messages:
        if message["role"] != "user":
            continue
        content = message["content"]
        if isinstance(content, list):
            content = "\n".join(part["text"] for part in content if part["type"] == "text")
        return re.split(r"[\r\n]", content, maxsplit=1)[0].strip()[:200]
    return ""


@dataclass(frozen=True)
class SessionInfo:
    session_id: str
    cwd: str
    title: str
    model: str
    effort: str
    fast: str
    created_at: str
    updated_at: str
    truncated: bool

    def sort_key(self) -> tuple[str, str]:
        return self.updated_at, self.session_id


@dataclass(frozen=True)
class StoredSession(SessionInfo):
    messages: list[dict]


def _read_info(data: dict, session_id: str) -> SessionInfo:
    info = SessionInfo(**data)
    if info.session_id != session_id or type(info.truncated) is not bool:
        raise ValueError
    if not all(isinstance(value, str) for key, value in data.items() if key != "truncated"):
        raise ValueError
    for timestamp in (info.created_at, info.updated_at):
        if timestamp != _timestamp(datetime.fromisoformat(timestamp)):
            raise ValueError
    return info


def _valid_message(message: object) -> bool:
    match message:
        case {"role": "user" | "assistant", "content": str()}:
            return True
        case {"role": "user", "content": list(parts)}:
            return all(_valid_part(part) for part in parts)
        case _:
            return False


def _valid_part(part: object) -> bool:
    match part:
        case {"type": "text", "text": str()}:
            return True
        case {"type": "image_url", "image_url": {"url": str(url)}}:
            return url.startswith("data:") and ";base64," in url
        case _:
            return False


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8", errors="backslashreplace")


def _cursor_key(cursor: str) -> tuple[str, str]:
    try:
        if len(cursor) > 256:
            raise ValueError
        timestamp, session_id = base64.b64decode(cursor, altchars=b"-_", validate=True).decode().split("\n")
        if timestamp != _timestamp(datetime.fromisoformat(timestamp)):
            raise ValueError
        return timestamp, _session_id(session_id)
    except (ValueError, OverflowError):
        raise InvalidCursor("Invalid session/list cursor") from None


class SessionStore:
    def __init__(self, root: Path, *, now: Callable[[], datetime] | None = None):
        self.root = root
        self.directory = root / "sessions"
        self._now = now if now is not None else lambda: datetime.now(UTC)

    def _read(self, path: Path, *, history: bool) -> SessionInfo | StoredSession | None:
        try:
            if path.is_symlink() or not path.is_file():
                return None
            with path.open("rb") as file:
                if history:
                    raw = file.read(_MAX_BYTES + 1)
                    if len(raw) > _MAX_BYTES:
                        raise ValueError
                    data = json.loads(raw)
                    messages = data.pop("messages")
                    if not isinstance(messages, list) or not all(_valid_message(message) for message in messages):
                        raise ValueError
                    _read_info(data, path.stem)
                    return StoredSession(**data, messages=messages)
                # The first line holds the metadata, so listing and pruning can skip the transcript.
                header = file.readline(_MAX_BYTES).rstrip(b"\n")
                if not header.endswith(b","):
                    raise ValueError
                return _read_info(json.loads(header[:-1] + b"}"), path.stem)
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
            raise SessionDataError("Saved session could not be read") from None

    def load(self, session_id: object) -> StoredSession | None:
        path = self.directory / f"{_session_id(session_id)}.json"
        session = self._read(path, history=True)
        if isinstance(session, StoredSession) and session.updated_at >= _timestamp(self._now() - _MAX_AGE):
            return session
        return None

    def _index(self) -> list[SessionInfo]:
        sessions = []
        try:
            for path in self.directory.iterdir():
                if path.suffix != ".json" or not _SESSION_ID.fullmatch(path.stem):
                    continue
                try:
                    info = self._read(path, history=False)
                except SessionDataError:
                    continue
                if info is not None:
                    sessions.append(info)
        except FileNotFoundError:
            pass
        return sorted(sessions, key=SessionInfo.sort_key, reverse=True)

    def list_sessions(self, *, cwd: str | None, cursor: str | None) -> tuple[list[SessionInfo], str | None]:
        cutoff = _timestamp(self._now() - _MAX_AGE)
        after = _cursor_key(cursor) if cursor is not None else None
        sessions = [
            info for info in self._index()
            if info.updated_at >= cutoff
            and (cwd is None or same_cwd(info.cwd, cwd))
            and (after is None or info.sort_key() < after)
        ]
        page = sessions[:_PAGE_SIZE]
        next_cursor = None
        if len(sessions) > _PAGE_SIZE:
            next_cursor = base64.urlsafe_b64encode("\n".join(page[-1].sort_key()).encode()).decode()
        return page, next_cursor

    def save(
        self, *, session_id: str, cwd: str, model: str, effort: str, fast: str,
        messages: list[dict], created_at: datetime | None = None,
    ) -> None:
        path = self.directory / f"{_session_id(session_id)}.json"
        now = self._now()
        previous = self._read(path, history=False)
        info = SessionInfo(
            session_id, cwd, previous.title if previous else _title(messages), model, effort, fast,
            previous.created_at if previous else _timestamp(created_at or now),
            _timestamp(now), previous.truncated if previous else False,
        )
        metadata = asdict(info)
        header = _json_bytes(metadata)[:-1] + b',\n"messages":['
        footer = b"]}\n"
        budget = _MAX_BYTES - len(header) - len(footer)
        if budget < 0:
            raise OSError("ACP session metadata exceeds the file size limit")
        newest = []
        for message in reversed(messages):
            encoded = _json_bytes(message)
            size = len(encoded) + bool(newest)
            if size > budget:
                metadata["truncated"] = True
                header = _json_bytes(metadata)[:-1] + b',\n"messages":['
                break
            newest.append(encoded)
            budget -= size

        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if sys.platform != "win32":
            self.root.chmod(0o700)
        self.directory.mkdir(mode=0o700, exist_ok=True)
        if sys.platform != "win32":
            self.directory.chmod(0o700)
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.directory, suffix=".tmp", delete=False) as file:
                temp_path = Path(file.name)
                if sys.platform != "win32":
                    os.fchmod(file.fileno(), 0o600)
                file.write(header)
                file.write(b",".join(reversed(newest)))
                file.write(footer)
            temp_path.replace(path)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

        cutoff = _timestamp(now - _MAX_AGE)
        for index, old in enumerate(self._index()):
            if index >= _MAX_SESSIONS or old.updated_at < cutoff:
                (self.directory / f"{old.session_id}.json").unlink(missing_ok=True)
