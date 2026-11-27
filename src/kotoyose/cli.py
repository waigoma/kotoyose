"""Kotoyose のローカル会話履歴取り込み CLI。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from collections.abc import Sequence

import asyncpg

from . import db
from .session_import import ImportOptions, PathSecurityError, SessionImportStore, parse_since, run_import
from .session_import.models import KNOWN_SOURCE_KINDS


async def _import_sessions(args: argparse.Namespace):
    if not db.DATABASE_URL and not args.dry_run:
        raise ValueError("DATABASE_URL is required to import sessions; use --dry-run to preview")
    options = ImportOptions(
        source=args.source, path=args.path, since=parse_since(args.since),
        origin_hint=args.origin_hint, source_machine=args.source_machine, dry_run=args.dry_run,
        skip_live_duplicates=not args.include_live_duplicates,
    )
    if args.dry_run:
        ro_pool = None
        if db.DATABASE_URL:
            # --dry-run は DB への DDL・書き込み・イベント削除を一切行わない。
            ro_pool = await asyncpg.create_pool(db.DATABASE_URL, min_size=1, max_size=2)
        try:
            return await run_import(options, store=SessionImportStore(pool=ro_pool))
        finally:
            if ro_pool is not None:
                await ro_pool.close()
    try:
        await db.init_db()
        return await run_import(options)
    finally:
        await db.close_db()


def main(argv: Sequence[str] | None = None) -> int:
    """CLI を実行し終了コードを返す。不正な入力は stderr とコード 2。"""
    parser = argparse.ArgumentParser(prog="kotoyose")
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("import-sessions", help="Import local CLI session history")
    command.add_argument("--source", choices=("all", *KNOWN_SOURCE_KINDS), default="all")
    command.add_argument("--path", type=Path)
    command.add_argument("--since")
    command.add_argument("--origin-hint", choices=("personal", "work"))
    command.add_argument("--source-machine")
    command.add_argument("--include-live-duplicates", action="store_true")
    command.add_argument("--dry-run", action="store_true")
    command.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv)
    try:
        summary = asyncio.run(_import_sessions(args))
    except (PathSecurityError, ValueError) as exc:
        print(f"kotoyose: {exc}", file=sys.stderr)
        return 2
    if args.json_output:
        print(json.dumps(summary.to_dict(), ensure_ascii=False))
    else:
        print(
            f"{'Dry-run: ' if summary.dry_run else ''}sessions detected: {summary.sessions_detected}; "
            f"turns extracted: {summary.turns_extracted}; imported: {summary.turns_imported}; "
            f"duplicate skips: {summary.turns_skipped_duplicate}; redactions: {summary.redactions_count}; "
            f"truncations: {summary.truncations_count}; errors: {len(summary.errors)}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
