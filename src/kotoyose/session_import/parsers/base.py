"""Parser contract and shared normalization helpers for local session logs."""

from __future__ import annotations

from contextlib import contextmanager
import io
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, BinaryIO, Protocol

from kotoyose.session_import.models import DEFAULT_FALLBACK_EPOCH, ImportedTurn, ensure_utc

_MILLISECONDS_THRESHOLD = 1e12
_MAX_DECOMPRESSED_BYTES = 256 * 1024 * 1024
_DRIVE_ROOT = re.compile(r"[A-Za-z]:")
_UNSAFE_BASENAMES = ("", ".", "..", "tmp")
_OPENING_TAG = re.compile(r"<([A-Za-z_][\w-]*)(?:\s[^<>]*)?>")
_QUERY_OPEN = "<user_query>"
_QUERY_CLOSE = "</user_query>"


class SessionParser(Protocol):
    """Plugin that turns one local session log file into normalized turns."""

    source_kind: str

    def parse_file(self, path: Path) -> list[ImportedTurn]:
        """Return the complete turns found in ``path``, in log order."""
        ...


def parse_timestamp(value: Any) -> datetime | None:
    """Parse ISO-8601, ``datetime`` or Unix (seconds / milliseconds) values into UTC.

    Returns ``None`` for anything that cannot be interpreted.
    """
    if isinstance(value, bool):
        return None
    try:
        if isinstance(value, datetime):
            return ensure_utc(value)
        if isinstance(value, (int, float)):
            seconds = value / 1000 if value > _MILLISECONDS_THRESHOLD else value
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        if isinstance(value, str):
            return ensure_utc(datetime.fromisoformat(value.strip()))
    except (ValueError, OverflowError, OSError):
        return None
    return None


def fallback_timestamp(turn_index: int, previous_ts: datetime | None = None) -> datetime:
    """Return a deterministic, order-preserving UTC timestamp for a turn without one.

    File mtime is deliberately not used: it changes between runs and would break
    the deterministic request id.
    """
    if previous_ts is not None:
        return ensure_utc(previous_ts) + timedelta(seconds=1)
    return DEFAULT_FALLBACK_EPOCH + timedelta(seconds=turn_index + 1)


def extract_cwd_basename(cwd: str | None) -> str | None:
    """Return only the last path segment of ``cwd`` (POSIX or Windows), if it is meaningful."""
    if not isinstance(cwd, str):
        return None
    basename = re.split(r"[/\\]", cwd.strip().rstrip("/\\"))[-1]
    if basename in _UNSAFE_BASENAMES or _DRIVE_ROOT.fullmatch(basename):
        return None
    return basename


class _BoundedReader(io.RawIOBase):
    """Stop streaming decompression once a file exceeds ``limit`` decoded bytes."""

    def __init__(self, stream: BinaryIO, limit: int) -> None:
        self._stream = stream
        self._remaining = limit

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: bytearray | memoryview) -> int:
        if self._remaining <= 0:
            return 0
        chunk = self._stream.read(min(len(buffer), self._remaining))
        if not chunk:
            return 0
        size = len(chunk)
        buffer[:size] = chunk
        self._remaining -= size
        return size


@contextmanager
def _open_jsonl_lines(path: Path) -> Iterator[Iterator[str]]:
    if not path.name.endswith(".zst"):
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            yield handle
        return
    try:
        from compression import zstd as stdlib_zstd  # type: ignore[import-not-found]
    except ImportError:
        stdlib_zstd = None
    if stdlib_zstd is not None:
        with stdlib_zstd.open(path, "rb") as raw:
            with io.TextIOWrapper(io.BufferedReader(_BoundedReader(raw, _MAX_DECOMPRESSED_BYTES)), encoding="utf-8", errors="replace") as text:
                yield text
        return
    import zstandard

    with path.open("rb") as raw:
        with zstandard.ZstdDecompressor().stream_reader(raw, read_across_frames=True) as reader:
            with io.TextIOWrapper(io.BufferedReader(_BoundedReader(reader, _MAX_DECOMPRESSED_BYTES)), encoding="utf-8", errors="replace") as text:
                yield text


def iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    """Yield the JSON objects of a JSONL (or ``.jsonl.zst``) file, skipping malformed lines."""
    target = Path(path)
    try:
        with _open_jsonl_lines(target) as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except (ValueError, RecursionError):
                    continue
                if isinstance(value, dict):
                    yield value
    except (OSError, EOFError, ValueError):
        return
    except Exception as exc:
        if type(exc).__name__ == "ZstdError":
            return
        raise


def as_dict(value: Any) -> dict[str, Any]:
    """Return ``value`` if it is a JSON object, otherwise an empty one."""
    return value if isinstance(value, dict) else {}


def non_empty_str(value: Any) -> str | None:
    """Return ``value`` if it is a string with non-whitespace content."""
    return value if isinstance(value, str) and value.strip() else None


def token_count(value: Any) -> int | None:
    """Return ``value`` if it is an integer token count (``bool`` is rejected)."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def add_tokens(total: int | None, value: Any) -> int | None:
    """Add a token count to a running total, keeping ``None`` while nothing was reported."""
    count = token_count(value)
    if count is None:
        return total
    return count if total is None else total + count


def join_text_blocks(content: Any, block_types: tuple[str, ...]) -> str:
    """Extract user-visible text from a string or a list of typed content blocks.

    Only blocks whose ``type`` is listed in ``block_types`` are read.
    """
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    texts = [
        block["text"].strip()
        for block in content
        if isinstance(block, dict) and block.get("type") in block_types and non_empty_str(block.get("text"))
    ]
    return "\n\n".join(texts)


def extract_typed_text(text: str, injected_tags: tuple[str, ...]) -> str:
    """Return what the person typed in a user text block that a harness may have decorated.

    Complete ``<tag>…</tag>`` blocks listed in ``injected_tags`` are removed from the
    start of ``text``. A ``<user_query>`` wrapper that follows them is unwrapped and
    its content is returned untouched. Tags later in the text are the person's own.
    """
    rest = text.strip()
    while opening := _OPENING_TAG.match(rest):
        tag = opening.group(1)
        closing = f"</{tag}>"
        end = rest.find(closing, opening.end())
        if tag not in injected_tags or end < 0:
            break
        rest = rest[end + len(closing) :].lstrip()
    if rest.startswith(_QUERY_OPEN):
        end = rest.rfind(_QUERY_CLOSE)
        if end >= 0:
            return rest[len(_QUERY_OPEN) : end].strip()
    return rest


@dataclass
class TurnDraft:
    """A turn that is still being collected from log lines."""

    session_id: str
    prompt: str
    received_at: datetime | None = None
    completed_at: datetime | None = None
    model: str | None = None
    cwd: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    has_tool_calls: bool = False
    is_subagent: bool = False
    agent_origin: str | None = None
    response_parts: list[str] = field(default_factory=list)

    def response_text(self) -> str:
        """Return the assistant response collected so far."""
        return "\n\n".join(self.response_parts)

    def token_totals(self) -> tuple[int | None, int | None]:
        """Return ``(tokens_in, tokens_out)`` for the turn."""
        return self.tokens_in, self.tokens_out


def append_turn(turns: list[ImportedTurn], source_kind: str, draft: TurnDraft | None) -> None:
    """Finalize ``draft`` into ``turns`` unless either side of the conversation is empty.

    Assigns the next 0-based ``turn_index`` and fills missing timestamps.
    """
    if draft is None:
        return
    prompt = draft.prompt.strip()
    response = draft.response_text().strip()
    if not prompt or not response:
        return

    turn_index = len(turns)
    received_at = draft.received_at or draft.completed_at
    completed_at = draft.completed_at or draft.received_at
    timestamp_fallback = received_at is None
    if received_at is None or completed_at is None:
        previous_ts = turns[-1].completed_at if turns else None
        received_at = completed_at = fallback_timestamp(turn_index, previous_ts)

    tokens_in, tokens_out = draft.token_totals()
    turns.append(
        ImportedTurn(
            source_kind=source_kind,
            session_id=draft.session_id,
            turn_index=turn_index,
            received_at=received_at,
            completed_at=max(received_at, completed_at),
            prompt=prompt,
            response=response,
            model=draft.model or "unknown",
            provider=source_kind,
            cwd_basename=extract_cwd_basename(draft.cwd),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            has_tool_calls=draft.has_tool_calls,
            timestamp_fallback=timestamp_fallback,
            is_subagent=draft.is_subagent,
            agent_origin=draft.agent_origin,
        )
    )
