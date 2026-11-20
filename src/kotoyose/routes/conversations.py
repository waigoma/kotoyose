"""汎用の明示履歴 API。既存 chat / Execution API の意味を変更しない。"""

from contextlib import aclosing
import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from fastapi.routing import APIRoute

from ..config import get_config
from ..data_policy import alias_data_policy, request_capabilities, training_binding
from ..conversation.contracts import Continue, Limits, Start
from ..conversation.service import Conversations
from ..execution_http import authorize_execution, enforce_model_scope
from ..execution_resources import ExecutionError, agent_profile_allowed

class BoundedBodyRoute(APIRoute):
    """Pydantic の JSON 展開より前に、会話 / tool 結果の入力 byte 上限を適用する。"""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def bounded(request: Request):
            if request.method != "POST" or request.url.path.endswith("/cancel"):
                return await handler(request)
            value = getattr(request.app.state, "conversations", None)
            limits = value.limits if value is not None else Limits()
            maximum = limits.max_tool_result_bytes if request.url.path.endswith("/tool-results") else limits.max_input_bytes
            body, size = [], 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > maximum:
                    raise ExecutionError("context_input_too_large", "Conversation HTTP input exceeds byte limit", 413)
                body.append(chunk)
            payload = b"".join(body)
            replayed = False

            async def receive():
                nonlocal replayed
                if not replayed:
                    replayed = True
                    return {"type": "http.request", "body": payload, "more_body": False}
                return await request.receive()

            return await handler(Request(request.scope, receive))
        return bounded


router = APIRouter(prefix="/v1/conversations", route_class=BoundedBodyRoute,
                   dependencies=[Depends(authorize_execution)])


def service(request: Request) -> Conversations:
    """未設定なら 501。旧 session runtime へ暗黙に戻さない。"""
    value = getattr(request.app.state, "conversations", None)
    if value is None:
        raise ExecutionError("agent_contract_unsupported", "Conversation API is not configured", 501)
    principal = getattr(request.state, "execution_principal", None)
    if principal is not None and principal not in value.caller_ids:
        raise ExecutionError("permission_denied", "Client is not enabled for the Conversation API", 403)
    return value


def owner(request: Request) -> str:
    return request.state.execution_principal


def check_model(request: Request, model: str) -> None:
    """Native Caller の model 許可を拡大しない。"""
    enforce_model_scope(request, model)


def stream(value: Conversations, execution):
    """内容は SSE 本文だけに配信する。"""
    async def events():
        async with aclosing(value.events(execution)) as stream_events:
            async for event in stream_events:
                yield "data: " + json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n\n"
    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no",
                                      "X-Kotoyose-Execution-ID": execution.id})


@router.get("/models")
async def models(request: Request):
    """configured input/output 予算と完全な契約 version を返す。検証フラグの代用にしない。"""
    value = service(request)
    policy = getattr(request.state, "execution_policy", None)
    exec_service = getattr(request.app.state, "execution_service", None)
    settings = getattr(exec_service, "config", None)
    cfg = get_config()
    return {"schema_version": "1", "contract": "explicit-history-v1", "models": [
        {"id": alias, "input_token_budget": config.input_token_budget,
         "max_output_tokens": config.max_output_tokens, "estimator": "utf8_bytes"}
        for alias, config in value.model_config.items()
        if policy is None or agent_profile_allowed(alias, policy, settings, config=cfg)]}


def check_training_policy(model: str, capabilities: set[str]) -> None:
    if message := alias_data_policy(get_config(), model).refusal(capabilities):
        raise ExecutionError("permission_denied", message, 403, reason_code="data_policy_restricted")


@router.post("/executions")
async def start(body: Start, request: Request):
    """新規実行だけを開始する。同一送信を自動再試行・再開しない。"""
    check_model(request, body.model)
    value = service(request)
    if body.model not in value.model_config:
        raise ExecutionError("model_not_found", f"Model '{body.model}' does not exist", 404)
    capabilities = request_capabilities(body)
    check_training_policy(body.model, capabilities)
    if ("attachments" in capabilities or body.tool_choice is not None
            or any(not isinstance(item.content, str) for item in body.messages)):
        raise ExecutionError("invalid_request", "Attachments and tool_choice are unsupported in conversations", 422)
    execution = value.start(owner(request), body)
    execution.training_binding = training_binding(get_config(), body.model)
    return stream(value, execution)


@router.post("/executions/{execution_id}/tool-results")
async def continue_tools(execution_id: str, body: Continue, request: Request):
    """認証主体、実行状態、round、Call ID を照合して一度だけ継続する。"""
    value = service(request)
    execution = value.get(owner(request), execution_id)
    model = execution.model
    check_model(request, model)
    check_training_policy(model, {"tools"})
    if execution.training_binding != training_binding(get_config(), model):
        raise ExecutionError("permission_denied", "Training policy changed since the conversation started", 403,
                             reason_code="data_policy_restricted")
    return stream(value, value.continue_tools(owner(request), execution_id, body))


@router.post("/executions/{execution_id}/cancel")
async def cancel(execution_id: str, request: Request):
    """local cancel の受理。remote compute termination は保証しない。"""
    state = service(request).cancel(owner(request), execution_id)
    return {"schema_version": "1", "state": state, "remote_stop_confirmed": False}
