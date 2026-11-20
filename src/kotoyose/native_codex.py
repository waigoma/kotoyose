"""固定した Codex app-server と journal 付き workspace tool を接続する。"""

from __future__ import annotations

from datetime import datetime, timezone
from .provider_usage_limits import normalize_provider_usage_limit

import asyncio
from collections import deque
from dataclasses import dataclass
import contextlib
import json
import math
import secrets
from pathlib import Path
import sys

from .native_claude import NativeAgentJob, NativeAgentResult, _extract_json_object, _valid_argv, _valid_cwd
from .process_identity import capture_process_identity
from .codex_process_stop import stop_codex_process
from .provider_jail import provider_environment
from .codex_egress import CodexEgress
from .codex_rootfs import validate_rootfs
from .codex_coding_policy import (
    resolve_codex_sandbox,
    validate_mode, validate_thread_sandbox, validate_coding_policy,
)
from .codex_soft_stop import CodexSoftStop
from .codex_model_policy import model_catalog, validate_reasoning_effort
from .codex_subscription import AUTH_NAME, CAPABILITY_ENV
from .codex_images import image_data_uris
from .codex_request_policy import parse_json
from .execution_resources import codex_execution_failure
from .agent_deadlines import AgentStartRejected
from .runtime.base import RuntimeEvent
from .runtime.codex import _AssistantResponse
from openai_codex.generated.v2_all import AgentMessageThreadItem
from .runtime.resume import ResumeSupport
from .native_codex_diagnostics import NativeCodexLimitError, NativeCodexProviderError


_TOOL_REF = "builtin:sandbox-exec@1"
_SANDBOX_EXEC_INSTRUCTIONS = (
    "Kotoyose workspace tool contract:\n"
    "The Codex provider CLI's read-only sandbox is separate from the admitted Kotoyose workspace "
    "used by sandbox_exec. For the user-authorized task, use sandbox_exec to edit workspace "
    "files and run tests as needed. The provider CLI's read-only metadata does not itself "
    "prohibit these tool operations. Do not conclude that all editing or testing is unavailable "
    "from that metadata alone; report actual tool failures. Pass cwd as a workspace-relative "
    "directory, using '.' "
    "for the workspace root. Respect the task scope, admission restrictions, and actual tool "
    "errors; this contract grants no additional access or permissions."
)
_SANDBOX_EXEC_DESCRIPTION = (
    "Execute commands in the admitted Kotoyose workspace, separately from the provider CLI's "
    "read-only sandbox. Supports workspace edits and tests for the user-authorized task, "
    "subject to admission restrictions and actual tool errors. cwd must be workspace-relative; "
    "use '.' for the workspace root."
)
_PROTOCOL_OVERHEAD = 64 * 1024
_MAX_PENDING = 512
_EXEC_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["argv", "cwd"],
    "properties": {"argv": {"type": "array", "minItems": 1, "items": {"type": "string"}},
                   "cwd": {"type": "string"}},
}
# tool set は固定 catalog・環境なし・egress schema で検査し、実操作を Kotoyose に限定する。
_CONFIG = {
    "features.shell_tool": False,
    "features.code_mode_host": False, "features.goals": False, "features.sleep_tool": False,
    "features.view_image": False, "features.unified_exec": False,
    "agents.enabled": False, "features.apps": False, "web_search": "disabled",
    "tools.update_plan.enabled": False, "tools.experimental_request_user_input.enabled": False,
    "model_provider": "kotoyose_native",
    "model_providers.kotoyose_native": {
        "name": "Kotoyose restricted OpenAI", "base_url": "http://127.0.0.1:18080/v1",
        "env_key": "OPENAI_API_KEY", "wire_api": "responses", "requires_openai_auth": False,
        "supports_websockets": False, "supports_standalone_web_search": False,
    },
}


async def _discard_stream(reader) -> None:
    # 終端処理では本文を保持せず pipe を空け、Process.wait の EOF 待ちを詰まらせない。
    while await reader.read(4096):
        pass


