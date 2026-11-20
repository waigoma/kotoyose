"""隔離実行と tool round の状態機械。履歴の正本と tool 実行は Caller が所有する。"""

import asyncio
from contextlib import aclosing
from collections.abc import AsyncIterator, Callable
from copy import deepcopy
from dataclasses import dataclass, field
import time
from typing import Literal
from uuid import uuid4

from jsonschema import Draft202012Validator, SchemaError

from ..execution_resources import ExecutionError
from .contracts import Continue, Limits, Model, Start
from .ports import Delta, Provider, Round, ToolResult


def _has_reference(value) -> bool:
    if isinstance(value, dict):
        return any(key in {"$ref", "$dynamicRef", "$recursiveRef"} or _has_reference(child)
                   for key, child in value.items())
    return isinstance(value, list) and any(_has_reference(child) for child in value)


@dataclass
class Execution:
    """認証主体に束縛した一時状態。再起動後に再開・自動再実行しない。"""

    id: str
    owner: str
    model: str
    provider: Provider
    tools: dict[str, dict]
    remaining_tokens: int
    tool_results: tuple[ToolResult, ...] = ()
    state: Literal["pending", "running", "waiting_tools", "completed", "failed", "cancelled"] = "pending"
    round: int = 0
    pending: list[str] = field(default_factory=list)
    task: asyncio.Task | None = None
    touched: float = field(default_factory=time.monotonic)
    output_bytes: int = 0
    training_binding: str | None = None


