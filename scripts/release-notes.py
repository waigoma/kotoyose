#!/usr/bin/env python3
"""Print one version's CHANGELOG section, or the highest semver tag.

The section is the lines under `## <version>` or `## <version> (<date>)`,
stopping before the next `## ` heading. A missing or empty section prints
one line that points at CHANGELOG.md.
"""

import argparse
from pathlib import Path
import re
import sys


_VERSION = re.compile(r"([0-9]+)\.([0-9]+)\.([0-9]+)(?:[.-]([0-9A-Za-z.]+))?")
_MISSING = "See CHANGELOG.md."


def release_notes(changelog, version):
    pattern = re.compile(rf"^## {re.escape(version)}(?: \([^)]+\))?\s*$")
    lines = changelog.splitlines()
    start = next((index + 1 for index, line in enumerate(lines) if pattern.fullmatch(line)), None)
    if start is None:
        return _MISSING
    body = []
    for line in lines[start:]:
        if line.startswith("## "):
            break
        body.append(line)
    return "\n".join(body).strip() or _MISSING


def _version_key(name):
    text = name[1:] if name.startswith("v") else name
    match = _VERSION.fullmatch(text)
    if match is None:
        return None
    major, minor, patch, rest = match.groups()
    if rest is None:
        suffix = (1, ())
    else:
        parts = []
        for part in rest.split("."):
            if not part:
                continue
            parts.append((0, int(part)) if part.isdigit() else (1, part))
        suffix = (0, tuple(parts))
    return (int(major), int(minor), int(patch), suffix)


def highest_semver(names):
    """Return the highest semver name, ignoring names that are not semver.

    A plain X.Y.Z ranks above the same numbers with a - or . suffix.
    Suffix identifiers compare numerically when they are all digits.
    """
    ranked = []
    for name in names:
        key = _version_key(name)
        if key is not None:
            ranked.append((key, name))
    if not ranked:
        return None
    return max(ranked)[1]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", help="Version heading to extract, without a leading v")
    parser.add_argument("--changelog", type=Path, help="CHANGELOG.md path. Defaults to stdin")
    parser.add_argument("--highest", action="store_true", help="Print the highest semver among the positional names")
    parser.add_argument("names", nargs="*", help="Tags or versions, with --highest")
    args = parser.parse_args(argv)
    if args.highest:
        if args.version or args.changelog:
            parser.error("--highest does not take --version or --changelog")
        winner = highest_semver(args.names)
        if winner:
            print(winner)
        return 0
    if args.names:
        parser.error("positional names require --highest")
    if not args.version:
        parser.error("--version is required")
    try:
        changelog = args.changelog.read_text(encoding="utf-8") if args.changelog else sys.stdin.read()
    except OSError as exc:
        parser.exit(1, f"Could not read changelog: {exc}\n")
    print(release_notes(changelog, args.version))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
