"""認証済み Caller の checkpoint 保存・取得・復元 API。"""

from uuid import UUID

from fastapi import APIRouter, Request

from ..artifact_blob_store import ArtifactStorageError
from ..checkpoint_contract import CheckpointCapture, CheckpointRestore, CheckpointRelease, CheckpointAbandon
from ..checkpoint_restore import CheckpointRestorer
from ..checkpoint_restore_recovery import CheckpointRestoreRecovery
from ..execution_admission import workspace_path
from ..execution_http import execution_service
from ..execution_resources import ExecutionError

router = APIRouter()


@router.post('/v1/checkpoints', status_code=201)
async def create_checkpoint(body: CheckpointCapture, request: Request) -> dict:
    """現在の Caller root と source workspace を照合し、保存済み receipt を返す。"""
    service = execution_service(request)
    principal = request.state.execution_principal
    prior = await service.store.checkpoints.replay(principal, body)
    if prior is not None:
        return prior
    attempt = await service.store.attempt(principal, body.source_attempt_id)
    workspace_path(request.state.execution_policy, attempt['snapshot']['workspace_path'])
    try:
        return await service.store.checkpoints.capture(principal, body)
    except ArtifactStorageError as exc:
        raise ExecutionError('artifact_storage_error', 'Checkpoint could not be saved', 503) from exc


@router.get('/v1/checkpoints/{checkpoint_ref}')
async def get_checkpoint(checkpoint_ref: str, request: Request) -> dict:
    """削除済みの場合も保持 metadata と回復点を取得できる。"""
    return await execution_service(request).store.checkpoints.get(request.state.execution_principal, checkpoint_ref)


@router.post('/v1/checkpoints/{checkpoint_ref}/restores', status_code=201)
async def restore_checkpoint(checkpoint_ref: str, body: CheckpointRestore, request: Request) -> dict:
    """既存 workspace は上書きせず、新しい workspace ref と回復範囲を返す。"""
    return await CheckpointRestorer(execution_service(request).store).restore(
        request.state.execution_principal, request.state.execution_policy, checkpoint_ref, body,
    )


@router.post('/v1/checkpoints/{checkpoint_ref}/release')
async def release_checkpoint(checkpoint_ref: str, body: CheckpointRelease, request: Request) -> dict:
    """回復に必要な参照を自動期限で失わず、Caller の明示解除を記録する。"""
    return await execution_service(request).store.checkpoints.release(request.state.execution_principal, checkpoint_ref, body)


@router.get('/v1/checkpoints/{checkpoint_ref}/restores/{restore_request_id}')
async def get_checkpoint_restore(checkpoint_ref: str, restore_request_id: UUID, request: Request) -> dict:
    """未完了を含む保存済み復元の状態と、確定済み receipt を照会する。"""
    return await CheckpointRestoreRecovery(execution_service(request).store).get(
        request.state.execution_principal, request.state.execution_policy, checkpoint_ref, restore_request_id,
    )


@router.post('/v1/checkpoints/{checkpoint_ref}/restores/{restore_request_id}/abandon')
async def abandon_checkpoint_restore(checkpoint_ref: str, restore_request_id: UUID, body: CheckpointAbandon, request: Request) -> dict:
    """Caller の判断根拠を固定し、未完了の復元先を安全に破棄する。"""
    return await CheckpointRestoreRecovery(execution_service(request).store).abandon(
        request.state.execution_principal, request.state.execution_policy, checkpoint_ref, restore_request_id, body,
    )