class _Protocol:
    def __init__(self, process, limit: int, image_uris: tuple[str, ...] = (), prompt: str = ""):
        self.process = process
        self.limit = limit
        self.bytes = 0
        self.sequence = 0
        self.pending = deque()
        self.pending_bytes = 0
        self.frame_bytes = 0
        self.image_uris = image_uris
        self.line_limit = limit + sum(len(uri.encode("ascii")) for uri in image_uris)
        self.prompt = prompt
        self.thread_id = None
        self.turn_id = None
        self.echo_item_id = None
        self.echo_methods = set()

    def _image_echo_bytes(self, value: dict) -> int:
        params = value.get("params", {})
        item = params.get("item", {}) if isinstance(params, dict) else {}
        if not self.image_uris or not isinstance(item, dict) or item.get("type") != "userMessage":
            return 0
        method = value.get("method")
        if (set(value) - {"method", "params", "emittedAtMs"}
                or value.get("emittedAtMs") is not None and (not isinstance(value["emittedAtMs"], int)
                    or isinstance(value["emittedAtMs"], bool) or value["emittedAtMs"] < 0)
                or method not in {"item/started", "item/completed"}
                or method in self.echo_methods or params.get("threadId") != self.thread_id
                or not isinstance(params.get("turnId"), str)
                or self.turn_id is not None and params["turnId"] != self.turn_id
                or set(params) - {"threadId", "turnId", "item", "startedAtMs", "completedAtMs"}
                or any(not isinstance(params[key], int) or isinstance(params[key], bool)
                       for key in ("startedAtMs", "completedAtMs") if params.get(key) is not None)
                or set(item) - {"type", "id", "content", "clientId"} or item.get("clientId") is not None
                or not isinstance(item.get("id"), str)
                or self.echo_item_id is not None and self.echo_item_id != item["id"]):
            raise ValueError("invalid_protocol")
        content = item.get("content")
        if not isinstance(content, list) or len(content) != len(self.image_uris) + 1:
            raise ValueError("invalid_protocol")
        text = content[0]
        if (not isinstance(text, dict) or text.get("type") != "text" or text.get("text") != self.prompt
                or set(text) - {"type", "text", "text_elements"} or text.get("text_elements", []) != []):
            raise ValueError("invalid_protocol")
        for part, uri in zip(content[1:], self.image_uris, strict=True):
            if (not isinstance(part, dict) or set(part) - {"type", "url", "detail"}
                    or part.get("type") != "image" or part.get("url") != uri or part.get("detail") is not None):
                raise ValueError("invalid_protocol")
        self.echo_item_id = item["id"]
        self.echo_methods.add(method)
        self.turn_id = params["turnId"]
        # 大きな raw/base64 は pending queue、journal、event に保持しない。
        item["content"] = [text, *({"type": "image"} for _ in self.image_uris)]
        return sum(len(uri.encode("ascii")) for uri in self.image_uris)

    async def send(self, message: dict) -> None:
        self.process.stdin.write((json.dumps(message, separators=(",", ":")) + "\n").encode())
        await self.process.stdin.drain()

    async def read(self) -> dict:
        try:
            raw = await self.process.stdout.readuntil(b"\n")
        except asyncio.LimitOverrunError as exc:
            # readline は量を持たない ValueError に変換するため、元の有限境界で分類する。
            raise NativeCodexLimitError("protocol_line_bytes", exc.consumed, self.line_limit) from None
        except asyncio.IncompleteReadError as exc:
            raise ValueError("invalid_protocol" if exc.partial else "missing_completion") from None
        value = parse_json(raw)
        if not isinstance(value, dict):
            raise ValueError("invalid_protocol")
        size = len(raw) - self._image_echo_bytes(value)
        if size > self.limit:
            raise NativeCodexLimitError("protocol_frame_bytes", size, self.limit)
        # 累積値は観測用だけ。処理済み frame は保持せず、保存予算による通信打切りに使わない。
        self.bytes += size
        self.frame_bytes = size
        return value

    async def request(self, method: str, params: dict) -> dict:
        self.sequence += 1
        request_id = f"kotoyose-{self.sequence}"
        await self.send({"id": request_id, "method": method, "params": params})
        while True:
            message = await self.read()
            if message.get("id") == request_id and "method" not in message:
                if "error" in message or not isinstance(message.get("result"), dict):
                    raise NativeCodexProviderError("rpc_response", message.get("error"))
                return message["result"]
            if len(self.pending) >= _MAX_PENDING:
                raise NativeCodexLimitError("rpc_pending_count", len(self.pending) + 1, _MAX_PENDING)
            held = self.pending_bytes + self.frame_bytes
            if held > self.limit:
                raise NativeCodexLimitError("rpc_pending_bytes", held, self.limit)
            self.pending.append((self.frame_bytes, message))
            self.pending_bytes = held

    async def next(self) -> dict:
        if not self.pending:
            return await self.read()
        self.frame_bytes, message = self.pending.popleft()
        self.pending_bytes -= self.frame_bytes
        return message

    async def drain_stderr(self) -> None:
        while data := await self.process.stderr.read(4096):
            # stderr は保存しない。排出を続け、pipe の逆流や累積保存量で停止させない。
            self.bytes += len(data)


@dataclass
class _Outcome:
    """実行の終端を一箇所に集める。container と jail の両経路で同じ分類を使う。"""

    status: str
    reason: str
    prestart_rejection: AgentStartRejected | None = None
    diagnostic: object = None
    provider_limit: object = None


def _is_host_coding(job: NativeAgentJob) -> bool:
    """coding だけを container 経路へ回す。未知 mode は従来の検証で拒否させる。"""
    try:
        return validate_mode(job.codex_execution_mode)
    except ValueError:
        return False


def _resolve_deadline(job: NativeAgentJob) -> float:
    """hard deadline と soft 通知期限を検証し、実行の期限を返す。"""
    loop = asyncio.get_running_loop()
    deadline = (job.deadline_monotonic if job.work_remaining is not None and job.deadline_monotonic is not None
                else loop.time() + job.timeout_seconds)
    if job.deadline_monotonic is not None:
        if (isinstance(job.deadline_monotonic, bool)
                or not isinstance(job.deadline_monotonic, (int, float))
                or not math.isfinite(job.deadline_monotonic)):
            raise ValueError("invalid_job")
        deadline = min(deadline, job.deadline_monotonic)
    if job.soft_deadline_monotonic is not None:
        CodexSoftStop(job.soft_deadline_monotonic)
        if job.soft_deadline_monotonic > deadline:
            raise ValueError("invalid_job")
    if deadline <= loop.time():
        raise TimeoutError
    return deadline


def _codex_config(job: NativeAgentJob, subscription: bool) -> dict:
    """app-server へ渡す設定。coding の shell 設定は経路ごとに呼び出し側が足す。"""
    config = dict(_CONFIG)
    if job.reasoning_effort is not None:
        config["model_reasoning_effort"] = job.reasoning_effort
    if subscription:
        config["model_providers.kotoyose_native"] = {
            **_CONFIG["model_providers.kotoyose_native"], "env_key": CAPABILITY_ENV,
            "name": "Kotoyose ChatGPT subscription broker", "request_max_retries": 0, "stream_max_retries": 0,
        }
    return config


