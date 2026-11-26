"""Cursor Python SDK provider adapter."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Awaitable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cursor_sdk import (
    AgentOptions, AsyncAgent, AsyncClient, HttpMcpServerConfig,
    LocalAgentOptions, SDKImage, SseMcpServerConfig, StdioMcpServerConfig,
    TokenUsage, UserMessage,
)

from .. import process_env
from ..activity import emit_runtime_event
from ..config import get_config
from ..logging_setup import log
from ..secret_store import SecretStoreError, resolve_secret_async
from .base import (
    RuntimeEvent,
    RuntimeImageInput,
    RuntimePrompt,
    RuntimeResult,
    RuntimeTurnError,
    RuntimeTextInput,
    image_data_url_parts,
    prompt_text,
)
from .translation import apply_common_timeout, translate_runtime_config

_DEFAULT_TIMEOUT_SECONDS = 600.0
_CLEANUP_TIMEOUT_SECONDS = 5.0
_CLEANUP_FAILED = object()
_TERMINAL_STATUSES = frozenset({"finished", "error", "cancelled", "expired"})
_SUMMARY_LIMIT = 500
_CURSOR_SETTING_SOURCES = frozenset({"user", "project", "team", "mdm", "plugins", "all"})
_IGNORED_CLI_KEYS = {"command", "args", "output_format", "stream_partial_output", "force", "resume_flag"}
_CURSOR_AUTH_RECOVERY_MARKERS = (
    "try logging out and back in",
    "error_not_logged_in",
)
_MAX_AUTH_RECOVERY_ATTEMPTS = 1
# 共通 effort を Cursor へ転送してよいと見なす値の fallback。正本は model ごとの
# Cursor catalog (model_details.parameters[id="effort"].values) で、これはそれを
# 引けない session 構築時にだけ使う推定値。admin.js の CURSOR_EFFORT_FALLBACK と
# 対で維持すること。
_CURSOR_FALLBACK_EFFORTS = frozenset({"low", "medium", "high", "xhigh"})


@dataclass(frozen=True, slots=True)
class CursorRunOutcome:
    """Provider run identity and verified outcome; unknown is never success."""

    run_id: str | None
    status: str
    stop_confirmed: bool


class CursorRunError(RuntimeTurnError):
    """A failed/incomplete Cursor turn, retaining safe diagnostic fields."""

    def __init__(
        self, message: str, *, run_id: str | None = None, status: str = "unknown",
        partial_text: str = "", diagnostics: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            message,
            partial_result=RuntimeResult(partial_text, provider="cursor"),
            diagnostics={"terminal_reason": status, **(diagnostics or {})},
            retryable=(diagnostics or {}).get("is_retryable"),
        )
        self.run_id = run_id
        self.status = status
        self.partial_text = partial_text


@dataclass(slots=True)
class CursorRuntimeSession:
    """Cursor SDK の local agent を RuntimeSession 境界へ適合させる。"""

    model: str | None
    cwd: str
    config: dict[str, Any] = field(default_factory=dict)
    model_params: dict[str, str] = field(default_factory=dict)
    session_id: str | None = None
    system_preamble: str | None = None
    mcp_servers: dict[str, Any] = field(default_factory=dict)
    tools: list[str] | None = None
    disallowed_tools: list[str] = field(default_factory=list)
    setting_sources: list[str] | None = None
    sandbox_options: dict[str, Any] | None = None
    mode: str | None = None
    connection: str | None = None
    provider: str = "cursor"
    last_outcome: CursorRunOutcome | None = field(default=None, init=False)
    _client: Any = field(default=None, init=False, repr=False)
    _agent: Any = field(default=None, init=False, repr=False)
    _active_run: Any = field(default=None, init=False, repr=False)
    _active_events: Any = field(default=None, init=False, repr=False)
    _pending_io: asyncio.Task | None = field(default=None, init=False, repr=False)
    _cleanup_task: asyncio.Task | None = field(default=None, init=False, repr=False)
    _streaming: bool = field(default=False, init=False, repr=False)
    _closing: bool = field(default=False, init=False, repr=False)
    _connection_epoch: int = field(default=0, init=False, repr=False)

    async def disconnect(self) -> None:
        """Cancel/reconcile an active run, then close agent and bridge exactly once.

        A single cleanup task owns cancellation, stream draining and resource
        closure. In-flight SDK reads are cancelled first, so wait() never races
        the events() iterator. Shielding keeps caller cancellation from orphaning
        cleanup; every provider operation has a finite cleanup deadline.
        """
        if self._cleanup_task is None or self._cleanup_task.done():
            self._closing = True
            self._connection_epoch += 1
            self._cleanup_task = asyncio.create_task(self._disconnect_owned())
        await asyncio.shield(self._cleanup_task)

    async def _disconnect_owned(self) -> None:
        try:
            pending = self._pending_io
            read_stopped = True
            if pending is not None and not pending.done():
                pending.cancel()
                done, _ = await asyncio.wait({pending}, timeout=_CLEANUP_TIMEOUT_SECONDS)
                read_stopped = bool(done)
                pending.add_done_callback(_consume_task_exception)
            run = self._active_run
            if run is not None:
                run_id = _run_id(run)
                status = _status(getattr(run, "status", None))
                if status not in _TERMINAL_STATUSES:
                    await self._cleanup_call("cancel", run.cancel())
                events = self._active_events
                if read_stopped and events is not None:
                    closed = await self._cleanup_call("events_close", events.aclose())
                    read_stopped = closed is not _CLEANUP_FAILED
                # wait() can return a terminal result cached by events(), or
                # reconcile an interrupted local Send. Do not use Send offsets
                # as ObserveRun offsets, and never resend this prompt.
                terminal = (
                    await self._cleanup_call("reconcile", run.wait())
                    if read_stopped else _CLEANUP_FAILED
                )
                if terminal is not _CLEANUP_FAILED:
                    status = _status(getattr(terminal, "status", None))
                    run_id = _run_id(terminal) or run_id
                self.last_outcome = CursorRunOutcome(
                    run_id, status or "unknown", read_stopped and status in _TERMINAL_STATUSES,
                )
                if not self.last_outcome.stop_confirmed:
                    log.warning("cursor.stop_unconfirmed", run_id=run_id, status=status)
            elif (
                self.last_outcome is not None and not self.last_outcome.stop_confirmed
                and (self._agent is not None or self._client is not None)
            ):
                log.warning("cursor.stop_unconfirmed", run_id=None, status="unknown")
            agent, client = self._agent, self._client
            # Detach before awaiting close so future requests cannot reuse stale
            # objects, including after failed cancellation or bridge shutdown.
            self._active_run = self._active_events = None
            self._pending_io = None
            self._agent = self._client = None
            if agent is not None:
                await self._cleanup_call("agent_close", agent.close())
            if client is not None:
                await self._cleanup_call("client_close", client.aclose())
        finally:
            self._closing = False

    async def _cleanup_call(self, operation: str, awaitable: Awaitable) -> Any:
        task = asyncio.ensure_future(awaitable)
        try:
            # wait_for waits for cancellation acknowledgement and can hang when
            # a transport suppresses CancelledError. Bound the caller's wait,
            # retain exception retrieval, then close the underlying client.
            done, _ = await asyncio.wait({task}, timeout=_CLEANUP_TIMEOUT_SECONDS)
            if not done:
                task.cancel()
                task.add_done_callback(_consume_task_exception)
                raise TimeoutError
            return task.result()
        except (asyncio.CancelledError, Exception) as exc:
            # Do not include provider payloads, headers or credentials in logs.
            log.warning(
                "cursor.cleanup_failed", operation=operation,
                run_id=_run_id(self._active_run), error_type=type(exc).__name__,
            )
            return _CLEANUP_FAILED

    async def _io(self, awaitable: Awaitable, timeout: float) -> Any:
        epoch = self._connection_epoch
        task = asyncio.ensure_future(awaitable)
        self._pending_io = task
        try:
            done, _ = await asyncio.wait({task}, timeout=timeout)
            if not done:
                raise TimeoutError
            value = task.result()
            if epoch != self._connection_epoch:
                # A cancellation-resistant operation must not resume an old
                # request after disconnect already closed its connection.
                raise asyncio.CancelledError
            return value
        except BaseException:
            if not task.done():
                task.cancel()
                task.add_done_callback(_consume_task_exception)
            raise
        finally:
            # Cleanup needs ownership of a cancelled task until it really stops.
            if self._pending_io is task and task.done():
                self._pending_io = None

    async def ask(self, prompt: RuntimePrompt) -> RuntimeResult:
        """stream の最終結果を非ストリーミング呼び出しへ集約する。"""
        result: RuntimeResult | None = None
        async for item in self.stream(prompt):
            if isinstance(item, RuntimeEvent):
                emit_runtime_event(item)
            elif isinstance(item, RuntimeResult):
                result = item
        assert result is not None
        return result

    async def stream(
        self, prompt: RuntimePrompt
    ) -> AsyncIterator[str | RuntimeEvent | RuntimeResult]:
        """Verify the terminal outcome rather than treating stream EOF as success."""
        if self._streaming or self._closing:
            raise RuntimeError("Cursor session already has an active turn or cleanup")
        if self.last_outcome is not None and not self.last_outcome.stop_confirmed:
            raise RuntimeError("Previous Cursor run stop is unconfirmed; start a new session instead of resuming")
        if self.system_preamble and self.session_id is None:
            if isinstance(prompt, str):
                prompt = f"{self.system_preamble}\n\n{prompt}"
            else:
                prompt = [RuntimeTextInput(self.system_preamble), *prompt]
        # Validate before starting a bridge, including direct runtime callers.
        message_input = _cursor_user_message(prompt)
        timeout = _resolve_timeout(self.config)
        self._streaming = True
        self.last_outcome = None
        completed = False
        chunks: list[str] = []
        try:
            for attempt in range(_MAX_AUTH_RECOVERY_ATTEMPTS + 1):
                try:
                    agent = await self._io(self._ensure_agent(), timeout)
                    break
                except Exception as exc:
                    # Only setup is safe to retry. send() primes the first wire
                    # envelope and can raise after dispatch, before returning a
                    # run handle or visible text. Absence of text is not proof
                    # that tools/prompt side effects did not occur.
                    if attempt >= _MAX_AUTH_RECOVERY_ATTEMPTS or not _is_recoverable_auth_error(str(exc)):
                        raise
                    await self._recover_auth_connection()
            self.session_id = getattr(agent, "agent_id", None) or self.session_id
            self.last_outcome = CursorRunOutcome(None, "unknown", False)
            run = await self._io(agent.send(message_input), timeout)
            self._active_run = run
            self._active_events = run.events()
            turn = 1
            turn_emitted = False
            while True:
                try:
                    event = await self._io(anext(self._active_events), timeout)
                except StopAsyncIteration:
                    break
                message = getattr(event, "sdk_message", None)
                message_type = getattr(message, "type", "")
                error = _event_error(event, message)
                if error:
                    status = _event_status(event, message) or "error"
                    raise CursorRunError(error, run_id=_run_id(run), status=status)
                if not turn_emitted and message_type in {"thinking", "tool_call", "task"}:
                    turn_emitted = True
                    yield RuntimeEvent(
                        kind="turn", summary=f"Assistant turn {turn}",
                        payload={"turn": turn, "run_id": _run_id(run)},
                    )
                activity = _runtime_event_from_message(message)
                if activity is not None:
                    activity.payload = {**(activity.payload or {}), "run_id": _run_id(run)}
                    yield activity
                if message_type == "assistant":
                    for block in getattr(getattr(message, "message", None), "content", ()):
                        text = getattr(block, "text", "")
                        if text:
                            chunks.append(text)
                            yield text
                # SDK reports per-turn usage at turn end, including background
                # follow-up turns. Its run.usage performs cumulative accounting.
                if message_type == "usage":
                    turn += 1
                    turn_emitted = False
            terminal = await self._io(run.wait(), timeout)
            status = _status(getattr(terminal, "status", None))
            run_id = _run_id(terminal) or _run_id(run)
            self.last_outcome = CursorRunOutcome(run_id, status or "unknown", status in _TERMINAL_STATUSES)
            if status != "finished":
                raise CursorRunError(
                    f"Cursor run did not finish successfully ({status or 'unknown'})",
                    run_id=run_id, status=status or "unknown",
                )
            # Some successful runs contain only a terminal result, with no text
            # messages. Preserve that output without replaying streamed text.
            if not chunks and (text := getattr(terminal, "result", "")):
                chunks.append(text)
                yield text
            usage = _normalize_usage(getattr(terminal, "usage", None) or getattr(run, "usage", None))
            self._active_run = self._active_events = None
            completed = True
            yield RuntimeResult("".join(chunks), usage, self.session_id, self.provider)
        except Exception as exc:
            run_id = _run_id(self._active_run)
            if isinstance(exc, CursorRunError):
                error = exc
                error.partial_text = "".join(chunks)
            elif isinstance(exc, TimeoutError):
                error = CursorRunError(
                    f"cursor が {timeout:g} 秒間イベントを返しませんでした (idle timeout)",
                    run_id=run_id, partial_text="".join(chunks),
                )
            else:
                error = CursorRunError(
                    "Cursor SDK request failed", run_id=run_id,
                    partial_text="".join(chunks), diagnostics=_safe_diagnostics(exc),
                )
            failed_run = self._active_run
            await self.disconnect()
            error.partial_result = RuntimeResult(
                "".join(chunks), _normalize_usage(getattr(failed_run, "usage", None)),
                self.session_id, self.provider,
            )
            yield RuntimeEvent(
                kind="error", summary=_summarize(str(error)),
                payload={
                    "error": _truncate(str(error)), "run_id": error.run_id,
                    "status": error.status,
                    "stop_confirmed": bool(self.last_outcome and self.last_outcome.stop_confirmed),
                    **error.diagnostics,
                },
            )
            raise error from None
        finally:
            try:
                if not completed:
                    await self.disconnect()
            finally:
                self._streaming = False

    async def _recover_auth_connection(self) -> None:
        """Only pre-Send authentication failures may recreate the bridge."""
        log.warning("cursor.auth_recovery", session_id=self.session_id)
        await self.disconnect()

    async def _ensure_agent(self) -> Any:
        if self._agent is not None:
            return self._agent
        cfg = get_config()
        if self.connection:
            conn = cfg.connections.get(self.connection)
            if conn is None:
                raise RuntimeError(f"Cursor SDK の connection '{self.connection}' が設定に見つかりません")
        else:
            conn = cfg.cursor_connection()
        if conn is not None and conn.secret_name:
            try:
                api_key = await resolve_secret_async(conn.secret_name)
            except SecretStoreError as exc:
                raise RuntimeError(f"Cursor SDK の secret '{conn.secret_name}' を解決できません: {exc}") from exc
        elif conn is not None and conn.env_name:
            api_key = process_env.get(conn.env_name) or None
        elif conn is not None:
            api_key = None
        else:
            api_key = process_env.get("CURSOR_API_KEY") or None
        if not api_key:
            raise RuntimeError(
                "Cursor SDK の API key が未設定です。Connections 画面 (cursor connection) または CURSOR_API_KEY で設定してください"
            )
        epoch = self._connection_epoch
        client = await AsyncClient.launch_bridge(workspace=self.cwd)
        if epoch != self._connection_epoch:
            await self._cleanup_call("late_client_close", client.aclose())
            raise asyncio.CancelledError
        self._client = client
        model_selection = None
        if self.model:
            model_selection = {"id": self.model}
            if self.model_params:
                model_selection["params"] = [
                    {"id": key, "value": value}
                    for key, value in self.model_params.items()
                ]
        options = AgentOptions(
            model=model_selection,
            api_key=api_key,
            local=LocalAgentOptions(
                cwd=self.cwd,
                setting_sources=self.setting_sources,
                sandbox_options=self.sandbox_options,
            ),
            mcp_servers=self.mcp_servers or None,
            tools=self.tools,
            disallowed_tools=self.disallowed_tools or None,
            agents=None,
            mode=self.mode,
        )
        try:
            if self.session_id:
                agent = await AsyncAgent.resume(self.session_id, options, client=client)
            else:
                agent = await AsyncAgent.create(options, client=client, api_key=api_key)
        except Exception:
            if self._client is client:
                self._client = None
                await self._cleanup_call("setup_client_close", client.aclose())
            raise
        if epoch != self._connection_epoch:
            await self._cleanup_call("late_agent_close", agent.close())
            raise asyncio.CancelledError
        self._agent = agent
        return agent


def _cursor_user_message(prompt: RuntimePrompt):
    """runtime 共通入力を Cursor SDK の UserMessage へ変換する。"""
    if isinstance(prompt, str):
        return prompt
    images: list[SDKImage] = []
    for part in prompt:
        if not isinstance(part, RuntimeImageInput):
            continue
        from ..chat_policy import validate_image_input
        from ..models import ImageURL

        validate_image_input("cursor", [part.url])
        validated = ImageURL(url=part.url)
        data_url = image_data_url_parts(validated.url)
        assert data_url is not None
        media_type, data = data_url
        images.append(SDKImage.from_data(data, media_type))
    return UserMessage(text=prompt_text(prompt), images=images)


def build_cursor_session(
    preset,
    cwd_override: str | None,
    session_id: str | None,
    request_headers: dict[str, str] | None = None,
    *,
    effort_override: str | None = None,
    service_tier: str | None = None,
) -> CursorRuntimeSession:
    """preset の prompt/MCP resource を Cursor SDK の設定へ変換する。"""
    from ..config import get_config

    cwd = os.path.expanduser(cwd_override) if cwd_override is not None else preset.cwd
    cfg = get_config()
    translated = translate_runtime_config(
        cfg, preset, request_headers, effort_override=effort_override,
    )
    cursor_config = apply_common_timeout(dict(preset.cursor or {}), translated.timeout_seconds)

    explicit_setting_sources = "setting_sources" in cursor_config
    raw_setting_sources = (
        cursor_config.get("setting_sources")
        if explicit_setting_sources
        else preset.setting_sources
    )
    setting_sources = _cursor_setting_sources(
        raw_setting_sources,
        strict=explicit_setting_sources,
    )
    if preset.require_isolation and setting_sources:
        raise ValueError(
            "cursor provider は require_isolation=True のとき setting_sources を有効化できません"
        )
    if preset.repository_skills == "include" and not {"project", "all"}.intersection(setting_sources or ()):
        setting_sources = [*(setting_sources or ()), "project"]
    elif preset.repository_skills == "exclude" and setting_sources is not None:
        if "all" in setting_sources:
            setting_sources = ["user", "team", "mdm", "plugins"]
        else:
            setting_sources = [source for source in setting_sources if source != "project"]
    sandbox_options = _cursor_sandbox_options(cursor_config.get("sandbox_options"))
    model_params = _cursor_model_params(cursor_config.get("model_params"))
    # Common effort is a portable Kotoyose knob. Cursor exposes the same concept as
    # model parameter id "effort"; an explicit cursor.model_params.effort wins.
    if translated.effort is not None:
        if (
            "effort" not in model_params
            and translated.effort not in _CURSOR_FALLBACK_EFFORTS
        ):
            # catalog を引けない以上 drop は断定が過ぎるため転送はする。ただし
            # 以前は unsupported として黙殺されていた値なので、実行時に Cursor
            # 側が拒否したときに原因を辿れるよう警告だけ残す。
            log.warning(
                "cursor.effort_outside_fallback",
                effort=translated.effort,
                fallback=sorted(_CURSOR_FALLBACK_EFFORTS),
            )
        model_params.setdefault("effort", translated.effort)
    if service_tier == "priority":
        # Cursor の Fast は model parameter id "fast" の boolean。SDK は値を文字列で受ける。
        # リクエストの priority は preset の model_params.fast より優先する。
        model_params["fast"] = "true"

    if (
        translated.plugin_paths
        or translated.skills
        or translated.unsupported_fields
    ):
        log.warning(
            "cursor.runtime_resources_unsupported",
            plugins=list(translated.plugin_paths),
            skills=[skill.name for skill in translated.skills],
            fields=list(translated.unsupported_fields),
        )
    for key in _IGNORED_CLI_KEYS & set(cursor_config.keys()):
        log.debug("cursor.cli_config.ignored", key=key)
    route_name = getattr(preset, "route", None)
    route = get_config().routes.get(route_name) if route_name else None
    connection_id = route.connection if route is not None else None
    return CursorRuntimeSession(
        model=preset.model,
        cwd=str(Path(cwd).expanduser()),
        config=cursor_config,
        model_params=model_params,
        session_id=session_id or None,
        system_preamble=translated.instructions,
        mcp_servers={
            name: mapped
            for name, server in translated.mcp_servers.items()
            if (mapped := _to_sdk_mcp(name, server)) is not None
        },
        tools=preset.tools,
        disallowed_tools=list(preset.deny),
        setting_sources=setting_sources,
        sandbox_options=sandbox_options,
        mode="plan" if preset.permission_mode == "plan" else None,
        connection=connection_id,
    )


def _cursor_model_params(value: object) -> dict[str, str]:
    """Cursor ModelSelection.params 用の key/value を検証・文字列化する。"""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("cursor.model_params は object で指定してください")
    resolved: dict[str, str] = {}
    for raw_key, raw_value in value.items():
        if not isinstance(raw_key, str) or not raw_key.strip():
            raise ValueError("cursor.model_params の key は空でない文字列で指定してください")
        key = raw_key.strip()
        if isinstance(raw_value, bool):
            text = "true" if raw_value else "false"
        elif isinstance(raw_value, (str, int, float)):
            text = str(raw_value)
        else:
            raise ValueError(
                f"cursor.model_params.{key} は string / number / boolean で指定してください"
            )
        resolved[key] = text
    return resolved


def _cursor_setting_sources(
    sources: object,
    *,
    strict: bool = False,
) -> list[str] | None:
    """Cursor local agent が読める ambient setting layer を正規化する。

    共通 ModelPreset.setting_sources 由来では Claude 専用の ``local`` を従来どおり
    warning + drop する。``cursor.setting_sources`` を明示した場合は Cursor native
    語彙として扱い、typo を silent ignore せず fail-fast する。
    """
    if sources is None:
        return None
    if not isinstance(sources, list) or not all(isinstance(source, str) for source in sources):
        raise ValueError("cursor setting_sources は文字列リストで指定してください")
    supported = [source for source in sources if source in _CURSOR_SETTING_SOURCES]
    unsupported = [source for source in sources if source not in _CURSOR_SETTING_SOURCES]
    if unsupported and strict:
        raise ValueError(
            "cursor.setting_sources に未対応の値があります: " + ", ".join(unsupported)
        )
    if unsupported:
        log.warning("cursor.setting_sources_unsupported", sources=unsupported)
    return supported


def _cursor_sandbox_options(value: object) -> dict[str, Any] | None:
    """Cursor SDK の local.sandbox_options を provider config から安全に受け取る。"""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("cursor.sandbox_options は object で指定してください")
    enabled = value.get("enabled")
    if enabled is not None and not isinstance(enabled, bool):
        raise ValueError("cursor.sandbox_options.enabled は boolean で指定してください")
    return dict(value)


def _to_sdk_mcp(name: str, server: dict[str, Any]) -> Any:
    stype = server.get("type") or ("stdio" if "command" in server else None)
    if stype == "stdio":
        return StdioMcpServerConfig(**{key: server[key] for key in ("command", "args", "env", "cwd") if key in server})
    if stype == "http":
        return HttpMcpServerConfig(**{key: server[key] for key in ("type", "url", "headers") if key in server})
    if stype == "sse":
        return SseMcpServerConfig(**{key: server[key] for key in ("type", "url", "headers") if key in server})
    log.warning("cursor.mcp_unsupported", name=name, type=stype)
    return None


def _resolve_timeout(config: dict[str, Any]) -> float:
    timeout = config.get("timeout")
    return float(timeout) if isinstance(timeout, (int, float)) and timeout > 0 else _DEFAULT_TIMEOUT_SECONDS


def _normalize_usage(usage: TokenUsage | Any) -> dict[str, int | None] | None:
    if usage is None:
        return None
    return {key: int(value) if (value := getattr(usage, key, None)) is not None else None
            for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "total_tokens", "reasoning_tokens")}


def _runtime_event_from_message(message: Any) -> RuntimeEvent | None:
    """Cursor SDKMessage を Kotoyose 共通の activity event へ防御的に変換する。"""
    if message is None:
        return None
    message_type = getattr(message, "type", "")
    if message_type == "tool_call":
        status = str(getattr(message, "status", "")).lower()
        tool_name = str(getattr(message, "name", "") or "tool")
        call_id = getattr(message, "call_id", None)
        truncated = bool(getattr(message, "truncated", False))
        if status == "running":
            args = getattr(message, "args", None)
            return RuntimeEvent(
                kind="tool_use",
                tool_name=tool_name,
                summary=_summarize(_json_text(args)),
                payload={
                    "tool_use_id": call_id,
                    "input": _truncate_value(args),
                    "truncated": truncated,
                },
            )
        if status in {"completed", "error", "failed"}:
            result = getattr(message, "result", None)
            is_error = status in {"error", "failed"}
            return RuntimeEvent(
                kind="tool_result",
                tool_name=tool_name,
                summary=_summarize(f"{'error' if is_error else 'ok'}: {_json_text(result)}"),
                payload={
                    "tool_use_id": call_id,
                    "is_error": is_error,
                    "output": _truncate_value(result),
                    "truncated": truncated,
                },
            )
    if message_type == "thinking":
        text = str(getattr(message, "text", "") or "")
        return RuntimeEvent(
            kind="thinking",
            summary=_summarize(text),
            payload={"text": _truncate(text)},
        )
    if message_type == "task":
        status = str(getattr(message, "status", "") or "")
        text = str(getattr(message, "text", "") or "")
        return RuntimeEvent(
            kind="task",
            summary=_summarize(f"{status}: {text}" if status else text),
            payload={"status": status, "text": _truncate(text)},
        )
    return None


def _consume_task_exception(task: asyncio.Future) -> None:
    if not task.cancelled():
        task.exception()


def _run_id(run: Any) -> str | None:
    """Use SDK .id, avoiding the deprecated .run_id property."""
    return getattr(run, "id", None) or None


def _status(value: Any) -> str:
    # SDK canonical status names, plus older event wire aliases.
    return {"completed": "finished", "failed": "error", "canceled": "cancelled"}.get(
        str(value or "").lower(), str(value or "").lower(),
    )


def _event_status(event: Any, message: Any) -> str:
    result = getattr(event, "result", None)
    if isinstance(result, Mapping):
        return _status(result.get("status"))
    if getattr(message, "type", "") == "status":
        return _status(getattr(message, "status", None))
    return ""


def _event_error(event: Any, message: Any) -> str | None:
    status = _event_status(event, message)
    if status not in {"error", "cancelled", "expired"}:
        return None
    # Provider text/envelopes can contain echoed credentials or prompt material.
    return f"Cursor run ended with status {status}"


def _safe_diagnostics(error: Exception) -> dict[str, Any]:
    """Whitelist documented scalar error metadata; never copy headers/details."""
    fields = ("code", "proto_error_code", "request_id", "retry_after")
    values = {key: value for key in fields if isinstance(value := getattr(error, key, None), str)}
    api_status = getattr(error, "status", None)
    if isinstance(api_status, int) and not isinstance(api_status, bool):
        values["api_error_status"] = api_status
    retryable = getattr(error, "is_retryable", None)
    if isinstance(retryable, bool):
        values["is_retryable"] = retryable
    return values


def _is_recoverable_auth_error(error: str) -> bool:
    """Cursor local bridge の既知の stale-auth 症状だけを recovery 対象にする。"""
    normalized = " ".join(error.lower().split())
    return any(marker in normalized for marker in _CURSOR_AUTH_RECOVERY_MARKERS)


def _truncate(value: str) -> str:
    return value[:_SUMMARY_LIMIT]


def _summarize(value: str) -> str:
    return _truncate(" ".join(value.split()))


def _json_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def _truncate_value(value: object) -> object:
    if isinstance(value, str):
        return _truncate(value)
    return _truncate(_json_text(value))
