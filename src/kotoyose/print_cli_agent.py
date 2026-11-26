"""Host subprocess worker for generic print-mode (`-p`) CLIs."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import contextlib
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile
from typing import TYPE_CHECKING, Any, Literal

import structlog

from .agent_deadlines import AgentStartRejected
from .execution_resources import ExecutionError
from .grok_skills import grok_skill_env, uses_grok_native_skills
from .host_place import _shutdown, _watch_limits, _watch_work_timeout, kill_session
from .print_cli_events import PrintCliStreamOutcome, create_print_cli_parser, read_stderr_tail
from .print_cli_hooks import classify_stderr
from .secret_store import SecretStoreError, resolve_secret_async
from .runtime.skill_resources import CliInstructions
from .print_cli_home import PrintCliHomeBusyError, use_login_home
from .print_cli_profiles import PrintCliProfile, build_print_cli_env, get_print_cli_profile
from .provider_usage_limits import classify_print_cli_usage_limit
from .repository_skills import RepositorySkills

if TYPE_CHECKING:
    from .native_claude import NativeAgentResult


log = structlog.get_logger()

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_SECRET_PATTERNS = (
    (re.compile(r"(Bearer\s+)[^\s]+", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{10,}\b"), "[REDACTED]"),
    (re.compile(r"\bsk-[0-9A-Za-z_-]{10,}\b"), "[REDACTED]"),
    (re.compile(r"([?&](?:token|key|secret|auth|code|session)=)[^&\s]+", re.IGNORECASE), r"\1[REDACTED]"),
)
_STREAM_READER_MIN_LIMIT = 65536


def _normalize_home_key(login_home: Path | str) -> str:
    # Normalize path lexically without touching or opening files under login_home.
    return os.path.abspath(os.path.normpath(str(login_home)))


class _HomeSemaphoreRegistry:
    """In-memory per-(profile, login_home) concurrency limit registry."""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], tuple[int, asyncio.Semaphore]] = {}

    def get(self, profile_id: str, login_home: Path | str, limit: int) -> asyncio.Semaphore:
        safe_limit = max(1, int(limit))
        key = (profile_id, _normalize_home_key(login_home))
        existing = self._entries.get(key)
        if existing is None or existing[0] != safe_limit:
            sem = asyncio.Semaphore(safe_limit)
            self._entries[key] = (safe_limit, sem)
            return sem
        return existing[1]

    def has_capacity(self, profile_id: str, login_home: Path | str, limit: int) -> bool:
        safe_limit = max(1, int(limit))
        key = (profile_id, _normalize_home_key(login_home))
        existing = self._entries.get(key)
        if existing is None or existing[0] != safe_limit:
            return True
        return not existing[1].locked()

    def clear(self) -> None:
        """Reset registry entries (intended for test isolation)."""
        self._entries.clear()


_HOME_SEMAPHORES = _HomeSemaphoreRegistry()


def has_print_cli_home_capacity(profile_id: str, login_home: Path | str) -> bool:
    """Return False when `login_home` already runs its maximum concurrent print CLIs."""
    profile = get_print_cli_profile(profile_id)
    return _HOME_SEMAPHORES.has_capacity(profile.id, login_home, profile.max_concurrent_per_home)


def sanitize_stderr_tail(stderr: str, *, max_chars: int = 512) -> str:
    """Strip ANSI escapes, secret-looking tokens, and control characters from stderr tails."""
    cleaned = _ANSI_ESCAPE_RE.sub("", stderr)
    for pattern, replacement in _SECRET_PATTERNS:
        cleaned = pattern.sub(replacement, cleaned)
    cleaned = "".join(ch if ch in ("\n", "\t") or ord(ch) >= 32 else " " for ch in cleaned)
    cleaned = cleaned.strip()
    if len(cleaned) > max_chars:
        cleaned = cleaned[-max_chars:]
    return cleaned


def compose_print_cli_prompt(
    instructions: str,
    prompt: str,
    *,
    output_contract: str = "text",
    output_schema: dict[str, Any] | None = None,
) -> str:
    """Combine system instructions, prompt, and JSON-output directive if needed."""
    parts: list[str] = []
    if instructions.strip():
        parts.append(instructions.strip())
    if prompt:
        parts.append(prompt)
    if output_contract == "json":
        if output_schema:
            schema_text = json.dumps(output_schema, ensure_ascii=False, sort_keys=True)
            parts.append(
                "Return ONLY a single valid JSON object matching this schema "
                f"(no markdown prose outside the JSON): {schema_text}"
            )
        else:
            parts.append("Return ONLY a single valid JSON object (no surrounding prose).")
    return "\n\n".join(parts)


@dataclass(frozen=True)
class PrintCliJob:
    """Self-contained print-CLI execution request for `PrintCliWorker`."""

    profile_id: str
    cli_binary: Path | str
    model: str
    workspace: Path
    login_home: Path | str | None
    surface: str
    instructions: str
    prompt: str
    output_contract: str
    timeout_seconds: float
    grace_seconds: float
    output_limit_bytes: int
    memory_bytes: int
    pids_limit: int
    cpus: float
    output_schema: dict[str, Any] | None = None
    add_dirs: tuple[Path | str, ...] = ()
    validate_start: Callable[[], Awaitable[None]] | None = None
    work_remaining: Callable[[], float] | None = None
    execution_environment: str = "host"
    image_id: str | None = None
    workspace_device: int | None = None
    workspace_inode: int | None = None
    generation: int = 0
    attempt_id: str = "print-cli"
    connection_id: str = ""
    api_key_secret: str | None = None
    acp: bool = False
    acp_tools: tuple[str, ...] | None = None
    acp_deny: tuple[str, ...] = ()
    alias_skills: CliInstructions | None = field(default=None, repr=False)
    repository_skills: RepositorySkills | None = None

    def place_alias_skills(
        self, root: Path, profile: PrintCliProfile, *, visible_root: Path | None = None,
        layout: Literal["prefix", "grok"] = "prefix",
    ) -> PrintCliJob:
        """alias スキルを配置し、CLI に見えるパスと本文を実行指示へ加える。"""
        if self.alias_skills is None:
            return self
        try:
            prefix = self.alias_skills.place_grok(root) if layout == "grok" else self.alias_skills.place(root, visible_root=visible_root)
        except ValueError as exc:
            raise AgentStartRejected("unsupported_input", str(exc), 422) from None
        instructions = "\n\n".join(filter(None, (prefix, self.instructions)))
        prompt = compose_print_cli_prompt(
            instructions, self.prompt, output_contract=self.output_contract, output_schema=self.output_schema,
        )
        if len(prompt.encode("utf-8")) > profile.max_prompt_bytes:
            raise AgentStartRejected("payload_too_large", "Prompt exceeds print CLI limit after skill placement", 413)
        return replace(self, instructions=instructions, alias_skills=None)



def classify_print_cli_outcome(
    job: PrintCliJob,
    profile: PrintCliProfile,
    outcome: PrintCliStreamOutcome,
    stderr_tail: str,
    returncode: int,
    *,
    overflowed: bool = False,
    classify_auth: bool = False,
) -> tuple[str, str, bytes, int, Any]:
    """Classify print-CLI stdout/stderr/exit outcome uniformly for host and container runs."""
    encoded = outcome.content.encode("utf-8")
    if len(encoded) > job.output_limit_bytes:
        content = encoded[: job.output_limit_bytes]
        output_bytes = len(content)
        overflowed = True
    else:
        content = encoded
        output_bytes = len(encoded)

    sanitized_tail = sanitize_stderr_tail(stderr_tail)
    evidence: dict[str, Any] = {
        **outcome.diagnostics,
        **classify_stderr(profile.classify_stderr, stderr_tail),
        "profile": profile.id,
        "stderr_tail": sanitized_tail,
        "error_lines": outcome.raw_error_texts,
    }
    if classify_auth and (returncode != 0 or outcome.result_status != "SUCCESS") and (
        evidence.get("status") == "UNAUTHENTICATED"
        or evidence.get("api_error_status") == 401
        or evidence.get("http_status") == 401
        or any(pattern.lower() in stderr_tail.lower() for pattern in profile.auth_error_patterns)
    ):
        return "failed", "authentication_failed", content, output_bytes, None
    detected_limit = classify_print_cli_usage_limit(
        profile,
        evidence,
        datetime.now(timezone.utc),
    )
    if detected_limit is not None:
        return "failed", "provider_usage_limit", content, output_bytes, detected_limit

    mapped_exit = profile.classify_exit_code(returncode)
    if mapped_exit == "timeout":
        return "timeout", "timeout", content, output_bytes, None
    if overflowed:
        return "failed", "output_limit_exceeded", content, output_bytes, None
    if not outcome.saw_result:
        return "failed", "missing_completion", content, output_bytes, None
    if outcome.result_status is not None and outcome.result_status != "SUCCESS":
        return "failed", "provider_error", content, output_bytes, None
    if returncode != 0:
        return "failed", "provider_error", content, output_bytes, None

    if job.output_contract == "json":
        from .native_claude import _extract_json_object

        extracted = _extract_json_object(content)
        if extracted is None:
            return "failed", "invalid_json_output", content, output_bytes, None
        if len(extracted) > job.output_limit_bytes:
            trimmed = extracted[: job.output_limit_bytes]
            return "failed", "output_limit_exceeded", trimmed, len(trimmed), None
        return "succeeded", "completed", extracted, len(extracted), None

    return "succeeded", "completed", content, output_bytes, None


def _redact_cli_key(value: Any, key: str) -> Any:
    if isinstance(value, str):
        return value.replace(key, "[REDACTED]")
    if isinstance(value, dict):
        return {_redact_cli_key(name, key): _redact_cli_key(item, key) for name, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_cli_key(item, key) for item in value]
    return value


async def read_print_cli_stdout_events(
    stdout: asyncio.StreamReader,
    profile: PrintCliProfile,
    job: PrintCliJob,
    *,
    on_event: Callable[[str, dict[str, Any]], Awaitable[None]],
    secret: str | None = None,
) -> tuple[PrintCliStreamOutcome, bool]:
    """Read stdout through EOF or until the output limit is exceeded."""
    if profile.parser == "text":
        try:
            raw = await stdout.readexactly(job.output_limit_bytes + 1)
        except asyncio.IncompleteReadError as exc:
            raw = exc.partial
        content = raw.decode("utf-8", errors="replace")
        if secret:
            content = content.replace(secret, "[REDACTED]")
        return PrintCliStreamOutcome(saw_result=True, result_status="SUCCESS", content=content), len(raw) > job.output_limit_bytes

    parser = create_print_cli_parser(profile.parser)
    streamed_bytes = 0
    line_overflow = False
    while True:
        try:
            raw_line = await stdout.readline()
        except ValueError:
            line_overflow = True
            break
        if not raw_line:
            break
        line = raw_line.decode("utf-8", errors="replace")
        for event in parser.feed_line(line):
            if streamed_bytes < job.output_limit_bytes:
                payload = asdict(event)
                if secret:
                    payload = _redact_cli_key(payload, secret)
                streamed_bytes += len(json.dumps(payload, default=str).encode("utf-8"))
                if streamed_bytes <= job.output_limit_bytes:
                    await on_event("provider_progress", payload)
    outcome = parser.finalize()
    if secret:
        outcome.content = outcome.content.replace(json.dumps(secret)[1:-1], "[REDACTED]").replace(secret, "[REDACTED]")
        outcome.raw_error_texts = _redact_cli_key(outcome.raw_error_texts, secret)
        outcome.diagnostics = _redact_cli_key(outcome.diagnostics, secret)
    return outcome, line_overflow


_STDERR_AFTER_EXIT_SECONDS = 2.0


async def _reap_session_after_exit(process: asyncio.subprocess.Process) -> None:
    """Once the CLI itself exits, kill helpers it left in its session so the pipes reach EOF.

    Helpers that inherit stdout/stderr (Cursor's worker-server and language servers) otherwise keep
    the pipes open and the turn waits for the full timeout.
    """
    # asyncio's Process.wait() only returns after every pipe closed, which is exactly what the
    # helpers prevent, so watch the leader's own state instead.
    while not _leader_exited(process.pid):
        await asyncio.sleep(0.2)
    await asyncio.sleep(_STDERR_AFTER_EXIT_SECONDS)
    kill_session(process.pid)


def _leader_exited(pid: int) -> bool:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return True
    state = raw[raw.rfind(")") + 2:].split()[0]
    return state in {"Z", "X"}


async def _collect_stderr_after_exit(stderr_task: asyncio.Task, session_id: int) -> str:
    """Read the stderr tail once the CLI exited, without waiting on helpers that inherited it.

    A helper daemon left in the CLI's session can hold the stderr pipe open forever (Cursor's
    worker-server did, which made every chat wait for the full timeout). After a short grace the
    session's leftovers are killed; if the pipe still does not close, the tail read so far is used.
    """
    try:
        return await asyncio.wait_for(asyncio.shield(stderr_task), _STDERR_AFTER_EXIT_SECONDS)
    except TimeoutError:
        kill_session(session_id)
    try:
        return await asyncio.wait_for(asyncio.shield(stderr_task), _STDERR_AFTER_EXIT_SECONDS)
    except TimeoutError:
        stderr_task.cancel()
        return ""


class PrintCliWorker:
    """Run an official `-p` CLI in an isolated env and dedicated process group."""

    def __init__(
        self, *, container_runtime: tuple[Any, Any, str | None] | None = None, chat: bool = False,
    ) -> None:
        self.container_runtime = container_runtime
        self.chat = chat

    async def execute(
        self,
        job: PrintCliJob,
        cancel: asyncio.Event,
        *,
        on_process_started: Callable[..., Awaitable[None]],
        on_event: Callable[[str, dict[str, Any]], Awaitable[None]],
        on_runtime_stopped: Callable[[], None] | None = None,
    ) -> NativeAgentResult:
        if job.login_home is None:
            if job.execution_environment != "host" or not job.api_key_secret:
                raise AgentStartRejected("unsupported_capability", "cli.print API-key authentication requires host placement", 422)
            return await self._execute(
                job, cancel, on_process_started=on_process_started, on_event=on_event,
                on_runtime_stopped=on_runtime_stopped,
            )
        if job.api_key_secret is not None:
            raise AgentStartRejected("unsupported_capability", "cli.print cannot combine API key and login home", 422)
        try:
            with use_login_home(job.login_home):
                return await self._execute(
                    job, cancel, on_process_started=on_process_started, on_event=on_event,
                    on_runtime_stopped=on_runtime_stopped,
                )
        except PrintCliHomeBusyError as exc:
            raise AgentStartRejected("login_in_progress", str(exc), 409) from exc

    async def _execute(
        self,
        job: PrintCliJob,
        cancel: asyncio.Event,
        *,
        on_process_started: Callable[..., Awaitable[None]],
        on_event: Callable[[str, dict[str, Any]], Awaitable[None]],
        on_runtime_stopped: Callable[[], None] | None,
    ) -> NativeAgentResult:
        from .native_claude import NativeAgentResult

        profile = get_print_cli_profile(job.profile_id)
        if profile.host_only and job.execution_environment != "host":
            raise AgentStartRejected("unsupported_capability", f"{profile.id} supports host placement only", 422)
        sem = _HOME_SEMAPHORES.get(profile.id, job.login_home or job.connection_id, profile.max_concurrent_per_home)
        if sem.locked():
            raise AgentStartRejected(
                "capacity_exhausted",
                f"Print CLI login home concurrency limit ({profile.max_concurrent_per_home}) reached",
                409,
            )

        await sem.acquire()
        sem_acquired = True
        if job.execution_environment == "isolated":
            try:
                if self.container_runtime is None:
                    raise AgentStartRejected(
                        "unsupported_capability",
                        "Agent container runtime is not configured for isolated print CLI",
                        501,
                    )
                container, container_settings, fallback_image_id = self.container_runtime
                from .container_print import execute_container_print

                return await execute_container_print(
                    job,
                    cancel,
                    on_process_started=on_process_started,
                    on_event=on_event,
                    on_runtime_stopped=on_runtime_stopped,
                    container=container,
                    settings=container_settings,
                    image_id=job.image_id or fallback_image_id,
                )
            finally:
                sem.release()

        process: asyncio.subprocess.Process | None = None
        spawn: asyncio.Task[asyncio.subprocess.Process] | None = None
        tasks: list[asyncio.Task[Any]] = []
        stderr_task: asyncio.Task[str] | None = None
        content = b""
        output_bytes = 0
        turns: int | None = None
        usage: dict[str, Any] = {
            "source": "unknown",
            "cost": None,
            "input_tokens": None,
            "output_tokens": None,
        }
        status: str = "failed"
        reason: str = "provider_error"
        stop = "confirmed"
        provider_limit = None
        session_id = None
        acp = None
        skill_temp = None
        deadline = asyncio.get_running_loop().time() + job.timeout_seconds

        try:
            if cancel.is_set():
                return NativeAgentResult(
                    status="cancelled",
                    reason="cancelled",
                    content=b"",
                    stop_confirmation="confirmed",
                    usage=usage,
                    output_bytes=0,
                    launch_outcome="not_started",
                )
            remaining_now = job.work_remaining() if job.work_remaining is not None else (
                deadline - asyncio.get_running_loop().time()
            )
            if remaining_now <= 0:
                return NativeAgentResult(
                    status="timeout",
                    reason="timeout",
                    content=b"",
                    stop_confirmation="confirmed",
                    usage=usage,
                    output_bytes=0,
                    launch_outcome="not_started",
                )
            if job.validate_start is not None:
                await job.validate_start()

            native = uses_grok_native_skills(profile, Path(job.cli_binary))
            if job.repository_skills is not None and not native:
                raise AgentStartRejected(
                    "unsupported_capability", "repository_skills requires the pinned Linux x64 Grok 1.0.89 binary", 422,
                )
            grok_home = None
            if native:
                skill_temp = tempfile.TemporaryDirectory(prefix="kotoyose-print-home-")
                grok_home = Path(skill_temp.name)
                job = job.place_alias_skills(grok_home, profile, layout="grok")
            elif job.alias_skills is not None:
                skill_temp = tempfile.TemporaryDirectory(prefix="kotoyose-print-skills-")
                job = job.place_alias_skills(Path(skill_temp.name) / "skills", profile)

            # Keep --print-timeout strictly below Kotoyose's own hard deadline.
            print_timeout_seconds = max(0.1, remaining_now * 0.85)

            with tempfile.TemporaryDirectory(prefix="kotoyose-print-cli-") as settings_dir:
                prompt = compose_print_cli_prompt(
                    job.instructions, job.prompt,
                    output_contract=job.output_contract, output_schema=job.output_schema,
                )
                stdin_text = None
                if profile.data_declaration is not None:
                    from .provider_data_runtime import data_env

                    env = data_env()
                    secret = None
                    if not job.acp and profile.data_declaration.prompt_via == "stdin":
                        stdin_text = prompt
                else:
                    env = build_print_cli_env(profile, job.login_home or settings_dir, minimal=self.chat)
                    secret = None
                    if grok_home is not None:
                        grok_skill_env(env, grok_home, job.repository_skills, profile)
                if profile.data_declaration is None and job.api_key_secret is not None:
                    if profile.api_key_env is None:
                        raise AgentStartRejected("unsupported_capability", "cli.print profile does not declare api_key_env", 422)
                    try:
                        secret = await resolve_secret_async(job.api_key_secret)
                    except SecretStoreError as exc:
                        raise AgentStartRejected("authentication_failed", "cli.print API key is unavailable", 401) from exc
                    env[profile.api_key_env] = secret
                if job.acp:
                    from .acp_client import LINE_LIMIT

                    if profile.acp is None:
                        raise AgentStartRejected("unsupported_capability", "Profile does not declare ACP", 422)
                    argv = profile.acp.arguments(job.model)
                    env.update(profile.acp.env)
                    stream_limit = LINE_LIMIT
                else:
                    settings_path = profile.write_settings_file(job.surface, Path(settings_dir))
                    argv = profile.render_argv(
                        model=job.model,
                        prompt=prompt,
                        timeout_seconds=print_timeout_seconds,
                        surface=job.surface,
                        settings_path=settings_path,
                        add_dirs=job.add_dirs,
                    )
                    stream_limit = max(_STREAM_READER_MIN_LIMIT, job.output_limit_bytes * 6 + 65536)
                spawn = asyncio.create_task(
                    asyncio.create_subprocess_exec(
                        str(job.cli_binary),
                        *argv,
                        cwd=job.workspace,
                        start_new_session=True,
                        stdin=asyncio.subprocess.PIPE if job.acp or stdin_text is not None else asyncio.subprocess.DEVNULL,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        limit=stream_limit,
                        env=env,
                    )
                )
                process = await asyncio.shield(spawn)
                if profile.data_declaration is not None and process.pid:
                    from .provider_data_runtime import register_declared_process

                    register_declared_process(profile.data_declaration.id, process.pid)
                if stdin_text is not None and process.stdin is not None:
                    process.stdin.write(stdin_text.encode())
                    async with asyncio.timeout_at(deadline):
                        await process.stdin.drain()
                    process.stdin.close()

                assert process.stdout is not None
                assert process.stderr is not None
                stderr_task = asyncio.create_task(read_stderr_tail(process.stderr, max_bytes=32768))

                if job.acp:
                    from .acp_execution import AcpExecution

                    acp = AcpExecution(process, job, profile, on_event, cwd=str(job.workspace))

                async def run_stream() -> tuple[str, str]:
                    nonlocal content, output_bytes, turns, usage, provider_limit, session_id
                    await on_process_started(process.pid)
                    if cancel.is_set():
                        raise asyncio.CancelledError
                    if (
                        job.work_remaining() <= 0
                        if job.work_remaining is not None
                        else asyncio.get_running_loop().time() >= deadline
                    ):
                        raise TimeoutError

                    await on_event(
                        "provider_started",
                        {"backend": "print_cli", "profile": profile.id, "pid": process.pid},
                    )
                    reaper = asyncio.create_task(_reap_session_after_exit(process))
                    try:
                        if acp is not None:
                            status_out, reason_out, content = await acp.run()
                            output_bytes = len(content)
                            turns = 1
                            return status_out, reason_out
                        outcome, line_overflow = await read_print_cli_stdout_events(
                            process.stdout,
                            profile,
                            job,
                            on_event=on_event,
                            secret=secret,
                        )
                    finally:
                        reaper.cancel()
                    if self.chat:
                        session_id = outcome.session_id
                    if line_overflow:
                        usage = dict(outcome.usage)
                        turns = outcome.turns
                        encoded = outcome.content.encode("utf-8")[: job.output_limit_bytes]
                        content = encoded
                        output_bytes = len(encoded)
                        return "failed", "output_limit_exceeded"

                    returncode = await process.wait()
                    stderr_tail = await _collect_stderr_after_exit(stderr_task, process.pid)
                    if secret:
                        stderr_tail = stderr_tail.replace(secret, "[REDACTED]")
                    usage = dict(outcome.usage)
                    turns = outcome.turns
                    status_out, reason_out, content, output_bytes, provider_limit = classify_print_cli_outcome(
                        job,
                        profile,
                        outcome,
                        stderr_tail,
                        returncode,
                        classify_auth=self.chat,
                    )
                    return status_out, reason_out

                response_task = asyncio.create_task(run_stream())
                cancelled_task = asyncio.create_task(cancel.wait())
                limits_task = asyncio.create_task(_watch_limits(process.pid, job))
                tasks = [response_task, cancelled_task, limits_task]
                if job.work_remaining is not None:
                    tasks.append(asyncio.create_task(_watch_work_timeout(job.work_remaining)))

                wait_timeout = (
                    None
                    if job.work_remaining is not None
                    else max(0.0, deadline - asyncio.get_running_loop().time())
                )
                done, _ = await asyncio.wait(
                    tasks,
                    timeout=wait_timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if cancel.is_set():
                    status, reason = "cancelled", "cancelled"
                elif not done or (job.work_remaining is not None and tasks[-1] in done):
                    status, reason = "timeout", "timeout"
                elif limits_task in done:
                    status, reason = "failed", limits_task.result()
                else:
                    status, reason = response_task.result()

        except asyncio.CancelledError:
            status, reason = "cancelled", "cancelled"
        except TimeoutError:
            status, reason = "timeout", "timeout"
        except AgentStartRejected:
            if spawn is None:
                raise
            status, reason = "failed", "provider_error"
        except ExecutionError:
            raise
        except Exception as exc:
            log.warning("print_cli.worker_failed", profile=profile.id, error_type=type(exc).__name__)
            status, reason = "failed", "provider_error"
        finally:
            if process is None and spawn is not None:
                while not spawn.done():
                    try:
                        await asyncio.shield(spawn)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if not spawn.cancelled() and spawn.exception() is None:
                    process = spawn.result()
            if acp is not None:
                pending = tasks[0] if tasks and not tasks[0].done() else None
                if pending is not None or status == "timeout":
                    await acp.client.cancel(pending)
                content = content or bytes(acp.turn.content)
                output_bytes = len(content)
                usage = acp.turn.usage
                session_id = acp.client.session_id
            for task in tasks:
                task.cancel()
            if stderr_task is not None and not stderr_task.done():
                stderr_task.cancel()
            if process is not None:
                if acp is not None:
                    await asyncio.gather(*tasks, return_exceptions=True)
                    shutdown = acp.client.close(grace_seconds=job.grace_seconds)
                else:
                    shutdown = _shutdown(process, job.grace_seconds)
                cleanup = asyncio.create_task(shutdown)
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        continue
                stop = cleanup.result()
                if profile.data_declaration is not None and process.pid:
                    from .provider_data_runtime import untrack_process

                    untrack_process(profile.data_declaration.id, process.pid)
                if stop == "confirmed" and on_runtime_stopped is not None:
                    try:
                        on_runtime_stopped()
                    except AgentStartRejected:
                        status, reason = "failed", "provider_error"
            else:
                stop = "unconfirmed" if spawn is not None else "confirmed"
            await asyncio.gather(*tasks, return_exceptions=True)
            if stderr_task is not None:
                await asyncio.gather(stderr_task, return_exceptions=True)
            if sem_acquired:
                sem.release()
            if skill_temp is not None:
                skill_temp.cleanup()

        with contextlib.suppress(Exception):
            await on_event(
                "provider_finished",
                {
                    "backend": "print_cli",
                    "profile": profile.id,
                    "status": status,
                    "reason": reason,
                    "stop_confirmation": stop,
                },
            )
        return NativeAgentResult(
            status=status,
            reason=reason,
            content=content,
            stop_confirmation=stop,
            usage=usage,
            provider_limit=provider_limit,
            provider_session_id=session_id,
            output_bytes=output_bytes,
            turns=turns,
            launch_outcome=(
                "started" if process is not None else "unknown" if spawn is not None else "not_started"
            ),
        )
