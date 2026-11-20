#!/usr/bin/env python3
"""Copy a reviewed subset of the Git working tree to an empty directory."""

import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess


def compile_rules(manifest):
    rules = []
    for line in manifest.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        include = not line.startswith("!")
        pattern = line.removeprefix("!").removeprefix("/")
        suffix = ".*" if pattern.endswith("/") else ""
        parts = re.split(r"(\*\*/|\*\*|\*|\?|\[[^\]]+\])", pattern)
        wildcards = {"**/": "(?:.*/)?", "**": ".*", "*": "[^/]*", "?": "[^/]"}
        expression = ""
        for part in parts:
            if part.startswith("[") and part.endswith("]"):
                characters = part[1:-1]
                negate = characters.startswith("!")
                characters = re.escape(characters.removeprefix("!")).replace(r"\-", "-")
                expression += "(?!/)[" + ("^" if negate else "") + characters + "]"
            else:
                expression += wildcards.get(part, re.escape(part))
        rules.append((re.compile(expression + suffix), include))
    return rules


def selected(path, rules):
    include = False
    for pattern, decision in rules:
        if pattern.fullmatch(path):
            include = decision
    return include


def export(root, out):
    rules = compile_rules(root / "release/public-paths.txt")
    tracked = subprocess.check_output(["git", "-C", str(root), "ls-files", "-z", "--cached"])
    paths = sorted(set(os.fsdecode(tracked).split("\0")) - {""})
    included = {Path(path) for path in paths if selected(path, rules)}
    if not included:
        raise ValueError("no tracked files match the public allow-list")
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise ValueError("output must be an empty directory")
    for path in sorted(included):
        source = root / path
        if source.parent.resolve() != source.parent:
            raise ValueError("public path traverses a symlinked directory")
        if source.is_symlink():
            target = source.resolve(strict=True)
            if (Path(os.readlink(source)).is_absolute() or not target.is_relative_to(root)
                    or target.relative_to(root) not in included or not target.is_file()):
                raise ValueError("public symlink must resolve to an exported file via a relative target")
            direct = Path(os.path.normpath(source.parent / os.readlink(source)))
            if not direct.is_relative_to(root) or direct.relative_to(root) not in included:
                raise ValueError("public symlink target must be exported")
        elif not source.is_file():
            raise ValueError("selected tracked path is missing or is not a regular file")
    for path in sorted(included):
        destination = out / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / path, destination, follow_symlinks=False)
    print(f"Exported {len(included)} files, excluded {len(paths) - len(included)} tracked paths.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.out.is_symlink():
            raise ValueError("output must not be a symlink")
        export(Path(__file__).resolve().parents[1], args.out.resolve())
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"Export failed: {exc}\n")


if __name__ == "__main__":
    main()
