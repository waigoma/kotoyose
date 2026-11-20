#!/usr/bin/env python3
"""公式の口の外にある route を、risk_acks に貼れる YAML で出す。

使い方: uv run python scripts/sanction_candidates.py path/to/config.yml

route ごとに reasons・現在の digest・使っている alias を出す。acknowledged_by と at は
貼る人が埋める (at は埋めるまで config の検証で落ちる)。credential は env 名か名前だけで、
環境変数の値は読まない。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def render(config) -> str:
    """official 以外の route を risk_acks 形式の YAML にする。"""
    from kotoyose.agent_sanction import route_digest, route_sanction

    lines = ["risk_acks:"]
    count = 0
    for name, route in config.routes.items():
        sanction = route_sanction(config, name)
        if sanction.status == "official":
            continue
        count += 1
        aliases = [alias for alias, preset in config.models.items() if preset.route == name]
        conn = config.connections.get(route.connection)
        lines += [
            f"  # {sanction.status}: aliases = {', '.join(aliases) if aliases else '(none)'}",
            f"  - route: {json.dumps(name)}",
            f"    digest: {route_digest(route, conn)}",
            f"    reasons: {json.dumps(list(sanction.reasons))}",
            '    acknowledged_by: "REPLACE_WITH_OWNER"',
            '    at: "REPLACE_WITH_ISO_8601_TIMESTAMP"',
            '    note: ""',
        ]
    if count == 0:
        return "risk_acks: []  # every route is official\n"
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: sanction_candidates.py <config.yml>", file=sys.stderr)
        return 2
    path = Path(argv[1])
    if not path.is_file():
        print(f"config not found: {path}", file=sys.stderr)
        return 2
    os.environ["KOTOYOSE_CONFIG"] = str(path)
    from kotoyose.config import get_config

    sys.stdout.write(render(get_config()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
