"""Claude Agent SDK provider adapter。"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeSDKClient,
    ConversationResetMessage,
    ResultError,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from ..activity import emit_runtime_event
from ..connections_probe import record_claude_rate_limit, record_connection_auth_failure
from ..logging_setup import log
from .claude_observations import OBSERVATION_MESSAGES, TASK_MESSAGES, claude_observation

from ..output_guard import guard_enabled, redact_event
from .base import (
    RuntimeEvent,
    RuntimeImageInput,
    RuntimePrompt,
    RuntimeResult,
    RuntimeTextInput,
    RuntimeTurnError,
    image_data_url_parts,
)

_SUMMARY_LIMIT = 500
_CLEANUP_TIMEOUT_SECONDS = 5.0
_cleanup_tasks: set[asyncio.Task[None]] = set()


@dataclass(slots=True)
class ClaudeRuntimeSession:
    """ClaudeSDKClient を provider-aware session として包む薄い adapter。"""

    client: ClaudeSDKClient
    timeout_seconds: float | None = None
    provider: str = "claude"
    invalidated: bool = False
    connection_id: str | None = None

    async def disconnect(self) -> None:
        await self.client.disconnect()

    async def ask(self, prompt: RuntimePrompt) -> RuntimeResult:
        """Claude SDK 応答を RuntimeResult に正規化して返す。"""
        chunks: list[str] = []
        usage: dict | None = None
        session_id: str | None = None
        structured_output: object | None = None
        async for item in self._stream(prompt, enforce_timeout=True):
            if isinstance(item, RuntimeResult):
                usage = item.usage
                session_id = item.runtime_session_id
                structured_output = item.structured_output
                continue
            if isinstance(item, RuntimeEvent):
                emit_runtime_event(item)
                continue
            chunks.append(item)
        return RuntimeResult(
            text="".join(chunks),
            usage=usage,
            runtime_session_id=session_id,
            provider=self.provider,
            structured_output=structured_output,
        )

    async def stream(self, prompt: RuntimePrompt) -> AsyncIterator[str | RuntimeEvent | RuntimeResult]:
        """Claude SDK の message stream を runtime 共通形式へ変換する。

        SSE client の消費待ち中に timeout cancellation が caller task へ届くのを
        防ぐため、streaming 応答では idle timeout を適用しない。
        """
        iterator = self._stream(prompt, enforce_timeout=False)
        try:
            async for item in iterator:
                yield item
        finally:
            # Closing an outer async generator does not synchronously close an
            # inner async-for iterator. Await it so cache invalidation is visible
            # before the HTTP/SSE caller finishes cleanup.
            await iterator.aclose()

    async def _stream(
        self, prompt: RuntimePrompt, *, enforce_timeout: bool
    ) -> AsyncIterator[str | RuntimeEvent | RuntimeResult]:
        """Claude SDK の応答を変換し、非ストリーミング時だけ idle timeout を適用する。"""
        usage: dict | None = None
        session_id: str | None = None
        chunks: list[str] = []
        turn = 1
        tool_names: dict[str, str] = {}
        active_origin: str | None = None
        unclassified_text = False
        terminal_received = False
        completed = False
        cleanup_attempted = False
        structured_output: object | None = None
        assistant_error: str | None = None
        observation_session_id: str | None = None

        def partial_result() -> RuntimeResult:
            return RuntimeResult(
                text="".join(chunks), usage=usage,
                runtime_session_id=session_id, provider=self.provider,
                structured_output=structured_output,
            )

        async def invalidate_incomplete_turn() -> None:
            nonlocal cleanup_attempted
            if cleanup_attempted:
                return
            cleanup_attempted = True
            self.invalidated = True
            await _bounded_disconnect(self)

        try:
            timeout_context = (
                asyncio.timeout(self.timeout_seconds)
                if enforce_timeout
                else contextlib.nullcontext()
            )
            async with timeout_context as timeout, contextlib.aclosing(self.receive_response()) as response:
                await self.query(prompt)
                async for message in response:
                    if isinstance(message, SystemMessage) and message.subtype == "init":
                        initialized_id = message.data.get("session_id")
                        if isinstance(initialized_id, str) and initialized_id:
                            observation_session_id = initialized_id
                    if isinstance(message, OBSERVATION_MESSAGES):
                        observation = claude_observation(message, session_id=observation_session_id)
                        if observation is not None:
                            if enforce_timeout and self.timeout_seconds is not None and isinstance(message, TASK_MESSAGES):
                                timeout.reschedule(asyncio.get_running_loop().time() + self.timeout_seconds)
                            if observation.kind == "rate_limit" and self.connection_id:
                                await record_claude_rate_limit(self.connection_id, observation.payload)
                            if observation.kind == "session_store_warning":
                                log.warning("claude.session_store_mirror_error")
                            yield observation
                        # Preserve SDK task progress liveness. Quota/mirror or
                        # foreign telemetry must not extend the idle timeout.
                        continue
                    # Only provider progress refreshes the non-streaming idle
                    # timeout; waiting on a slow SSE consumer never does.
                    if enforce_timeout and self.timeout_seconds is not None:
                        timeout.reschedule(
                            asyncio.get_running_loop().time() + self.timeout_seconds
                        )
                    if isinstance(message, ConversationResetMessage):
                        # new_conversation_id is a UI identity, NOT a resume ID.
                        session_id = None
                        usage = None
                        structured_output = None
                        observation_session_id = None
                        tool_names.clear()
                        yield RuntimeEvent(kind="session_reset", summary="Claude conversation reset")
                        continue
                    if isinstance(message, UserMessage):
                        origin = _origin_kind(message)
                        if origin is not None:
                            active_origin = origin
                            unclassified_text = False
                    if isinstance(message, AssistantMessage):
                        parent_id = message.parent_tool_use_id
                        metadata = _event_metadata(active_origin, parent_id)
                        foreground = active_origin in (None, "human") and parent_id is None
                        yield RuntimeEvent(
                            kind="turn",
                            summary=f"Assistant turn {turn}",
                            payload={"turn": turn, **metadata},
                        )
                        turn += 1
                        if foreground and session_id is None:
                            session_id = message.session_id or None
                        if message.session_id:
                            observation_session_id = message.session_id
                        if message.error is not None:
                            error_kind = (
                                message.error if message.error in _SAFE_ASSISTANT_ERRORS else "unknown"
                            )
                            if foreground:
                                assistant_error = error_kind
                            if error_kind == "authentication_failed" and self.connection_id:
                                await record_connection_auth_failure(
                                    self.connection_id,
                                    error="authentication_failed",
                                )
                            # SDK API-error assistant blocks may contain raw
                            # exception prose. They are not answer text.
                            yield RuntimeEvent(
                                kind="provider_error", summary="Claude assistant reported an error",
                                payload={"assistant_error": error_kind, **metadata},
                            )
                            continue
                        for block in message.content:
                            if isinstance(block, TextBlock):
                                yield RuntimeEvent(
                                    kind="text", summary=_summarize(block.text),
                                    payload={"text": _truncate(block.text), **metadata},
                                )
                                if foreground:
                                    chunks.append(block.text)
                                    unclassified_text = True
                                    yield block.text
                            elif isinstance(block, ToolUseBlock):
                                tool_names[block.id] = block.name
                                detail = _json_text(block.input)
                                yield RuntimeEvent(
                                    kind="tool_use", tool_name=block.name,
                                    summary=_summarize(detail),
                                    payload={
                                        "tool_use_id": block.id,
                                        "input": _truncate_value(block.input),
                                        **metadata,
                                    },
                                )
                            elif isinstance(block, ToolResultBlock):
                                yield _tool_result_event(block, tool_names, metadata)
                    elif isinstance(message, UserMessage) and isinstance(message.content, list):
                        metadata = _event_metadata(active_origin, message.parent_tool_use_id)
                        for block in message.content:
                            if isinstance(block, ToolResultBlock):
                                yield _tool_result_event(block, tool_names, metadata)
                    elif isinstance(message, ResultMessage):
                        origin = _origin_kind(message)
                        if origin not in (None, "human") or (
                            origin is None and active_origin not in (None, "human")
                        ):
                            # receive_response() ends at *any* result, including
                            # injected turns. Do not attribute their usage or ID.
                            if active_origin in (None, "human") and unclassified_text:
                                raise RuntimeTurnError(
                                    "Claude turn attribution is ambiguous",
                                    partial_result=partial_result(),
                                    diagnostics={"terminal_reason": "ambiguous_origin"},
                                )
                            yield RuntimeEvent(
                                kind="background_result", summary="Claude background turn completed",
                                payload=_event_metadata(origin or active_origin, None),
                            )
                            active_origin = None
                            unclassified_text = False
                            continue
                        terminal_received = True
                        usage = _extract_usage(message)
                        structured_output = message.structured_output
                        # The result ID is authoritative, including result-only
                        # turns and sessions replaced by /clear.
                        session_id = message.session_id or session_id
                        if _result_failed(message) or assistant_error is not None:
                            error = _turn_error(message, partial_result())
                            if assistant_error is not None:
                                error.diagnostics["assistant_error"] = assistant_error
                            raise error
                        completed = True
                        break
                if not terminal_received:
                    raise RuntimeTurnError(
                        "Claude stream ended without a terminal result",
                        partial_result=partial_result(),
                        diagnostics={"terminal_reason": "missing_result"},
                    )
        except TimeoutError:
            await invalidate_incomplete_turn()
            error = RuntimeTurnError(
                f"claude が {self.timeout_seconds:g} 秒間応答を返しませんでした (idle timeout)",
                partial_result=partial_result(),
                diagnostics={"terminal_reason": "idle_timeout"},
            )
            yield _error_event(error)
            raise error from None
        except ResultError as exc:
            # SDK exceptions contain raw API prose. Use only typed, bounded
            # classification and retain usage from the terminal payload.
            if _origin_kind(exc.data) not in (None, "human"):
                diagnostics = {"terminal_reason": "background_process_error"}
            else:
                usage = _extract_usage(exc.data) or usage
                session_id = exc.session_id or session_id
                diagnostics = _safe_diagnostics(exc)
            await invalidate_incomplete_turn()
            error = RuntimeTurnError(
                "Claude turn failed", partial_result=partial_result(), diagnostics=diagnostics,
            )
            yield _error_event(error)
            raise error from None
        except RuntimeTurnError as exc:
            # An interrupted/failed turn may leave unread provider messages.
            # Never reuse the live client (nor resume it through the cache).
            await invalidate_incomplete_turn()
            yield _error_event(exc)
            raise
        except Exception:
            await invalidate_incomplete_turn()
            error = RuntimeTurnError(
                "Claude runtime error", partial_result=partial_result(),
                diagnostics={"terminal_reason": "sdk_error"},
            )
            yield _error_event(error)
            raise error from None
        finally:
            # Includes CancelledError and GeneratorExit, which are BaseException
            # subclasses and deliberately are not converted to provider errors.
            if not completed:
                await invalidate_incomplete_turn()
        yield RuntimeResult(
            text="".join(chunks), usage=usage, runtime_session_id=session_id, provider=self.provider,
            structured_output=structured_output,
        )

    async def query(self, prompt: RuntimePrompt) -> None:
        """文字列または画像 content blocks を Claude SDK へ送る。"""
        await self.client.query(_claude_query_input(prompt))

    async def receive_response(self):
        """Read past injected results, using the SDK's supported iterator API."""
        while True:
            saw_result = False
            async with contextlib.aclosing(self.client.receive_response()) as response:
                async for message in response:
                    yield message
                    if isinstance(message, ResultMessage):
                        saw_result = True
                        if _origin_kind(message) in (None, "human"):
                            return
            if not saw_result:
                return


