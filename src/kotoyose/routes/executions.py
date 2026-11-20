"""Native API の認証と wire 変換。実行保証は構成済み service に委ねる。"""

import asyncio
from collections.abc import AsyncIterator, Mapping
import json
from typing import Any

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.routing import APIRoute
from pydantic import TypeAdapter, ValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse

from ..execution_http import authorize_execution as authorize, enforce_model_scope, execution_service
from ..capabilities import CapabilityManifest, CapabilityName, capability_manifest
from ..execution_contract import ContractModel, ExecutionCancellation, ExecutionEnvelope, RuntimeAuthenticationResolution
from ..execution_response import unsupported_response
from ..execution_snapshot import resolved_attempt_snapshot
from ..execution_resources import ExecutionError, ResourceEnvelope, WorkspaceRegistration
from ..input_artifacts import InputArtifactUpload
from ..runtime_identity import runtime_identity
from ..artifact_retention import ArtifactRetentionChange
from ..artifact_blob_store import ArtifactStorageError
from ..client_tool_bridge import ClientConnectionRequest, ClientToolResultRequest, ClientToolReconciliationRequest
from .external_mcp import router as external_mcp_router
from .checkpoints import router as checkpoints_router
from .workspace_retention import router as workspace_retention_router
from .agent_executions import agent_execution_create, agent_execution_error_response, router as agent_execution_router


router = APIRouter(dependencies=[Depends(authorize)])
router.include_router(external_mcp_router)
router.include_router(checkpoints_router)
router.include_router(workspace_retention_router)
router.include_router(agent_execution_router)


@router.get("/v1/runtime-identity")
async def get_runtime_identity(request: Request) -> dict[str, str]:
    """Identify the durable store and authenticated Caller without revealing credentials."""
    return runtime_identity(execution_service(request), request.state.execution_principal)


@router.get("/v1/capabilities", response_model=CapabilityManifest)
async def get_capabilities(request: Request) -> CapabilityManifest:
    """現在の保証を返す。未対応を host 実行で代替しない。"""
    service = getattr(request.app.state, "execution_service", None)
    if service is not None:
        service.resume_support()
    return service.manifest if service is not None else capability_manifest()


class ExecutionCreateRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def wrapped(request):
            try:
                return await handler(request)
            except ExecutionError as exc:
                try:
                    raw = await request.json()
                except ValueError:
                    raise exc
                if isinstance(raw, dict) and isinstance(raw.get("execution"), dict) and raw["execution"].get("schema_version") == "agent_execution_v1":
                    return agent_execution_error_response(exc)
                raise

        return wrapped


async def create_execution(request: Request) -> JSONResponse:
    """Dispatch the Hub agent contract or the existing command contract."""
    try:
        raw = await request.json()
    except ValueError:
        return JSONResponse(status_code=422, content={"detail": "Invalid JSON"})
    if isinstance(raw, dict) and isinstance(raw.get("execution"), dict) and raw["execution"].get("schema_version") == "agent_execution_v1":
        return await agent_execution_create(request)
    try:
        body = ExecutionEnvelope.model_validate(raw)
    except ValidationError:
        return JSONResponse(status_code=422, content={"detail": "Invalid execution"})
    if body.execution.kind == "agent":
        return JSONResponse(
            status_code=422,
            content={"detail": "Set execution.schema_version to agent_execution_v1 for agent execution"},
        )
    service = getattr(request.app.state, "execution_service", None)
    if service is not None:
        attempt_id = await service.dispatch(
            request.state.execution_principal, request.state.execution_policy, body.execution,
            request.state.api_key_name,
        )
        return JSONResponse(status_code=202, content={"schema_version": "1", "attempt_id": attempt_id})
    capability: CapabilityName = "command_execution"
    return unsupported_response([capability], getattr(request.state, "request_id", "unknown"))


router.add_api_route("/v1/executions", create_execution, methods=["POST"], status_code=202,
                     route_class_override=ExecutionCreateRoute)


@router.post("/v1/runtime-authentication/resolve")
async def resolve_runtime_authentication(body: RuntimeAuthenticationResolution, request: Request) -> JSONResponse:
    """登録済み runtime の認証 hash だけを、process/外部通信なしで読み取る。"""
    if body.model is not None:
        enforce_model_scope(request, body.model)
    result = execution_service(request).resolve_runtime_authentication(request.state.execution_policy, body)
    return JSONResponse(content=result, headers={"Cache-Control": "private, no-store"})


