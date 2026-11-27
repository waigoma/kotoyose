"""Path policy for session history scanning: allowed roots, symlink containment, safe names."""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Sequence
from pathlib import Path

ALLOWED_ROOTS_ENV = "KOTOYOSE_IMPORT_ALLOWED_ROOTS"
_ANTIGRAVITY_TRANSCRIPT = "transcript.jsonl"
_IGNORED_PROJECT_NAMES = frozenset({"", ".", "..", "tmp", "/"})
_SEPARATORS = re.compile(r"[\\/]+")


class PathSecurityError(ValueError):
    """Raised when a scan root or path falls outside the permitted roots."""


def default_source_roots(home: Path | None = None) -> dict[str, Path]:
    """Return the default local history directory for each source kind."""
    base = (home or Path.home()).expanduser()
    return {
        "claude": base / ".claude" / "projects",
        "codex": base / ".codex" / "sessions",
        "antigravity": base / ".gemini" / "antigravity-cli" / "brain",
        "cursor": base / ".cursor" / "chats",
        "grok": base / ".grok" / "sessions",
    }


def configured_allowed_roots(extra_roots: Sequence[Path] | None = None, home: Path | None = None) -> tuple[Path, ...]:
    """Combine default roots, ``KOTOYOSE_IMPORT_ALLOWED_ROOTS`` and ``extra_roots``.

    Duplicates are removed by resolved path. The first occurrence keeps its
    order and spelling.
    """
    candidates: list[Path] = list(default_source_roots(home).values())
    for entry in os.environ.get(ALLOWED_ROOTS_ENV, "").split(os.pathsep):
        if entry.strip():
            candidates.append(Path(entry.strip()).expanduser())
    if extra_roots:
        candidates.extend(Path(root).expanduser() for root in extra_roots)

    seen: set[Path] = set()
    unique: list[Path] = []
    for candidate in candidates:
        key = candidate.resolve()
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return tuple(unique)


def _is_within(path: Path, root: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root.resolve())
    except (OSError, RuntimeError):
        return False


def is_within_root(path: Path, root: Path) -> bool:
    """Return True when ``path`` resolves to a location inside ``root``."""
    return _is_within(path, root)


def validate_scan_root(target: Path, allowed_roots: Sequence[Path]) -> Path:
    """Resolve ``target`` and require it to sit inside one of ``allowed_roots``.

    Resolution follows every symlink in the target and in its parents, so a
    symlink whose destination lies outside the allowed roots is rejected.
    Returns the resolved path.
    """
    resolved = target.expanduser().resolve()
    if not any(_is_within(resolved, Path(allowed).expanduser()) for allowed in allowed_roots):
        raise PathSecurityError("scan root is outside the allowed import roots")
    return resolved


def _is_antigravity_transcript(path: Path) -> bool:
    return path.name == _ANTIGRAVITY_TRANSCRIPT and path.parent.name == "logs" and path.parent.parent.name == ".system_generated"


def _matches_source(source_kind: str, path: Path) -> bool:
    if source_kind == "antigravity":
        return _is_antigravity_transcript(path)
    if source_kind == "cursor":
        return path.name == "store.db"
    if source_kind == "grok":
        return path.name == "chat_history.jsonl"
    return path.name.endswith(".jsonl")


def iter_safe_source_files(source_kind: str, root: Path, allowed_roots: Sequence[Path] | None = None) -> list[Path]:
    """List history files under ``root`` for ``source_kind`` without following escaping links.

    * With ``allowed_roots``, ``root`` must resolve inside one of them.
    * Without it, the resolved ``root`` is the containment boundary.
    * Symlinked directories are never descended into. Symlinked files are
      included only when they resolve inside the boundary.
    * A missing root yields ``[]``.
    """
    if allowed_roots is not None:
        resolved_root = validate_scan_root(root, allowed_roots)
    else:
        resolved_root = root.expanduser().resolve()

    if not resolved_root.exists():
        return []

    if resolved_root.is_file():
        if source_kind == "antigravity":
            matched = resolved_root.name == _ANTIGRAVITY_TRANSCRIPT
        else:
            matched = _matches_source(source_kind, resolved_root)
        return [resolved_root] if matched else []

    results: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(resolved_root, followlinks=False):
        current = Path(dirpath)
        # Never descend into a symlinked directory, whatever it points to.
        dirnames[:] = [name for name in dirnames if not (current / name).is_symlink()]
        for name in filenames:
            path = current / name
            if path.is_symlink() and not _is_within(path, resolved_root):
                continue
            if not path.is_file():
                continue
            if _matches_source(source_kind, path):
                results.append(path)
    return sorted(results)


def hash_source_path(path: Path) -> str:
    """Return a stable ``sha256:`` digest of the resolved path. The path itself is not stored."""
    return f"sha256:{hashlib.sha256(str(path.resolve()).encode('utf-8')).hexdigest()}"


def _basename_or_none(value: str) -> str | None:
    parts = [part for part in _SEPARATORS.split(value.strip()) if part]
    if not parts:
        return None
    name = parts[-1]
    if name in _IGNORED_PROJECT_NAMES:
        return None
    return name


def safe_project_name(cwd: str | None = None, fallback_path: Path | None = None) -> str | None:
    """Return only the final path component of a working directory, never a full path.

    POSIX and Windows separators are both accepted. When ``cwd`` is None and
    ``fallback_path`` names a Claude project directory (its encoded form starts
    with ``-``), the basename cannot be recovered reliably, so None is returned.
    """
    if cwd is not None:
        return _basename_or_none(cwd)
    if fallback_path is None:
        return None
    name = fallback_path.name
    if name.startswith("-"):
        return None
    return _basename_or_none(name)