def _consume_cleanup_result(task: asyncio.Task[None]) -> None:
    _cleanup_tasks.discard(task)
    with contextlib.suppress(BaseException):
        task.result()


async def _bounded_disconnect(session: ClaudeRuntimeSession) -> None:
    """Bound caller cleanup even if a custom SDK transport ignores cancellation.

    Keep a strong reference until it actually stops and consume its outcome; a
    timed-out disconnect is best effort, not evidence of an OS process stop.
    """
    task = asyncio.create_task(session.disconnect())
    _cleanup_tasks.add(task)
    task.add_done_callback(_consume_cleanup_result)
    try:
        done, _ = await asyncio.wait({task}, timeout=_CLEANUP_TIMEOUT_SECONDS)
    except BaseException:
        task.cancel()
        raise
    if not done:
        task.cancel()


def _claude_query_input(prompt: RuntimePrompt):
    """runtime 共通入力を Claude streaming input protocol へ変換する。"""
    if isinstance(prompt, str):
        return prompt

    async def _messages():
        content: list[dict[str, object]] = []
        for part in prompt:
            if isinstance(part, RuntimeTextInput):
                content.append({"type": "text", "text": part.text})
                continue
            assert isinstance(part, RuntimeImageInput)
            data_url = image_data_url_parts(part.url)
            source: dict[str, str]
            if data_url is None:
                source = {"type": "url", "url": part.url}
            else:
                media_type, data = data_url
                source = {
                    "type": "base64",
                    "media_type": media_type,
                    "data": data,
                }
            content.append({"type": "image", "source": source})
        yield {
            "type": "user",
            "session_id": "",
            "message": {"role": "user", "content": content},
            "parent_tool_use_id": None,
        }

    return _messages()


