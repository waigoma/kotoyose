"""ローカル会話の正規化、冪等な保存、増分スキャンを提供する。"""

from __future__ import annotations

import hashlib
import json
import socket
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .. import db
from ..config import get_config
from .models import (
    IMPORT_VERSION, VALID_ORIGIN_HINTS, FileLedgerEntry, ImportedTurn, ImportOptions,
    ImportSummary, SourceStatus, ensure_utc, format_utc_iso, normalize_session_title,
)
from .parsers import get_parser, list_parser_kinds
from .paths import configured_allowed_roots, default_source_roots, hash_source_path, iter_safe_source_files
from .redaction import sanitize_text


def compute_request_id(source_kind: str, source_machine: str, session_id: str, turn_index: int, completed_at: datetime) -> str:
    """同一マシン・セッション・ターン・完了時刻から決定論的 ID を返す。"""
    key = f"{source_machine}|{session_id}|{turn_index}|{format_utc_iso(completed_at)}"
    return f"import:{source_kind}:{hashlib.sha256(key.encode('utf-8')).hexdigest()[:32]}"


def build_interaction_row(
    turn: ImportedTurn, *, source_machine: str, source_path_hash: str,
    origin_hint: str | None = None, imported_at: datetime | None = None,
) -> tuple[dict[str, Any], bool, bool]:
    """資格情報を除去した interactions 行と、除去・切り詰めの有無を返す。"""
    prompt, prompt_redacted, prompt_truncated = sanitize_text(turn.prompt)
    response, response_redacted, response_truncated = sanitize_text(turn.response)
    was_redacted = prompt_redacted or response_redacted
    was_truncated = prompt_truncated or response_truncated
    metadata = {
        "imported": True, "import_version": IMPORT_VERSION,
        "source_kind": turn.source_kind, "source_machine": source_machine,
        "source_path_hash": source_path_hash, "project": turn.project or turn.cwd_basename,
        "origin_hint": origin_hint, "turn_index": turn.turn_index,
        "has_tool_calls": turn.has_tool_calls, "truncated": was_truncated, "redacted": was_redacted,
        "imported_at": format_utc_iso(imported_at or datetime.now(timezone.utc)),
    }
    if isinstance(turn.session_title, str):
        title, title_redacted, title_truncated = sanitize_text(turn.session_title)
        normalized = normalize_session_title(title)
        was_redacted |= title_redacted
        was_truncated |= title_truncated or len(" ".join(title.split())) > 160
        if normalized:
            metadata["session_title"] = normalized
        metadata.update(redacted=was_redacted, truncated=was_truncated)
    if turn.timestamp_fallback:
        metadata["timestamp_fallback"] = True
    if turn.is_subagent:
        metadata["is_subagent"] = True
    if turn.agent_origin is not None:
        metadata["agent_origin"] = turn.agent_origin
    provider = turn.provider or turn.source_kind
    row = {
        "request_id": compute_request_id(turn.source_kind, source_machine, turn.session_id, turn.turn_index, turn.completed_at),
        "conversation_key": f"import:{turn.source_kind}:{turn.session_id}",
        "provider": provider, "runtime_session_id": turn.session_id,
        "claude_session_id": turn.session_id if provider == "claude" else None,
        "model": turn.model or "unknown", "source": f"import:{turn.source_kind}",
        "received_at": ensure_utc(turn.received_at), "completed_at": ensure_utc(turn.completed_at),
        "prompt": prompt, "response": response, "prompt_chars": len(prompt), "response_chars": len(response),
        "tokens_in": turn.tokens_in, "tokens_out": turn.tokens_out,
        "tokens_source": "import" if turn.tokens_in is not None or turn.tokens_out is not None else None,
        "streamed": False, "status": "completed", "metadata": metadata,
    }
    return row, was_redacted, was_truncated


_INTERACTION_COLUMNS = (
    "request_id", "conversation_key", "provider", "runtime_session_id", "claude_session_id",
    "model", "source", "received_at", "completed_at", "prompt", "response", "prompt_chars",
    "response_chars", "tokens_in", "tokens_out", "tokens_source", "streamed", "status", "metadata",
)
_DEFAULT_POOL = object()


