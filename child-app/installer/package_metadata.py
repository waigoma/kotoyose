"""Write the offer sidecar for a child package."""

import json
from pathlib import Path
import sys

stage, name, version, build = sys.argv[1:]
package = Path(stage) / name
size = package.stat().st_size
if not 0 < size <= 64 * 1024 * 1024:
    raise SystemExit(f"Package exceeds the 64 MiB child link limit ({size} bytes)")
sha256 = package.with_name(name + ".sha256").read_text().split()[0]
package.with_name(name + ".json").write_text(
    json.dumps(dict(version=version, build_id=build, sha256=sha256, size=size)) + "\n",
    encoding="utf-8",
)
