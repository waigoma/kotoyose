"""Inspect agent deliverables before the execution store sees them."""

import os
import stat
from collections.abc import Iterable
from itertools import chain
from pathlib import Path

from .artifact_file import ArtifactFile
from .credential_leak_guard import credential_byte_kinds, find_credential_shapes
from .execution_artifacts import (
    ArtifactCaptureError,
    CapturedArtifact,
    _open_root,
    open_artifact_entry,
)
from .output_guard import report_findings

_WORKSPACE_LIMIT = 1024 * 1024
_ARTIFACT_OVERLAP = 128 * 1024
_PRIVATE_DIRS = {".git", ".codex", ".gemini", ".config", ".task-hub", ".local", ".aws"}


def _private_path(relative: str) -> bool:
    return any(
        part in _PRIVATE_DIRS or part.startswith((".claude", ".env"))
        for part in Path(relative).parts
    )


def _changed_paths(workspace: Path):
    # Never run git (or any tool that reads repository config/attributes) against the
    # agent-writable workspace from the host: `.git/config` and `.gitattributes` can name
    # filter drivers or hooks that would execute on the host. Enumerate with lstat-only
    # walking instead; the caller compares against the pre-run identities.
    for root, dirs, files in os.walk(workspace, followlinks=False):
        dirs[:] = [name for name in dirs if not _private_path(name)]
        yield from (str((Path(root) / name).relative_to(workspace)) for name in files)


def workspace_state(
    workspace: Path, previous_paths: Iterable[str] = (),
) -> dict[str, tuple[int, int, int, int, int]]:
    """変更候補の同一性を、ファイル本文を開く前に記録する。"""
    state = {}
    for relative in dict.fromkeys(chain(_changed_paths(workspace), previous_paths)):
        if _private_path(relative):
            continue
        try:
            info = (workspace / relative).lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
            state[relative] = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    return state


def check_workspace(workspace: Path, device: int, inode: int, baseline: dict) -> bool:
    root_fd = _open_root(workspace, device, inode)
    found = False
    try:
        for relative, identity in workspace_state(workspace, baseline).items():
            if baseline.get(relative) == identity:
                continue
            kinds = [item.kind for item in find_credential_shapes(relative)]
            try:
                with open_artifact_entry(root_fd, relative, identity[2]) as fd:
                    with os.fdopen(os.dup(fd), "rb") as reader:
                        data = reader.read(_WORKSPACE_LIMIT)
                if b"\0" not in data:
                    kinds.extend(credential_byte_kinds(data))
            except (FileNotFoundError, ArtifactCaptureError):
                # A file replaced or grown after the snapshot is outside this read.
                pass
            if kinds:
                report_findings(kinds, path=relative)
                found = True
    finally:
        os.close(root_fd)
    return found


def _blob_kinds(blob: bytes | ArtifactFile) -> list[str]:
    if isinstance(blob, bytes):
        return credential_byte_kinds(blob)
    tail = b""
    for chunk in blob.chunks():
        data = tail + chunk
        # The first retained byte is context for the unquoted key's left boundary.
        kinds = credential_byte_kinds(data, start=1 if len(tail) > _ARTIFACT_OVERLAP else 0)
        if kinds:
            return kinds
        tail = data[-(_ARTIFACT_OVERLAP + 1):]
    return []


def check_artifacts(artifacts: dict[str, bytes | CapturedArtifact], artifact_spec: dict | None) -> bool:
    paths = {entry["name"]: entry["path"] for entry in (artifact_spec or {}).get("entries", [])}
    found = False
    for name, artifact in artifacts.items():
        path = paths.get(name, name)
        blob = artifact.content if isinstance(artifact, CapturedArtifact) else artifact
        kinds = [item.kind for item in find_credential_shapes(path)]
        kinds.extend(_blob_kinds(blob))
        if kinds:
            report_findings(kinds, path=path)
            found = True
    return found