def _bool_config_args(config: dict) -> list[str]:
    """bool の設定だけを CLI の --config として渡す。残りは thread/start の config で渡す。"""
    return [argument for key, value in config.items() if isinstance(value, bool)
            for argument in ("--config", f"{key}={json.dumps(value)}")]


async def _timeout_outcome(job: NativeAgentJob, process, launch_attempted: bool) -> _Outcome:
    outcome = _Outcome("timeout", "timeout")
    if job.limits_mode and process is None and not launch_attempted and job.validate_start is not None:
        try:
            await job.validate_start()
        except AgentStartRejected as exc:
            outcome = _Outcome("failed", exc.code, prestart_rejection=exc)
    return outcome


def _exception_outcome(job: NativeAgentJob, exc: Exception, process, launch_attempted: bool) -> _Outcome:
    if isinstance(exc, AgentStartRejected) and process is None and not launch_attempted:
        return _Outcome("failed", exc.code, prestart_rejection=exc)
    # provider の例外本文には secret や project text が入り得るため保存しない。
    reason = (None if isinstance(exc, AgentStartRejected) else codex_execution_failure(exc)) or (str(exc) if isinstance(exc, ValueError) and str(exc) in {
        "invalid_job", "invalid_protocol", "provider_error", "missing_completion",
        "output_limit_exceeded", "turn_limit_exceeded", "tool_denied", "invalid_json_output",
        "builtin_tool_failed", "soft_timeout", "provider_scope_unavailable",
        "sandbox_policy_mismatch",
    } else "provider_error")
    outcome = _Outcome("failed", reason)
    if reason == "provider_error":
        outcome.diagnostic = exc if isinstance(exc, NativeCodexProviderError) else NativeCodexProviderError("unknown")
        if job.limits_mode:
            outcome.provider_limit = normalize_provider_usage_limit("codex", {"code": outcome.diagnostic.classification.code,
                "http_status": outcome.diagnostic.classification.http_status_code}, datetime.now(timezone.utc))
            if outcome.provider_limit:
                outcome.reason = "provider_usage_limit"
    elif reason == "output_limit_exceeded":
        outcome.diagnostic = exc if isinstance(exc, NativeCodexLimitError) else NativeCodexLimitError("unknown")
    return outcome


