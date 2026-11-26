"""Isolated per-Run container launcher for print-mode (`-p`) CLIs.

Runs the unmodified official CLI binary inside an `AgentContainer` with no
network (`--network=none`), mounting the connection's login home read-write at
`/home/agent` without reading or modifying it, and routing HTTPS/HTTP traffic
through `egress_relay.py` -> host `PrintCliEgress` (plain CONNECT allowlist).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import contextlib
from pathlib import Path
import tempfile
from typing import Any

import structlog

from .acp_client import LINE_LIMIT
from .acp_execution import AcpExecution
from .agent_container import CONTAINER_ALIAS_SKILLS, CONTAINER_RUNTIME_BUNDLE, AgentContainerSpec
from .agent_deadlines import AgentStartRejected
from .codex_coding_policy import protected_workspace_entries, validate_workspace
from .execution_resources import ExecutionError
from .grok_skills import grok_skill_env, uses_grok_native_skills
from .jail_place import (
    CONTAINER_HOME,
    CONTAINER_RELAY,
    CONTAINER_SOCKET,
    CONTAINER_WORKSPACE,
    PROXY_URL,
    RELAY_PATH,
    WALL_MARGIN_SECONDS,
    JailPlace,
    bounded_limit,
    build_connect_proxy_env,
    build_relay_exec_argv,
    container_identity,
    container_job_id_for,
    create_egress_socket_dir,
    protected_ro_dirs,
    reap_late_process,
    remove_container_confirmed,
)
from .host_place import _shutdown, _watch_limits, _watch_work_timeout
from .native_claude import NativeAgentResult
from .print_cli_agent import (
    PrintCliJob,
    classify_print_cli_outcome,
    compose_print_cli_prompt,
    read_print_cli_stdout_events,
)
from .print_cli_egress import PrintCliEgress
from .print_cli_events import read_stderr_tail
from .print_cli_profiles import PrintCliProfile, build_print_cli_env, get_print_cli_profile

log = structlog.get_logger()

_CONTAINER_HOME = CONTAINER_HOME
_CONTAINER_RELAY = CONTAINER_RELAY
_CONTAINER_SOCKET = CONTAINER_SOCKET
_RELAY_PATH = RELAY_PATH
_PROXY_URL = PROXY_URL
_WALL_MARGIN_SECONDS = WALL_MARGIN_SECONDS
_STREAM_READER_MIN_LIMIT = 65536
_socket_dir = create_egress_socket_dir
_bounded_limit = bounded_limit
_remove_container = remove_container_confirmed
_reap_late_process = reap_late_process


def _validate_login_home(login_home: Path | str) -> Path:
    path = Path(login_home)
    if path.is_symlink() or not path.exists() or not path.is_dir():
        raise ValueError("Print CLI login_home must be an existing non-symlink directory")
    return path


def build_print_cli_container_plan(
    job: PrintCliJob,
    profile: PrintCliProfile,
    settings,
    *,
    image_id: str | None,
    egress_dir: Path,
    settings_dir: Path,
    print_timeout_seconds: float,
    job_id: str,
    skill_root: Path | None = None,
    grok_home: Path | None = None,
) -> tuple[AgentContainerSpec, list[str], dict[str, str], list[str]]:
    """Build container spec, exec argv, exec env, and inner CLI args.

    Pure separation lets unit tests inspect the exact container launch contract
    (mounts, env, network=none, argv) without starting Docker.
    """
    if settings is None or not image_id:
        raise ValueError("Container settings and image_id are required for isolated print CLI")
    validate_workspace(job.workspace, device=job.workspace_device, inode=job.workspace_inode)
    protected = protected_workspace_entries(job.workspace, optional=True)
    login_home = _validate_login_home(job.login_home)
    cli_binary = Path(job.cli_binary).resolve(strict=True)

    ro_files = {_CONTAINER_RELAY: _RELAY_PATH}
    ro_dirs = protected_ro_dirs(protected, job.workspace)
    if skill_root is not None:
        ro_dirs[CONTAINER_ALIAS_SKILLS] = skill_root
    bundle = profile.isolated_bundle_root(cli_binary)
    if bundle is None:
        container_cli = f"/opt/agent/bin/{profile.binary}"
        ro_files[container_cli] = cli_binary
    else:
        container_cli = f"{CONTAINER_RUNTIME_BUNDLE}/{cli_binary.name}"
        ro_dirs[CONTAINER_RUNTIME_BUNDLE] = bundle
    env = build_print_cli_env(profile, _CONTAINER_HOME)
    if grok_home is not None:
        ro_dirs[CONTAINER_ALIAS_SKILLS] = grok_home
        grok_skill_env(env, Path(CONTAINER_ALIAS_SKILLS), job.repository_skills, profile)
    if job.acp:
        if profile.acp is None:
            raise ValueError(f"Profile {profile.id} does not declare ACP")
        cli_args = list(profile.acp.arguments(job.model))
        env.update(profile.acp.env)
    else:
        host_settings = profile.write_settings_file(job.surface, settings_dir)
        container_settings: Path | None = None
        if host_settings is not None:
            container_settings = Path(f"/opt/agent/{host_settings.name}")
            ro_files[str(container_settings)] = host_settings
        cli_args = profile.render_argv(
            model=job.model,
            prompt=compose_print_cli_prompt(
                job.instructions, job.prompt, output_contract=job.output_contract, output_schema=job.output_schema,
            ),
            timeout_seconds=print_timeout_seconds,
            surface=job.surface,
            settings_path=container_settings,
            add_dirs=job.add_dirs,
        )

    exec_env = build_connect_proxy_env(env)
    # AgentContainer always binds HOME=/home/agent at create time and rejects
    # passing HOME in spec.env; exec_env carries the full child environment.
    spec_env = {k: v for k, v in exec_env.items() if k != "HOME"}

    spec = JailPlace(None, settings, image_id).build_spec(
        job_id=job_id,
        workspace=job.workspace,
        home=login_home,
        egress_dir=egress_dir,
        timeout_seconds=job.timeout_seconds,
        grace_seconds=job.grace_seconds,
        env=spec_env,
        ro_files=ro_files,
        ro_dirs=ro_dirs,
        workspace_readonly=job.surface == "read",
        memory_bytes=job.memory_bytes,
        cpus=job.cpus,
        pids_limit=job.pids_limit,
    )
    argv = build_relay_exec_argv(container_cli, cli_args)
    return spec, argv, exec_env, cli_args


async def execute_container_print(
    job: PrintCliJob,
    cancel: asyncio.Event,
    *,
    on_process_started: Callable[..., Awaitable[None]],
    on_event: Callable[[str, dict[str, Any]], Awaitable[None]],
    on_runtime_stopped: Callable[[], None] | None = None,
    container,
    settings,
    image_id: str | None,
) -> NativeAgentResult:
    """Run a print-CLI job inside an isolated per-Run container."""
    profile = get_print_cli_profile(job.profile_id)
    usage: dict[str, Any] = {
        "source": "unknown",
        "cost": None,
        "input_tokens": None,
        "output_tokens": None,
    }
    content = b""
    output_bytes = 0
    turns: int | None = None
    provider_limit = None
    status = "failed"
    reason = "provider_error"
    stop = "confirmed"

    loop = asyncio.get_running_loop()
    deadline = loop.time() + job.timeout_seconds
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
    remaining_now = job.work_remaining() if job.work_remaining is not None else (deadline - loop.time())
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

    settings_temp = tempfile.TemporaryDirectory(prefix="kotoyose-print-cfg-", ignore_cleanup_errors=True)
    settings_dir = Path(settings_temp.name)
    socket_dir = _socket_dir(settings.egress_socket_root)
    job_id = container_job_id_for(job.attempt_id, job.generation)
    place = JailPlace(container, settings, image_id)
    egress: PrintCliEgress | None = None
    egress_closed = False
    process: asyncio.subprocess.Process | None = None
    launch_task: asyncio.Task[asyncio.subprocess.Process] | None = None
    stderr_task: asyncio.Task[str] | None = None
    tasks: list[asyncio.Task[Any]] = []
    launch_attempted = False
    container_id: str | None = None
    container_confirmation = "confirmed"
    process_exited = True
    acp: AcpExecution | None = None

    try:
        skill_root = None
        grok_home = None
        if uses_grok_native_skills(profile, Path(job.cli_binary)):
            grok_home = settings_dir / "grok-session-home"
            grok_home.mkdir(mode=0o700)
            job = job.place_alias_skills(grok_home, profile, layout="grok")
        elif job.repository_skills is not None:
            raise AgentStartRejected(
                "unsupported_capability", "repository_skills requires the pinned Linux x64 Grok 1.0.89 binary", 422,
            )
        elif job.alias_skills is not None:
            staged = settings_dir / "skills"
            job = job.place_alias_skills(staged, profile, visible_root=Path(CONTAINER_ALIAS_SKILLS))
            if staged.is_dir():
                skill_root = staged
        print_timeout_seconds = max(0.1, remaining_now * 0.85)
        spec, argv, exec_env, _ = build_print_cli_container_plan(
            job,
            profile,
            settings,
            image_id=image_id,
            egress_dir=socket_dir,
            settings_dir=settings_dir,
            print_timeout_seconds=print_timeout_seconds,
            job_id=job_id,
            skill_root=skill_root,
            grok_home=grok_home,
        )
        egress = PrintCliEgress(
            socket_dir / "egress.sock",
            allowed_hosts=profile.egress_hosts,
            allowed_ports=profile.egress_ports,
            run_id=job.attempt_id,
            idle_timeout_seconds=profile.egress_idle_seconds,
        )
        await egress.start()

        if job.validate_start is not None:
            await job.validate_start()
        if cancel.is_set():
            raise asyncio.CancelledError
        if loop.time() >= deadline:
            raise TimeoutError

        stream_limit = LINE_LIMIT if job.acp else max(_STREAM_READER_MIN_LIMIT, job.output_limit_bytes * 6 + 65536)
        launch_attempted = True
        async with asyncio.timeout_at(deadline):
            container_id = await place.start_container(spec)
            launch_task = asyncio.create_task(
                container.exec(job_id, argv, env=exec_env, limit=stream_limit)
            )
            process = await asyncio.shield(launch_task)

        assert process.stdout is not None
        assert process.stderr is not None
        stderr_task = asyncio.create_task(read_stderr_tail(process.stderr, max_bytes=32768))

        if job.acp:
            acp = AcpExecution(process, job, profile, on_event, cwd=CONTAINER_WORKSPACE)

        async def run_stream() -> tuple[str, str]:
            nonlocal content, output_bytes, turns, usage, provider_limit
            await on_process_started(
                process.pid,
                container_identity=container_identity(container, job_id, container_id),
            )
            if cancel.is_set():
                raise asyncio.CancelledError
            if (
                job.work_remaining() <= 0
                if job.work_remaining is not None
                else loop.time() >= deadline
            ):
                raise TimeoutError

            await on_event(
                "provider_started",
                {
                    "backend": "print_cli",
                    "profile": profile.id,
                    "pid": process.pid,
                    "isolated": True,
                },
            )
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
            )
            if line_overflow:
                usage = dict(outcome.usage)
                turns = outcome.turns
                encoded = outcome.content.encode("utf-8")[: job.output_limit_bytes]
                content = encoded
                output_bytes = len(encoded)
                return "failed", "output_limit_exceeded"

            returncode = await process.wait()
            stderr_tail = await stderr_task
            usage = dict(outcome.usage)
            turns = outcome.turns
            status_out, reason_out, content, output_bytes, provider_limit = classify_print_cli_outcome(
                job,
                profile,
                outcome,
                stderr_tail,
                returncode,
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
            else max(0.0, deadline - loop.time())
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
        if not launch_attempted:
            raise
        status, reason = "failed", "provider_error"
    except ExecutionError:
        raise
    except Exception as exc:
        log.warning("container_print.failed", profile=profile.id, error_type=type(exc).__name__)
        status, reason = "failed", "provider_error"
    finally:
        if launch_task is not None and not launch_task.done():
            launch_task.cancel()
            launch_task.add_done_callback(_reap_late_process)
        if acp is not None:
            pending = tasks[0] if tasks and not tasks[0].done() else None
            if pending is not None or status == "timeout":
                await acp.client.cancel(pending)
            content = content or bytes(acp.turn.content)
            output_bytes = len(content)
            usage = acp.turn.usage
        remove_task: asyncio.Task[str] | None = None
        if launch_attempted:
            remove_task = asyncio.create_task(place.remove_container(job_id, 0))
        for task in tasks:
            task.cancel()
        if stderr_task is not None and not stderr_task.done():
            stderr_task.cancel()
        if egress is not None:
            with contextlib.suppress(Exception):
                await egress.close()
            egress_closed = True
        if remove_task is not None:
            container_confirmation = await remove_task
        if process is not None:
            if acp is not None:
                await asyncio.gather(*tasks, return_exceptions=True)
                proc_stop = await acp.client.close(grace_seconds=job.grace_seconds)
            else:
                proc_stop = await _shutdown(process, job.grace_seconds)
            if proc_stop != "confirmed":
                process_exited = False
        elif launch_attempted and container_confirmation != "confirmed":
            process_exited = False

        stop = "confirmed" if (container_confirmation == "confirmed" and process_exited) else "unconfirmed"
        await asyncio.gather(*tasks, return_exceptions=True)
        if stderr_task is not None:
            await asyncio.gather(stderr_task, return_exceptions=True)
        # Never remove job.login_home (spec.home): it is the connection's persistent home.
        settings_temp.cleanup()
        JailPlace.cleanup_run_dirs(
            container_confirmation=container_confirmation,
            egress_closed=(egress is None or egress_closed),
            temp_home=None,
            socket_dir=socket_dir,
        )
        if stop == "confirmed" and on_runtime_stopped is not None:
            try:
                on_runtime_stopped()
            except AgentStartRejected:
                status, reason = "failed", "provider_error"

    with contextlib.suppress(Exception):
        await on_event(
            "provider_finished",
            {
                "backend": "print_cli",
                "profile": profile.id,
                "status": status,
                "reason": reason,
                "stop_confirmation": stop,
                "isolated": True,
            },
        )
    return NativeAgentResult(
        status=status,
        reason=reason,
        content=content,
        stop_confirmation=stop,
        usage=usage,
        provider_limit=provider_limit,
        provider_session_id=acp.client.session_id if acp is not None else None,
        output_bytes=output_bytes,
        turns=turns,
        launch_outcome=(
            "started" if process is not None else "unknown" if launch_attempted else "not_started"
        ),
    )
