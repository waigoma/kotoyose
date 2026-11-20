"""Codex coding (host_native_coding_v1) を per-Run container の中で実行する。

container の中で Codex CLI を app-server として起動し、egress broker の UNIX socket は
bind mount された relay 経由で届く。停止の証明は次の二つだけに置く。
- container が存在しないこと (AgentContainer.remove が confirmed を返すこと)
- egress broker が閉じたこと
systemd scope や namespace の観測は使わない。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import tempfile
from dataclasses import replace
from pathlib import Path

from . import native_codex as _native
from .agent_spec import admitted_execution_cell, runtime_for
from .codex_coding_policy import (
    protected_workspace_entries, resolve_codex_sandbox, validate_coding_policy, validate_workspace,
)
from .codex_images import image_data_uris
from .codex_model_policy import model_catalog, validate_reasoning_effort
from .codex_subscription import AUTH_NAME, CAPABILITY_ENV
from .execution_resources import CodexEgressLimits
from .jail_place import (
    CONTAINER_HOME,
    CONTAINER_RELAY,
    CONTAINER_SOCKET,
    RELAY_PATH,
    WALL_MARGIN_SECONDS,
    JailPlace,
    build_connect_proxy_env,
    build_relay_exec_argv,
    create_egress_socket_dir,
    container_identity,
    container_job_id_for,
    protected_ro_dirs,
    reap_late_process,
    remove_container_confirmed,
)
from .native_claude import NativeAgentJob, NativeAgentResult
from .print_cli_egress import PrintCliEgress

_CONTAINER_HOME = CONTAINER_HOME
_CONTAINER_CODEX_HOME = "/home/agent/.codex"
_CONTAINER_SESSIONS = "/home/agent/.codex/sessions"
_CONTAINER_CODEX = "/opt/agent/bin/codex"
_CONTAINER_RELAY = CONTAINER_RELAY
_CONTAINER_SOCKET = CONTAINER_SOCKET
# Re-measure these exact hosts on a real run, including token refresh.
_CODEX_CONNECT_HOSTS = ("chatgpt.com", "auth.openai.com")
# The CLI opens several chatgpt.com connections at once at start-up (measured 2026-11-18:
# with 4 the 5th was refused and the run failed in 14 s). Run-wide totals stay pinned by
# the caller's egress limits.
_CODEX_MAX_TUNNELS = 16
_CONTAINER_CATALOG = "/run/egress/models.json"
# container 作成から終了までの余白。CLI の停止と container の回収に使う。
_WALL_MARGIN_SECONDS = WALL_MARGIN_SECONDS
_RELAY_PATH = RELAY_PATH
_remove_container = remove_container_confirmed


def _is_personal_codex_home(path: Path) -> bool:
    """The user's everyday Codex home (``~/.codex``), which must never be bound into a jail."""
    try:
        return path.resolve() == (Path.home() / ".codex").resolve()
    except OSError:
        return True
_reap_late_process = reap_late_process


def _container_job_id(job: NativeAgentJob) -> str:
    return container_job_id_for(job.attempt_id, job.generation)


