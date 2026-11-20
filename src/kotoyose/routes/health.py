from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..session import sessions

router = APIRouter()


@router.get(
    "/health",
    summary="ヘルスチェック",
    description="HTTP process の liveness。実行の受付可否は /ready で確認します。",
)
async def health():
    """liveness と旧会話セッション数を返す。execution の readiness は検査しない。"""
    return {"status": "ok", "active_sessions": len(sessions)}


@router.get("/ready", summary="Native execution readiness", responses={503: {"description": "実行受付不可"}})
async def ready(request: Request) -> JSONResponse:
    """検証済み lease の鮮度と監視・受付状態を返す。Native 未設定時は 503。"""
    service = getattr(request.app.state, "execution_service", None)
    reason = "native_execution_disabled" if service is None else service.readiness_reason()
    return JSONResponse(
        status_code=200 if reason is None else 503,
        content={"execution_ready": reason is None, "reason": reason or "ready"},
        headers={"Cache-Control": "no-store"},
    )
