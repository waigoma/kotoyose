"""execution artifact collector の契約と filesystem 境界を検証する。"""

import os
import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError

from kotoyose.execution_artifacts import (
    ArtifactCaptureError,
    ArtifactCaptureSpec,
    ArtifactSpecEntry,
    collect_execution_artifacts,
)
from kotoyose.execution_service import ExecutionService


def _entry(name: str, path: str, *, required: bool = True, max_bytes: int = 1024):
    return ArtifactSpecEntry(
        name=name,
        path=path,
        media_type="text/plain",
        required=required,
        max_bytes=max_bytes,
    )


def _spec(*entries: ArtifactSpecEntry, max_total_bytes: int = 4096):
    return ArtifactCaptureSpec(
        schema_version="1",
        kind="artifact_spec",
        entries=list(entries),
        max_total_bytes=max_total_bytes,
    )


def _identity(workspace: Path) -> tuple[int, int]:
    info = workspace.stat()
    return info.st_dev, info.st_ino


def test_captures_bytes_media_type_digest_and_manifest(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "evidence.txt").write_bytes(b"evidence")
    dev, inode = _identity(workspace)

    result = collect_execution_artifacts(
        workspace,
        dev,
        inode,
        _spec(_entry("evidence", "evidence.txt")),
    )

    assert result.contract_valid is True
    assert result.artifacts["evidence"].content == b"evidence"
    assert result.artifacts["evidence"].media_type == "text/plain"
    assert result.manifest[0].state == "captured"
    assert result.manifest[0].digest is not None


def test_optional_missing_is_recorded_but_required_missing_invalid(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    identity = _identity(workspace)

    optional = collect_execution_artifacts(
        workspace,
        *identity,
        _spec(_entry("optional", "missing.txt", required=False)),
    )
    required = collect_execution_artifacts(
        workspace,
        *identity,
        _spec(_entry("required", "missing.txt")),
    )

    assert optional.contract_valid is True
    assert optional.manifest[0].state == "missing"
    assert required.contract_valid is False
    assert required.manifest[0].state == "missing"


def test_per_file_and_aggregate_limits_are_bounded(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "large.bin").write_bytes(b"x" * 10)
    (workspace / "one.bin").write_bytes(b"y" * 5)
    (workspace / "two.bin").write_bytes(b"z" * 5)
    identity = _identity(workspace)

    per_file = collect_execution_artifacts(
        workspace,
        *identity,
        _spec(_entry("large", "large.bin", max_bytes=4)),
    )
    aggregate = collect_execution_artifacts(
        workspace,
        *identity,
        _spec(
            _entry("one", "one.bin", max_bytes=5),
            _entry("two", "two.bin", max_bytes=5),
            max_total_bytes=6,
        ),
    )

    assert per_file.contract_valid is False
    assert per_file.manifest[0].state == "error"
    assert aggregate.contract_valid is False
    assert [item.state for item in aggregate.manifest] == ["captured", "error"]
    assert sum(len(item.content) for item in aggregate.artifacts.values()) == 5


def test_spec_rejects_duplicate_names():
    with pytest.raises(ValidationError):
        _spec(_entry("dup", "a.txt"), _entry("dup", "b.txt"))


@pytest.mark.parametrize("path", ["../outside", "/etc/passwd", "a/../b", "a//b", "a\\b", "a\0b"])
def test_spec_rejects_path_traversal_and_nonportable_path(path: str):
    with pytest.raises(ValidationError):
        _entry("artifact", path)


def test_spec_rejects_duplicate_paths_and_malformed_media_type():
    with pytest.raises(ValidationError):
        _spec(_entry("one", "same.txt"), _entry("two", "same.txt"))

    valid = ArtifactSpecEntry(
        name="artifact",
        path="a.txt",
        media_type="text/html",
        required=True,
        max_bytes=10,
    )
    assert valid.media_type == "text/html"

    for media_type in ("text/html; charset=utf-8", "text/plain\r\nX-Test: evil", "not-a-type"):
        with pytest.raises(ValidationError):
            ArtifactSpecEntry(
                name="artifact",
                path="a.txt",
                media_type=media_type,
                required=True,
                max_bytes=10,
            )


def test_symlink_traversal_fifo_and_hardlink_are_errors(tmp_path: Path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside.txt"
    workspace.mkdir()
    outside.write_bytes(b"outside")
    (workspace / "escape.txt").symlink_to(outside)
    os.mkfifo(workspace / "pipe")
    (workspace / "original.txt").write_bytes(b"original")
    os.link(workspace / "original.txt", workspace / "hardlink.txt")
    identity = _identity(workspace)

    result = collect_execution_artifacts(
        workspace,
        *identity,
        _spec(
            _entry("escape", "escape.txt", required=False),
            _entry("pipe", "pipe", required=False),
            _entry("hardlink", "hardlink.txt", required=False),
        ),
    )

    assert result.contract_valid is False
    assert [item.state for item in result.manifest] == ["error", "error", "error"]
    assert result.artifacts == {}


def test_workspace_identity_mismatch_is_rejected_before_read(tmp_path: Path):
    workspace = tmp_path / "workspace"
    replacement = tmp_path / "replacement"
    workspace.mkdir()
    (workspace / "artifact.txt").write_bytes(b"before")
    identity = _identity(workspace)
    workspace.rename(replacement)
    workspace.mkdir()
    (workspace / "artifact.txt").write_bytes(b"attacker")

    with pytest.raises(ArtifactCaptureError, match="identity"):
        collect_execution_artifacts(
            workspace,
            *identity,
            _spec(_entry("artifact", "artifact.txt")),
        )


def test_workspace_symlink_is_rejected(tmp_path: Path):
    target = tmp_path / "target"
    target.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(target, target_is_directory=True)

    with pytest.raises(ArtifactCaptureError):
        collect_execution_artifacts(linked, target.stat().st_dev, target.stat().st_ino, _spec(_entry("x", "x")))


def test_service_collects_only_after_confirmed_stop(tmp_path: Path):
    """未停止確認では workspace を読まず、確認済みなら collector 結果を返す。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "evidence.bin").write_bytes(b"evidence")
    identity = _identity(workspace)
    snapshot = {
        "artifact_spec": _spec(_entry("evidence", "evidence.bin", max_bytes=64)).model_dump(mode="json"),
        "workspace_device": identity[0], "workspace_inode": identity[1],
    }
    service = object.__new__(ExecutionService)
    from types import SimpleNamespace
    service.config = SimpleNamespace(artifact_disk_limits=None)
    artifacts, manifest, valid = asyncio.run(
        service._capture_artifacts(snapshot, workspace, "confirmed", {"stdout": b"out"})
    )
    assert valid is True
    assert artifacts["evidence"].content == b"evidence"
    assert manifest[0]["digest"] is not None

    (workspace / "evidence.bin").unlink()
    artifacts, manifest, valid = asyncio.run(
        service._capture_artifacts(snapshot, workspace, "unconfirmed", {"stdout": b"out"})
    )
    assert artifacts == {"stdout": b"out"}
    assert manifest == []
    assert valid is True