def _validate_job(worker, job: NativeAgentJob, settings) -> list[dict]:
    """部屋の入力を launch 前に検証し、保護する workspace entry を返す。"""
    cell = admitted_execution_cell("codex", job.codex_execution_mode)
    if cell is None or runtime_for(cell.surface) != "room":
        raise ValueError("invalid_job")
    if job.codex_egress_limits is not None:
        CodexEgressLimits.model_validate(job.codex_egress_limits)
    resolve_codex_sandbox(job.codex_execution_mode, job.codex_sandbox)
    subscription = job.credential_name == AUTH_NAME
    if not subscription or set(job.tool_refs) != set(cell.tool_refs):
        raise ValueError("invalid_job")
    external = validate_coding_policy(
        job.execution_environment, job.codex_sandbox_policy, job.codex_tool_policy_digest,
        mode=job.codex_execution_mode,
    )
    validate_workspace(job.workspace, device=job.workspace_device, inode=job.workspace_inode)
    if cell.protected_entries_required and external and job.codex_protected_workspace is None:
        raise ValueError("invalid_job")
    # 存在する entry だけを pin 検証して read-only にする。無い entry は mount せず job も失敗させない。
    protected = protected_workspace_entries(job.workspace, job.codex_protected_workspace, optional=True) if (
        job.codex_protected_workspace is not None) else protected_workspace_entries(job.workspace, optional=True)
    if (job.credential_value or job.codex_auth_file is None or not job.codex_auth_file.is_absolute()
            or job.codex_auth_file.name != "auth.json" or not job.codex_auth_identity
            or job.base_url is not None or job.client_tool_specs or job.approval_bindings
            or (job.resume_mode == "native" or job.provider_session_ref is not None) and worker.sessions is None
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
    if _is_personal_codex_home(job.codex_auth_file.parent):
        # jail には CODEX_HOME ごと bind する。普段の ~/.codex には会話履歴・設定・AGENTS.md があり、
        # モデルの道具から読めてしまう。Kotoyose 専用の login home (別 login) だけを受け付ける。
        raise ValueError("personal_codex_home")
    if job.reasoning_effort is not None:
        validate_reasoning_effort(job.model, job.reasoning_effort)
    return protected


async def execute_container_coding(
    worker,
    job: NativeAgentJob,
    cancel,
    execute_tool,
    *,
    on_process_started,
    container,
    settings,
    image_id: str,
) -> NativeAgentResult:
    """coding job を container で実行し、停止を確認できた結果だけを confirmed にする。

    container / settings / image_id のどれかが無いときは何も作らず invalid_job で返す。
    """
    usage = {"source": "unknown", "cost": None, "input_tokens": None, "output_tokens": None}
    content = b""
    messages = {}
    process = None
    operation = stderr = cancellation = startup_cancel = launch_task = None
    egress = None
    home = socket_dir = None
    rpc = None
    launch_attempted = False
    confirmation = "confirmed"
    egress_confirmation = "confirmed"
    egress_closed = False
    container_confirmation = "confirmed"
    process_exited = True
    remove_task = None
    status, reason = "failed", "invalid_job"
    prestart_rejection = None
    diagnostic = None
    provider_limit = None
    tool_idle = job.tool_stop_idle or asyncio.Event()
    tool_idle.set()
    job_id = _container_job_id(job)
    place = JailPlace(container, settings, image_id)
    loop = asyncio.get_running_loop()

    async def tracked_tool(argv, cwd):
        tool_idle.clear()
        try:
            return await execute_tool(argv, cwd)
        finally:
            if job.tool_stop_idle is None:
                tool_idle.set()

    try:
        if container is None or settings is None or image_id is None:
            raise ValueError("invalid_job")
        protected = _validate_job(worker, job, settings)
        deadline = _native._resolve_deadline(job)
        owner = asyncio.current_task()

        async def cancel_owner():
            await cancel.wait()
            owner.cancel()

        startup_cancel = asyncio.create_task(cancel_owner())
        image_uris = image_data_uris(job.image_inputs)
        config = _native._codex_config(job, subscription=False)
        config.pop("model_providers.kotoyose_native")
        config["model_provider"] = "openai"
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError
        limits = (CodexEgressLimits.model_validate(job.codex_egress_limits)
                  if job.codex_egress_limits is not None else None)
        byte_limit = limits.total_bytes if limits else max(1048576, job.output_limit_bytes * 8)
        connection_limit = limits.total_connections if limits else 32
        socket_dir = create_egress_socket_dir(settings.egress_socket_root)
        egress = PrintCliEgress(
            socket_dir / "egress.sock",
            allowed_hosts=_CODEX_CONNECT_HOSTS,
            allowed_ports=(443,),
            run_id=job.attempt_id,
            max_tunnels=_CODEX_MAX_TUNNELS,
            max_total_tunnels=None if connection_limit == "disabled" else connection_limit,
            max_total_bytes=None if byte_limit == "disabled" else byte_limit,
            wall_timeout_seconds=remaining,
        )
        async with asyncio.timeout_at(deadline):
            await egress.start()
        catalog = socket_dir / "models.json"
        catalog.write_text(json.dumps(model_catalog(
            job.model, images=bool(image_uris), reasoning_effort=job.reasoning_effort, coding=True)))
        catalog.chmod(0o600)

        image_env = await container.image_env(image_id)
        shell_env = {key: value for key, value in image_env.items() if key != CAPABILITY_ENV}
        shell_env.update({"HOME": _CONTAINER_HOME, "LANG": "C.UTF-8"})
        config.update({"features.shell_tool": True, "features.unified_exec": True,
                       "shell_environment_policy.inherit": "none",
                       "shell_environment_policy.set": shell_env,
                       "sandbox_workspace_write.network_access": False})

        home = Path(tempfile.mkdtemp(prefix="kotoyose-agent-home-", dir=settings.home_root))
        rw_dirs = {_CONTAINER_CODEX_HOME: job.codex_auth_file.parent}
        env = build_connect_proxy_env({"CODEX_HOME": _CONTAINER_CODEX_HOME})
        if worker.sessions is not None and job.session_scope is not None:
            session_dir = worker.sessions.directory(job.session_scope)
            rw_dirs[_CONTAINER_SESSIONS] = session_dir
        ro_dirs = protected_ro_dirs(protected, job.workspace)
        cell = admitted_execution_cell("codex", job.codex_execution_mode)
        spec = place.build_spec(
            job_id=job_id,
            workspace=job.workspace,
            home=home,
            egress_dir=socket_dir,
            timeout_seconds=job.timeout_seconds,
            grace_seconds=job.grace_seconds,
            env=env,
            ro_files={_CONTAINER_CODEX: Path(job.cli_binary).resolve(strict=True), _CONTAINER_RELAY: _RELAY_PATH},
            rw_dirs=rw_dirs,
            ro_dirs=ro_dirs,
            workspace_readonly=bool(cell and cell.workspace_readonly),
        )
        argv = build_relay_exec_argv(
            _CONTAINER_CODEX,
            [
                "app-server",
                "--listen",
                "stdio://",
                "--config",
                f'model_catalog_json="{_CONTAINER_CATALOG}"',
                "--config", 'model_provider="openai"',
                "--config", 'cli_auth_credentials_store="file"',
                "--config", f"model={json.dumps(job.model, ensure_ascii=False)}",
                "--config", 'sandbox_mode="read-only"',
                "--config", 'approval_policy="never"',
                *_native._bool_config_args(config),
            ],
        )
        stream_limit = job.output_limit_bytes + _native._PROTOCOL_OVERHEAD + sum(len(uri.encode("ascii")) for uri in image_uris)
        # container の外で cell の sandbox pin を適用する。image に bubblewrap が無いため SDK 内の sandbox は使わない。
        # read と write で pin が違うので、ここで write の digest に揃え直さない。
        if cell is None or cell.sandbox_policy is None or cell.isolated_tool_policy_digest is None:
            raise ValueError("invalid_job")
        run_job = replace(job, execution_environment="isolated", codex_sandbox_policy=cell.sandbox_policy,
                          codex_tool_policy_digest=cell.isolated_tool_policy_digest)
        if job.validate_start is not None:
            await job.validate_start()
        if cancel.is_set():
            raise asyncio.CancelledError
        if loop.time() >= deadline:
            raise TimeoutError
        launch_attempted = True
        async with asyncio.timeout_at(deadline):
            container_id = await place.start_container(spec)
            launch_task = asyncio.create_task(container.exec(
                job_id, argv, env=env,
                limit=stream_limit,
            ))
            process = await asyncio.shield(launch_task)
        if cancel.is_set():
            raise asyncio.CancelledError
        if loop.time() >= deadline:
            raise TimeoutError
        async with asyncio.timeout_at(deadline) as runtime_timeout:
            await on_process_started(process.pid, container_identity=container_identity(container, job_id, container_id))
            if job.work_remaining is not None:
                deadline = loop.time() + job.work_remaining()
                runtime_timeout.reschedule(deadline)
            rpc = _native._Protocol(process, job.output_limit_bytes + _native._PROTOCOL_OVERHEAD, image_uris, job.prompt)
            stderr = asyncio.create_task(rpc.drain_stderr())
            operation = asyncio.create_task(worker._run(
                rpc, run_job, cancel, tracked_tool if job.on_runtime_stopped else execute_tool, usage, messages, config))
            cancellation = asyncio.create_task(cancel.wait())
            done, _ = await asyncio.wait({operation, cancellation, stderr},
                                         return_when=asyncio.FIRST_COMPLETED)
            if cancellation in done and cancel.is_set():
                status, reason = "cancelled", "cancelled"
            elif stderr in done:
                stderr.result()
                raise ValueError("missing_completion")
            else:
                content = operation.result()
                status, reason = "succeeded", "completed"
    except TimeoutError:
        outcome = await _native._timeout_outcome(job, process, launch_attempted)
        status, reason, prestart_rejection = outcome.status, outcome.reason, outcome.prestart_rejection
    except asyncio.CancelledError:
        status, reason = "cancelled", "cancelled"
    except Exception as exc:
        outcome = _native._exception_outcome(job, exc, process, launch_attempted)
        status, reason, prestart_rejection = outcome.status, outcome.reason, outcome.prestart_rejection
        diagnostic, provider_limit = outcome.diagnostic, outcome.provider_limit
    finally:
        # container の remove は協調的な task の後始末を待たずに始める。provider の効果は container 内にある。
        if startup_cancel is not None:
            startup_cancel.cancel()
        if launch_task is not None and not launch_task.done():
            launch_task.cancel()
            launch_task.add_done_callback(_reap_late_process)
        if launch_attempted:
            remove_task = asyncio.create_task(place.remove_container(job_id, 0))
        tasks = [task for task in (operation, cancellation, stderr) if task is not None]
        for task in tasks:
            task.cancel()
        pending = set()
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=job.grace_seconds)
            for task in tasks:
                if task.done():
                    with contextlib.suppress(BaseException):
                        task.result()
        if egress is not None:
            try:
                await egress.close()
                egress_closed = True
            except Exception:
                egress_confirmation = "unconfirmed"
        if remove_task is not None:
            container_confirmation = await remove_task
        if process is not None:
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
            drains = [asyncio.create_task(_native._discard_stream(reader)) for reader, owner in (
                (process.stdout, operation), (process.stderr, stderr),
            ) if reader is not None and owner not in pending]
            try:
                await asyncio.wait_for(process.wait(), timeout=max(0.1, job.grace_seconds))
            except Exception:
                process_exited = False
            finally:
                for drain in drains:
                    drain.cancel()
                await asyncio.gather(*drains, return_exceptions=True)
        confirmation = "confirmed" if (
            container_confirmation == "confirmed" and egress_confirmation == "confirmed"
            and process_exited and not pending
        ) else "unconfirmed"
        if confirmation == "confirmed" and job.on_runtime_stopped is not None and tool_idle.is_set():
            with contextlib.suppress(Exception):
                job.on_runtime_stopped()
        # 停止を確認できなかった container は書き込みを続けうるため、home も残して回収に任せる。
        JailPlace.cleanup_run_dirs(
            container_confirmation=container_confirmation,
            egress_closed=egress_closed,
            temp_home=home,
            socket_dir=socket_dir,
        )
    return await worker._finish(
        job, cancel, status=status, reason=reason, content=content, messages=messages,
        confirmation=confirmation, usage=usage, egress=None, rpc=rpc, diagnostic=diagnostic,
        provider_limit=provider_limit, prestart_rejection=prestart_rejection,
        launch_attempted=launch_attempted, started=process is not None,
    )