class Conversations:
    """プロセス内の有界実行。transport 切断と cancel で終端化し、遅延結果を拒否する。"""

    def __init__(self, limits: Limits, provider_factory: Callable[[Start], Provider],
                 models: dict[str, Model] | None = None, caller_ids: frozenset[str] = frozenset()):
        self.limits, self.provider_factory = limits, provider_factory
        self.model_config = models or {}
        self.caller_ids = caller_ids
        self.executions: dict[str, Execution] = {}

    def start(self, owner: str, request: Start) -> Execution:
        """副作用なしで受理する。tool schema と provider は入力 snapshot に固定する。"""
        self._expire()
        active = sum(item.state in {"pending", "running", "waiting_tools"} for item in self.executions.values())
        if active >= self.limits.max_executions:
            raise ExecutionError("gateway_busy", "Conversation capacity is exhausted", 429)
        # 終端 tombstone も有界にする。消去後の遅延結果は 404 で拒否される。
        if len(self.executions) >= self.limits.max_executions * 16:
            oldest = next((key for key, value in self.executions.items()
                           if value.state in {"completed", "failed", "cancelled"}), None)
            if oldest is not None:
                del self.executions[oldest]
        serialized = request.model_dump_json().encode("utf-8")
        if len(serialized) > self.limits.max_input_bytes:
            raise ExecutionError("context_input_too_large", "Conversation input exceeds byte limit", 413)
        for tool in request.tools:
            try:
                Draft202012Validator.check_schema(tool.input_schema)
            except SchemaError:
                # SchemaError 本文には入力 schema が含まれるため、共通例外ログへ流さない。
                raise ExecutionError("invalid_request", "Tool schema is invalid", 422) from None
            if tool.input_schema.get("type") != "object" or _has_reference(tool.input_schema):
                raise ExecutionError("invalid_request", "Tool schema must be an object without references", 422)
        item = Execution(str(uuid4()), owner, request.model, self.provider_factory(request),
                         {tool.name: deepcopy(tool.input_schema) for tool in request.tools}, request.max_output_tokens)
        self.executions[item.id] = item
        return item

    def get(self, owner: str, execution_id: str) -> Execution:
        """他主体・期限切れの ID は同じ 404。存在の漏洩や再開を許さない。"""
        self._expire()
        item = self.executions.get(execution_id)
        if item is None or item.owner != owner:
            raise ExecutionError("not_found", "Conversation execution is unavailable", 404)
        return item

    def continue_tools(self, owner: str, execution_id: str, request: Continue) -> Execution:
        """現在の全 Call ID に対応する結果を一度だけ追加する。重複 round は 409。"""
        item = self.get(owner, execution_id)
        calls = [result.call_id for result in request.results]
        if (item.state != "waiting_tools" or item.round != request.round
                or len(calls) != len(set(calls)) or set(calls) != set(item.pending)):
            raise ExecutionError("request_conflict", "Tool results do not match the pending round", 409)
        result_bytes = request.model_dump_json().encode("utf-8")
        if len(result_bytes) > self.limits.max_tool_result_bytes:
            raise ExecutionError("invalid_request", "Tool results exceed byte limit", 413)
        item.tool_results = tuple(ToolResult(result.call_id, deepcopy(result.output)) for result in request.results)
        item.pending, item.state, item.touched = [], "pending", time.monotonic()
        return item

    def cancel(self, owner: str, execution_id: str) -> str:
        """ローカル終端を確定する。接続 close は remote compute の停止証拠ではない。"""
        item = self.get(owner, execution_id)
        if item.state in {"pending", "running", "waiting_tools"}:
            item.state = "cancelled"
            if item.task is not None:
                item.task.cancel()
        return item.state

    async def events(self, item: Execution) -> AsyncIterator[dict]:
        """SSE の 1 round。終端が確定した後の provider completion は採用しない。"""
        if item.state != "pending":
            return
        item.state, item.task = "running", asyncio.current_task()
        item.round += 1
        round_number = item.round
        try:
            if item.round == 1:
                yield {"type": "accepted", "execution_id": item.id}
            if item.round > self.limits.max_rounds or item.remaining_tokens <= 0:
                raise ExecutionError("output_limit_exceeded", "Conversation budget is exhausted", 422)
            async with asyncio.timeout(self.limits.stream_timeout_seconds), aclosing(
                item.provider.stream(item.remaining_tokens, item.tool_results)
            ) as provider_events:
                item.tool_results = ()
                async for event in provider_events:
                    if item.state != "running" or item.round != round_number:
                        return
                    item.touched = time.monotonic()
                    if isinstance(event, Delta):
                        item.output_bytes += len(event.text.encode("utf-8"))
                        if item.output_bytes > self.limits.max_output_bytes:
                            raise ExecutionError("output_limit_exceeded", "Conversation output exceeds byte limit", 422)
                        yield {"type": "delta", "text": event.text}
                        continue
                    if not isinstance(event, Round):
                        raise ExecutionError("upstream_protocol_error", "Unknown provider event", 502)
                    calls = event.calls
                    if type(event.output_tokens) is not int or not 0 <= event.output_tokens <= item.remaining_tokens:
                        raise ExecutionError("upstream_protocol_error", "Provider output usage is invalid", 502)
                    if calls and (item.round >= self.limits.max_rounds or event.output_tokens >= item.remaining_tokens):
                        raise ExecutionError("output_limit_exceeded", "Conversation continuation budget is exhausted", 422)
                    for call in calls:
                        if call.name not in item.tools or not isinstance(call.arguments, dict):
                            raise ExecutionError("upstream_protocol_error", "Provider requested an unknown tool", 502)
                        if not Draft202012Validator(item.tools[call.name]).is_valid(call.arguments):
                            raise ExecutionError("upstream_protocol_error", "Provider tool arguments are invalid", 502)
                    ids = [call.call_id for call in calls]
                    if len(ids) != len(set(ids)):
                        raise ExecutionError("upstream_protocol_error", "Duplicate provider Call ID", 502)
                    item.remaining_tokens -= event.output_tokens
                    item.pending = ids
                    item.state = "waiting_tools" if calls else "completed"
                    if calls:
                        yield {"type": "tool_calls", "round": item.round,
                               "calls": [{"call_id": call.call_id, "name": call.name, "arguments": call.arguments}
                                         for call in calls]}
                    else:
                        yield {"type": "completed"}
                    return
            raise ExecutionError("upstream_disconnected", "Provider ended without a terminal event", 502)
        except asyncio.CancelledError:
            if item.state == "running" and item.round == round_number:
                item.state = "cancelled"
            raise
        except Exception as error:
            if item.state == "running" and item.round == round_number:
                item.state = "failed"
                yield {"type": "failed", "code": error.code if isinstance(error, ExecutionError)
                       else "upstream_disconnected", "accepted": True, "retryable": False}
        finally:
            # 前 round の SSE close が、新しく始まった継続の状態 / task を消さない。
            if item.round == round_number:
                if item.state == "running":
                    item.state = "cancelled"
                item.task = None
                item.touched = time.monotonic()

    async def close(self) -> None:
        """shutdown は未完了実行を取り消し、処理を再起動へ持ち越さない。"""
        tasks = []
        for item in self.executions.values():
            if item.state in {"pending", "running", "waiting_tools"}:
                item.state = "cancelled"
                if item.task is not None:
                    item.task.cancel()
                    tasks.append(item.task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _expire(self) -> None:
        now = time.monotonic()
        for key, item in list(self.executions.items()):
            if now - item.touched >= self.limits.ttl_seconds:
                item.state = "cancelled"
                if item.task is not None:
                    item.task.cancel()
                del self.executions[key]
