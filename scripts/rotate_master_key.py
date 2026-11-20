#!/usr/bin/env python3
"""Re-encrypt all kotoyose.secrets rows from an old master key file to a new one."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

_SRC_DIR = str(Path(__file__).resolve().parent.parent / "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

import asyncpg  # noqa: E402

from kotoyose.secret_store import (  # noqa: E402
    SecretKeyError,
    SecretStore,
    SecretStoreError,
    compute_key_id,
    load_master_key_file,
)


async def rotate_database_secrets(
    *,
    old_key_path: str | Path,
    new_key_path: str | Path,
    database_url: str,
) -> tuple[int, str, str]:
    """Rotate all kotoyose.secrets rows in Postgres from old_key_path to new_key_path."""
    old_key = load_master_key_file(old_key_path)
    new_key = load_master_key_file(new_key_path)
    old_id = compute_key_id(old_key)
    new_id = compute_key_id(new_key)

    pool = await asyncpg.create_pool(database_url, min_size=1, max_size=2)
    try:
        store = SecretStore(pool, master_key=old_key)
        count = await store.rotate_key(old_key, new_key)
        return count, old_id, new_id
    finally:
        await pool.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Re-encrypt all rows in kotoyose.secrets using a new master key file "
            "(under ACCESS EXCLUSIVE table lock). Stop the Kotoyose server first, then restart after."
        ),
        epilog="Important: stop the Kotoyose server first, run this rotation, update the key path, and restart the server after.",
    )
    parser.add_argument("--old-key", required=True, help="Path to the current master key file")
    parser.add_argument("--new-key", required=True, help="Path to the new master key file")
    parser.add_argument(
        "--database-url",
        default=None,
        help="Postgres connection URL (defaults to DATABASE_URL env var)",
    )
    args = parser.parse_args(argv)

    database_url = args.database_url or os.environ.get("DATABASE_URL")
    if not database_url:
        print("error: DATABASE_URL is not set and --database-url was not provided", file=sys.stderr)
        return 1

    try:
        count, old_id, new_id = asyncio.run(
            rotate_database_secrets(
                old_key_path=args.old_key,
                new_key_path=args.new_key,
                database_url=database_url,
            )
        )
    except (SecretKeyError, SecretStoreError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"Rotated {count} secret(s) from key_id={old_id} to key_id={new_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