_SAFE_TERMINAL_REASONS = frozenset({
    "completed", "max_turns", "max_budget_usd", "max_budget", "max_tokens",
    "aborted_streaming", "aborted_tools", "api_error", "error",
    "stop_sequence", "end_turn", "refusal", "tool_use",
})
_SAFE_RESULT_SUBTYPES = frozenset({
    "success", "error_during_execution", "error_max_turns",
    "error_max_budget_usd", "error_max_structured_output_retries",
})
_SAFE_ASSISTANT_ERRORS = frozenset({
    "authentication_failed", "billing_error", "rate_limit", "invalid_request",
    "server_error", "unknown",
})


def _origin_kind(message) -> str | None:
    origin = message.get("origin") if isinstance(message, dict) else getattr(message, "origin", None)
    if isinstance(origin, dict) and isinstance(origin.get("kind"), str):
        return origin["kind"]
    return None


def _event_metadata(origin: str | None, parent_id: str | None) -> dict[str, str]:
    metadata = {}
    if origin is not None:
        # Unknown origin kinds may contain untrusted text. Keep only the fact
        # that they are injected; no external origin metadata is copied.
        metadata["origin_kind"] = origin if origin in ("human", "task-notification", "channel", "peer") else "injected"
    if parent_id is not None:
        metadata["parent_tool_use_id"] = parent_id
    return metadata


