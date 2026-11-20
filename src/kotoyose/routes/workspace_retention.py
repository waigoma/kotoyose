"""認証済み Caller が現在世代の workspace 保持方針を固定する。"""

from fastapi import APIRouter, Request

from ..execution_http import execution_service
from ..workspace_retention import WorkspaceRetentionChange

router = APIRouter()


@router.get('/v1/workspaces/{workspace_ref}/retention')
async def workspace_retention(workspace_ref: str, request: Request) -> dict:
    """過去 ref から現在の状態を照会する。path は返さない。"""
    return await execution_service(request).store.workspace_retention.get(request.state.execution_principal, workspace_ref)


@router.put('/v1/workspaces/{workspace_ref}/retention')
async def change_workspace_retention(workspace_ref: str, body: WorkspaceRetentionChange, request: Request) -> dict:
    """認可済み Caller の判断を保存し、runtime の削除条件は GC 時にも再検査する。"""
    return await execution_service(request).store.workspace_retention.change(request.state.execution_principal, workspace_ref, body)
