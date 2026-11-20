"""Resume admission の transaction 内で、停止証拠と回復対象を再照合する。"""

import hashlib
import json
from pathlib import Path

from .checkpoint_archive import capture_checkpoint
from .checkpoint_contract import CheckpointCapture
from .execution_artifacts import ArtifactCaptureError
from .execution_contract import ResumeManifest
from .execution_resources import content_digest
from .execution_resume import check_predecessor, operations_digest, unsafe


async def lock_predecessor(conn, manifest: ResumeManifest) -> None:
    """Grant → 旧 Attempt → workspace の順に lock し、完了・遅延結果との競合を閉じる。"""
    row = await conn.fetchrow(
        "SELECT * FROM kotoyose.execution_attempts WHERE id=$1 AND principal=$2 FOR UPDATE",
        manifest.predecessor_attempt_id, manifest.principal,
    )
    if row is None:
        raise unsafe("snapshot_mismatch")
    previous = dict(row)
    check_predecessor(previous)
    if (previous["sequence"] != manifest.predecessor_sequence
            or previous["status"] != manifest.predecessor_status
            or previous["generation"] != manifest.predecessor_generation
            or content_digest(json.loads(previous["snapshot"])) != manifest.predecessor_snapshot_digest
            or (manifest.mode == "native" and previous["provider_session_ref"] != manifest.provider_session_ref)):
        raise unsafe("snapshot_mismatch")
    rows = await conn.fetch("SELECT * FROM kotoyose.execution_operations WHERE attempt_id=$1 FOR UPDATE", previous["id"])
    operations = [{**dict(row), "result": json.loads(row["result"]) if row["result"] else None} for row in rows]
    if operations_digest(operations) != manifest.operations_digest:
        raise unsafe("operation_unknown")


async def verify_workspace(conn, manifest: ResumeManifest, workspace: dict) -> None:
    """旧世代の直後の実体だけを継続し、指定 checkpoint の選択ファイルも再検証する。"""
    path = Path(workspace["path"])
    try:
        info = path.stat()
        if path.is_symlink() or not path.is_dir() or (info.st_dev, info.st_ino) != (workspace["device"], workspace["inode"]):
            raise unsafe("workspace_unavailable")
    except OSError as exc:
        raise unsafe("workspace_unavailable") from exc
    if manifest.checkpoint_ref is None:
        return
    checkpoint = await conn.fetchrow(
        "SELECT * FROM kotoyose.execution_checkpoints WHERE principal=$1 AND id=$2 FOR UPDATE",
        manifest.principal, manifest.checkpoint_ref.removeprefix("checkpoint:"),
    )
    if (checkpoint is None or manifest.checkpoint_ref != f"checkpoint:{checkpoint['id']}"
            or checkpoint["released_at"] is not None
            or checkpoint["source_attempt_id"] != manifest.predecessor_attempt_id
            or checkpoint["workspace_id"] != workspace["id"]
            or checkpoint["generation"] != manifest.workspace_generation):
        raise unsafe("checkpoint_mismatch")
    receipt = json.loads(checkpoint["result"])
    if receipt["artifact"]["digest"] != manifest.checkpoint_digest:
        raise unsafe("checkpoint_mismatch")
    capture = CheckpointCapture(
        schema_version="1", request_id=receipt["request_id"], workspace_ref=receipt["workspace_ref"],
        source_attempt_id=receipt["source_attempt_id"], source_revision=receipt["source_revision"], files=receipt["files"],
    )
    try:
        content = capture_checkpoint(path, workspace["device"], workspace["inode"], capture)
    except (OSError, ArtifactCaptureError) as exc:
        raise unsafe("checkpoint_mismatch") from exc
    if hashlib.sha256(content).hexdigest() != manifest.checkpoint_digest:
        raise unsafe("checkpoint_mismatch")