@router.post("/v1/resources", status_code=201)
async def register_resource(body: ResourceEnvelope, request: Request) -> dict:
    """内容を immutable ref として保存し、Caller 間の参照を隔離する。"""
    ref = await execution_service(request).register_resource(
        request.state.execution_principal, request.state.execution_policy, body.resource,
    )
    return {"schema_version": "1", "resource_ref": ref}


@router.post("/v1/input-artifacts", status_code=201)
async def upload_input_artifact(body: InputArtifactUpload, request: Request) -> dict:
    """Caller 所有の immutable image input を Attempt なしで保存する。"""
    service = execution_service(request)
    try:
        artifact = await service.store.put_input_artifact(
            request.state.execution_principal, body, service.config.input_artifact_limits,
        )
    except ArtifactStorageError as exc:
        raise ExecutionError("artifact_storage_error", "Input artifact storage is unavailable", 503) from exc
    except ValueError as exc:
        raise ExecutionError("invalid_request", str(exc), 400) from exc
    return {"schema_version": "1", "artifact": {
        "ref": artifact["ref"], "digest": artifact["digest"],
        "media_type": artifact["media_type"], "size": artifact["size"],
        "created_at": artifact["created_at"].isoformat(),
        "expires_at": artifact["expires_at"].isoformat(),
        "retention_pin": artifact["retention_pin"],
    }}


@router.post("/v1/execution-cancellations")
async def cancel_dispatch(body: ExecutionCancellation, request: Request) -> dict:
    """受理証の応答消失時にも、再送による新規開始を伴わず dispatch を取り消す。"""
    return await execution_service(request).cancel_dispatch(
        request.state.execution_principal, body.idempotency_key,
    )


@router.post("/v1/workspaces", status_code=201)
async def register_workspace(body: WorkspaceRegistration, request: Request) -> dict:
    """Caller の管理下にある clone を世代付き ref に変換する。"""
    return await execution_service(request).register_workspace_response(
        request.state.execution_principal, request.state.execution_policy, body,
    )


@router.get("/v1/attempts/{attempt_id}")
async def get_attempt(attempt_id: str, request: Request) -> dict:
    """状態と停止確認を分離した Attempt を返す。"""
    return await execution_service(request).describe(request.state.execution_principal, attempt_id)


@router.post("/v1/attempts/{attempt_id}/client-connections", status_code=201)
async def claim_client_connection(attempt_id: str, body: ClientConnectionRequest, request: Request) -> dict:
    """Attempt に束縛した client key / generation を durable に claim する。"""
    connection = await execution_service(request).store.client_tools.register_connection(
        request.state.execution_principal, attempt_id, body.client_key, body.generation,
    )
    return {"schema_version": "1", "connection": connection}


@router.get("/v1/attempts/{attempt_id}/client-calls")
async def get_client_calls(
    attempt_id: str, request: Request, client_key: str = Query(..., min_length=1, max_length=128),
    generation: int = Query(..., ge=0), limit: int = Query(32, ge=1, le=100),
) -> dict:
    """client connection が pending call を一度だけ送信 claim する。"""
    calls = await execution_service(request).store.client_tools.pending(
        request.state.execution_principal, attempt_id, client_key, generation, limit,
    )
    return {"schema_version": "1", "calls": calls}


@router.post("/v1/attempts/{attempt_id}/client-calls/{call_id}/results")
async def post_client_call_result(
    attempt_id: str, call_id: str, body: ClientToolResultRequest, request: Request,
) -> dict:
    """同じ connection generation の client result を一度だけ確定する。"""
    result = await execution_service(request).store.client_tools.save_result(
        request.state.execution_principal, attempt_id, call_id,
        body.client_key, body.generation, body.result,
        tool_ref=body.tool_ref, arguments_digest=body.arguments_digest, disposition=body.disposition,
    )
    return {"schema_version": "1", "call": result}


@router.post("/v1/attempts/{attempt_id}/client-calls/{call_id}/reconciliation")
async def reconcile_client_call(attempt_id: str, call_id: str, body: ClientToolReconciliationRequest, request: Request) -> dict:
    """現世代の認証済み Caller による照合証拠を保存する。Tool の再実行はしない。"""
    result = await execution_service(request).store.client_tools.save_result(
        request.state.execution_principal, attempt_id, call_id, body.client_key, body.generation,
        body.result, tool_ref=body.tool_ref, arguments_digest=body.arguments_digest,
        disposition=body.disposition, reconciliation_ref=body.evidence_ref,
    )
    return {"schema_version": "1", "call": result}