def _result_failed(message: ResultMessage) -> bool:
    return (
        message.is_error
        or message.subtype != "success"
        or message.terminal_reason not in (None, "completed")
    )


def _safe_diagnostics(message) -> dict[str, str | int]:
    diagnostics: dict[str, str | int] = {}
    reason = getattr(message, "terminal_reason", None)
    subtype = getattr(message, "subtype", None)
    if reason is not None:
        diagnostics["terminal_reason"] = reason if reason in _SAFE_TERMINAL_REASONS else "unknown"
    if subtype is not None:
        diagnostics["subtype"] = subtype if subtype in _SAFE_RESULT_SUBTYPES else "unknown"
    status = getattr(message, "api_error_status", None)
    if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
        diagnostics["api_error_status"] = status
    return diagnostics


def _turn_error(message: ResultMessage, result: RuntimeResult) -> RuntimeTurnError:
    return RuntimeTurnError(
        "Claude turn failed", partial_result=result, diagnostics=_safe_diagnostics(message),
    )


def _error_event(error: RuntimeTurnError) -> RuntimeEvent:
    return RuntimeEvent(kind="error", summary=str(error), payload=dict(error.diagnostics))


def _extract_usage(message) -> dict | None:
    """ResultMessage.usage を dict 化する (None / dict / オブジェクトに対応)。"""
    usage = message.get("usage") if isinstance(message, dict) else getattr(message, "usage", None)
    result: dict
    if isinstance(usage, dict):
        result = dict(usage)
    elif hasattr(usage, "model_dump"):
        result = usage.model_dump()
    elif hasattr(usage, "__dict__"):
        result = dict(usage.__dict__)
    else:
        result = {}

    # Claude SDK は料金を usage ではなく ResultMessage.total_cost_usd に持つ。
    # route 層の provider 共通 normalizer へ渡すため、ここで共通キーに正規化する。
    total_cost = message.get("total_cost_usd") if isinstance(message, dict) else getattr(message, "total_cost_usd", None)
    if isinstance(total_cost, (int, float)) and not isinstance(total_cost, bool):
        result["cost_usd"] = float(total_cost)
    return result or None


def _truncate(value: str) -> str:
    """イベントへ保存する文字列を上限内へ切り詰める。"""
    if guard_enabled():
        value = redact_event(value)
    return value[:_SUMMARY_LIMIT]


def _summarize(value: str) -> str:
    """改行を空白へ畳み、人間可読の 1 行要約を作る。"""
    return _truncate(" ".join(value.split()))


def _json_text(value: object) -> str:
    """SDK の任意値を安定した要約用文字列へ変換する。"""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def _truncate_value(value: object) -> object:
    """payload の詳細も DB の肥大を避けるため 500 文字へ制限する。"""
    if isinstance(value, str):
        return _truncate(value)
    return _truncate(_json_text(value))


def _tool_result_event(
    block: ToolResultBlock,
    tool_names: dict[str, str],
    metadata: dict[str, str] | None = None,
) -> RuntimeEvent:
    """tool use ID を名前へ対応付けた結果イベントを作る。"""
    tool_name = tool_names.pop(block.tool_use_id, None)
    detail = _json_text(block.content)
    status = "error" if block.is_error else "ok"
    return RuntimeEvent(
        kind="tool_result",
        tool_name=tool_name,
        summary=_summarize(f"{status}: {detail}"),
        payload={
            "tool_use_id": block.tool_use_id,
            "is_error": bool(block.is_error),
            "output": _truncate_value(block.content),
            **(metadata or {}),
        },
    )
