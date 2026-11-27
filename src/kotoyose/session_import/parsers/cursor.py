"""Parser for Cursor CLI chat stores (``~/.cursor/chats/<workspace>/<chat>/store.db``).

The store is a content-addressed SQLite database. ``meta['0']`` names the latest
root blob, a protobuf node that lists the conversation by referencing other blobs
with their 32-byte ids; message blobs themselves are JSON objects.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from kotoyose.session_import.models import ImportedTurn, normalize_session_title
from kotoyose.session_import.parsers.base import (
    TurnDraft,
    append_turn,
    as_dict,
    extract_typed_text,
    join_text_blocks,
    non_empty_str,
    parse_timestamp,
)

_BLOB_ID_BYTES = 32
_LENGTH_DELIMITED = 2
_FIXED_WIDTHS = {1: 8, 5: 4}
# Root field listing the archives of summarized (older) messages. It is serialized
# after the current message list although its messages come first in the conversation.
_ROOT_ARCHIVE_FIELD = 13
_TOOL_CALL_BLOCKS = ("tool-call", "tool_use")
_MESSAGE_TIME_KEYS = ("timestamp", "createdAt", "created_at")
# Blocks Cursor puts in front of (or instead of) the typed text.
_INJECTED_TAGS = (
    "additional_data",
    "agent_skills",
    "agent_transcripts",
    "available_subagent_models",
    "available_subagent_types",
    "conversation_summary",
    "dynamic_tools",
    "git_status",
    "hooks_context",
    "open_and_recently_viewed_files",
    "rules",
    "system_reminder",
    "timestamp",
    "user_info",
)
# A notification is written by the harness, including the <user_query> that follows it.
_NOTIFICATION = re.compile(r"\s*(?:<timestamp>[^<]*</timestamp>\s*)?<system_notification>")
# e.g. "<timestamp>Friday, Jan 2, 2026, 12:04 PM (UTC+9)</timestamp>"
_TIMESTAMP_TAG = re.compile(
    r"\s*<timestamp>(?:[A-Za-z]+, )?([A-Za-z]{3})[a-z]* (\d{1,2}), (\d{4}), (\d{1,2}):(\d{2}) ([AP]M) "
    r"\(UTC(?:([+-])(\d{1,2})(?::(\d{2}))?)?\)</timestamp>"
)
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")


@dataclass
class _Chat:
    """Metadata of one chat. Only these fields are taken; the store also holds a key."""

    session_id: str
    root_id: str
    cwd: str | None
    created_at: datetime | None
    updated_at: datetime | None
    session_title: str | None


@contextmanager
def _open_read_only(path: Path) -> Iterator[sqlite3.Connection]:
    """Open ``path`` without modifying it or creating SQLite sidecar files next to it."""
    database = path.resolve()
    with database.open("rb") as handle:
        wal_mode = handle.read(20)[18:] == b"\x02\x02"
    wal = database.with_name(f"{database.name}-wal")
    has_wal = wal.exists()
    with tempfile.TemporaryDirectory() as scratch:
        query = "?mode=ro"
        if wal_mode and not has_wal:
            # A plain read-only open would create -wal / -shm; the main file is complete here.
            query += "&immutable=1"
        elif has_wal and not database.with_name(f"{database.name}-shm").exists():
            # Reading an orphaned WAL rebuilds its index, so do that on a private copy.
            copy = Path(scratch) / database.name
            shutil.copyfile(database, copy)
            shutil.copyfile(wal, copy.with_name(wal.name))
            database = copy
        connection = sqlite3.connect(database.as_uri() + query, uri=True, timeout=1.0)
        try:
            yield connection
        finally:
            connection.close()


def _json_object(data: str | bytes) -> dict[str, Any]:
    try:
        return as_dict(json.loads(data))
    except (ValueError, RecursionError):
        return {}


def _read_sidecar(path: Path) -> dict[str, Any]:
    try:
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents) or not path.is_file():
            return {}
        return _json_object(path.read_bytes())
    except OSError:
        return {}


def _read_chat(connection: sqlite3.Connection, path: Path) -> _Chat | None:
    row = connection.execute("SELECT value FROM meta WHERE key = '0'").fetchone()
    if row is None or not isinstance(row[0], str):
        return None
    meta = _json_object(bytes.fromhex(row[0]))
    root_id = non_empty_str(meta.get("latestRootBlobId"))
    if root_id is None:
        return None
    title = normalize_session_title(meta.get("name"))
    if title and title.casefold() in {"new agent", "new chat", "untitled"}:
        title = None
    sidecar = _read_sidecar(path.parent / "meta.json")
    return _Chat(
        session_id=non_empty_str(meta.get("agentId")) or path.parent.name or path.stem,
        root_id=root_id,
        session_title=title,
        cwd=non_empty_str(sidecar.get("cwd")),
        created_at=parse_timestamp(meta.get("createdAt")) or parse_timestamp(sidecar.get("createdAtMs")),
        updated_at=parse_timestamp(sidecar.get("updatedAtMs")),
    )


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    for shift in range(0, 70, 7):
        if offset >= len(data):
            break
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, offset
    raise ValueError("invalid protobuf varint")


def _blob_references(data: bytes) -> list[tuple[int, str]]:
    """Return ``(field number, hex id)`` for each id-sized field of a protobuf message.

    Data that is not a well-formed message has no references.
    """
    references: list[tuple[int, str]] = []
    offset = 0
    try:
        while offset < len(data):
            key, offset = _read_varint(data, offset)
            number, wire_type = key >> 3, key & 7
            if number == 0:
                return []
            if wire_type == 0:
                _, offset = _read_varint(data, offset)
                continue
            if wire_type == _LENGTH_DELIMITED:
                length, offset = _read_varint(data, offset)
            elif wire_type in _FIXED_WIDTHS:
                length = _FIXED_WIDTHS[wire_type]
            else:
                return []
            end = offset + length
            if end > len(data):
                return []
            if wire_type == _LENGTH_DELIMITED and length == _BLOB_ID_BYTES:
                references.append((number, data[offset:end].hex()))
            offset = end
    except ValueError:
        return []
    return references


def _iter_messages(connection: sqlite3.Connection, root_id: str) -> Iterator[dict[str, Any]]:
    """Walk the blob tree depth-first from ``root_id``, yielding JSON blobs in conversation order."""
    visited: set[str] = set()
    pending = [root_id]
    while pending:
        blob_id = pending.pop()
        if blob_id in visited:
            continue
        visited.add(blob_id)
        row = connection.execute("SELECT data FROM blobs WHERE id = ?", (blob_id,)).fetchone()
        if row is None or not isinstance(row[0], bytes):
            continue
        data = row[0]
        if data.startswith(b"{"):
            message = _json_object(data)
            if message:
                yield message
            continue
        references = _blob_references(data)
        if blob_id == root_id:
            references.sort(key=lambda reference: reference[0] != _ROOT_ARCHIVE_FIELD)
        # The stack pops from the end, so push in reverse to keep the listed order.
        pending.extend(child for _, child in reversed(references))


def _blocks(content: Any) -> list[dict[str, Any]]:
    return [block for block in content if isinstance(block, dict)] if isinstance(content, list) else []


def _user_texts(content: Any) -> list[str]:
    if isinstance(content, str):
        return [content]
    return [block["text"] for block in _blocks(content) if block.get("type") == "text" and isinstance(block.get("text"), str)]


def _typed_prompt(message: dict[str, Any]) -> str:
    if as_dict(as_dict(message.get("providerOptions")).get("cursor")).get("isSummary") is True:
        return ""
    texts = _user_texts(message.get("content"))
    if any(_NOTIFICATION.match(text) for text in texts):
        return ""
    typed = (extract_typed_text(text, _INJECTED_TAGS) for text in texts)
    return "\n\n".join(text for text in typed if text)


def _parse_timestamp_tag(text: str) -> datetime | None:
    match = _TIMESTAMP_TAG.match(text)
    if match is None:
        return None
    month, day, year, hour, minute, half, sign, offset_hours, offset_minutes = match.groups()
    offset = timedelta(hours=int(offset_hours or 0), minutes=int(offset_minutes or 0))
    try:
        local = datetime(
            int(year),
            _MONTHS.index(month.lower()) + 1,
            int(day),
            int(hour) % 12 + (12 if half == "PM" else 0),
            int(minute),
            tzinfo=timezone(-offset if sign == "-" else offset),
        )
        return local.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def _message_time(message: dict[str, Any]) -> datetime | None:
    for key in _MESSAGE_TIME_KEYS:
        if parsed := parse_timestamp(message.get(key)):
            return parsed
    return None


def _prompt_time(message: dict[str, Any], chat: _Chat, turns: list[ImportedTurn]) -> datetime | None:
    """Return when a prompt was sent, never earlier than the previous turn.

    Without a time on the message, turns are spaced one second apart from the chat's
    creation and capped at its last update. Spreading them evenly over that range would
    move earlier turns, and so their request ids, whenever the chat grows.
    """
    stamp = _message_time(message)
    if stamp is None:
        stamp = next(filter(None, map(_parse_timestamp_tag, _user_texts(message.get("content")))), None)
    if stamp is None and chat.created_at is not None:
        stamp = chat.created_at + timedelta(seconds=len(turns))
        if chat.updated_at is not None:
            stamp = min(stamp, chat.updated_at)
    if stamp is None or not turns:
        return stamp
    return max(stamp, turns[-1].completed_at)


def _model_name(message: dict[str, Any]) -> str | None:
    """Find the model name. Of a content block only its provider metadata is read."""
    for holder in (message, *_blocks(message.get("content"))):
        options = as_dict(holder.get("providerOptions"))
        for source in (holder, options, *map(as_dict, options.values())):
            if model := non_empty_str(source.get("modelName")) or non_empty_str(source.get("model")):
                return model
    return None


def _collect_turns(source_kind: str, chat: _Chat, messages: Iterator[dict[str, Any]]) -> list[ImportedTurn]:
    turns: list[ImportedTurn] = []
    draft: TurnDraft | None = None

    for message in messages:
        role = message.get("role")
        if role == "user":
            prompt = _typed_prompt(message)
            if not prompt:
                continue
            append_turn(turns, source_kind, draft)
            sent_at = _prompt_time(message, chat, turns)
            # Replies carry no time of their own; reusing the prompt's keeps request ids stable.
            draft = TurnDraft(session_id=chat.session_id, prompt=prompt, received_at=sent_at, completed_at=sent_at, cwd=chat.cwd)
        elif draft is None:
            continue
        elif role == "tool":
            draft.has_tool_calls = True
        elif role == "assistant":
            content = message.get("content")
            if any(block.get("type") in _TOOL_CALL_BLOCKS for block in _blocks(content)):
                draft.has_tool_calls = True
            if text := join_text_blocks(content, ("text",)):
                draft.response_parts.append(text)
            draft.model = _model_name(message) or draft.model
            draft.completed_at = _message_time(message) or draft.completed_at

    append_turn(turns, source_kind, draft)
    for turn in turns:
        turn.session_title = chat.session_title
    return turns


class CursorSessionParser:
    """Pair typed prompts with visible assistant text; reasoning and tool payloads are dropped."""

    source_kind = "cursor"

    def parse_file(self, path: Path) -> list[ImportedTurn]:
        """Return the turns of a ``store.db``; an unreadable, locked or damaged store yields ``[]``."""
        path = Path(path)
        try:
            with _open_read_only(path) as connection:
                chat = _read_chat(connection, path)
                if chat is None:
                    return []
                return _collect_turns(self.source_kind, chat, _iter_messages(connection, chat.root_id))
        except (OSError, sqlite3.Error, ValueError):
            return []