@router.post("/v1/attempts/{attempt_id}/cancel")
async def cancel_attempt(attempt_id: str, body: ContractModel, request: Request) -> dict:
    """取消を要求し、まだ停止していなければ cancelling を返す。"""
    return await execution_service(request).cancel(request.state.execution_principal, attempt_id)


@router.post("/v1/grants/{ref}/revoke")
async def revoke_grant(ref: str, body: ContractModel, request: Request) -> dict:
    """不変 Grant の失効を記録し、対象実行へ伝播する。"""
    cancelled = await execution_service(request).revoke(request.state.execution_principal, ref)
    return {"schema_version": "1", "cancelled_attempt_ids": cancelled}


@router.post("/v1/attempts/{attempt_id}/reconcile")
async def reconcile_attempt(attempt_id: str, body: ContractModel, request: Request) -> dict:
    """終端後の停止状態だけを再照合し、新たな実行は開始しない。"""
    return await execution_service(request).reconcile(request.state.execution_principal, attempt_id)


SSE_POLL_MIN_SECONDS = 0.5
SSE_POLL_MAX_SECONDS = 2.0
SSE_POLL_BACKOFF_FACTOR = 2.0
SSE_HEARTBEAT_SECONDS = 15.0
SSE_MAX_LIFETIME_SECONDS = 3600.0
SSE_BATCH_LIMIT = 100
_TERMINAL_ATTEMPT_STATUSES = frozenset({
    "succeeded",
    "completed",
    "failed",
    "cancelled",
    "timeout",
    "worker_lost",
    "budget_exceeded",
    "approval_required",
})


def _resolve_stream_after(after: int, last_event_id: str | None) -> int:
    """Last-Event-ID が有効な非負整数なら優先し、それ以外は after を使う。"""
    if last_event_id is not None:
        candidate = last_event_id.strip()
        if candidate.isascii() and candidate.isdigit():
            return int(candidate)
    return after


def _is_terminal_attempt(attempt: Mapping[str, Any] | None) -> bool:
    if attempt is None:
        return True
    if attempt.get("finished_at") is not None:
        return True
    status = attempt.get("status")
    return isinstance(status, str) and status in _TERMINAL_ATTEMPT_STATUSES


_EVENT_JSON_ADAPTER = TypeAdapter(dict[str, Any])


def _format_attempt_sse_event(event: Mapping[str, Any]) -> str:
    raw_kind = event.get("type") or event.get("kind")
    kind = raw_kind.replace("\r", "").replace("\n", "").strip() if isinstance(raw_kind, str) else ""
    event_name = kind or "event"
    normalized = _EVENT_JSON_ADAPTER.dump_python(dict(event), mode="json")
    payload = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
    return f"id: {int(event['sequence'])}\nevent: {event_name}\ndata: {payload}\n\n"


