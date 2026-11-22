"""Native Claude Agent SDK worker と provider / tool 境界。"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import re
import math
import shutil
import sys
import tempfile
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Literal, TypedDict, TYPE_CHECKING

if TYPE_CHECKING:
    from .execution_resources import CodexEgressLimits

import structlog

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    create_sdk_mcp_server,
    tool,
)

from .execution_jobs import WorkerResult
from .codex_coding_policy import validate_mode
from .native_sessions import ScopedSessionStore, private_directory
from .execution_images import (
    MAX_IMAGE_BYTES, MAX_IMAGE_COUNT, MAX_IMAGE_TOTAL_BYTES, NativeImageInput, image_signature_valid,
)
from .client_tool_bridge import ClientToolCallOutcome
from .execution_delegation import TOOL_SPECS
from .native_compression import NativeCompressionObserver
from .execution_contract import ResumeMode
from .execution_limits import ProviderUsageLimit
from .agent_deadlines import AgentStartRejected
from .provider_usage_limits import normalize_provider_usage_limit
from .runtime.resume import ResumeSupport
from .runtime.base import RuntimeEvent
from .runtime.claude import _summarize, _truncate
from .runtime.claude_options import claude_disallowed_tools
from .runtime.claude_observations import OBSERVATION_MESSAGES, claude_observation


log = structlog.get_logger()
_TOOL_REF = "builtin:sandbox-exec@1"
_ALLOWED_TOOL = "mcp__sandbox__exec"
_SDK_PROTOCOL_OVERHEAD = 64 * 1024
_CREDENTIAL_KEYS = (
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
)
_STATIC_ERRORS = frozenset(
    {
        "cancelled",
        "timeout",
        "sdk_error",
        "provider_error",
        "worker_lost",
        "invalid_job",
        "unsupported_tool_ref",
        "tool_denied",
        "tool_error",
        "tool_output_limit_exceeded",
        "output_limit_exceeded",
        "invalid_json_output",
    }
)


class NativeUsage(TypedDict):
    """Native worker が公開する provider usage 項目。"""

    source: Literal["reported", "unknown"]
    cost: float | None
    input_tokens: int | None
    output_tokens: int | None


@dataclass(frozen=True, slots=True)
class NativeAgentJob:
    """NativeClaudeWorker が消費する不変の admission 結果。"""

    attempt_id: str
    model: str
    cli_binary: Path
    credential_name: str
    credential_value: str
    instructions: str
    prompt: str
    output_contract: Literal["text", "json"]
    max_turns: int
    timeout_seconds: float
    grace_seconds: float
    output_limit_bytes: int
    tool_refs: tuple[str, ...]
    base_url: str | None = None
    image_inputs: tuple["NativeImageInput", ...] = ()
    client_tool_specs: tuple[dict[str, Any], ...] = ()
    approval_bindings: tuple[dict[str, Any], ...] = ()
    client_tool_call: Callable[[str, Mapping[str, Any]], Awaitable[ClientToolCallOutcome]] | None = None
    compression_event: Callable[[str, dict], Awaitable[None]] | None = None
    activity_event: Callable[[RuntimeEvent], None] | None = None
    progress_event: Callable[[RuntimeEvent], None] | None = None
    resume_mode: ResumeMode | None = None
    provider_session_ref: str | None = None
    codex_rootfs: Path | None = None
    codex_rootfs_digest: str | None = None
    codex_auth_file: Path | None = None
    codex_auth_identity: str | None = None
    provider_authorize: Callable[[], Awaitable[None]] | None = None
    # controller 専用。保持中の停止観測を operation 台帳へ保存し、失敗時は確定させない。
    record_codex_stop: Callable[[dict], Awaitable[None]] | None = None
    reasoning_effort: str | None = None
    deadline_monotonic: float | None = None
    codex_execution_mode: str = "sandbox_exec_v1"
    codex_tool_policy_digest: str | None = None
    codex_sandbox: str | None = None
    execution_environment: str | None = None
    codex_sandbox_policy: str | None = None
    codex_protected_workspace: list[dict] | None = None
    workspace: Path | None = None
    workspace_device: int | None = None
    workspace_inode: int | None = None
    generation: int = 0
    memory_bytes: int | None = None
    pids_limit: int | None = None
    cpus: float | None = None
    soft_deadline_monotonic: float | None = None
    codex_egress_limits: CodexEgressLimits | None = None
    session_scope: str | None = None
    limits_mode: Literal["agent_work_v1"] | None = None
    validate_start: Callable[[], Awaitable[None]] | None = None
    work_remaining: Callable[[], float] | None = None
    on_runtime_stopped: Callable[[], None] | None = None
    # Codex の通常 tool は、停止確認を journal 保存より前に通知する。
    tool_stop_idle: asyncio.Event | None = None


@dataclass(frozen=True, slots=True)
class NativeAgentResult:
    """起動と停止の証拠を別々に保持し、例外詳細や secret を返さない。

    launch_outcome の not_started は起動を試みていない証拠、started は
    process を取得した証拠、unknown は起動結果を証明できない状態を表す。
    """

    status: Literal["succeeded", "failed", "cancelled", "timeout", "worker_lost", "approval_required"]
    reason: str
    content: bytes
    stop_confirmation: Literal["confirmed", "unconfirmed"]
    usage: NativeUsage
    required_action: dict[str, str] | None = None
    provider_session_id: str | None = None
    codex_egress_metrics: dict[str, int] | None = None
    provider_limit: ProviderUsageLimit | None = None
    output_bytes: int | None = None
    turns: int | None = None
    launch_outcome: Literal["not_started", "started", "unknown"] = "unknown"


@dataclass(slots=True)
class _Invocation:
    deadline: float
    cancel: asyncio.Event
    output_limit: int
    lock: asyncio.Lock
    idle: asyncio.Event
    work_remaining: Callable[[], float] | None = None
    callbacks: int = 0
    terminal: bool = False
    tool_overflow: bool = False
    tool_output_bytes: int = 0
    interruption: Literal["cancelled", "timeout", "approval_required"] | None = None
    interruption_reason: str | None = None
    required_action: dict[str, str] | None = None
    owner: asyncio.Task[Any] | None = None


class NativeClaudeWorker:
    """リポジトリの provider launcher 経由で Native SDK session を実行する。"""

    def __init__(self, launcher_path: Path | None = None, *, session_store=None,
                 container_runtime: tuple | None = None) -> None:
        repository_launcher = Path(__file__).resolve().parents[2] / "scripts" / "native-claude"
        installed_launcher = Path(sys.executable).parent / "kotoyose-native-claude"
        self.launcher_path = launcher_path or (
            repository_launcher if repository_launcher.is_file() else installed_launcher
        )
        self.session_store = session_store
        # (AgentContainer, AgentContainerConfig | None, image_id)。coding の唯一の実行経路。
        self.container_runtime = container_runtime

    def resume_support(self) -> ResumeSupport:
        """Task所有の公式Storeを明示接続した構成だけnativeを広告する。"""
        return ResumeSupport(("native", "durable_state") if self.session_store is not None else ("durable_state",),
                             "provider_session_state_not_retained")

    def resume_session_ref(self, session_id: str | None) -> str | None:
        """Storeへの保持をexecuteで確認したSDK IDだけを永続refにする。"""
        return session_id if self.session_store is not None else None

    async def can_resume_session(self, scope: str, session_id: str) -> bool:
        """公式Storeに指定sessionが残るときだけnativeを選択する。"""
        return await ScopedSessionStore.available(self.session_store, scope, session_id)

    async def execute(
        self,
        job: NativeAgentJob,
        cancel: asyncio.Event,
        execute_tool: Callable[[tuple[str, ...], str], Awaitable[WorkerResult]],
        *,
        on_process_started: Callable[[int], Awaitable[None]],
    ) -> NativeAgentResult:
        """SDK 接続、process 停止、結果分類を同じ owner task で管理する。"""
        if _is_host_coding(job):
            # coding は per-Run container の中で CLI を動かす。非 coding の経路には入らない。
            from .container_claude import execute_container_claude
            runtime = self.container_runtime
            return await execute_container_claude(
                self, job, cancel, on_process_started=on_process_started,
                container=runtime[0] if runtime is not None else None,
                settings=runtime[1] if runtime is not None else None,
                image_id=runtime[2] if runtime is not None else None,
            )
        try:
            _validate_job(job)
            if (job.resume_mode == "native" or job.provider_session_ref is not None) and self.session_store is None:
                # 継続状態を持たない構成で fresh conversation を native と偽らない。
                raise ValueError("Native session state is not retained")
        except (TypeError, ValueError):
            return _result("failed", "invalid_job", b"", "unconfirmed")
        allowed_refs = {_TOOL_REF, "builtin:request-approval@1"}
        allowed_refs.update(spec["resource_ref"] for spec in job.client_tool_specs)
        if any(ref not in allowed_refs for ref in job.tool_refs):
            return _result("failed", "unsupported_tool_ref", b"", "unconfirmed")

        invocation = _Invocation(
            deadline=asyncio.get_running_loop().time() + job.timeout_seconds,
            cancel=cancel,
            output_limit=job.output_limit_bytes,
            lock=asyncio.Lock(),
            idle=asyncio.Event(),
            work_remaining=job.work_remaining,
        )
        invocation.owner = asyncio.current_task()
        invocation.idle.set()
        client: ClaudeSDKClient | None = None
        process: Any = None
        launch_attempted = False
        late_start_rejection = False
        content = bytearray()
        usage = _unknown_usage()
        session_id = None
        store_view = None
        observation_session_id = None
        turn = 1
        provider_limit = None
        output_bytes = 0
        tool_names: dict[str, str] = {}
        compression = NativeCompressionObserver(job.compression_event)
        reason = "sdk_error"
        status: Literal["succeeded", "failed", "cancelled", "timeout", "worker_lost", "approval_required"] = "failed"
        empty_dir: str | None = None
        owner = asyncio.current_task()
        watchdog = asyncio.create_task(self._watchdog(invocation, owner))
        try:
            # SDK process は transport 停止中も cwd を使うため、disconnect 完了まで残す。
            with contextlib.nullcontext(
                tempfile.mkdtemp(prefix="kotoyose-native-claude-")
            ) as empty_dir:
                server = (
                    self._build_server(job, invocation, execute_tool)
                    if job.tool_refs
                    else None
                )
                options = _build_options(job, self.launcher_path, Path(empty_dir), server)
                if self.session_store is not None and job.session_scope is not None:
                    config_dir = private_directory(Path(empty_dir)/'session-config')
                    store_view = ScopedSessionStore(self.session_store, job.session_scope)
                    options = replace(options, cwd='/tmp',
                        resume=job.provider_session_ref if job.resume_mode == 'native' else None,
                        session_store=store_view,
                        env={**options.env, 'CLAUDE_CONFIG_DIR': str(config_dir),
                             'KOTOYOSE_NATIVE_CLAUDE_SESSION_ENABLED': '1'})
                if job.validate_start is not None:
                    await job.validate_start()
                client = ClaudeSDKClient(options=options)
                launch_attempted = True
                await client.connect()
                process = _capture_process(client)
                pid = getattr(process, "pid", None)
                if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
                    invocation.terminal = True
                    status, reason = "failed", "worker_lost"
                    raise _ProcessIdentityError
                if invocation.cancel.is_set() or _expired(invocation):
                    if invocation.cancel.is_set():
                        invocation.interruption = "cancelled"
                        raise asyncio.CancelledError
                    invocation.interruption = "timeout"
                    raise asyncio.CancelledError
                try:
                    await on_process_started(pid)
                except asyncio.CancelledError:
                    raise
                except AgentStartRejected:
                    raise
                except Exception:
                    invocation.terminal = True
                    status, reason = "failed", "sdk_error"
                    raise _ProcessIdentityError
                if invocation.cancel.is_set() or _expired(invocation):
                    if invocation.cancel.is_set():
                        invocation.interruption = "cancelled"
                        raise asyncio.CancelledError
                    invocation.interruption = "timeout"
                    raise asyncio.CancelledError
                await client.query(_query_input(job) if job.image_inputs else job.prompt)
                async for message in client.receive_response():
                    if isinstance(message, SystemMessage) and message.subtype == "init":
                        initialized_id = message.data.get("session_id")
                        if isinstance(initialized_id, str) and initialized_id:
                            observation_session_id = initialized_id
                    if isinstance(message, OBSERVATION_MESSAGES):
                        observation = claude_observation(message, session_id=observation_session_id)
                        if observation is not None:
                            if observation.kind == "session_store_warning":
                                log.warning("claude.session_store_mirror_error")
                            _emit_progress(job, observation)
                            if job.limits_mode and observation.kind == "rate_limit":
                                limit = normalize_provider_usage_limit("claude", observation.payload, datetime.now(timezone.utc))
                                if limit is not None:
                                    provider_limit = limit
                                    status, reason = "failed", "provider_usage_limit"
                            if job.activity_event is not None:
                                try:
                                    job.activity_event(observation)
                                except Exception as exc:
                                    log.warning("native.activity_callback_failed", error_type=type(exc).__name__)
                            if provider_limit is not None:
                                break
                        continue
                    if isinstance(message, SystemMessage) and message.subtype == 'compact_boundary':
                        await compression.boundary(message.data)
                    elif isinstance(message, AssistantMessage):
                        if job.limits_mode and getattr(message, "error", None) == "rate_limit":
                            provider_limit = normalize_provider_usage_limit("claude", {"subtype": "error_rate_limit"}, datetime.now(timezone.utc))
                            status, reason = "failed", "provider_usage_limit"
                            continue
                        if getattr(message, "error", None) == "authentication_failed":
                            _emit_progress(job, RuntimeEvent(
                                kind="provider_error",
                                summary="Claude assistant reported an authentication error",
                                payload={"assistant_error": "authentication_failed"},
                            ))
                        if message.session_id:
                            observation_session_id = message.session_id
                        _emit_progress(job, RuntimeEvent(
                            kind="turn", summary=f"Assistant turn {turn}", payload={"turn": turn},
                        ))
                        if not getattr(message, "error", None):
                            turn += 1
                        for block in message.content:
                            if isinstance(block, TextBlock):
                                redacted = _redact_text(block.text, job)
                                _emit_progress(job, RuntimeEvent(
                                    kind="text", summary=_summarize(redacted),
                                    payload={"text": _truncate(redacted)},
                                ))
                                if job.activity_event is not None:
                                    job.activity_event(RuntimeEvent(kind="text", summary=_truncate(redacted)))
                                encoded = redacted.encode("utf-8")
                                output_bytes += len(encoded)
                                content.extend(encoded)
                                if len(content) > job.output_limit_bytes:
                                    content = content[: job.output_limit_bytes]
                                    reason = "output_limit_exceeded"
                                    status = "failed"
                                    raise _OutputLimit
                            elif isinstance(block, ThinkingBlock):
                                redacted = _redact_text(block.thinking, job)
                                _emit_progress(job, RuntimeEvent(
                                    kind="thinking", summary=_summarize(redacted),
                                    payload={"text": _truncate(redacted)},
                                ))
                            elif isinstance(block, ToolUseBlock):
                                tool_names[block.id] = block.name
                                detail = _tool_use_detail(block)
                                redacted = _redact_text(detail, job)
                                _emit_progress(job, RuntimeEvent(
                                    kind="tool_use", tool_name=block.name,
                                    summary=_summarize(redacted),
                                    payload={"tool_use_id": _truncate(block.id), "input": _truncate(redacted)},
                                ))
                                if job.activity_event is not None:
                                    job.activity_event(RuntimeEvent(kind="tool_use", tool_name=block.name,
                                                                    summary=block.name))
                            elif isinstance(block, ToolResultBlock):
                                _emit_sdk_tool_result(job, block, tool_names)
                    elif isinstance(message, UserMessage) and isinstance(message.content, list):
                        for block in message.content:
                            if isinstance(block, ToolResultBlock):
                                _emit_sdk_tool_result(job, block, tool_names)
                    elif isinstance(message, ResultMessage):
                        observed_id = getattr(message, "session_id", None)
                        if isinstance(observed_id, str) and observed_id:
                            session_id = observed_id
                        usage = _extract_usage(message)
                        if provider_limit is not None:
                            reason, status = "provider_usage_limit", "failed"
                        elif message.is_error:
                            subtype = getattr(message, "subtype", None)
                            provider_limit = (normalize_provider_usage_limit("claude", {"subtype": subtype, "http_status": getattr(message, "api_error_status", None)}, datetime.now(timezone.utc))
                                              if job.limits_mode else None)
                            reason = ("provider_usage_limit" if provider_limit else
                                      "turn_limit_exceeded" if job.limits_mode and subtype == "error_max_turns" else "provider_error")
                            status = "failed"
                            # 失敗の種別 (max turns / execution error) は provider の result にしか無い。
                            log.warning(
                                "native.provider_error",
                                subtype=getattr(message, "subtype", None),
                                num_turns=getattr(message, "num_turns", None),
                            )
                        else:
                            reason = "completed"
                            status = "succeeded"
                compression_deadline = (asyncio.get_running_loop().time() + invocation.work_remaining()
                                        if invocation.work_remaining is not None else invocation.deadline)
                await compression.sample(client, compression_deadline)
                if invocation.tool_overflow:
                    reason = "tool_output_limit_exceeded"
                    status = "failed"
                elif status == "succeeded" and job.output_contract == "json":
                    extracted = _extract_json_object(bytes(content))
                    if extracted is None:
                        reason = "invalid_json_output"
                        status = "failed"
                    else:
                        content = bytearray(extracted)
        except AgentStartRejected:
            if not launch_attempted:
                raise
            # 型付き拒否は未起動の証拠なので、起動試行後には伝播させない。
            late_start_rejection = True
            invocation.terminal = True
            invocation.required_action = None
            status, reason = "failed", "sdk_error"
        except _OutputLimit:
            invocation.terminal = True
        except _ProcessIdentityError:
            invocation.terminal = True
        except asyncio.CancelledError:
            # 中断由 watchdog または caller が要求するが、停止確認は process 状態だけで行う。
            invocation.terminal = True
            status = invocation.interruption or "cancelled"
            reason = invocation.interruption_reason or status
        except Exception:
            invocation.terminal = True
            status, reason = "failed", "sdk_error"
        finally:
            invocation.terminal = True
            watchdog.cancel()
            with contextlib.suppress(BaseException):
                await watchdog
            if client is not None:
                confirmed = await self._shutdown(client, process, invocation, job.grace_seconds)
            else:
                confirmed = False
            if confirmed and job.on_runtime_stopped is not None:
                try:
                    job.on_runtime_stopped()
                except AgentStartRejected:
                    # 停止通知も起動試行後なので、未起動の証拠として返さない。
                    late_start_rejection = True
                    invocation.required_action = None
                    status, reason = "failed", "sdk_error"
            if empty_dir is not None:
                shutil.rmtree(empty_dir, ignore_errors=True)
        if invocation.required_action is not None and not cancel.is_set() and not late_start_rejection:
            status, reason = "approval_required", "approval_required"
        if cancel.is_set() and status == "succeeded":
            status, reason = "cancelled", "cancelled"
        if status == "succeeded" and not confirmed:
            status, reason = "worker_lost", "worker_lost"
        observed_session_id = session_id or observation_session_id
        retained = (await store_view.retained_ref(observed_session_id) if store_view is not None else None)
        if self.session_store is not None:
            session_id = retained
        if provider_limit is not None:
            native = bool(retained and "native" in self.resume_support().modes)
            provider_limit = provider_limit.model_copy(update={"resume": "native" if native else "unavailable",
                "unavailable_reason": None if native else ("not_retained" if observed_session_id else "no_session")})
        return NativeAgentResult(
            status,
            reason,
            bytes(content),
            "confirmed" if confirmed else "unconfirmed",
            usage,
            invocation.required_action,
            session_id,
            provider_limit=provider_limit, output_bytes=output_bytes + invocation.tool_output_bytes, turns=turn - 1,
            launch_outcome=("unknown" if late_start_rejection else
                            "started" if process is not None else "unknown" if launch_attempted else "not_started"),
        )

    async def _watchdog(self, invocation: _Invocation, owner: asyncio.Task[Any] | None) -> None:
        """cancel Event と deadline を owner task の中断へ変換する。"""
        if owner is None:
            return
        try:
            while True:
                if invocation.cancel.is_set():
                    invocation.interruption = "cancelled"
                    owner.cancel()
                    return
                remaining = (invocation.work_remaining() if invocation.work_remaining is not None
                             else invocation.deadline - asyncio.get_running_loop().time())
                if remaining <= 0:
                    invocation.interruption = "timeout"
                    owner.cancel()
                    return
                try:
                    await asyncio.wait_for(invocation.cancel.wait(), timeout=min(remaining, 0.25) if invocation.work_remaining else remaining)
                except TimeoutError:
                    if invocation.work_remaining is None:
                        invocation.interruption = "timeout"
                        owner.cancel()
                        return
                    continue
                if invocation.cancel.is_set():
                    invocation.interruption = "cancelled"
                    owner.cancel()
                    return
        except asyncio.CancelledError:
            return

    def _build_server(
        self,
        job: NativeAgentJob,
        invocation: _Invocation,
        execute_tool: Callable[[tuple[str, ...], str], Awaitable[WorkerResult]],
    ) -> Any:
        """唯一の in-process MCP grant を作り、dispatch 前に入力を検証する。"""

        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["argv", "cwd"],
            "properties": {
                "argv": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "cwd": {"type": "string"},
            },
        }

        @tool(
            "exec",
            "Execute one command in the admitted sandbox. argv is the program and its arguments "
            "(no shell, no network). cwd is relative to the workspace root; use \".\" for the root. "
            "Absolute paths and \"..\" are denied.",
            schema,
        )
        async def sandbox_exec(arguments: Any) -> dict[str, Any]:
            observed: WorkerResult | None = None

            async def observe(argv: tuple[str, ...], cwd: str) -> WorkerResult:
                nonlocal observed
                observed = await execute_tool(argv, cwd)
                return observed

            result = await self._dispatch_tool(arguments, job, invocation, observe)
            if observed is not None:
                output = bytes(getattr(observed, "stdout", b"")) + bytes(getattr(observed, "stderr", b""))
                detail = _redact_text(output.decode("utf-8", errors="replace"), job)
                exit_code = getattr(observed, "exit_code", None)
                status = str(getattr(observed, "status", "unknown"))
            else:
                detail = _redact_text(str(result.get("content", [{}])[0].get("text", "")), job)
                exit_code = None
                status = "failed" if result.get("isError") else "succeeded"
            prefix = f"exit code {exit_code}" if exit_code is not None else status
            _emit_progress(job, RuntimeEvent(
                kind="tool_result", tool_name=_ALLOWED_TOOL,
                summary=_summarize(f"{prefix}: {detail}"),
                payload={"exit_code": exit_code, "status": status, "output": _truncate(detail)},
            ))
            if job.activity_event is not None:
                job.activity_event(RuntimeEvent(
                    kind="tool_result", tool_name="exec",
                    summary="failed" if result.get("isError") else "completed",
                ))
            return result

        servers: dict[str, Any] = {}
        if _TOOL_REF in job.tool_refs:
            servers["sandbox"] = create_sdk_mcp_server("sandbox", version="1.0.0", tools=[sandbox_exec])
        client_tools = []
        for spec in job.client_tool_specs:
            tool_ref = spec["resource_ref"]
            name = spec["name"]

            @tool(name, spec["description"], spec["input_schema"])
            async def client_tool(arguments: Any, *, _ref=tool_ref) -> dict[str, Any]:
                return await self._dispatch_client_tool(_ref, arguments, job, invocation)

            client_tools.append(client_tool)
        if "builtin:request-approval@1" in job.tool_refs:
            approval_schema = {
                "type": "object", "additionalProperties": False,
                "required": ["approval_id", "subject_digest"],
                "properties": {
                    "approval_id": {"type": "string", "pattern": r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"},
                    "subject_digest": {"type": "string", "pattern": r"^(?:sha256:)?[0-9a-f]{64}$"},
                },
            }

            @tool("request_approval", "Request a pre-bound human approval.", approval_schema)
            async def request_approval(arguments: Any) -> dict[str, Any]:
                return await self._dispatch_client_tool(
                    "builtin:request-approval@1", arguments, job, invocation,
                )

            client_tools.append(request_approval)
        if client_tools:
            servers["client"] = create_sdk_mcp_server("client", version="1.0.0", tools=client_tools)
        return servers

    async def _dispatch_client_tool(
        self, tool_ref: str, arguments: Any, job: NativeAgentJob, invocation: _Invocation,
    ) -> dict[str, Any]:
        if invocation.terminal or invocation.cancel.is_set() or _expired(invocation):
            return _tool_error("tool_denied")
        if tool_ref not in job.tool_refs:
            return _tool_error("tool_denied")
        if not isinstance(arguments, Mapping) or job.client_tool_call is None:
            return _tool_error("tool_denied")
        if tool_ref == "builtin:request-approval@1":
            if set(arguments) != {"approval_id", "subject_digest"}:
                return _tool_error("tool_denied")
            bindings = {item["approval_id"]: item for item in job.approval_bindings}
            binding = bindings.get(arguments["approval_id"])
            if binding is None or binding["subject_digest"] != arguments["subject_digest"]:
                return _tool_error("tool_denied")
        async with invocation.lock:
            if invocation.terminal or invocation.cancel.is_set() or _expired(invocation):
                return _tool_error("tool_denied")
            invocation.callbacks += 1
            invocation.idle.clear()
            try:
                outcome = await job.client_tool_call(tool_ref, arguments)
                if outcome.required_action is not None:
                    invocation.interruption = "approval_required"
                    invocation.interruption_reason = "approval_required"
                    invocation.required_action = outcome.required_action
                    invocation.terminal = True
                    if invocation.owner is not None:
                        invocation.owner.cancel()
                    return _tool_error("tool_denied")
                if outcome.state != "succeeded":
                    error = {"error": outcome.state, "call_id": outcome.call_id}
                    if tool_ref in TOOL_SPECS and isinstance(outcome.result, dict):
                        error.update(outcome.result)
                    return {"content": [{"type": "text", "text": json.dumps(error)}], "isError": True}
                payload = outcome.result if outcome.result is not None else {}
                return {"content": [{"type": "text", "text": json.dumps(payload, sort_keys=True)}]}
            finally:
                invocation.callbacks -= 1
                if invocation.callbacks == 0:
                    invocation.idle.set()

    async def _dispatch_tool(
        self,
        arguments: Any,
        job: NativeAgentJob,
        invocation: _Invocation,
        execute_tool: Callable[[tuple[str, ...], str], Awaitable[WorkerResult]],
    ) -> dict[str, Any]:
        if invocation.terminal or invocation.cancel.is_set() or _expired(invocation):
            return _tool_error("tool_denied")
        if not isinstance(arguments, Mapping):
            return _tool_error("tool_denied")
        if set(arguments) != {"argv", "cwd"}:
            return _tool_error("tool_denied")
        argv = arguments.get("argv")
        cwd = arguments.get("cwd")
        if not _valid_argv(argv) or not _valid_cwd(cwd):
            return _tool_error("tool_denied")
        async with invocation.lock:
            if invocation.terminal or invocation.cancel.is_set() or _expired(invocation):
                return _tool_error("tool_denied")
            invocation.callbacks += 1
            invocation.idle.clear()
            try:
                try:
                    result = await execute_tool(tuple(argv), cwd)
                except Exception:
                    return _tool_error("tool_error")
                output = bytes(getattr(result, "stdout", b"")) + bytes(
                    getattr(result, "stderr", b"")
                )
                invocation.tool_output_bytes = getattr(invocation, "tool_output_bytes", 0) + len(output)
                if len(output) > invocation.output_limit:
                    invocation.tool_overflow = True
                    return _tool_error("tool_output_limit_exceeded")
                if getattr(result, "status", None) != "succeeded":
                    return _tool_failure(result, invocation.output_limit)
                text = output.decode("utf-8", errors="replace")
                return {"content": [{"type": "text", "text": _redact_text(text, job)}]}
            finally:
                invocation.callbacks -= 1
                if invocation.callbacks == 0:
                    invocation.idle.set()

    async def _shutdown(
        self, client: ClaudeSDKClient, process: Any, invocation: _Invocation, grace: float
    ) -> bool:
        """disconnect と process 回収を同じ owner task で期限付き実行する。"""
        limit = max(1.0, grace + 15.0)
        try:
            async with asyncio.timeout(limit):
                await client.disconnect()
        except BaseException:
            pass
        try:
            async with asyncio.timeout(max(0.1, grace + 15.0)):
                await invocation.idle.wait()
        except BaseException:
            return False
        if process is None:
            return False
        if getattr(process, "returncode", None) is None:
            for method in ("terminate", "kill"):
                try:
                    getattr(process, method)()
                except (AttributeError, OSError):
                    continue
                try:
                    async with asyncio.timeout(max(0.1, grace)):
                        await process.wait()
                except BaseException:
                    continue
                if getattr(process, "returncode", None) is not None:
                    break
        return getattr(process, "returncode", None) is not None


class _OutputLimit(Exception):
    pass


class _ProcessIdentityError(Exception):
    pass


def _emit_progress(job: NativeAgentJob, event: RuntimeEvent) -> None:
    """進捗の記録失敗を provider 実行へ伝播させない。"""
    if job.progress_event is None:
        return
    try:
        job.progress_event(event)
    except Exception as exc:
        log.warning("native.progress_callback_failed", error_type=type(exc).__name__)


def _tool_use_detail(block: ToolUseBlock) -> str:
    """sandbox command は argv を、他の tool は入力の概要を表示する。"""
    if block.name == _ALLOWED_TOOL and isinstance(block.input.get("argv"), list):
        return " ".join(str(arg) for arg in block.input["argv"])
    return json.dumps(block.input, ensure_ascii=False, default=str)


def _emit_sdk_tool_result(job: NativeAgentJob, block: ToolResultBlock,
                          tool_names: dict[str, str]) -> None:
    """SDK が返す client tool 結果を追記する。sandbox は callback 側で記録する。"""
    tool_name = tool_names.pop(block.tool_use_id, None)
    if tool_name == _ALLOWED_TOOL:
        return
    content = block.content
    if isinstance(content, list):
        detail = " ".join(str(item.get("text", "")) for item in content if isinstance(item, dict))
    else:
        detail = str(content or "")
    redacted = _redact_text(detail, job)
    status = "failed" if block.is_error else "succeeded"
    _emit_progress(job, RuntimeEvent(
        kind="tool_result", tool_name=tool_name,
        summary=_summarize(f"{status}: {redacted}"),
        payload={"tool_use_id": _truncate(block.tool_use_id),
                 "is_error": bool(block.is_error), "output": _truncate(redacted)},
    ))
    if job.activity_event is not None:
        try:
            job.activity_event(RuntimeEvent(
                kind="tool_result",
                tool_name=tool_name,
                summary="failed" if block.is_error else "completed",
            ))
        except Exception as exc:
            log.warning("native.activity_callback_failed", error_type=type(exc).__name__)


def _build_options(
    job: NativeAgentJob, launcher: Path, empty_dir: Path, server: Any | None
) -> ClaudeAgentOptions:
    servers = server if isinstance(server, dict) else ({"sandbox": server} if server is not None else {})
    allowed = [_ALLOWED_TOOL] if "sandbox" in servers else []
    allowed.extend("mcp__client__" + spec["name"] for spec in job.client_tool_specs)
    if "builtin:request-approval@1" in job.tool_refs:
        allowed.append("mcp__client__request_approval")
    environment = {key: "" for key in _CREDENTIAL_KEYS}
    environment[job.credential_name] = job.credential_value
    environment["ANTHROPIC_BASE_URL"] = job.base_url or ""
    environment["KOTOYOSE_NATIVE_CLAUDE_BINARY"] = str(job.cli_binary)
    return ClaudeAgentOptions(
        tools=[],
        allowed_tools=allowed,
        system_prompt=job.instructions,
        mcp_servers=servers,
        strict_mcp_config=True,
        permission_mode="dontAsk",
        max_turns=job.max_turns,
        model=job.model,
        fallback_model=None,
        cwd=empty_dir,
        cli_path=launcher,
        settings=None,
        env=environment,
        extra_args={},
        hooks=None,
        agents=None,
        disallowed_tools=claude_disallowed_tools(()),
        setting_sources=[],
        skills=[],
        plugins=[],
        max_buffer_size=job.output_limit_bytes + _SDK_PROTOCOL_OVERHEAD,
    )


def _validate_job(job: NativeAgentJob) -> None:
    if not isinstance(job, NativeAgentJob):
        raise TypeError
    if not job.attempt_id or "\x00" in job.attempt_id:
        raise ValueError
    if not job.model or "\x00" in job.model:
        raise ValueError
    if (
        not isinstance(job.cli_binary, Path)
        or not job.cli_binary.is_absolute()
        or "\x00" in str(job.cli_binary)
    ):
        raise ValueError
    if job.credential_name not in _CREDENTIAL_KEYS:
        raise ValueError
    if job.base_url is not None and (not job.base_url or "\x00" in job.base_url):
        raise ValueError
    if (
        not job.credential_value
        or "\x00" in job.credential_value
        or "\x00" in job.instructions
        or "\x00" in job.prompt
    ):
        raise ValueError
    if job.output_contract not in {"text", "json"}:
        raise ValueError
    if not isinstance(job.max_turns, int) or isinstance(job.max_turns, bool) or job.max_turns < 1:
        raise ValueError
    for value in (job.timeout_seconds, job.grace_seconds):
        if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError
    if not isinstance(job.output_limit_bytes, int) or job.output_limit_bytes < 1:
        raise ValueError
    if not isinstance(job.tool_refs, tuple) or any(
        not isinstance(ref, str) for ref in job.tool_refs
    ):
        raise ValueError
    if not isinstance(job.image_inputs, tuple):
        raise ValueError
    if not isinstance(job.client_tool_specs, tuple) or not isinstance(job.approval_bindings, tuple):
        raise ValueError
    names: set[str] = set()
    for spec in job.client_tool_specs:
        if not isinstance(spec, Mapping) or set(spec) != {
            "resource_ref", "name", "description", "input_schema", "effect", "max_payload_bytes", "timeout_seconds",
        }:
            raise ValueError
        if not isinstance(spec["resource_ref"], str) or not isinstance(spec["name"], str):
            raise ValueError
        if spec["name"] in names or spec["name"] == "request_approval":
            raise ValueError
        names.add(spec["name"])
        if spec["effect"] not in {"set", "play", "append", "read", "write"}:
            raise ValueError
    for binding in job.approval_bindings:
        if not isinstance(binding, Mapping) or set(binding) != {"approval_id", "subject_digest", "wait_seconds", "mode"}:
            raise ValueError
    for image in job.image_inputs:
        if not isinstance(image, NativeImageInput):
            raise ValueError
        if not image.artifact_ref or "\x00" in image.artifact_ref:
            raise ValueError
        if len(image.content) > MAX_IMAGE_BYTES:
            raise ValueError
        if len(image.digest) != 64 or any(char not in "0123456789abcdef" for char in image.digest):
            raise ValueError
        if hashlib.sha256(image.content).hexdigest() != image.digest:
            raise ValueError
        if not image_signature_valid(image.media_type, image.content):
            raise ValueError
    if len(job.image_inputs) > MAX_IMAGE_COUNT or sum(
        len(image.content) for image in job.image_inputs
    ) > MAX_IMAGE_TOTAL_BYTES:
        raise ValueError


def _query_input(job: NativeAgentJob):
    """text と base64 image を SDK の streaming user-message block にする。"""
    async def messages():
        content: list[dict[str, Any]] = [{"type": "text", "text": job.prompt}]
        for image in job.image_inputs:
            content.append({
                "type": "image",
                "source": {
                    "type": "base64", "media_type": image.media_type,
                    "data": base64.b64encode(image.content).decode("ascii"),
                },
            })
        yield {"type": "user", "message": {"role": "user", "content": content}}
    return messages()





def _valid_argv(value: Any) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(part, str) and bool(part) and "\x00" not in part for part in value[:1])
        and all(isinstance(part, str) and "\x00" not in part for part in value[1:])
    )


def _valid_cwd(value: Any) -> bool:
    if not isinstance(value, str) or not value or "\x00" in value or value.startswith("/"):
        return False
    if value == ".":
        return True
    parts = value.split("/")
    return all(part not in {"", ".", ".."} for part in parts)


def _expired(invocation: _Invocation) -> bool:
    remaining = getattr(invocation, "work_remaining", None)
    return (remaining() <= 0 if remaining is not None
            else asyncio.get_running_loop().time() >= invocation.deadline)


def _is_host_coding(job: NativeAgentJob) -> bool:
    """admission が coding と判定した job だけを container 経路へ回す。未知 mode は従来の検証で拒否する。"""
    try:
        return validate_mode(job.codex_execution_mode)
    except ValueError:
        return False


def _capture_process(client: Any) -> Any:
    transport = getattr(client, "_transport", None)
    return getattr(transport, "_process", None)


def _tool_error(code: str) -> dict[str, Any]:
    if code not in _STATIC_ERRORS:
        code = "tool_error"
    return {
        "content": [{"type": "text", "text": code}],
        "is_error": True,
        "isError": True,
    }


def _tool_failure(result: Any, limit: int) -> dict[str, Any]:
    """失敗時に再試行判断へ必要な結果を bounded JSON で返す。"""
    payload = {
        "status": str(getattr(result, "status", "failed")),
        "exit_code": getattr(result, "exit_code", None),
        "stdout": _bounded_text(getattr(result, "stdout", b""), limit // 2),
        "stderr": _bounded_text(getattr(result, "stderr", b""), limit // 2),
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > limit:
        payload["stdout"] = ""
        payload["stderr"] = ""
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > limit:
        encoded = encoded[:limit]
    return {
        "content": [{"type": "text", "text": encoded.decode("utf-8", errors="replace")}],
        "is_error": True,
        "isError": True,
    }


def _bounded_text(value: Any, limit: int) -> str:
    raw = bytes(value) if isinstance(value, (bytes, bytearray)) else str(value).encode()
    return raw[: max(0, limit)].decode("utf-8", errors="replace")


def _unknown_usage() -> NativeUsage:
    return {"source": "unknown", "cost": None, "input_tokens": None, "output_tokens": None}


def _extract_usage(message: ResultMessage) -> NativeUsage:
    raw = getattr(message, "usage", None)
    if not isinstance(raw, Mapping):
        return _unknown_usage()
    input_tokens = _integer(raw.get("input_tokens"))
    output_tokens = _integer(raw.get("output_tokens"))
    cost = _finite_nonnegative(getattr(message, "total_cost_usd", None))
    if input_tokens is None and output_tokens is None and cost is None:
        return _unknown_usage()
    return {
        "source": "reported",
        "cost": float(cost) if cost is not None else None,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }


def _integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _is_json_object(value: bytes) -> bool:
    try:
        parsed = json.loads(value.decode("utf-8"), parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return False
    return isinstance(parsed, dict)


_JSON_FENCE = re.compile(r"```(?:json)?\s*\n(.*?)\n\s*```", re.DOTALL)


def _extract_json_object(value: bytes) -> bytes | None:
    """json 契約の出力から唯一の JSON object を取り出し、正規化した bytes を返す。

    provider は tool 呼び出しの前後に自由文を挟むことがある。判定は構造化 JSON にだけ基づくため、
    全文が object でなければ、最後の fenced block か末尾の object だけを採用する。どれも object で
    なければ None (invalid_json_output) を返す。
    """
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError:
        return None
    candidates = [text.strip()]
    candidates.extend(match.group(1).strip() for match in _JSON_FENCE.finditer(text))
    decoder = json.JSONDecoder(parse_constant=_reject_json_constant)
    for candidate in reversed(candidates):
        try:
            parsed = json.loads(candidate, parse_constant=_reject_json_constant)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return json.dumps(parsed, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    stripped = text.rstrip()
    for start in (index for index, char in enumerate(stripped) if char == "{"):
        try:
            parsed, end = decoder.raw_decode(stripped, start)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict) and stripped[end:].strip() == "":
            return json.dumps(parsed, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return None


def _reject_json_constant(_value: str) -> Any:
    raise ValueError("non-finite JSON number")


def _finite_nonnegative(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    converted = float(value)
    return converted if math.isfinite(converted) and converted >= 0 else None


def _redact_text(text: str, job: NativeAgentJob) -> str:
    if not text:
        return text
    return text.replace(job.credential_value, "[redacted]") if job.credential_value else text


def _result(
    status: Literal["succeeded", "failed", "cancelled", "timeout", "worker_lost", "approval_required"],
    reason: str,
    content: bytes,
    confirmation: Literal["confirmed", "unconfirmed"],
    usage: NativeUsage | None = None,
    required_action: dict[str, str] | None = None,
) -> NativeAgentResult:
    return NativeAgentResult(status, reason, content, confirmation, usage or _unknown_usage(), required_action,
                             launch_outcome="not_started")
