#!/usr/bin/env bash
set -euo pipefail

allow_dirty=false
if [[ ${1:-} == --allow-dirty ]]; then
    allow_dirty=true
    shift
elif [[ $# == 2 && $2 == --allow-dirty ]]; then
    allow_dirty=true
    set -- "$1"
fi
if [[ $# != 1 || $1 == --* ]]; then
    echo "Usage: $0 [--allow-dirty] <dist dir>" >&2
    exit 2
fi
dist=$(realpath -- "$1")
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$root"
PYTHONPATH=src python3 -B - "$dist" "$allow_dirty" <<'PY'
from pathlib import Path
import sys
import tomllib

from kotoyose.child_update import directory, publish_available

version = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
root = directory()
try:
    for name, offer in publish_available(Path(sys.argv[1]), root, version, allow_dirty=sys.argv[2] == "true"):
        print(f"Published {name} ({offer.build_id}) in {root}")
except (OSError, ValueError) as error:
    raise SystemExit(f"Could not publish the child update: {error}") from None
PY
