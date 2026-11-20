#!/usr/bin/env python3
"""Create a new 32-byte Kotoyose master key file with mode 0600 (refuses to overwrite)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_SRC_DIR = str(Path(__file__).resolve().parent.parent / "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from kotoyose.secret_store import create_master_key_file  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate a 32-byte Kotoyose master key file with mode 0600."
    )
    parser.add_argument("path", help="Destination path for the new master key file")
    args = parser.parse_args(argv)

    try:
        key_id = create_master_key_file(args.path)
    except FileExistsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: failed to write master key file: {exc}", file=sys.stderr)
        return 1

    print(f"Created master key at {args.path} (mode 0600, key_id={key_id})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