class NativeCodexWorker:
    """空の jail で provider を実行し、固定された tool だけを Kotoyose へ戻す。

    coding (host_native_coding_v1) は container_runtime の per-Run container で CLI を動かす。
    非 coding (sandbox_exec_v1) は bubblewrap jail のまま。
    job は admission 済みの NativeAgentJob。credential は provider 環境だけに渡す。
    各 tool 呼出しは execute_tool に委ねる。再開は検証済み durable_state のみ。
    """

    def __init__(self, launcher_path: Path | None = None, *, session_root: Path | None = None,
                 container_runtime: tuple | None = None) -> None:
        repository = Path(__file__).resolve().parents[2] / "scripts" / "native-codex"
        installed = Path(sys.executable).parent / "kotoyose-native-codex"
        self.launcher_path = launcher_path or (repository if repository.is_file() else installed)
        self._namespace_policy = "fixture_process" if launcher_path is not None else "private_pid"
        # (AgentContainer, AgentContainerConfig | None, image_id). coding の唯一の実行経路。
        self.container_runtime = container_runtime
        from .native_sessions import CodexSessionDirectory
        self.sessions = CodexSessionDirectory(session_root) if session_root is not None else None

    def resume_support(self) -> ResumeSupport:
        """公式sessionsのprivate保存先を明示した構成だけnativeを広告する。"""
        return ResumeSupport(("native", "durable_state") if self.sessions is not None else ("durable_state",),
                             "provider_session_state_not_retained")

    def resume_session_ref(self, session_id: str | None) -> str | None:
        """executeで存在を確認した公式rollout locatorだけを再開refにする。"""
        return session_id if self.sessions is not None else None

    async def can_resume_session(self, scope: str, ref: str) -> bool:
        """保持した公式rolloutが所有領域に存在するときだけresumeを許可する。"""
        return self.sessions is not None and self.sessions.retained(scope, ref)

    async def execute(self, job: NativeAgentJob, cancel, execute_tool, *, on_process_started) -> NativeAgentResult:
        """provider の停止を観測して返す。停止未確認の成功は worker_lost へ落とす。"""
        if _is_host_coding(job):
            from .container_codex import execute_container_coding
            runtime = self.container_runtime
            return await execute_container_coding(
                self, job, cancel, execute_tool, on_process_started=on_process_started,
                container=runtime[0] if runtime is not None else None,
                settings=runtime[1] if runtime is not None else None,
                image_id=runtime[2] if runtime is not None else None,
            )
        usage = {"source": "unknown", "cost": None, "input_tokens": None, "output_tokens": None}
        content = b""
        messages = {}
        process = identity = stderr = operation = cancellation = broker_failure = None
        egress = None
        confirmation = "confirmed"
        egress_closed = False
        egress_confirmation = "confirmed"
        launch_attempted = False
        status, reason = "failed", "invalid_job"
        prestart_rejection = None
        diagnostic = None
        provider_limit = None
        rpc = None
        tool_idle = job.tool_stop_idle or asyncio.Event()
        tool_idle.set()

        async def tracked_tool(argv, cwd):
            tool_idle.clear()
            try:
                return await execute_tool(argv, cwd)
            finally:
                # Executor は実停止を journal 保存より前に通知する。
                # 旧 callback には通知口がないため、完了を保守的に待つ。
                if job.tool_stop_idle is None:
                    tool_idle.set()

        try:
            validate_mode(job.codex_execution_mode)
            if job.codex_egress_limits is not None:
                raise ValueError("invalid_job")
            resolve_codex_sandbox(job.codex_execution_mode, job.codex_sandbox)
            subscription = job.credential_name == AUTH_NAME
            if (job.workspace is not None or job.codex_tool_policy_digest is not None or job.soft_deadline_monotonic is not None
                    or job.execution_environment is not None or job.codex_sandbox_policy is not None
                    or job.codex_protected_workspace is not None):
                raise ValueError("invalid_job")
            if ((subscription and (job.credential_value or job.codex_auth_file is None or not job.codex_auth_identity))
                    or (not subscription and (job.credential_name != "OPENAI_API_KEY" or not job.credential_value
                                             or job.codex_auth_file is not None or job.codex_auth_identity is not None))
                    or job.base_url is not None or job.client_tool_specs
                    or job.approval_bindings or set(job.tool_refs) - {_TOOL_REF}
                    or (job.resume_mode == "native" or job.provider_session_ref is not None) and self.sessions is None
                    or not job.model or "\x00" in job.model or not job.cli_binary.is_absolute()
                    or job.output_contract not in {"text", "json"}
                    or not isinstance(job.max_turns, int) or isinstance(job.max_turns, bool) or job.max_turns < 1
                    or not isinstance(job.output_limit_bytes, int) or isinstance(job.output_limit_bytes, bool)
                    or job.output_limit_bytes < 1
                    or not all(isinstance(value, (int, float)) and not isinstance(value, bool)
                               and math.isfinite(value) and value >= 0
                               for value in (job.timeout_seconds, job.grace_seconds))
                    or job.timeout_seconds <= 0):
                raise ValueError("invalid_job")
            if job.reasoning_effort is not None:
                validate_reasoning_effort(job.model, job.reasoning_effort)
            deadline = _resolve_deadline(job)
            image_uris = image_data_uris(job.image_inputs)
            # 実 OAuth は broker が所有する。CLI は実行限定 capability だけを受け取る。
            credential = secrets.token_urlsafe(32) if subscription else job.credential_value
            environment = provider_environment({CAPABILITY_ENV if subscription else "OPENAI_API_KEY": credential}, provider="codex")
            config = _codex_config(job, subscription)
            rootfs = validate_rootfs(job.codex_rootfs, job.codex_rootfs_digest, coding=False)
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError
            egress = CodexEgress(remaining, max(1048576, job.output_limit_bytes * 8),
                                 job.model, credential,
                                 _EXEC_SCHEMA if _TOOL_REF in job.tool_refs else None,
                                 auth_file=job.codex_auth_file, auth_identity=job.codex_auth_identity,
                                 authorize=job.provider_authorize, image_uris=image_uris,
                                 image_request_limit=min(32, job.max_turns + 1), reasoning_effort=job.reasoning_effort)
            async with asyncio.timeout_at(deadline if job.limits_mode else None):
                socket_path = await egress.start()
            catalog = Path(egress.directory.name) / "models.json"
            catalog.write_text(json.dumps(model_catalog(job.model, images=bool(image_uris), reasoning_effort=job.reasoning_effort)))
            catalog.chmod(0o600)
            environment["KOTOYOSE_NATIVE_CODEX_BINARY"] = str(job.cli_binary)
            environment["KOTOYOSE_NATIVE_CODEX_ROOTFS"] = str(rootfs)
            environment["KOTOYOSE_NATIVE_CODEX_ROOTFS_DIGEST"] = job.codex_rootfs_digest
            environment["KOTOYOSE_NATIVE_CODEX_EGRESS_SOCKET"] = str(socket_path)
            environment["KOTOYOSE_NATIVE_CODEX_MODEL_CATALOG"] = str(catalog)
            if self.sessions is not None and job.session_scope is not None:
                environment['KOTOYOSE_NATIVE_CODEX_SESSION_DIR'] = str(self.sessions.directory(job.session_scope))
            # launcher の execve で HOME/CODEX_HOME と loader を含む全 host 環境を破棄する。
            if deadline <= asyncio.get_running_loop().time():
                raise TimeoutError
            if cancel.is_set():
                raise asyncio.CancelledError
            argv = [str(self.launcher_path), "app-server", "--listen", "stdio://",
                    "--config", 'model_catalog_json="/runtime/models.json"',
                    *_bool_config_args(config)]
            if job.validate_start is not None:
                await job.validate_start()
            launch_attempted = True
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env=environment, limit=job.output_limit_bytes + _PROTOCOL_OVERHEAD
                    + sum(len(uri.encode("ascii")) for uri in image_uris),
            )
            confirmation = "unconfirmed"
            identity = capture_process_identity(process.pid)
            async with asyncio.timeout_at(deadline) as runtime_timeout:
                await on_process_started(process.pid)
                if job.work_remaining is not None:
                    deadline = asyncio.get_running_loop().time() + job.work_remaining()
                    runtime_timeout.reschedule(deadline)
                rpc = _Protocol(process, job.output_limit_bytes + _PROTOCOL_OVERHEAD, image_uris, job.prompt)
                stderr = asyncio.create_task(rpc.drain_stderr())
                operation = asyncio.create_task(self._run(rpc, job, cancel, tracked_tool if job.on_runtime_stopped else execute_tool, usage, messages, config))
                cancellation = asyncio.create_task(cancel.wait())
                awaited = {operation, cancellation, stderr}
                if image_uris or job.reasoning_effort is not None:
                    broker_failure = asyncio.create_task(egress.failure_event.wait())
                    awaited.add(broker_failure)
                done, _ = await asyncio.wait(awaited, return_when=asyncio.FIRST_COMPLETED)
                if cancellation in done and cancel.is_set():
                    status, reason = "cancelled", "cancelled"
                elif broker_failure in done:
                    status, reason = "failed", egress.failure_reason
                elif stderr in done:
                    stderr.result()
                    raise ValueError("missing_completion")
                else:
                    content = operation.result()
                    status, reason = "succeeded", "completed"
        except TimeoutError:
            outcome = await _timeout_outcome(job, process, launch_attempted)
            status, reason, prestart_rejection = outcome.status, outcome.reason, outcome.prestart_rejection
        except asyncio.CancelledError:
            status, reason = "cancelled", "cancelled"
        except Exception as exc:
            outcome = _exception_outcome(job, exc, process, launch_attempted)
            status, reason, prestart_rejection = outcome.status, outcome.reason, outcome.prestart_rejection
            diagnostic, provider_limit = outcome.diagnostic, outcome.provider_limit
        finally:
            stop_observer = None
            if job.limits_mode and launch_attempted and process is None:
                confirmation = "unconfirmed"
            try:
                # provider process の停止は、協調的な task の後始末を待つ前に開始する。
                process_stop = (asyncio.create_task(stop_codex_process(identity, job.grace_seconds, self._namespace_policy))
                                if identity is not None and job.on_runtime_stopped is not None else None)

                async def observe_runtime_stop():
                    # provider の停止と通常 tool の停止が確定した時点を、
                    # coroutine や broker の後処理より先に時計へ通知する。
                    try:
                        provider_stop = await asyncio.shield(process_stop)
                        await tool_idle.wait()
                        if provider_stop == "confirmed":
                            job.on_runtime_stopped()
                    except Exception:
                        pass  # 最終の停止証拠は既存の cleanup 経路で評価する。

                stop_observer = asyncio.create_task(observe_runtime_stop()) if process_stop is not None else None
                tasks = [task for task in (operation, cancellation, stderr, broker_failure) if task is not None]
                for task in tasks:
                    task.cancel()
                if tasks:
                    # Tool 停止の収束を確認するまで provider だけの終了を完了証明にしない。
                    _, pending = await asyncio.wait(tasks, timeout=job.grace_seconds)
                    for task in tasks:
                        if task.done():
                            with contextlib.suppress(BaseException):
                                task.result()
                else:
                    pending = set()
                if not egress_closed and egress is not None and job.image_inputs and status in {"cancelled", "timeout"}:
                    # 取消後は pending provider 接続も直ちに閉じ、停止待ち中の retry を許さない。
                    egress_confirmation = await egress.close(job.grace_seconds)
                    egress_closed = True
                if process is not None:
                    if process_stop is not None:
                        try:
                            confirmation = await process_stop
                        except Exception:
                            confirmation = "unconfirmed"
                    elif identity is not None:
                        confirmation = await stop_codex_process(identity, job.grace_seconds, self._namespace_policy)
                    if process.returncode is None:
                        with contextlib.suppress(ProcessLookupError):
                            process.kill()
                    drains = [asyncio.create_task(_discard_stream(reader)) for reader, owner in (
                        (process.stdout, operation), (process.stderr, stderr),
                    ) if reader is not None and owner not in pending]
                    try:
                        await asyncio.wait_for(process.wait(), timeout=max(0.1, job.grace_seconds))
                    except Exception:
                        confirmation = "unconfirmed"
                    finally:
                        for drain in drains:
                            drain.cancel()
                        await asyncio.gather(*drains, return_exceptions=True)
                if stop_observer is not None:
                    await asyncio.wait({stop_observer}, timeout=0.01)
                    if not stop_observer.done():
                        stop_observer.cancel()
                    await asyncio.gather(stop_observer, return_exceptions=True)
                    stop_observer = None
                if pending:
                    confirmation = "unconfirmed"
                if egress is not None and not egress_closed:
                    try:
                        egress_confirmation = await egress.close(job.grace_seconds)
                    except Exception:
                        if prestart_rejection is None:
                            raise
                        egress_confirmation = "unconfirmed"
                if egress_confirmation != "confirmed":
                    confirmation = "unconfirmed"
            finally:
                if stop_observer is not None:
                    stop_observer.cancel()
                    await asyncio.gather(stop_observer, return_exceptions=True)
        return await self._finish(job, cancel, status=status, reason=reason, content=content, messages=messages,
                            confirmation=confirmation, usage=usage, egress=egress, rpc=rpc,
                            diagnostic=diagnostic, provider_limit=provider_limit,
                            prestart_rejection=prestart_rejection, launch_attempted=launch_attempted,
                            started=process is not None)

    async def _finish(self, job, cancel, *, status, reason, content, messages, confirmation, usage, egress, rpc,
                diagnostic, provider_limit, prestart_rejection, launch_attempted, started) -> NativeAgentResult:
        """終端の status・停止証拠・出力の redaction を経路に依らず同じ規則で確定する。"""
        if not content and messages:
            content = "\n".join(messages.values()).encode()[:job.output_limit_bytes]
        if egress is not None:
            content = egress.redact(content)
            if len(content) > job.output_limit_bytes:
                diagnostic = NativeCodexLimitError("redacted_output_bytes", len(content), job.output_limit_bytes)
                content = content[:job.output_limit_bytes]
                status, reason = "failed", "output_limit_exceeded"
            if prestart_rejection is None and egress.failure_reason is not None and not (job.image_inputs and status in {"cancelled", "timeout"}):
                status, reason = "failed", egress.failure_reason
                if job.limits_mode and egress.failure_limit is not None:
                    provider_limit = egress.failure_limit
                    reason = "provider_usage_limit"
                if reason == "provider_error":
                    diagnostic = NativeCodexProviderError("broker", http_status=egress.failure_http_status,
                                                          broker_diagnostic=egress.failure_diagnostic)
            elif status == "succeeded" and job.image_inputs and (egress.image_requests_completed == 0
                    or egress.image_requests_completed != egress.connections):
                # 接続開始は最初の await 前に記録される。cleanup の取消も未完要求を消せない。
                status, reason = "failed", ("image_delivery_missing" if egress.image_requests_completed == 0
                                            else "image_delivery_incomplete")
        elif job.credential_value:
            content = content.replace(job.credential_value.encode(), b"[redacted]")
        if reason == "soft_timeout":
            status = "timeout"
        if cancel.is_set() and status == "succeeded":
            status, reason = "cancelled", "cancelled"
        if status == "succeeded" and confirmation != "confirmed":
            status, reason = "worker_lost", "worker_lost"
        if reason in {"provider_error", "output_limit_exceeded"} and diagnostic is not None:
            # 診断の観測先が壊れても、既に得た終端結果や停止証拠を変更しない。
            with contextlib.suppress(Exception):
                self._emit(job, diagnostic.as_event())
        retained = getattr(rpc, 'session_ref', None)
        if retained and not await self.can_resume_session(job.session_scope, retained):
            retained = None
        if provider_limit is not None:
            provider_limit = provider_limit.model_copy(update={"resume": "native" if retained else "unavailable",
                "unavailable_reason": None if retained else ("not_retained" if getattr(rpc, "session_ref", None) else "no_session")})
        if job.limits_mode and reason == "output_limit_exceeded" and isinstance(diagnostic, NativeCodexLimitError) and diagnostic.stage == "tool_result_bytes":
            reason = "tool_output_limit_exceeded"
        counter = getattr(egress, "response_counter", None)
        return NativeAgentResult(status, reason, content, confirmation, usage, provider_session_id=retained,
                                 provider_limit=provider_limit, output_bytes=getattr(rpc, "output_bytes", 0),
                                 turns=0 if job.limits_mode and not launch_attempted else counter.value() if counter is not None else None,
                                 launch_outcome="started" if started else "unknown" if launch_attempted else "not_started",
                                 codex_egress_metrics=(egress.metrics() if egress is not None
                                                      and job.codex_egress_limits is not None else None))

    async def _apply_external_policy(self, rpc, thread_id: str) -> None:
        policy = {"type": "externalSandbox", "networkAccess": "restricted"}
        await rpc.request("thread/settings/update", {
            "threadId": thread_id, "cwd": "/workspace", "approvalPolicy": "never", "sandboxPolicy": policy,
        })
        # The empty RPC response only acknowledges queuing. Use the same reader
        # and wait for the actual applied-settings notification before any turn.
        deferred = []
        deferred_bytes = 0
        for _ in range(_MAX_PENDING):
            message = await rpc.next()
            if message.get("method") == "thread/settings/updated":
                params = message.get("params", {})
                settings = params.get("threadSettings") if isinstance(params, dict) else None
                if (not isinstance(params, dict) or not isinstance(settings, dict)
                        or params.get("threadId") != thread_id or settings.get("cwd") != "/workspace"
                        or settings.get("approvalPolicy") != "never" or settings.get("sandboxPolicy") != policy):
                    raise ValueError("sandbox_policy_mismatch")
                rpc.pending.extendleft(reversed(deferred))
                rpc.pending_bytes += deferred_bytes
                return
            if ("id" in message or not isinstance(message.get("method"), str)
                    or message["method"].startswith(("turn/", "item/"))
                    or message.get("method") == "error"):
                raise ValueError("invalid_protocol")
            # 待機中データだけを制限し、処理済み通信量とは結びつけない。
            held = deferred_bytes + rpc.pending_bytes + rpc.frame_bytes
            if held > rpc.limit:
                raise NativeCodexLimitError("settings_pending_bytes", held, rpc.limit)
            deferred.append((rpc.frame_bytes, message))
            deferred_bytes += rpc.frame_bytes
        raise NativeCodexLimitError("settings_pending_count", len(deferred), _MAX_PENDING)

    async def _run(self, rpc, job, cancel, execute_tool, usage, messages, config) -> bytes:
        await rpc.request("initialize", {"clientInfo": {"name": "kotoyose-native-codex", "version": "1"},
                                        "capabilities": {"experimentalApi": True}})
        await rpc.send({"method": "initialized"})
        coding = validate_mode(job.codex_execution_mode)
        sandbox = resolve_codex_sandbox(job.codex_execution_mode, job.codex_sandbox)
        external = coding and validate_coding_policy(
            job.execution_environment, job.codex_sandbox_policy, job.codex_tool_policy_digest,
            mode=job.codex_execution_mode)
        # 部屋 (write と read) は externalSandbox へ委譲する。容器に bubblewrap が無く、
        # SDK 内の sandbox では command が動かないため。read の書き込み禁止は /workspace の
        # ro mount が担う (SDK の read-only に頼らない)。
        delegates = bool(external)
        if delegates:
            # No model input is sent before official external delegation is applied.
            sandbox = "read-only"
        resuming = job.resume_mode == 'native'
        if resuming and (not job.session_scope or not job.provider_session_ref
                         or not await self.can_resume_session(job.session_scope, job.provider_session_ref)):
            raise ValueError('provider_session_unavailable')
        model_provider = config.get("model_provider", _CONFIG["model_provider"])
        thread = await rpc.request("thread/resume" if resuming else "thread/start", {
            "model": job.model, "cwd": "/workspace" if coding else "/tmp",
            **({'threadId': job.provider_session_ref.split(':', 2)[1], 'excludeTurns': True} if resuming else
               {'ephemeral': self.sessions is None or job.session_scope is None}),
            "sandbox": sandbox,
            "modelProvider": model_provider, **({} if coding or resuming else {"environments": []}),
            "approvalPolicy": "never", "developerInstructions": (
                job.instructions + "\n\n" + _SANDBOX_EXEC_INSTRUCTIONS if _TOOL_REF in job.tool_refs else job.instructions
            ),
            "config": config, **({"dynamicTools": ([{
                "type": "function", "name": "sandbox_exec", "description": _SANDBOX_EXEC_DESCRIPTION,
                "inputSchema": _EXEC_SCHEMA,
            }] if _TOOL_REF in job.tool_refs else [])} if not resuming else {}),
        })
        if (thread.get("model") != job.model or thread.get("modelProvider") != model_provider
                or job.reasoning_effort is not None and thread.get("reasoningEffort") != job.reasoning_effort):
            raise NativeCodexProviderError("thread_start")
        if coding and (job.codex_sandbox is not None or external):
            validate_thread_sandbox(thread, sandbox)
        thread_id = thread["thread"]["id"]
        if resuming and thread_id != job.provider_session_ref.split(':', 2)[1]:
            raise ValueError("invalid_protocol")
        rpc.thread_id = thread_id
        if self.sessions is not None and job.session_scope is not None:
            rpc.session_ref = (job.provider_session_ref if resuming else
                               self.sessions.reference(thread_id, thread['thread'].get('path'),
                                                       home='/home/agent' if coding else '/home/kotoyose'))
        if delegates:
            await self._apply_external_policy(rpc, thread_id)
        turn = await rpc.request("turn/start", {"threadId": thread_id,
            **({"effort": job.reasoning_effort} if job.reasoning_effort is not None else {}),
            "input": [{"type": "text", "text": job.prompt},
                      *({"type": "image", "url": uri} for uri in rpc.image_uris)]})
        turn_id = turn["turn"]["id"]
        if rpc.turn_id is not None and rpc.turn_id != turn_id:
            raise ValueError("invalid_protocol")
        rpc.turn_id = turn_id
        rpc.output_bytes = 0
        assistant_items = set()
        tool_calls = {}
        semantic_calls = {}
        response_body = _AssistantResponse()
        tool_bytes = 0
        builtin_items = set()
        # soft は controller の進捗保存要求。provider turn を中断する期限にはしない。
        # hard deadline と所有 process tree の停止処理は外側で引き続き強制する。
        soft = CodexSoftStop(None)
        interrupt_id = None
        read_task = None
        try:
            while True:
                if not coding or rpc.pending:
                    message = await rpc.next()
                else:
                    soft.mark_ready()
                    if read_task is None:
                        read_task = asyncio.create_task(rpc.next())
                    if not read_task.done() and soft.claim_interrupt(asyncio.get_running_loop().time()):
                        rpc.sequence += 1
                        interrupt_id = f"kotoyose-{rpc.sequence}"
                        await rpc.send({"id": interrupt_id, "method": "turn/interrupt",
                                        "params": {"threadId": thread_id, "turnId": turn_id}})
                    done, _ = await asyncio.wait({read_task}, timeout=soft.wake_timeout(asyncio.get_running_loop().time()))
                    if not done:
                        continue
                    message = read_task.result()
                    read_task = None
                if interrupt_id is not None and message.get("id") == interrupt_id and "method" not in message:
                    # Acknowledgment is not a process-stop proof. Never retry it.
                    continue
                method, params = message.get("method"), message.get("params", {})
                if "id" in message and method is not None:
                    call_id = message["id"]
                    if not isinstance(call_id, (str, int)) or isinstance(call_id, bool):
                        raise ValueError("invalid_protocol")
                    if (isinstance(params.get("threadId"), str) and params.get("threadId") != thread_id
                            or isinstance(params.get("turnId"), str) and params.get("turnId") != turn_id):
                        await rpc.send({"id": call_id, "result": {
                            "contentItems": [{"type": "inputText", "text": "tool_denied"}], "success": False}})
                        continue
                    fingerprint = json.dumps({"method": method, "params": params}, sort_keys=True, separators=(",", ":"))
                    if call_id in tool_calls:
                        previous, cached = tool_calls[call_id]
                        if previous != fingerprint:
                            raise ValueError("invalid_protocol")
                        await rpc.send({"id": call_id, "result": cached})
                        continue
                    response = {"contentItems": [{"type": "inputText", "text": "tool_denied"}], "success": False}
                    admitted = False
                    in_scope = params.get("threadId") == thread_id and params.get("turnId") == turn_id
                    call = params.get("callId")
                    semantic_key = (thread_id, turn_id, call) if isinstance(call, str) and call else None
                    if in_scope and semantic_key in semantic_calls:
                        previous, cached = semantic_calls[semantic_key]
                        if previous != fingerprint:
                            raise ValueError("invalid_protocol")
                        tool_calls[call_id] = (fingerprint, cached)
                        await rpc.send({"id": call_id, "result": cached})
                        continue
                    if (method == "item/tool/call" and params.get("threadId") == thread_id
                            and params.get("turnId") == turn_id and params.get("tool") == "sandbox_exec"
                            and params.get("namespace") is None and semantic_key is not None
                            and _TOOL_REF in job.tool_refs and not cancel.is_set()):
                        arguments = params.get("arguments")
                        if (isinstance(arguments, dict) and set(arguments) == {"argv", "cwd"}
                                and _valid_argv(arguments["argv"]) and _valid_cwd(arguments["cwd"])):
                            admitted = True
                            if len(assistant_items) + len(semantic_calls) >= job.max_turns:
                                raise ValueError("turn_limit_exceeded")
                            result = await execute_tool(tuple(arguments["argv"]), arguments["cwd"])
                            if result.stop_confirmation != "confirmed":
                                cancel.set()
                                raise ValueError("provider_error")
                            text = json.dumps({"status": result.status, "exit_code": result.exit_code,
                                               "stdout": result.stdout.decode("utf-8", errors="replace"),
                                               "stderr": result.stderr.decode("utf-8", errors="replace")})
                            tool_bytes += len(text.encode())
                            rpc.output_bytes = tool_bytes + sum(len(value.encode()) for value in messages.values())
                            if tool_bytes > job.output_limit_bytes:
                                raise NativeCodexLimitError("tool_result_bytes", tool_bytes, job.output_limit_bytes)
                            response = {"contentItems": [{"type": "inputText", "text": text}],
                                        "success": result.status == "succeeded"}
                            self._emit(job, RuntimeEvent(kind="tool_result", tool_name="sandbox_exec", summary=result.status))
                    # Unknown approval/permission requests receive an RPC error, never an accept response.
                    if method != "item/tool/call":
                        await rpc.send({"id": message["id"], "error": {"code": -32601, "message": "tool_denied"}})
                        raise ValueError("tool_denied")
                    tool_calls[call_id] = (fingerprint, response)
                    if in_scope and semantic_key is not None:
                        semantic_calls[semantic_key] = (fingerprint, response)
                    await rpc.send({"id": call_id, "result": response})
                    if not admitted:
                        raise ValueError("tool_denied")
                    continue
                if (params.get("threadId") != thread_id
                        or params.get("turnId", params.get("turn", {}).get("id")) != turn_id):
                    continue
                if method in {"item/started", "item/completed"}:
                    item = params.get("item", {})
                    if coding:
                        soft.observe(method, item)
                    if item.get("type") == "agentMessage":
                        assistant_items.add(item["id"])
                        if len(assistant_items) + (len(builtin_items) if coding else len(semantic_calls)) > job.max_turns:
                            raise ValueError("turn_limit_exceeded")
                        if method == "item/completed":
                            value = item.get("text", "")
                            if not isinstance(value, str):
                                raise ValueError("invalid_protocol")
                            response_body.add_item(AgentMessageThreadItem.model_validate(item), completed=True)
                            messages[item["id"]] = value
                            rpc.output_bytes = tool_bytes + sum(len(value.encode()) for value in messages.values())
                            retained = sum(len(text.encode()) for text in messages.values()) + tool_bytes
                            if retained > job.output_limit_bytes:
                                raise NativeCodexLimitError("agent_message_bytes", retained, job.output_limit_bytes)
                    elif coding and item.get("type") in {"commandExecution", "fileChange"}:
                        item_id = item.get("id")
                        if not isinstance(item_id, str) or not item_id:
                            raise ValueError("invalid_protocol")
                        builtin_items.add(item_id)
                        if len(assistant_items) + len(builtin_items) > job.max_turns:
                            raise ValueError("turn_limit_exceeded")
                        if method == "item/completed":
                            state = item.get("status")
                            self._emit(job, RuntimeEvent(kind="tool_result", tool_name=item["type"], summary=str(state)))
                            if state not in {"completed", "failed", "declined"}:
                                raise ValueError("invalid_protocol")
                            # Ordinary failed tests/patches stay visible to the SDK's
                            # repair loop. A final answer is not a verification receipt.
                    elif item.get("type") not in {"userMessage", "reasoning", "dynamicToolCall", "contextCompaction"}:
                        raise ValueError("tool_denied")
                elif method == "thread/tokenUsage/updated":
                    tokens = params.get("tokenUsage", {}).get("total", {})
                    for key, target in (("inputTokens", "input_tokens"), ("outputTokens", "output_tokens")):
                        value = tokens.get(key)
                        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                            usage[target] = value
                            usage["source"] = "reported"
                elif method == "turn/completed":
                    if soft.interrupt_claimed and params.get("turn", {}).get("status") == "interrupted":
                        raise ValueError("soft_timeout")
                    if params.get("turn", {}).get("status") != "completed":
                        turn = params.get("turn", {})
                        raise NativeCodexProviderError("turn_completed", turn.get("error"), turn_status=turn.get("status"))
                    final, _ = response_body.final_response()
                    if not final:
                        raise ValueError("missing_completion")
                    text = (final.replace(job.credential_value, "[redacted]") if job.credential_value else final).encode()
                    if len(text) + tool_bytes > job.output_limit_bytes:
                        raise NativeCodexLimitError("final_response_bytes", len(text) + tool_bytes, job.output_limit_bytes)
                    if job.output_contract == "json":
                        text = _extract_json_object(text)
                        if text is None:
                            raise ValueError("invalid_json_output")
                    return text
                elif method == "error" and not params.get("willRetry", False):
                    raise NativeCodexProviderError("error_notification", params.get("error"))

        finally:
            if read_task is not None:
                read_task.cancel()
                await asyncio.gather(read_task, return_exceptions=True)

    @staticmethod
    def _emit(job, event) -> None:
        for callback in (job.activity_event, job.progress_event):
            if callback is not None:
                callback(event)