async def _attempt_event_stream(
    store: Any,
    principal: str,
    attempt_id: str,
    start_after: int,
    request: Request,
    initial_attempt: Mapping[str, Any],
) -> AsyncIterator[str]:
    """Attempt の永続 event を SSE で配信し、終端で end event を送って閉じる。"""
    loop = asyncio.get_running_loop()
    now = loop.time()
    deadline = now + SSE_MAX_LIFETIME_SECONDS
    last_output_at = now
    poll_interval = SSE_POLL_MIN_SECONDS
    next_poll_at = now
    cursor = start_after
    sent_any = False
    attempt: Mapping[str, Any] | None = initial_attempt
    first_poll = True

    while True:
        if await request.is_disconnected():
            return
        now = loop.time()
        if now >= deadline:
            return

        if now >= next_poll_at:
            batch_sent = False
            while True:
                batch = await store.events(principal, attempt_id, cursor, SSE_BATCH_LIMIT)
                for event in batch:
                    seq = int(event["sequence"])
                    if seq <= cursor:
                        continue
                    cursor = seq
                    sent_any = True
                    batch_sent = True
                    last_output_at = loop.time()
                    yield _format_attempt_sse_event(event)
                if len(batch) < SSE_BATCH_LIMIT:
                    break
                if await request.is_disconnected() or loop.time() >= deadline:
                    return

            already_terminal = first_poll and _is_terminal_attempt(attempt)
            if not already_terminal:
                attempt = await store.attempt(principal, attempt_id)
            first_poll = False

            if _is_terminal_attempt(attempt):
                if not already_terminal:
                    while True:
                        tail = await store.events(principal, attempt_id, cursor, SSE_BATCH_LIMIT)
                        for event in tail:
                            seq = int(event["sequence"])
                            if seq <= cursor:
                                continue
                            cursor = seq
                            sent_any = True
                            yield _format_attempt_sse_event(event)
                        if len(tail) < SSE_BATCH_LIMIT:
                            break
                        if await request.is_disconnected() or loop.time() >= deadline:
                            return
                if attempt is not None:
                    last_seq = (
                        cursor
                        if sent_any
                        else (int(attempt["sequence"]) if attempt.get("sequence") is not None else cursor)
                    )
                    end_data = json.dumps(
                        {"status": attempt.get("status"), "sequence": last_seq},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    yield f"event: end\ndata: {end_data}\n\n"
                return

            if batch_sent:
                poll_interval = SSE_POLL_MIN_SECONDS
            else:
                poll_interval = min(SSE_POLL_MAX_SECONDS, poll_interval * SSE_POLL_BACKOFF_FACTOR)
            next_poll_at = loop.time() + poll_interval

        now = loop.time()
        if now >= deadline:
            return
        if now - last_output_at >= SSE_HEARTBEAT_SECONDS:
            yield ": keep-alive\n\n"
            last_output_at = loop.time()
            continue

        next_wakeup = min(next_poll_at, last_output_at + SSE_HEARTBEAT_SECONDS, deadline)
        delay = max(0.0, next_wakeup - now)
        if delay > 0:
            await asyncio.sleep(delay)


@router.get("/v1/attempts/{attempt_id}/events")
async def get_events(attempt_id: str, request: Request, after: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=1000)) -> dict:
    """再接続時も同じ sequence を返し、consumer に重複排除可能な cursor を渡す。"""
    events = await execution_service(request).store.events(request.state.execution_principal, attempt_id, after, limit)
    return {"schema_version": "1", "events": events, "next_cursor": events[-1]["sequence"] if events else after}


@router.get("/v1/attempts/{attempt_id}/events/stream")
async def stream_events(
    attempt_id: str,
    request: Request,
    after: int = Query(0, ge=0),
    last_event_id: str | None = Header(default=None),
) -> StreamingResponse:
    """Attempt の永続 event を SSE で push 配信する。"""
    service = execution_service(request)
    principal = request.state.execution_principal
    initial_attempt = await service.store.attempt(principal, attempt_id)
    start_after = _resolve_stream_after(after, last_event_id)
    return StreamingResponse(
        _attempt_event_stream(service.store, principal, attempt_id, start_after, request, initial_attempt),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.get("/v1/attempts/{attempt_id}/snapshot")
async def get_attempt_snapshot(attempt_id: str, request: Request) -> dict:
    """Attempt 所有者へ、実行本文を含まない resolved snapshot を返す。"""
    attempt = await execution_service(request).store.attempt(request.state.execution_principal, attempt_id)
    return resolved_attempt_snapshot(attempt).model_dump(mode="json")


@router.get("/v1/artifacts/{ref}")
async def get_artifact(ref: str, request: Request) -> Response:
    """所有を照合した不変 bytes と digest を返す。"""
    artifact, chunks = await execution_service(request).store.artifact_stream(request.state.execution_principal, ref)
    return StreamingResponse(
        content=chunks, media_type=artifact["media_type"],
        headers={"ETag": '"' + artifact["digest"] + '"', "Content-Length": str(artifact["size"]),
                 "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.get('/v1/artifacts/{ref}/retention')
async def get_artifact_retention(ref: str, request: Request) -> dict:
    """所有する Artifact の保持理由と tombstone を返す。bytes の存在を保証する endpoint ではない。"""
    return await execution_service(request).store.artifact_retention.get(request.state.execution_principal, ref)


@router.put('/v1/artifacts/{ref}/retention')
async def change_artifact_retention(ref: str, body: ArtifactRetentionChange, request: Request) -> dict:
    """認証済み Caller の保持判断を CAS と不変 receipt で記録する。"""
    return await execution_service(request).store.artifact_retention.change(request.state.execution_principal, ref, body)
