"""Shared data models and constants for session history import and export."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

IMPORT_VERSION = 1
MAX_TEXT_BYTES = 64 * 1024
KNOWN_SOURCE_KINDS: tuple[str, ...] = ("claude", "codex", "antigravity", "cursor", "grok")
VALID_ORIGIN_HINTS: tuple[str, ...] = ("personal", "work")
EXPORT_MAX_LIMIT = 100
EXPORT_MAX_BODY_BYTES = 2 * 1024 * 1024
DEFAULT_FALLBACK_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def ensure_utc(dt: datetime) -> datetime:
    """Return a timezone-aware UTC datetime."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_utc_iso(dt: datetime) -> str:
    """Format a datetime as canonical UTC ISO-8601 ending in 'Z'."""
    return ensure_utc(dt).isoformat().replace("+00:00", "Z")


@dataclass(slots=True)
class ImportedTurn:
    """Normalized intermediate representation for a single imported conversation turn."""

    source_kind: str
    session_id: str
    turn_index: int
    received_at: datetime
    completed_at: datetime
    prompt: str
    response: str
    model: str = "unknown"
    provider: str = ""
    project: str | None = None
    cwd_basename: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    has_tool_calls: bool = False
    timestamp_fallback: bool = False
    is_subagent: bool = False
    agent_origin: str | None = None

    def __post_init__(self) -> None:
        self.received_at = ensure_utc(self.received_at)
        self.completed_at = ensure_utc(self.completed_at)
        if not self.provider:
            self.provider = self.source_kind
        if not self.model:
            self.model = "unknown"
        if not self.project and self.cwd_basename:
            self.project = self.cwd_basename


@dataclass(slots=True)
class ImportOptions:
    """Options controlling a session import run."""

    source: str = "all"
    path: Path | None = None
    since: datetime | None = None
    origin_hint: str | None = None
    source_machine: str | None = None
    dry_run: bool = False
    allowed_roots: tuple[Path, ...] | None = None
    max_files: int | None = None
    skip_live_duplicates: bool = True


@dataclass(slots=True)
class FileLedgerEntry:
    """Incremental scan progress entry stored in kotoyose.session_import_files."""

    source_kind: str
    source_machine: str
    path_hash: str
    file_mtime_ns: int
    file_size: int
    last_turn_index: int = -1
    imported_count: int = 0
    skipped_count: int = 0
    last_imported_at: datetime | None = None


@dataclass(slots=True)
class SourceStatus:
    """Discovery summary for a single local session source."""

    source_kind: str
    default_path: str
    exists: bool
    file_count: int
    last_imported_at: datetime | None = None
    imported_files: int = 0
    imported_turns: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_kind": self.source_kind,
            "default_path": self.default_path,
            "exists": self.exists,
            "file_count": self.file_count,
            "last_imported_at": format_utc_iso(self.last_imported_at) if self.last_imported_at else None,
            "imported_files": self.imported_files,
            "imported_turns": self.imported_turns,
        }


@dataclass(slots=True)
class ImportSummary:
    """Aggregated result of a session import run (real or dry-run)."""

    run_id: str | None = None
    source_kind: str = "all"
    source_machine: str = ""
    dry_run: bool = False
    origin_hint: str | None = None
    since: str | None = None
    files_scanned: int = 0
    files_skipped_unchanged: int = 0
    sessions_detected: int = 0
    turns_extracted: int = 0
    turns_imported: int = 0
    turns_skipped_duplicate: int = 0
    turns_skipped_since: int = 0
    redactions_count: int = 0
    truncations_count: int = 0
    errors: list[str] = field(default_factory=list)
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "source_kind": self.source_kind,
            "source_machine": self.source_machine,
            "dry_run": self.dry_run,
            "origin_hint": self.origin_hint,
            "since": self.since,
            "files_scanned": self.files_scanned,
            "files_skipped_unchanged": self.files_skipped_unchanged,
            "sessions_detected": self.sessions_detected,
            "turns_extracted": self.turns_extracted,
            "turns_imported": self.turns_imported,
            "turns_skipped_duplicate": self.turns_skipped_duplicate,
            "turns_skipped_since": self.turns_skipped_since,
            "redactions_count": self.redactions_count,
            "truncations_count": self.truncations_count,
            "errors": list(self.errors),
            "started_at": format_utc_iso(self.started_at),
            "completed_at": format_utc_iso(self.completed_at) if self.completed_at else None,
        }
