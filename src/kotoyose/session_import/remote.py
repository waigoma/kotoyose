"""子側で秘匿化した履歴の収集と、ホスト側の冪等な取り込み。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..child_protocol import SESSION_COLLECT_BATCH_MAX_BYTES, SESSION_COLLECT_POLICY_REFUSAL
from .importer import SessionImportStore, _resolve_store, build_interaction_row, parse_since
from .models import VALID_ORIGIN_HINTS, FileLedgerEntry, ImportedTurn, ImportSummary, format_utc_iso, normalize_session_title
from .parsers import get_parser, list_parser_kinds
from .paths import default_source_roots, hash_source_path, iter_safe_source_files, safe_project_name
from .redaction import sanitize_text


def resolve_effective_origin_hint(*hints: str | None) -> str | None:
    """work を優先して分類を統合する。不正な分類は ValueError。"""
    if any(hint is not None and hint not in VALID_ORIGIN_HINTS for hint in hints):
        raise ValueError("origin_hint must be personal or work")
    return "work" if "work" in hints else "personal" if "personal" in hints else None


def _encoded_size(batch: dict) -> int:
    return len(json.dumps(batch, ensure_ascii=True, separators=(",", ":")).encode())


def collect_local_batches(
    *, allowed_sources: Sequence[str], source_kind: str = "all", since: datetime | None = None,
    max_files: int = 200, max_turns: int = 2000, max_batch_bytes: int = SESSION_COLLECT_BATCH_MAX_BYTES,
    home: Path | None = None, known_files: Mapping[str, tuple[int, int]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """既定ルートだけを収集し、秘匿化済みの上限付きバッチと統計を返す。

    許可されないソースは PermissionError、負の件数や不足するバッチ容量は ValueError。
    部分収集したファイルは台帳で完了扱いにしない。
    """
    kinds = [kind for kind in list_parser_kinds() if "all" in allowed_sources or kind in allowed_sources]
    if not kinds or (source_kind != "all" and source_kind not in kinds):
        raise PermissionError(SESSION_COLLECT_POLICY_REFUSAL)
    if min(max_files, max_turns) < 0 or max_batch_bytes <= 0:
        raise ValueError("collection limits must be non-negative and batch capacity positive")
    since = parse_since(since)
    stats: dict[str, Any] = dict(files_scanned=0, files_skipped_unchanged=0, turns_skipped_since=0,
                                 redactions_count=0, truncations_count=0, errors=[])
    roots = default_source_roots(home)
    batches = []
    visited = emitted = 0
    for kind in kinds if source_kind == "all" else [source_kind]:
        root = roots[kind]
        # 既定ルート自体が外部へのリンクでも走査しない。
        if root.is_symlink() or any(parent.is_symlink() for parent in root.parents):
            continue
        for path in iter_safe_source_files(kind, root):
            if visited >= max_files or emitted >= max_turns:
                break
            visited += 1
            path_hash = hash_source_path(path)
            try:
                stat = path.stat()
                if known_files is not None and tuple(known_files.get(path_hash, ())) == (stat.st_mtime_ns, stat.st_size):
                    stats["files_skipped_unchanged"] += 1
                    continue
                turns = list(get_parser(kind).parse_file(path))
                stats["files_scanned"] += 1
                skipped = sum(bool(since and turn.completed_at < since) for turn in turns)
                stats["turns_skipped_since"] += skipped
                selected = [turn for turn in turns if not since or turn.completed_at >= since]
                partial = len(selected) > max_turns - emitted
                selected = selected[:max_turns - emitted]
                base = dict(source_kind=kind, source_path_hash=path_hash, file_mtime_ns=stat.st_mtime_ns,
                            file_size=stat.st_size, file_skipped_since=skipped + int(partial),
                            last_turn_index=max((turn.turn_index for turn in turns), default=-1))
                file_batches = []
                batch = dict(base, turns=[])
                for turn in selected:
                    raw = asdict(turn)
                    raw["received_at"] = format_utc_iso(turn.received_at)
                    raw["completed_at"] = format_utc_iso(turn.completed_at)
                    raw["project"] = safe_project_name(turn.project)
                    raw["cwd_basename"] = safe_project_name(turn.cwd_basename)
                    redacted = truncated = False
                    for key, value in raw.items():
                        if isinstance(value, str):
                            raw[key], r, t = sanitize_text(value)
                            if key == "session_title":
                                t |= len(" ".join(raw[key].split())) > 160
                                raw[key] = normalize_session_title(raw[key])
                            redacted |= r
                            truncated |= t
                    raw.update(redacted=redacted, truncated=truncated)
                    stats["redactions_count"] += redacted
                    stats["truncations_count"] += truncated
                    if _encoded_size(dict(base, turns=[raw])) > max_batch_bytes:
                        raise ValueError("sanitized turn exceeds batch capacity")
                    if _encoded_size(dict(batch, turns=[*batch["turns"], raw])) > max_batch_bytes:
                        file_batches.append(batch)
                        batch = dict(base, turns=[])
                    batch["turns"].append(raw)
                if _encoded_size(batch) > max_batch_bytes:
                    raise ValueError("batch metadata exceeds capacity")
                file_batches.append(batch)
                batches.extend(file_batches)
                emitted += len(selected)
            except Exception as exc:
                stats["errors"].append(f"{kind}:{path_hash}: {type(exc).__name__}")
    return batches, stats


async def ingest_remote_batches(
    batches: Sequence[Mapping[str, Any]], *, source_machine: str, origin_hint: str | None = None,
    source_kind: str = "all", since: str | None = None, dry_run: bool = False,
    skip_live_duplicates: bool = True, remote_stats: Mapping[str, Any] | None = None,
    pool=None, store: SessionImportStore | None = None,
) -> ImportSummary:
    """受信バッチを再秘匿化して保存する。dry-run は読み取りだけで重複を推定する。"""
    remote_stats = remote_stats or {}
    origin_hint = resolve_effective_origin_hint(origin_hint, remote_stats.get("child_origin_hint"))
    cutoff = parse_since(since)
    if source_kind != "all" and source_kind not in list_parser_kinds():
        raise ValueError("unknown session source")
    store = _resolve_store(pool, store)
    summary = ImportSummary(run_id=uuid4().hex, source_kind=source_kind, source_machine=source_machine,
                            origin_hint=origin_hint, since=since, dry_run=dry_run)
    summary.files_skipped_unchanged = int(remote_stats.get("files_skipped_unchanged", 0))
    summary.turns_skipped_since = int(remote_stats.get("turns_skipped_since", 0))
    summary.errors = list(remote_stats.get("errors", []))
    grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for batch in batches:
        kind = batch["source_kind"]
        if kind not in list_parser_kinds() or (source_kind != "all" and kind != source_kind):
            raise ValueError("unexpected session source")
        grouped.setdefault((kind, batch["source_path_hash"]), []).append(batch)
    sessions = set()
    seen = set()
    allowed_fields = {field.name for field in fields(ImportedTurn)}
    for (kind, path_hash), chunks in grouped.items():
        summary.files_scanned += 1
        rows = []
        skipped_since = sum(int(chunk.get("file_skipped_since", 0)) for chunk in chunks)
        live_skipped = 0
        turns = []
        for chunk in chunks:
            for raw in chunk["turns"]:
                values = {key: value for key, value in raw.items() if key in allowed_fields}
                values.update(source_kind=kind, received_at=parse_since(raw["received_at"]),
                              completed_at=parse_since(raw["completed_at"]))
                values["project"] = safe_project_name(values.get("project"))
                values["cwd_basename"] = safe_project_name(values.get("cwd_basename"))
                turns.append((ImportedTurn(**values), raw))
        live = set()
        if skip_live_duplicates:
            providers = {turn.provider for turn, _ in turns}
            for provider in providers:
                ids = sorted({turn.session_id for turn, _ in turns if turn.provider == provider})
                live.update((provider, sid) for sid in await store.find_live_session_ids(provider, ids))
        for turn, raw in turns:
            sessions.add((kind, turn.session_id))
            if cutoff and turn.completed_at < cutoff:
                skipped_since += 1
                summary.turns_skipped_since += 1
                continue
            summary.turns_extracted += 1
            if (turn.provider, turn.session_id) in live:
                live_skipped += 1
                continue
            row, redacted, truncated = build_interaction_row(
                turn, source_machine=source_machine, source_path_hash=path_hash, origin_hint=origin_hint,
            )
            row["metadata"]["redacted"] = redacted or bool(raw.get("redacted"))
            row["metadata"]["truncated"] = truncated or bool(raw.get("truncated"))
            summary.redactions_count += row["metadata"]["redacted"]
            summary.truncations_count += row["metadata"]["truncated"]
            rows.append(row)
        imported = skipped = 0
        existing = await store.find_existing_request_ids([row["request_id"] for row in rows]) if dry_run else set()
        for row in rows:
            request_id = row["request_id"]
            if dry_run:
                inserted = request_id not in existing and request_id not in seen
            else:
                inserted = await store.insert_interaction(row)
            seen.add(request_id)
            imported += inserted
            skipped += not inserted
        summary.turns_imported += imported
        summary.turns_skipped_duplicate += skipped + live_skipped
        if not dry_run and skipped_since == 0:
            previous = await store.get_file_ledger(kind, source_machine, path_hash)
            last = chunks[-1]
            await store.upsert_file_ledger(FileLedgerEntry(
                kind, source_machine, path_hash, last["file_mtime_ns"], last["file_size"],
                max(chunk["last_turn_index"] for chunk in chunks),
                (previous.imported_count if previous else 0) + imported, skipped + live_skipped,
            ))
    summary.sessions_detected = len(sessions)
    summary.completed_at = datetime.now(timezone.utc)
    if not dry_run:
        await store.record_run(summary)
    return summary
