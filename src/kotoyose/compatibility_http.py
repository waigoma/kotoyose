"""Native Attempt の観測を OpenAI 補完 / SSE に投影する。"""

import asyncio
import json

from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

from .compatibility_execution import CompatibilityExecution
from .config import get_config
from .execution_resources import ExecutionError
from .models import ChatCompletionRequest


def compatibility_adapter(request: Request) -> CompatibilityExecution:
    """service または設定世代の変更時に、次の受付用 adapter を再構築する。"""
    service = request.app.state.execution_service
    config = get_config().compatibility_config
    adapter = getattr(request.app.state, "compatibility_adapter", None)
    if adapter is None or adapter.service is not service or adapter.config is not config:
        adapter = CompatibilityExecution(service, config)
        request.app.state.compatibility_adapter = adapter
    return adapter


def uses_native_execution(request: Request, model: str) -> bool:
    """認証済み principal と alias 設定で経路を選び、Native の拒否を維持する。"""
    kotoyose = get_config()
    config = kotoyose.compatibility_config
    if not config.has_model(model):
        return False
    if getattr(request.app.state, "execution_service", None) is None:
        if model in kotoyose.models:
            return False
        raise ExecutionError("unsupported_capability", "Native execution is not configured", 501)
    if request.state.execution_principal is None:
        raise ExecutionError("authentication_failed", "Caller-bound credential required", 401)
    config.profile(request.state.execution_principal, model)
    return True


def error_body(error: ExecutionError, attempt_id: str) -> dict:
    """stream 開始後も失敗を成功の finish_reason へ変換しない。"""
    return {"error": {"message": str(error), "type": error.code,
                      "safe_to_retry": error.safe_to_retry, "attempt_id": attempt_id}}


async def _wait_for_completion(
    adapter: CompatibilityExecution, principal: str, attempt_id: str, model: str,
):
    while True:
        attempt = await adapter.service.describe(principal, attempt_id)
        if attempt["finished_at"] is not None:
            return await adapter.completion(principal, attempt_id, model)
        # 無出力と停止は別の観測。最終期限の実施は Native Core が所有する。
        await asyncio.sleep(0.2)


async def _stream(adapter: CompatibilityExecution, principal: str, attempt_id: str, model: str):
    task = asyncio.create_task(_wait_for_completion(adapter, principal, attempt_id, model))
    try:
        while not task.done():
            done, _ = await asyncio.wait({task}, timeout=10)
            if not done:
                yield ": observing Native Attempt\n\n"
        result = task.result()
        common = {key: result[key] for key in ("id", "created", "model")}
        common["object"] = "chat.completion.chunk"
        for delta, finish in [
            ({"role": "assistant", "content": result["choices"][0]["message"]["content"]}, None),
            ({}, "stop"),
        ]:
            chunk = {**common, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            if finish:
                chunk["kotoyose"] = result["kotoyose"]
                chunk["usage"] = result["usage"]
            yield "data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n"
    except ExecutionError as error:
        yield "data: " + json.dumps(error_body(error, attempt_id)) + "\n\n"
    finally:
        # 観測接続の切断だけで実行済み副作用を取り消したと扱わない。
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    yield "data: [DONE]\n\n"


async def native_chat_response(
    request: Request, body: ChatCompletionRequest, conversation_key: str | None,
):
    """Native の dispatch / Grant / Artifact を使い、旧 SDK session へ戻らない。"""
    raw = await request.json()
    if set(raw) - {"model", "messages", "stream", "cwd", "metadata"}:
        raise ExecutionError("unsupported_capability", "Unsupported Native chat request parameters", 501)
    for message in raw["messages"]:
        if set(message) - {"role", "content"}:
            raise ExecutionError("unsupported_capability", "Unsupported Native chat message parameters", 501)
        if isinstance(message["content"], list):
            for part in message["content"]:
                allowed = {"type", "text"} if part["type"] == "text" else {"type", "image_url"}
                if set(part) - allowed or (
                    part["type"] == "image_url" and set(part["image_url"]) - {"url", "detail"}
                ):
                    raise ExecutionError("unsupported_capability", "Unsupported Native chat content parameters", 501)
    adapter = compatibility_adapter(request)
    principal, policy = request.state.execution_principal, request.state.execution_policy
    attempt_id, request_key = await adapter.dispatch(
        principal, policy, body, request_key=request.headers.get("x-kotoyose-request-key"),
        conversation_key=conversation_key, api_key_name=request.state.api_key_name,
    )
    headers = {
        "X-Kotoyose-Attempt-ID": attempt_id, "X-Kotoyose-Request-Key": request_key,
        "X-Kotoyose-Session-Mode": "fresh-with-client-history", "Cache-Control": "no-store",
    }
    if body.stream:
        return StreamingResponse(
            _stream(adapter, principal, attempt_id, body.model), media_type="text/event-stream",
            headers={**headers, "X-Accel-Buffering": "no"},
        )
    try:
        result = await _wait_for_completion(adapter, principal, attempt_id, body.model)
    except ExecutionError as error:
        return JSONResponse(status_code=error.status, content=error_body(error, attempt_id), headers=headers)
    return JSONResponse(content=result, headers=headers)
