"""Claude Agent SDK provider adapter。"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from tempfile import TemporaryDirectory

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ConversationResetMessage,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultError,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk.types import ToolPermissionContext
from ..activity import emit_runtime_event
from ..chat_approvals import APPROVAL_REJECTED, OPT_IN_REQUIRED, TOOL_KIND_BY_NAME, TurnApprovals, current_turn_approvals
from ..connections_probe import record_claude_rate_limit, record_connection_auth_failure
from ..logging_setup import log
from .claude_observations import OBSERVATION_MESSAGES, TASK_MESSAGES, claude_observation

from ..output_guard import guard_enabled, redact_event
from .base import (
    RuntimeChatHistory,
    RuntimeEvent,
    RuntimeImageInput,
    RuntimePrompt,
    RuntimeResult,
    RuntimeTextInput,
    RuntimeTurnError,
    image_data_url_parts,
    prompt_text,
)

_SUMMARY_LIMIT = 500
_CLEANUP_TIMEOUT_SECONDS = 5.0
_cleanup_tasks: set[asyncio.Task[None]] = set()
# 許可の呼び出しが、その tool_use の message より先に届いたときに待つ上限。
_SEEN_WAIT_SECONDS = 30.0


class ClaudeToolApprovals:
    def __init__(self, options: ClaudeAgentOptions):
        self.tools = frozenset(options.tools) if isinstance(options.tools, list) else None
        # The CLI evaluates scoped denies. A callback for a tool with such a
        # rule cannot establish that it is safe for the client to override.
        self.denied = frozenset(rule.partition("(")[0] for rule in options.disallowed_tools)
        self.lock = asyncio.Lock()
        self.turn: TurnApprovals | None = None
        self.tool_use_ids: set[str] = set()
        self.turn_deadline: float | None = None
        self._changed = asyncio.Event()

    def _notify(self) -> None:
        changed, self._changed = self._changed, asyncio.Event()
        changed.set()

    def remember(self, ids) -> None:
        """この turn の stream で見えた tool_use の id。待っている許可の呼び出しを起こす。"""
        self.tool_use_ids.update(ids)
        self._notify()

    def forget(self) -> None:
        self.tool_use_ids.clear()
        self._notify()

    async def _seen(self, tool_id: str, tool_use_ids: set[str]) -> bool:
        """許可の呼び出しは、その tool_use を含む message を stream が読むより先に届くことがある。

        同じ turn のあいだだけ、id が見えるまで待つ。turn が終わるか期限が来たら見えなかった扱い。
        """
        loop = asyncio.get_running_loop()
        end = loop.time() + _SEEN_WAIT_SECONDS
        if self.turn_deadline is not None:
            end = min(end, self.turn_deadline)
        while tool_id not in tool_use_ids:
            if self.turn is None or tool_use_ids is not self.tool_use_ids:
                return False
            remaining = end - loop.time()
            if remaining <= 0:
                return False
            changed = self._changed
            try:
                await asyncio.wait_for(changed.wait(), remaining)
            except TimeoutError:
                return False
        return True

    @contextlib.asynccontextmanager
    async def bind(self, timeout_seconds: float | None = None):
        async with self.lock:
            self.turn = current_turn_approvals.get()
            self.tool_use_ids = set()
            if timeout_seconds is not None:
                self.turn_deadline = asyncio.get_running_loop().time() + timeout_seconds
            try:
                yield
            finally:
                self.turn = None
                self.tool_use_ids.clear()
                self.turn_deadline = None
                self._notify()

    async def decide(self, name: str, _input: dict, context: ToolPermissionContext):
        tool_id = context.tool_use_id
        tool_use_ids = self.tool_use_ids
        if not isinstance(tool_id, str) or not await self._seen(tool_id, tool_use_ids):
            return PermissionResultDeny(message=APPROVAL_REJECTED)
        if context.agent_id or name in self.denied or (
            self.tools is not None and name not in self.tools and not name.startswith("mcp__")
        ):
            return PermissionResultDeny(message=APPROVAL_REJECTED)
        turn = self.turn
        if turn is None:
            return PermissionResultDeny(message=OPT_IN_REQUIRED)
        message = await turn.request(
            tool_id, context.title or name, TOOL_KIND_BY_NAME.get(name.lower(), "other"),
            turn_deadline=self.turn_deadline,
        )
        if tool_use_ids is not self.tool_use_ids or tool_id not in tool_use_ids:
            return PermissionResultDeny(message=APPROVAL_REJECTED)
        return PermissionResultDeny(message=message) if message else PermissionResultAllow()


@dataclass(slots=True)
class ClaudeRuntimeSession:
    """ClaudeSDKClient を provider-aware session として包む薄い adapter。"""

    client: ClaudeSDKClient
    timeout_seconds: float | None = None
    provider: str = "claude"
    invalidated: bool = False
    connection_id: str | None = None
    tool_approvals: ClaudeToolApprovals | None = None
    skill_temp: TemporaryDirectory[str] | None = field(default=None, repr=False)

    async def disconnect(self) -> None:
        try:
            await self.client.disconnect()
        finally:
            if self.skill_temp is not None:
                self.skill_temp.cleanup()
                self.skill_temp = None

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
        binding = self.tool_approvals.bind(self.timeout_seconds) if self.tool_approvals else contextlib.nullcontext()
        async with binding, contextlib.aclosing(self._query_stream(prompt, enforce_timeout=enforce_timeout)) as items:
            async for item in items:
                yield item

    async def _query_stream(
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
            if self.tool_approvals is not None:
                self.tool_approvals.forget()
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
                        if self.tool_approvals is not None:
                            self.tool_approvals.forget()
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
                        if self.tool_approvals is not None and foreground and message.error is None:
                            self.tool_approvals.remember(
                                block.id for block in message.content if isinstance(block, ToolUseBlock)
                            )
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
                        if self.tool_approvals is not None:
                            self.tool_approvals.forget()
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
    if isinstance(prompt, (str, RuntimeChatHistory)):
        return prompt_text(prompt)

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