class SessionImportStore:
    """Postgres または独立したインメモリ領域へ保存する。

    pool 省略時は db._pool を使用し、明示的な None はインメモリを選択する。
    """

    def __init__(self, pool=_DEFAULT_POOL):
        self.pool = db._pool if pool is _DEFAULT_POOL else pool
        self._files: dict[tuple[str, str, str], FileLedgerEntry] = {}
        self._interactions: dict[str, dict[str, Any]] = {}
        self._runs: list[dict[str, Any]] = []

    async def get_file_ledger(self, source_kind: str, source_machine: str, path_hash: str) -> FileLedgerEntry | None:
        """ファイル台帳を返す。未登録なら None。"""
        if self.pool is None:
            entry = self._files.get((source_kind, source_machine, path_hash))
            return replace(entry) if entry else None
        row = await self.pool.fetchrow(
            "SELECT * FROM kotoyose.session_import_files WHERE source_kind = $1 AND source_machine = $2 AND path_hash = $3",
            source_kind, source_machine, path_hash,
        )
        return FileLedgerEntry(**dict(row)) if row else None

    async def upsert_file_ledger(self, entry: FileLedgerEntry) -> None:
        """台帳の進捗を置換する。取り込み件数は呼び出し側で累積する。"""
        entry = replace(entry, last_imported_at=entry.last_imported_at or datetime.now(timezone.utc))
        if self.pool is None:
            self._files[(entry.source_kind, entry.source_machine, entry.path_hash)] = entry
            return
        await self.pool.execute(
            """INSERT INTO kotoyose.session_import_files
               (source_kind, source_machine, path_hash, file_mtime_ns, file_size,
                last_turn_index, imported_count, skipped_count, last_imported_at)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
               ON CONFLICT (source_kind, source_machine, path_hash) DO UPDATE SET
                 file_mtime_ns = EXCLUDED.file_mtime_ns, file_size = EXCLUDED.file_size,
                 last_turn_index = EXCLUDED.last_turn_index, imported_count = EXCLUDED.imported_count,
                 skipped_count = EXCLUDED.skipped_count, last_imported_at = EXCLUDED.last_imported_at""",
            *asdict(entry).values(),
        )

    async def find_existing_request_ids(self, request_ids: Sequence[str]) -> set[str]:
        """重複キーを読み取り専用で検索する。"""
        if not request_ids:
            return set()
        if self.pool is None:
            return set(request_ids).intersection(self._interactions)
        try:
            rows = await self.pool.fetch(
                "SELECT request_id FROM kotoyose.interactions WHERE request_id = ANY($1::text[])", list(request_ids),
            )
        except Exception as exc:
            if getattr(exc, "sqlstate", None) == "42P01":
                return set()
            raise
        return {row["request_id"] for row in rows}

    async def find_live_session_ids(self, provider: str, session_ids: Sequence[str]) -> set[str]:
        """候補のうち、同じ provider の未削除・非 import 行に存在する ID を返す。

        読み取り専用で検索し、候補が空またはテーブル未作成なら空集合を返す。
        その他の DB エラーは呼び出し元へ伝播する。
        """
        if not session_ids:
            return set()
        if self.pool is None:
            candidates = set(session_ids)
            result = set()
            for row in self._interactions.values():
                session_id = row.get("runtime_session_id")
                if session_id is None:
                    session_id = row.get("claude_session_id")
                if (row.get("provider") == provider and session_id in candidates
                        and not str(row.get("source") or "").startswith("import:")
                        and row.get("deleted_at") is None):
                    result.add(session_id)
            return result
        try:
            rows = await self.pool.fetch(
                """SELECT DISTINCT COALESCE(runtime_session_id, claude_session_id) AS session_id
                   FROM kotoyose.interactions
                   WHERE provider = $1
                     AND COALESCE(runtime_session_id, claude_session_id) = ANY($2::text[])
                     AND (source IS NULL OR source NOT LIKE 'import:%')
                     AND deleted_at IS NULL""",
                provider, list(session_ids),
            )
        except Exception as exc:
            if getattr(exc, "sqlstate", None) == "42P01":
                return set()
            raise
        return {row["session_id"] for row in rows}

    async def insert_interaction(self, row: dict[str, Any]) -> bool:
        """重複を無視して保存し、新規挿入時だけ True を返す。"""
        if self.pool is None:
            if row["request_id"] in self._interactions:
                return False
            self._interactions[row["request_id"]] = deepcopy(row)
            return True
        columns = _INTERACTION_COLUMNS
        values = [json.dumps(row[key]) if key == "metadata" else row[key] for key in columns]
        result = await self.pool.fetchrow(
            f"INSERT INTO kotoyose.interactions ({', '.join(columns)}) "
            f"VALUES ({', '.join(f'${i}' for i in range(1, len(columns) + 1))}) "
            "ON CONFLICT (request_id) DO NOTHING RETURNING id", *values,
        )
        return result is not None

    async def record_run(self, summary: ImportSummary) -> None:
        """完了した実行結果のスナップショットを保存する。"""
        if self.pool is None:
            self._runs.append(summary.to_dict())
            return
        row = asdict(summary)
        columns = tuple(row)
        values = [json.dumps(row[key]) if key == "errors" else row[key] for key in columns]
        await self.pool.execute(
            f"INSERT INTO kotoyose.session_import_runs ({', '.join(columns)}) "
            f"VALUES ({', '.join(f'${i}' for i in range(1, len(columns) + 1))})", *values,
        )

    async def list_runs(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """新しい実行順に JSON 化可能な結果を最大 limit 件返す。"""
        if limit < 0:
            raise ValueError("limit must be non-negative")
        if self.pool is None:
            return deepcopy(sorted(self._runs, key=lambda row: row["started_at"], reverse=True)[:limit])
        rows = await self.pool.fetch("SELECT * FROM kotoyose.session_import_runs ORDER BY started_at DESC LIMIT $1", limit)
        results = []
        for row in rows:
            result = dict(row)
            for key in ("started_at", "completed_at"):
                if isinstance(result[key], datetime):
                    result[key] = format_utc_iso(result[key])
            if isinstance(result["errors"], str):
                result["errors"] = json.loads(result["errors"])
            results.append(result)
        return results

    async def source_ledger_stats(self, source_machine: str | None = None) -> dict[str, dict[str, Any]]:
        """ソース別の最終取り込み時刻、ファイル数、累積取り込み数を返す。"""
        if self.pool is not None:
            rows = await self.pool.fetch(
                """SELECT source_kind, MAX(last_imported_at) AS last_imported_at,
                          COUNT(*) AS imported_files, SUM(imported_count) AS imported_turns
                   FROM kotoyose.session_import_files
                   WHERE ($1::text IS NULL OR source_machine = $1) GROUP BY source_kind""", source_machine,
            )
            return {row["source_kind"]: {key: row[key] for key in ("last_imported_at", "imported_files", "imported_turns")} for row in rows}
        stats: dict[str, dict[str, Any]] = {}
        for entry in self._files.values():
            if source_machine is not None and entry.source_machine != source_machine:
                continue
            item = stats.setdefault(entry.source_kind, {"last_imported_at": None, "imported_files": 0, "imported_turns": 0})
            item["imported_files"] += 1
            item["imported_turns"] += entry.imported_count
            if entry.last_imported_at and (item["last_imported_at"] is None or entry.last_imported_at > item["last_imported_at"]):
                item["last_imported_at"] = entry.last_imported_at
        return stats


def parse_since(value: str | datetime | None) -> datetime | None:
    """日付または ISO 日時を UTC へ変換する。不正値は ValueError。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return ensure_utc(value)
    try:
        return ensure_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except (TypeError, ValueError, AttributeError):
        raise ValueError("since must be an ISO date or datetime") from None


def _resolve_store(pool, store):
    return store if store is not None else SessionImportStore(db._pool if pool is None else pool)


async def run_import(options: ImportOptions, *, pool=None, store: SessionImportStore | None = None, home: Path | None = None) -> ImportSummary:
    """安全なパスから会話を取り込む。dry-run は読み取りのみを行う。

    不正なオプションや許可範囲外のパスは ValueError を送出する。
    パース失敗は本文やパスを含めず errors に記録し、次のファイルへ進む。
    """
    kinds = list_parser_kinds()
    if options.source != "all" and options.source not in kinds:
        raise ValueError(f"unknown session source: {options.source}")
    if options.origin_hint is not None and options.origin_hint not in VALID_ORIGIN_HINTS:
        raise ValueError("origin_hint must be personal or work")
    if options.source == "all" and options.path is not None:
        raise ValueError("--path requires a specific --source (not 'all')")
    if options.max_files is not None and options.max_files < 0:
        raise ValueError("max_files must be non-negative")
    since = parse_since(options.since)
    source_machine = options.source_machine or socket.gethostname()
    store = _resolve_store(pool, store)
    allowed_roots = options.allowed_roots
    if allowed_roots is None:
        allowed_roots = configured_allowed_roots(extra_roots=get_config().session_import_allowed_roots, home=home)
    summary = ImportSummary(
        run_id=uuid4().hex, source_kind=options.source, source_machine=source_machine,
        dry_run=options.dry_run, origin_hint=options.origin_hint, since=format_utc_iso(since) if since else None,
    )
    sessions: set[tuple[str, str]] = set()
    seen_request_ids: set[str] = set()
    visited = 0
    roots = default_source_roots(home)
    for source_kind in kinds if options.source == "all" else (options.source,):
        root = options.path if options.path is not None else roots.get(source_kind)
        if root is None:
            continue
        for file_path in iter_safe_source_files(source_kind, root, allowed_roots=allowed_roots):
            if options.max_files is not None and visited >= options.max_files:
                break
            visited += 1
            path_hash = hash_source_path(file_path)
            ledger = None
            if not options.dry_run:
                ledger = await store.get_file_ledger(source_kind, source_machine, path_hash)
            try:
                stat = file_path.stat()
                if ledger and ledger.file_mtime_ns == stat.st_mtime_ns and ledger.file_size == stat.st_size:
                    summary.files_skipped_unchanged += 1
                    continue
                turns = list(get_parser(source_kind).parse_file(file_path))
            except Exception as exc:
                summary.errors.append(f"{source_kind}:{path_hash}: {type(exc).__name__}")
                continue
            summary.files_scanned += 1
            live_sessions: set[tuple[str, str]] = set()
            if options.skip_live_duplicates and turns:
                sessions_by_provider: dict[str, set[str]] = {}
                for turn in turns:
                    sessions_by_provider.setdefault(turn.provider or turn.source_kind, set()).add(turn.session_id)
                for provider, session_ids in sessions_by_provider.items():
                    live_sessions.update(
                        (provider, session_id)
                        for session_id in await store.find_live_session_ids(provider, sorted(session_ids))
                    )
            rows = []
            file_skipped_live = 0
            file_skipped_since = 0
            for turn in turns:
                sessions.add((source_kind, turn.session_id))
                if since and ensure_utc(turn.completed_at) < since:
                    summary.turns_skipped_since += 1
                    file_skipped_since += 1
                    continue
                summary.turns_extracted += 1
                if (turn.provider or turn.source_kind, turn.session_id) in live_sessions:
                    summary.turns_skipped_duplicate += 1
                    file_skipped_live += 1
                    continue
                row, redacted, truncated = build_interaction_row(
                    turn, source_machine=source_machine, source_path_hash=path_hash, origin_hint=options.origin_hint,
                )
                rows.append(row)
                summary.redactions_count += redacted
                summary.truncations_count += truncated
            if options.dry_run:
                existing = await store.find_existing_request_ids([row["request_id"] for row in rows])
                for row in rows:
                    request_id = row["request_id"]
                    if request_id in existing or request_id in seen_request_ids:
                        summary.turns_skipped_duplicate += 1
                    else:
                        summary.turns_imported += 1
                    seen_request_ids.add(request_id)
                continue
            imported = skipped = 0
            for row in rows:
                if await store.insert_interaction(row):
                    imported += 1
                else:
                    skipped += 1
            summary.turns_imported += imported
            summary.turns_skipped_duplicate += skipped
            if file_skipped_since == 0:
                await store.upsert_file_ledger(FileLedgerEntry(
                    source_kind=source_kind, source_machine=source_machine, path_hash=path_hash,
                    file_mtime_ns=stat.st_mtime_ns, file_size=stat.st_size,
                    last_turn_index=max((turn.turn_index for turn in turns), default=-1),
                    imported_count=(ledger.imported_count if ledger else 0) + imported,
                    skipped_count=skipped + file_skipped_live,
                ))
    summary.sessions_detected = len(sessions)
    summary.completed_at = datetime.now(timezone.utc)
    if not options.dry_run:
        await store.record_run(summary)
    return summary


async def list_import_sources(
    *, pool=None, store: SessionImportStore | None = None, home: Path | None = None,
    allowed_roots: Sequence[Path] | None = None,
) -> list[SourceStatus]:
    """既定ソースの安全なファイル数と台帳統計を返す。"""
    store = _resolve_store(pool, store)
    stats = await store.source_ledger_stats()
    if allowed_roots is None:
        allowed_roots = configured_allowed_roots(extra_roots=get_config().session_import_allowed_roots, home=home)
    roots = default_source_roots(home)
    return [SourceStatus(
        source_kind=kind, default_path=str(roots[kind]), exists=roots[kind].exists(),
        file_count=len(iter_safe_source_files(kind, roots[kind], allowed_roots=allowed_roots)),
        **stats.get(kind, {}),
    ) for kind in list_parser_kinds() if kind in roots]


async def list_import_runs(*, limit: int = 20, pool=None, store: SessionImportStore | None = None) -> list[dict[str, Any]]:
    """直近の取り込み履歴を新しい順に返す。"""
    return await _resolve_store(pool, store).list_runs(limit=limit)
