"""Native dispatch の調停。永続台帳と実 worker の間で停止確認を失わない。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import json
from importlib.metadata import version
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from dataclasses import asdict

import asyncpg
import structlog

from .capabilities import CapabilitySupport, capability_manifest, safe_resume_capability
from .command_worker import CommandWorker
from .coordinator_lease import CoordinatorLease
from .execution_shutdown import CloseTiming, NORMAL_CLOSE_TIMING
from .execution_replay import admission_closed_error, replay_execution
from .agent_execution_service import AgentExecutionService
from .execution_admission import ExecutionAdmission, workspace_path
from .workspace_gc import WorkspaceCollector
from .execution_artifacts import ArtifactCaptureSpec, CapturedArtifact, collect_execution_artifacts, read_artifact_file
from .artifact_blob_store import ArtifactStorageError
from .execution_agent import AgentExecutor, RuntimeEntry
from .agent_interactions import AgentInteraction
from .execution_contract import ExecutionRequest, ResumeRequest, RuntimeAuthenticationResolution
from .execution_resume import resume_request_digest, unsafe
from .runtime.resume import ResumeAdapter, ResumeSupport
from .execution_resources import (
    AgentDefinition, CallerPolicy, ClientToolSpec, ExecutionError, InputText, NativeExecutionConfig,
    PinnedContentResource, RepositoryContextPack, Resource, WorkspaceRegistration, content_digest,
    agent_model_allowed, agent_profile_allowed, resolve_agent_model, codex_authentication_binding, codex_profile_authentication,
)
from .execution_store import ExecutionStore
from .client_tool_bridge import ClientToolCallOutcome
from .delegation import redact_token
from .execution_delegation import (
    ASK_AGENT, LIST_AGENTS, MAX_TASK_BYTES, TOOL_SPECS, DelegationCalls, DelegationProgress,
    bind_child_progress, targets, unbind_child_progress,
)
from .execution_output_guard import check_artifacts, check_workspace, workspace_state
from .output_guard import LEAK_MESSAGE, LEAK_REASON, guard_enabled
from .agent_spec import HOST_CODING
from .execution_jobs import CommandJob
from .local_worker import DockerLocalWorker, _expected_container_identity
from .process_identity import ProcessIdentity, stop_identified_process
from .remote_worker_client import RemoteWorkerClient
from .remote_worker_journal import RemoteWorkerJournal
from .mcp_contract import McpBindingResource

# Native Claude adapter の conformance 試験を通した SDK。pyproject.toml / uv.lock の
# claude-agent-sdk もこの版に完全固定する (tests/test_native_sdk_pin.py が照合する)。
# 上げるときは Native の conformance を取り直してから、三か所を同時に変える。
NATIVE_CLAUDE_SDK_VERSION = "0.2.185"

log = structlog.get_logger()

_IMPLEMENTED = (
    "native_execution", "command_execution", "durable_attempts", "dispatch_idempotency",
    "durable_events", "confirmed_cancel", "artifact_access", "workspace_refs", "workspace_checkpoints",
    "execution_grants", "sandbox", "credential_isolation", "repository_context_pack", "skill_resources",
    "memory_bootstrap", "client_tools", "agent_execution_v1",
)



def _raise_site(exc: BaseException) -> str | None:
    """例外が起きた一番内側の frame を、本文を含めずに module:line で返す。"""
    tb = exc.__traceback__
    if tb is None:
        return None
    while tb.tb_next is not None:
        tb = tb.tb_next
    return f"{tb.tb_frame.f_globals.get('__name__', '?')}:{tb.tb_lineno}"

class ExecutionService:
    """LocalWorker の単一 coordinator。別プロセスの二重起動は DB lease で拒む。"""

    def __init__(self, config: NativeExecutionConfig, store: ExecutionStore, worker: CommandWorker):
        self.config = config
        self.store = store
        self.store.artifact_minimum_free_bytes = (
            config.artifact_disk_limits.minimum_free_bytes if config.artifact_disk_limits else 0
        )
        self.store.artifact_retention.limits = config.output_artifact_limits
        self.store.workspace_retention.limits = config.workspace_limits
        if config.agent_container is not None:
            from .agent_container import AgentRoomCapacity
            self.store.agent_room_capacity = AgentRoomCapacity(config.agent_container)
        self.worker = worker
        self.workers: dict[str, CommandWorker] = {"local-docker": worker}
        self._remote_journals: dict[str, RemoteWorkerJournal] = {}
        for worker_id, capability in config.worker_capabilities.items():
            if capability.workspace_locality == "remote" and capability.remote_connection is None:
                raise ValueError(f"Remote worker {worker_id} has no durable connection")
        self.manifest = capability_manifest()
        for name in _IMPLEMENTED:
            self.manifest.capabilities[name] = CapabilitySupport(
                status="supported", reason="Enabled Native command API with Postgres and isolated LocalWorker.",
            )
        self.manifest.capabilities["artifact_retention"] = CapabilitySupport(
            status="supported" if config.output_artifact_limits.gc_interval_seconds is not None else "conditional",
            reason="Caller retention CAS and durable GC; unresolved runtime effects always retain bytes.",
            conditions=() if config.output_artifact_limits.gc_interval_seconds is not None else (
                "Administrator must configure output_artifact_limits.gc_interval_seconds to run GC.",
            ),
        )
        if config.mcp_servers:
            self.manifest.capabilities["external_mcp"] = CapabilitySupport(
                status="supported", reason="Caller-scoped pinned Streamable HTTP tools with remote, administrator and Caller narrowing validation; unknown effects require evidence reconciliation.",
            )
            self.manifest.capabilities["memory_bootstrap"] = CapabilitySupport(
                status="supported",
                reason="Pinned Memory snapshot data injection and revocation epoch matching; Memory tools can be enforced through configured external_mcp with Caller narrowing.",
            )
        else:
            self.manifest.capabilities["memory_bootstrap"] = CapabilitySupport(
                status="supported",
                reason="Pinned Memory snapshot data injection and revocation epoch matching; Memory tool enforcement requires configured external_mcp and Caller narrowing.",
            )
        from .config import get_config

        native_alias_available = any(
            self._native_alias_available(alias) for alias in get_config().models
        )
        if native_alias_available:
            self.manifest.capabilities['runtime_managed_compression'] = CapabilitySupport(
                status='supported', reason='Pinned profiles permit provider runtime compaction; Kotoyose does not insert summaries.',
            )
            self.manifest.capabilities['context_observation'] = CapabilitySupport(
                status='conditional', reason='Only runtime-reported boundaries and bounded context samples are recorded; absence is not zero.',
                conditions=('The pinned CLI must implement compact_boundary and get_context_usage; observation coverage remains partial.',),
            )
        self.manifest.capabilities['workspace_retention'] = CapabilitySupport(
            status='supported' if config.workspace_limits.gc_interval_seconds is not None and shutil.rmtree.avoids_symlink_attacks else 'conditional',
            reason='Caller generation-bound retention decisions and confirmed filesystem deletion; enable workspace_limits.gc_interval_seconds for scheduling.',
            conditions=('Filesystem deletion requires fd-safe rmtree support.',),
        )
        self.manifest.capabilities['workspace_checkpoints'] = CapabilitySupport(
            status='supported', reason='Stopped generations can capture explicit regular-file manifests and restore fresh workspaces; Session and Git control state are excluded.',
        )
        self.admission = ExecutionAdmission(
            store, self.manifest, config.image_id,
            {key: capability for key, capability in config.worker_capabilities.items()
             if key in self.workers or key == "local-print-cli" or capability.workspace_locality == "remote"},
            config.mcp_servers, config,
        )
        self.agent_executor = AgentExecutor(config, store, worker)
        self.resume_support()
        self._tasks: dict[str, asyncio.Task] = {}
        # 実行中 attempt の container job ID。起動時の掃除で、生きている container を残すために使う。
        self._task_jobs: dict[str, str] = {}
        self._cancels: dict[str, asyncio.Event] = {}
        self._coordinator: CoordinatorLease | None = None
        self._close_task: asyncio.Task | None = None
        self._cleanup_timed_out = False
        self._input_artifact_gc: asyncio.Task | None = None
        self._output_artifact_gc: asyncio.Task | None = None
        self._workspace_gc: asyncio.Task | None = None
        self._container_gc: asyncio.Task | None = None
        self._container_cursor = 0
        self._accepting = False
        self.agent_executions = AgentExecutionService(self)

    @property
    def _lease(self) -> asyncpg.Connection | None:
        return self._coordinator.connection if self._coordinator is not None else None

    @property
    def _watchdog(self) -> asyncio.Task | None:
        return self._coordinator.watchdog if self._coordinator is not None else None

    def readiness_reason(self) -> str | None:
        """検証済み lease・受付・監視・heartbeat 鮮度から不成立理由を返す。DB 操作はしない。"""
        if self._coordinator is None:
            return "native_execution_disabled" if getattr(self.store, "pool", None) is None else "coordinator_starting"
        return self._coordinator.readiness_reason(self._accepting)

    def _close_admission(self) -> None:
        self._accepting = False
        for cancel in self._cancels.values():
            cancel.set()

    def _require_accepting(self) -> None:
        if not self._accepting:
            raise admission_closed_error(self._coordinator)

    def _native_alias_available(self, alias: str) -> bool:
        try:
            return resolve_agent_model(alias, self.config, resolve_prompts=False).backend == "native"
        except (OSError, ValueError):
            return False

    async def start(self) -> None:
        """永続化と worker の準備・旧処理照合を完了してから受付を開始する。"""
        self.config.validate_client_resources()
        native_profiles = []
        from .provider_jail import build_jail_command
        from .config import get_clients, get_config
        clients = get_clients()
        config = get_config()
        for alias, preset in config.models.items():
            if preset.agent is None or not any(agent_model_allowed(alias, policy) for policy in clients.values()):
                continue
            try:
                resolved = resolve_agent_model(alias, self.config, config=config, resolve_prompts=False)
            except (OSError, ValueError) as exc:
                log.error("native.agent.unresolvable", alias=alias, reason=str(exc))
                continue
            if resolved.backend != "native":
                continue
            if getattr(resolved, "provider", "claude") == "codex":
                from .provider_jail import _trusted_binary
                from .codex_rootfs import validate_rootfs
                from .agent_spec import rootfs_digest_is_coding
                _trusted_binary(str(resolved.cli_binary))
                validate_rootfs(
                    resolved.codex_rootfs, resolved.codex_rootfs_digest,
                    coding=rootfs_digest_is_coding(resolved.provider, resolved.codex_execution_mode),
                )
            else:
                build_jail_command(resolved.cli_binary, [])
            native_profiles.append(resolved)
        if any(getattr(profile, "provider", "claude") == "claude" for profile in native_profiles):
            if version("claude-agent-sdk") != NATIVE_CLAUDE_SDK_VERSION:
                raise RuntimeError(
                    f"Native Claude adapter requires the pinned SDK {NATIVE_CLAUDE_SDK_VERSION}"
                )
            if not shutil.which("bwrap") or not os.access(self.agent_executor.runtime.launcher_path, os.X_OK):
                raise RuntimeError("Native Claude requires bubblewrap and the executable provider launcher")
        if any(getattr(profile, "provider", "claude") == "codex" for profile in native_profiles):
            if version("openai-codex") != "0.170.0":
                raise RuntimeError("Native Codex adapter requires the pinned SDK 0.170.0")
            if not shutil.which("bwrap") or not os.access(self.agent_executor.codex_runtime.launcher_path, os.X_OK):
                raise RuntimeError("Native Codex requires bubblewrap and the executable provider launcher")
        local_ready = await asyncio.gather(*(
            worker.probe() for worker_id, worker in self.workers.items()
            if self.config.worker_capabilities.get(worker_id) is not None
        ))
        remote_ready = []
        for capability in self.config.worker_capabilities.values():
            if capability.workspace_locality != "remote":
                continue
            binding = capability.workspace_bindings[0]
            remote = self._remote_worker_for_binding(capability, binding)
            remote_ready.append(await remote.probe())
            remote_ready.append(await remote.verify_shared_binding(
                binding.local_path, binding.remote_workspace_ref,
                binding.remote_workspace_generation, binding.challenge_relative,
            ))
        if not all((*local_ready, *remote_ready)):
            raise RuntimeError("Native execution worker is unavailable; no fallback is permitted")
        pool = getattr(self.store, "pool", None)
        if pool is None:
            raise RuntimeError("Native execution requires a durable Postgres store")
        await self.store.initialize()
        if self.agent_executor.session_store is not None:
            await self.agent_executor.session_store.create_schema()
        self._coordinator = CoordinatorLease(pool, self._close_admission, self.close)
        await self._coordinator.acquire()
        try:
            for attempt in await self.store.active():
                # 新しい operation の受付を先に閉じ、照合中に古い世代が起動しないようにする。
                await self.store.cancel(attempt["principal"], attempt["id"], "coordinator_restarted")
                await self.store.limits.mark_unknown(attempt["id"])
                stop = await self._stop_attempt(attempt)
                await self.store.finish(
                    attempt["id"], "worker_lost", "coordinator_restarted", stop, None, {},
                )
                if stop == "confirmed":
                    await self.store.confirm_stop(attempt["id"])
            await self.agent_executions.recover()
            await self._sweep_agent_containers()
            await self._coordinator.start()
            self._accepting = True
            self.agent_executions.start_background()
            interval = self.config.input_artifact_limits.gc_interval_seconds
            if interval is not None:
                self._input_artifact_gc = asyncio.create_task(self._input_artifact_maintenance(interval))
            output_interval = self.config.output_artifact_limits.gc_interval_seconds
            if output_interval is not None:
                self._output_artifact_gc = asyncio.create_task(self._output_artifact_maintenance(output_interval))
            workspace_interval = self.config.workspace_limits.gc_interval_seconds
            if workspace_interval is not None:
                self._workspace_gc = asyncio.create_task(self._workspace_maintenance(workspace_interval))
            if isinstance(self.worker, DockerLocalWorker):
                self._container_gc = asyncio.create_task(
                    self._container_maintenance(self.config.container_cleanup.interval_seconds)
                )
        except BaseException:
            await self.close()
            raise

    async def close(self, timing: CloseTiming = NORMAL_CLOSE_TIMING) -> None:
        """即座に受付を閉じて取消通知し、指定した待機時間で共有 cleanup を待つ。

        通常は worker を 90 秒、取消後を 30 秒待ち、全体期限は設けない。
        lease 喪失の呼出元は、先行する通常 cleanup があっても自身の期限で待機を打ち切る。
        """
        self.agent_executions.runner.mark_shutdown()
        self._close_admission()
        if self._coordinator is not None:
            self._coordinator.begin_close()
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close_resources(timing))
            self._close_task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        # wait_for/timeout は取消に抵抗する依存の完了待ちで期限を超え得る。
        done, _ = await asyncio.wait({self._close_task}, timeout=timing.deadline_seconds)
        if not done:
            self._close_task.cancel()
            for task in self._tasks.values():
                task.cancel()
            if self._coordinator is not None:
                self._coordinator.abort()
            if not self._cleanup_timed_out:
                self._cleanup_timed_out = True
                log.error("execution.cleanup_timed_out", error_type="TimeoutError",
                          last_heartbeat_age_seconds=self._coordinator.heartbeat_age() if self._coordinator is not None else None)
        elif not self._close_task.cancelled():
            self._close_task.result()

    async def _close_resources(self, timing: CloseTiming) -> None:
        try:
            await self._stop_workers(timing)
        finally:
            if self._coordinator is not None:
                await self._coordinator.stop_watchdog()
                await self._coordinator.release()
            for journal in self._remote_journals.values():
                journal.close()
            self._remote_journals.clear()

    async def _stop_workers(self, timing: CloseTiming) -> None:
        await self.agent_executions.stop_background()
        for name in ("_input_artifact_gc", "_output_artifact_gc", "_workspace_gc", "_container_gc"):
            task = getattr(self, name)
            if task is not None and task is not asyncio.current_task():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                setattr(self, name, None)
        pending = list(self._cancels.items())
        for _, cancel in pending:
            cancel.set()

        async def record_shutdown(attempt_id: str) -> None:
            try:
                async with asyncio.timeout(5):
                    rows = await self.store.pool.fetchrow("SELECT principal FROM kotoyose.execution_attempts WHERE id=$1", attempt_id)
                    if rows:
                        await self.store.cancel(rows["principal"], attempt_id, "system_shutdown")
            except Exception:
                log.warning("execution.shutdown_record_failed", attempt_id=attempt_id)

        await asyncio.gather(*(record_shutdown(attempt_id) for attempt_id, _ in pending))
        if self._tasks:
            _, unfinished = await asyncio.wait(list(self._tasks.values()), timeout=timing.worker_wait_seconds)
            for task in unfinished:
                task.cancel()
            if unfinished:
                _, unsettled = await asyncio.wait(unfinished, timeout=timing.settle_wait_seconds)
                if unsettled:
                    log.error("execution.shutdown_stop_unconfirmed", count=len(unsettled))

    async def _input_artifact_maintenance(self, interval: int) -> None:
        """管理者が明示した場合だけ input の recovery と bounded GC を実行する。"""
        batch = self.config.input_artifact_limits.gc_batch_size
        while True:
            try:
                await self.store.reconcile_input_artifacts(limit=batch)
                await self.store.gc_expired_input_artifacts(limit=batch)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("execution.input_artifact_maintenance_failed")
            await asyncio.sleep(interval)

    async def _output_artifact_maintenance(self, interval: int) -> None:
        """Caller pin と機械的な安全条件を検査する bounded output GC。"""
        batch = self.config.output_artifact_limits.gc_batch_size
        while True:
            try:
                self.store.artifact_retention.storage = self.store.artifact_storage
                await self.store.artifact_retention.reconcile(limit=batch)
                await self.store.artifact_retention.gc(limit=batch)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning('execution.output_artifact_maintenance_failed')
            await asyncio.sleep(interval)

    async def _workspace_maintenance(self, interval: int) -> None:
        """現在の Client roots と保持設定を各周回で使い、確認不能な削除は台帳に残す。"""
        from .config import get_clients

        while True:
            try:
                collector = WorkspaceCollector(self.store.workspace_retention, get_clients())
                batch = self.config.workspace_limits.gc_batch_size
                await collector.reconcile(limit=batch)
                await collector.gc(limit=batch)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning('execution.workspace_maintenance_failed')
            await asyncio.sleep(interval)

    async def _container_maintenance(self, interval: int) -> None:
        """起動時照合の後、Docker command の停止済み実体を bounded に回収する。"""
        while True:
            try:
                await self._reap_containers_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("execution.container_maintenance_failed")
            await asyncio.sleep(interval)

    async def _reap_containers_once(self) -> int:
        """正確な実行世代と永続停止証拠が揃う停止済み container だけを回収する。"""
        if not isinstance(self.worker, DockerLocalWorker):
            return 0
        candidates = await self.worker.list_stopped_containers()
        if not candidates:
            return 0
        batch = min(self.config.container_cleanup.batch_size, len(candidates))
        start = self._container_cursor % len(candidates)
        selected = [candidates[(start + offset) % len(candidates)] for offset in range(batch)]
        self._container_cursor = (start + batch) % len(candidates)
        ids = [_expected_container_identity(name)[0] for _, name in selected]
        owners = await self.store.container_attempt_owners(ids)
        removed = 0
        retained = {"missing_owner": 0, "malformed_owner": 0, "unconfirmed_stop": 0, "generation_mismatch": 0}
        for container_id, name in selected:
            state = await self.worker.inspect_stopped_container(name, container_id)
            if state is None:
                continue
            attempt_id, generation = _expected_container_identity(name)
            owner = owners.get(attempt_id)
            if owner is None:
                # A missing sandbox operation is not evidence that its stop was persisted.
                retained["missing_owner"] += 1
                continue
            if (not isinstance(owner, tuple) or len(owner) != 3
                    or not isinstance(owner[0], str) or not owner[0]
                    or type(owner[2]) is not int or owner[2] < 0):
                retained["malformed_owner"] += 1
                continue
            if owner[1] is not True:
                retained["unconfirmed_stop"] += 1
                continue
            if owner[2] != generation:
                retained["generation_mismatch"] += 1
                continue
            if owner[0] in self._tasks:
                continue
            removed += await self.worker._remove_stopped(name, container_id)
        if any(retained.values()):
            log.info("execution.container_cleanup_retained", reasons=retained)
        return removed

    async def register_resource(self, principal: str, policy: CallerPolicy, resource: Resource) -> str:
        """入力の trusted 自己申告を、Caller の発行権限で制限する。"""
        if isinstance(resource, InputText) and resource.provenance not in policy.provenance:
            raise ExecutionError("permission_denied", "Caller cannot issue this provenance", 403)
        if isinstance(resource, PinnedContentResource) and resource.provenance not in policy.provenance:
            raise ExecutionError("permission_denied", "Caller cannot issue this provenance", 403)
        if isinstance(resource, RepositoryContextPack) and resource.provenance != "project":
            raise ExecutionError("permission_denied", "Repository context must be project provenance", 403)
        if isinstance(resource, AgentDefinition) and "trusted" not in policy.provenance:
            raise ExecutionError("permission_denied", "Caller cannot issue agent instructions", 403)
        if isinstance(resource, ClientToolSpec) and "trusted" not in policy.provenance:
            raise ExecutionError("permission_denied", "Caller cannot issue client tool definitions", 403)
        if isinstance(resource, McpBindingResource):
            await self.admission.mcp_resolver.validate(policy, resource)
        return await self.store.put_resource(principal, resource)

    async def register_workspace(self, principal: str, policy: CallerPolicy, body: WorkspaceRegistration) -> str:
        """既存の独立 clone を所有と実体の重複なしに登録する。"""
        result = await self._register_workspace_record(principal, policy, body)
        return f"workspace:{result['id']}:{result['generation']}"

    async def register_workspace_response(self, principal: str, policy: CallerPolicy, body: WorkspaceRegistration) -> dict:
        """workspace ref と DB の現在世代を wire response として返す。"""
        result = await self._register_workspace_record(principal, policy, body)
        return {
            "schema_version": "1",
            "workspace_ref": f"workspace:{result['id']}:{result['generation']}",
            "workspace_generation": result["generation"],
        }

    async def _register_workspace_record(self, principal: str, policy: CallerPolicy, body: WorkspaceRegistration) -> dict:
        """path の policy 検証と immutable workspace record の取得を一箇所で行う。"""
        path = workspace_path(policy, body.path)
        return await self.store.register_workspace(principal, body.caller_workspace_ref, path)

    async def dispatch(self, principal: str, policy: CallerPolicy, request: ExecutionRequest,
                       api_key_name: str | None = None) -> str:
        """受理済み key は再実行せず、初回だけ永続 admission の後に worker へ渡す。"""
        payload = request.model_dump(mode="json")
        prior = await replay_execution(self.store, principal, request.idempotency_key, content_digest(payload),
                                       coordinator=getattr(self, "_coordinator", None))
        if prior:
            return prior["id"]
        self._require_accepting()
        try:
            snapshot = await self.admission.resolve(principal, policy, request)
            if api_key_name is not None:  # 旧 token の実行は snapshot を変えない (digest が変わる)
                snapshot["api_key_name"] = api_key_name
            await self._prepare_remote(snapshot)
            self._require_accepting()
        except ExecutionError:
            # 解決中の喪失でも、同時に受理済みとなった key を未受理と報告しない。
            prior = await replay_execution(self.store, principal, request.idempotency_key, content_digest(payload),
                                           coordinator=getattr(self, "_coordinator", None))
            if prior:
                return prior["id"]
            raise
        attempt, created = await self.store.admit(principal, payload, snapshot, policy.max_concurrent)
        if created:
            self._schedule(attempt)
        return attempt["id"]

    def resolve_runtime_authentication(self, policy: CallerPolicy, request: RuntimeAuthenticationResolution) -> dict:
        """Caller 許可内の登録 profile の hash だけを返し、推論も認証更新も行わない。"""
        if request.model is None:
            raise ExecutionError("invalid_request", "runtime_profile was replaced by a model alias", 422)
        if not agent_profile_allowed(request.model, policy, self.config):
            raise ExecutionError("permission_denied", f"Model '{request.model}' is outside this API key's caller scope", 403)
        try:
            profile = resolve_agent_model(request.model, self.config, resolve_prompts=False)
        except (KeyError, OSError, ValueError):
            raise ExecutionError("unsupported_capability", "Runtime authentication is unavailable", 501) from None
        if profile.backend != "native" or profile.provider != "codex":
            raise ExecutionError("unsupported_capability", "Runtime authentication binding requires native Codex", 501)
        try:
            authentication = codex_authentication_binding(codex_profile_authentication(profile))
        except ValueError:
            raise ExecutionError("authentication_failed", "Codex ChatGPT login is required", 401) from None
        return {"schema_version": "1", "runtime_authentication": authentication}

    def resume_support(self, provider: str | None = None) -> ResumeSupport:
        """Native の構成と明示申告を合わせ、direct Session の能力から推測しない。"""
        runtimes = ([self.agent_executor.runtime_for(provider)] if provider is not None else
                    [self.agent_executor.runtime, self.agent_executor.codex_runtime])
        support = ResumeSupport((), "adapter_unsupported")
        native_available = False
        native_providers = set()
        from .config import get_config
        for alias in get_config().models:
            try:
                resolved = resolve_agent_model(alias, self.config, resolve_prompts=False)
                native_available |= resolved.backend == 'native'
                if resolved.backend == 'native':
                    native_providers.add(resolved.provider)
            except (OSError, ValueError):
                continue
        if (native_available
                and any(worker.workspace_locality == "local" for worker in self.config.worker_capabilities.values())):
            if provider is None:
                runtimes = [self.agent_executor.runtime_for(name) for name in native_providers if name in {'claude', 'codex'}]
            modes = {mode for runtime in runtimes if isinstance(runtime, ResumeAdapter)
                     for mode in runtime.resume_support().modes}
            support = ResumeSupport(tuple(mode for mode in ('native', 'durable_state') if mode in modes),
                                    'provider_session_state_not_retained')
        if provider is None:
            self.manifest.capabilities["safe_resume"] = safe_resume_capability(support.modes)
            self.manifest.safe_resume_modes = support.modes
        return support

    async def resume(self, principal: str, policy: CallerPolicy, attempt_id: str, request: ResumeRequest,
                     api_key_name: str | None = None) -> str:
        """受理済み再送を先に返し、初回だけ安全条件と fencing を原子的に固定する。"""
        prior = await replay_execution(self.store, principal, request.execution.idempotency_key,
                                       resume_request_digest(attempt_id, request), operation="resume",
                                       coordinator=getattr(self, "_coordinator", None))
        if prior:
            return prior["id"]
        self._require_accepting()
        if getattr(request.execution, "model", None) is None and getattr(request.execution, "runtime_profile", None):
            raise ExecutionError("invalid_request", "runtime_profile was replaced by a model alias", 422)
        previous = await self.store.attempt(principal, attempt_id)
        provider = previous['snapshot'].get('provider', 'claude')
        support = self.resume_support(provider)
        runtime = self.agent_executor.runtime_for(provider)
        available = getattr(runtime, 'can_resume_session', None)
        if request.required_resume_mode == "native" and available is None:
            raise unsafe("adapter_unsupported")
        if previous.get('provider_session_ref') and available is not None:
            from .native_sessions import session_scope
            if not await available(session_scope(principal, previous['workspace_id'], provider), previous['provider_session_ref']):
                previous = {**previous, 'provider_session_ref': None}
        if not support.modes:
            raise unsafe("adapter_unsupported")
        try:
            snapshot = await self.admission.resolve_resume(principal, policy, previous, request, support)
            if api_key_name is not None:  # 旧 token の実行は snapshot を変えない (digest が変わる)
                snapshot["api_key_name"] = api_key_name
            self._require_accepting()
            attempt, created = await self.store.admit(
                principal, request.execution.model_dump(mode="json"), snapshot, policy.max_concurrent,
            )
        except ExecutionError:
            # 並行する同一 POST が先に受理した場合、世代変化による拒否より受理証を優先する。
            prior = await replay_execution(self.store, principal, request.execution.idempotency_key,
                                           resume_request_digest(attempt_id, request), operation="resume",
                                           coordinator=getattr(self, "_coordinator", None))
            if prior:
                return prior["id"]
            raise
        if created:
            self._schedule(attempt)
        return attempt["id"]

    def _schedule(self, attempt: dict, *, after: Callable[[dict], Awaitable[None]] | None = None,
                  cancel: asyncio.Event | None = None) -> asyncio.Event:
        cancel = cancel if cancel is not None else asyncio.Event()
        self._cancels[attempt["id"]] = cancel
        async def run():
            try:
                return await self._execute(attempt, cancel)
            finally:
                if after is not None:
                    await after(attempt)
        task = asyncio.create_task(run())
        self._tasks[attempt["id"]] = task
        from .container_codex import container_job_id_for
        self._task_jobs[attempt["id"]] = container_job_id_for(attempt["id"], attempt["generation"])
        task.add_done_callback(lambda done, key=attempt["id"]: self._finished(key, done))
        return cancel

    def _finished(self, attempt_id: str, task: asyncio.Task) -> None:
        self._tasks.pop(attempt_id, None)
        self._task_jobs.pop(attempt_id, None)
        self._cancels.pop(attempt_id, None)
        if not task.cancelled() and task.exception():
            log.error("execution.unrecorded_failure", attempt_id=attempt_id, error_type=type(task.exception()).__name__)

    async def cancel(self, principal: str, attempt_id: str) -> dict:
        """まず取消を永続化し、停止結果の確定は worker 完了に委ねる。"""
        await self.store.cancel(principal, attempt_id)
        cancel = self._cancels.get(attempt_id)
        if cancel:
            cancel.set()
        return await self.describe(principal, attempt_id)

    async def cancel_dispatch(self, principal: str, key: str) -> dict:
        """受理の有無を確定し、既存 Attempt の停止は通常の取消・照合に委ねる。"""
        attempt_id = await self.store.cancel_dispatch(principal, key)
        if attempt_id is not None:
            cancel = self._cancels.get(attempt_id)
            if cancel:
                cancel.set()
        return {
            "schema_version": "1", "idempotency_key": key, "admission_closed": True,
            "outcome": "not_admitted" if attempt_id is None else "attempt_known",
            "attempt_id": attempt_id,
        }

    async def revoke(self, principal: str, ref: str) -> list[str]:
        """Grant の取消を進行中の worker に伝播する。"""
        attempts = await self.store.revoke_grant(principal, ref)
        for attempt_id in attempts:
            cancel = self._cancels.get(attempt_id)
            if cancel:
                cancel.set()
        return attempts

    async def reconcile(self, principal: str, attempt_id: str) -> dict:
        """終端後の実処理を再照合する。旧 verdict を変えず停止証拠だけを補完する。"""
        attempt = await self.store.attempt(principal, attempt_id)
        if attempt["finished_at"] is not None and attempt["stop_confirmation"] == "confirmed" and attempt["side_effect_resolution"] == "resolved":
            return await self.describe(principal, attempt_id)
        if attempt["finished_at"] is None or attempt_id in self._tasks:
            raise ExecutionError("request_conflict", "Attempt execution has not settled", 409)
        if attempt["stop_confirmation"] != "confirmed":
            stop = await self._stop_attempt(attempt)
            if stop == "confirmed":
                await self.store.confirm_stop(attempt_id)
        else:
            await self.store.confirm_stop(attempt_id)
        return await self.describe(principal, attempt_id)

    async def describe_limits(self, principal: str, attempt_id: str) -> dict:
        """所有者の Attempt から追加観測だけを返す。存在しない場合は 404。"""
        from .execution_limits import live_attempt_limits
        observation = await live_attempt_limits(self.store, getattr(self, "agent_executor", None), principal, attempt_id)
        return observation.model_dump(mode="json")

    async def describe(self, principal: str, attempt_id: str) -> dict:
        """内部の host path/env を wire に出さず、結果と安全状態を返す。"""
        row = await self.store.attempt(principal, attempt_id)
        delegated = row["snapshot"].get("parent_attempt_id")
        generation = row["generation"] + (not delegated and row["stop_confirmation"] == "confirmed"
                                          and row["side_effect_resolution"] == "resolved")
        compression = {}
        if row['snapshot'].get('compression_policy') is not None:
            compression = {'compression': {'policy': row['snapshot']['compression_policy'], **(await self.store.context_observation(attempt_id))}}
        authentication = {}
        if row["snapshot"].get("backend") == "native" and row["snapshot"].get("provider") == "codex":
            from .execution_resources import codex_authentication_binding
            authentication = {"runtime_authentication": codex_authentication_binding(row["snapshot"])}
        return {
            **compression,
            **authentication,
            **({key: row["snapshot"][key] for key in ("parent_attempt_id", "delegated_by", "depth")} if delegated else {}),
            **({"resolved_model": row["snapshot"]["resolved_model"]} if "resolved_model" in row["snapshot"] else {}),
            **({"resume_manifest": row["snapshot"]["resume_manifest"],
                "resume_manifest_digest": row["snapshot"]["resume_manifest_digest"],
                "resume_mode": row["snapshot"]["resume_mode"]} if "resume_manifest" in row["snapshot"] else {}),
            "schema_version": "1", "attempt_id": row["id"], "caller_run_ref": row["request"]["caller_run_ref"],
            "status": row["status"], "reason": row["reason"], "stop_confirmation": row["stop_confirmation"],
            "side_effect_resolution": row["side_effect_resolution"], "exit_code": row["exit_code"],
            "sequence": row["sequence"], "workspace_ref": f"workspace:{row['workspace_id']}:{generation}",
            "workspace_generation": generation,
            "usage": row["usage"], "artifacts": row["artifacts"],
            "artifact_manifest": row.get("artifact_manifest", []),
            "required_action": row.get("required_action"),
            "created_at": row["created_at"].isoformat(),
            "finished_at": row["finished_at"].isoformat() if row["finished_at"] else None,
        }

    async def _observe_pool(
        self,
        snapshot: dict,
        status: str,
        limit,
        *,
        reason: str | None = None,
        error_detail: str | None = None,
    ) -> None:
        """終わった attempt の証拠を pool と connection の状態へ。失敗しても attempt の結果は変えない。"""
        try:
            from .config import get_config
            from .connections_probe import (
                connection_for_model,
                record_connection_auth_failure,
                record_provider_usage_limit,
            )

            cfg = get_config()
            alias = snapshot.get("alias")
            conn_id = connection_for_model(cfg, alias)
            if reason == "authentication_failed":
                await record_connection_auth_failure(conn_id, error=error_detail or reason)
            if limit is not None:
                await record_provider_usage_limit(conn_id, limit)
            await self.store.model_pools.observe_attempt(cfg, alias, status, limit)
        except Exception as exc:
            log.warning("model_pool.observe_failed", error_type=type(exc).__name__)

    async def _delegation_tool(self, parent, calls, tool_ref, arguments, remaining_seconds):
        from .config import get_config
        from .execution_resources import authenticate_client

        try:
            if calls.closed or calls.cancel.is_set() or remaining_seconds() <= 0:
                raise ExecutionError("tool_denied", "Parent execution is stopping", 409)
            _, policy = authenticate_client(parent["principal"])
            if tool_ref == LIST_AGENTS and not arguments:
                agents = targets(get_config(), parent["snapshot"]["alias"], policy, self.config)
                return ClientToolCallOutcome("succeeded", agents)
            if tool_ref != ASK_AGENT or set(arguments) - {"alias", "task", "conversation_id"}:
                raise ExecutionError("invalid_request", "Invalid delegation arguments", 400)
            if "conversation_id" in arguments:
                raise ExecutionError("invalid_request", "conversation_id is unsupported for jailed delegation", 400)
            alias, task = arguments.get("alias"), arguments.get("task")
            if (not isinstance(alias, str) or not isinstance(task, str) or not task.strip()
                    or len(task.encode("utf-8", errors="replace")) > MAX_TASK_BYTES or redact_token(task) != task):
                raise ExecutionError("invalid_request", "A valid alias and non-empty task are required", 400)
            deadline = datetime.now(timezone.utc) + timedelta(seconds=max(0, remaining_seconds()))
            return await calls.run(lambda stop: self._run_delegated_child(parent, policy, alias, task, deadline, stop))
        except ExecutionError as exc:
            return ClientToolCallOutcome("failed", {"error": redact_token(str(exc))})
        except Exception as exc:
            # 例外の本文には依頼文などが入り得るので残さない。種類と、起きた場所 (module:line) だけを残す。
            log.warning("execution.delegation_failed", error_type=type(exc).__name__, at=_raise_site(exc))
            return ClientToolCallOutcome("failed", {"error": "Delegated execution failed"})

    async def _run_delegated_child(self, parent, policy, alias, task, deadline, stop):
        principal = parent["principal"]
        self._require_accepting()
        request, snapshot = await self.admission.resolve_shared_child(parent, policy, alias, task, deadline)
        if stop.is_set():
            raise ExecutionError("tool_denied", "Parent execution is stopping", 409)
        self._require_accepting()
        child, _ = await self.store.admit(principal, request.model_dump(mode="json"), snapshot, policy.max_concurrent)
        progress = DelegationProgress(self.store, parent["id"], child["id"])
        bind_child_progress(child["id"], progress)
        terminal = "failed"
        try:
            await progress.start(alias, task)
            if stop.is_set():
                raise ExecutionError("tool_denied", "Parent execution is stopping", 409)
            self._schedule(child, cancel=stop)
            execution = self._tasks[child["id"]]
            cancellation = asyncio.create_task(stop.wait())
            try:
                await asyncio.wait({execution, cancellation}, return_when=asyncio.FIRST_COMPLETED)
                if stop.is_set() and not execution.done():
                    await self.store.cancel(principal, child["id"], "parent_execution_ended")
            finally:
                cancellation.cancel()
                result = await asyncio.shield(execution)
            if stop.is_set() or not result or result[0] != "succeeded" or result[2] != "confirmed":
                reason = result[1] if result else "stopped_before_start"
                raise ExecutionError("delegated_execution_failed", f"Delegated execution failed ({reason})", 409)
            terminal = "completed"
            _, _, _, content, usage = result
            return ClientToolCallOutcome("succeeded", {
                "text": content.decode("utf-8", errors="replace"),
                **({"usage": usage} if usage.get("source") == "reported" else {}),
            })
        finally:
            await progress.event(status=terminal, summary=terminal)
            unbind_child_progress(child["id"], progress)

    async def _execute(self, attempt: dict, cancel: asyncio.Event) -> tuple[str, str | None, str | None, bytes, dict] | None:
        attempt_id, snapshot = attempt["id"], attempt["snapshot"]
        warning = None
        launched = False
        agent_launch_outcome = None
        runtime_entry = RuntimeEntry()
        observed_stop = None
        interaction: AgentInteraction | None = None
        terminal = ("failed", "worker_failure", "unconfirmed", b"", {})
        artifacts = {}
        calls = DelegationCalls(cancel) if snapshot.get("delegation_tools") else None
        try:
            grant = await self.store.resource(attempt["principal"], attempt["request"]["execution_grant_ref"])
            agent_activation = snapshot.get('agent_activation')
            if (not agent_activation and snapshot.get("limits_mode") == "agent_work_v1" and grant.expires_at <= datetime.now(timezone.utc)):
                from .execution_limits import AgentResourceUsage
                await self.store.finish(attempt_id, "failed", "grant_expired_before_start", "confirmed", None, {},
                    limits_result=(AgentResourceUsage(output_bytes=0, turns=0), None))
                return
            if agent_activation:
                self.agent_executions.leases.assert_startable((agent_activation['execution_id'], agent_activation['nonce']))
            elif snapshot.get("limits_mode") == "agent_work_v1" and snapshot["kind"] == "agent":
                from .agent_deadlines import validate_agent_start
                validate_agent_start({**snapshot, "grant": grant.model_dump(mode="json")}, datetime.now(timezone.utc))
            remaining = (self.agent_executions.leases.remaining_seconds((agent_activation['execution_id'], agent_activation['nonce']), 0)
                         if agent_activation else (datetime.fromisoformat(snapshot["deadline"]) - datetime.now(timezone.utc)).total_seconds())
            if remaining <= 0:
                await self.store.finish(attempt_id, "timeout", "deadline_exceeded", "confirmed", None, {})
                return
            path = Path(snapshot["workspace_path"])
            info = path.stat()
            if path.is_symlink() or (info.st_dev, info.st_ino) != (snapshot["workspace_device"], snapshot["workspace_inode"]):
                raise ExecutionError("request_conflict", "Workspace identity changed", 409)
            if not self._accepting or cancel.is_set() or not await self.store.mark_running(attempt_id):
                await self.store.finish(attempt_id, "cancelled", "cancelled_before_start", "confirmed", None, {})
                return
            timeout, budget = snapshot["timeout"], snapshot["budget"]
            worker = None if snapshot.get("backend") == "print_cli" else self._worker_for(snapshot)
            if snapshot.get("worker_capabilities", {}).get("workspace_locality") == "remote":
                capability = self.config.worker_capabilities[snapshot["worker_id"]]
                binding = next(item for item in capability.workspace_bindings
                               if item.caller_workspace_ref == snapshot["caller_workspace_ref"])
                if not await worker.verify_shared_binding(
                    path, binding.remote_workspace_ref, binding.remote_workspace_generation,
                    binding.challenge_relative,
                ):
                    raise ExecutionError("request_conflict", "Shared workspace binding changed before start", 409)
            warning = asyncio.create_task(self._warn(attempt_id, timeout["soft_seconds"]))
            if snapshot["kind"] == "agent":
                inspect_output = guard_enabled()
                inspect_workspace = inspect_output and (
                    snapshot.get("surface") == "write" or HOST_CODING in snapshot.get("tool_refs", ())
                )
                baseline = await asyncio.to_thread(workspace_state, path) if inspect_workspace else {}
                interaction = AgentInteraction(attempt)
                launched = True
                async def delegate(tool_ref, args, remaining):
                    return await self._delegation_tool({**attempt, "status": "running"}, calls, tool_ref, args, remaining)

                result = await self.agent_executor.execute(
                    attempt, cancel, worker=worker, interaction=interaction, runtime_entry=runtime_entry,
                    **({"delegation_call": delegate} if calls is not None else {}),
                )
                if calls is not None:
                    await calls.close()
                agent_launch_outcome = result.launch_outcome
                operations = await self.store.operations(attempt_id)
                stop = result.stop_confirmation
                if any(operation["state"] != "confirmed" for operation in operations if operation["kind"] not in {"local_execution", "client_tool", "mcp_tool"}):
                    stop = "unconfirmed"
                observed_stop = stop
                artifacts: dict[str, bytes | CapturedArtifact] = {"output": result.content}
                artifacts, manifest, contract_valid = await self._capture_artifacts(snapshot, path, stop, artifacts)
                if inspect_output:
                    leaked = await asyncio.to_thread(check_artifacts, artifacts, snapshot.get("artifact_spec"))
                    if not leaked and inspect_workspace and stop == "confirmed":
                        leaked = await asyncio.to_thread(
                            check_workspace, path, snapshot["workspace_device"], snapshot["workspace_inode"], baseline,
                        )
                    if leaked:
                        raise ExecutionError(LEAK_REASON, LEAK_MESSAGE, 502, safe_to_retry=False)
                status, reason = result.status, result.reason
                if status == "succeeded" and stop == "confirmed" and not contract_valid:
                    status, reason = "failed", "output_contract_error"
                limits_result = {}
                if snapshot.get("limits_mode") == "agent_work_v1":
                    from .execution_limits import AgentResourceUsage
                    limits_result = {"limits_result": (AgentResourceUsage(output_bytes=result.output_bytes,
                        turns=result.turns), result.provider_limit)}
                finished = await self.store.finish(
                    attempt_id, status, reason, stop, None, artifacts,
                    result.usage, manifest,
                    required_action=result.required_action,
                    provider_session_ref=(
                        self.agent_executor.runtime_for(snapshot.get('provider', 'claude')).resume_session_ref(result.provider_session_id)
                        if snapshot.get('backend') == 'native'
                        and isinstance(self.agent_executor.runtime_for(snapshot.get('provider', 'claude')), ResumeAdapter)
                        and "native" in self.agent_executor.runtime_for(snapshot.get('provider', 'claude')).resume_support().modes else None
                    ),
                    launch_outcome=agent_launch_outcome,
                    **limits_result,
                )
                if finished is not None:
                    status, reason, stop = finished
                await self._observe_pool(snapshot, status, result.provider_limit, reason=reason)
                terminal = (status, reason, stop, result.content, result.usage)
                return terminal
            job = CommandJob(
                attempt_id=attempt_id, generation=attempt["generation"], workspace=path,
                argv=tuple(snapshot["argv"]), cwd=snapshot["cwd"], env=snapshot["env"],
                timeout_seconds=min(remaining, timeout["hard_seconds"]), grace_seconds=timeout["grace_seconds"],
                output_limit_bytes=budget["output_limit_bytes"], memory_bytes=budget["memory_bytes"],
                pids_limit=budget["pids_limit"], cpus=budget["cpus"],
            )
            launched = True
            result = await worker.execute(job, cancel)
            observed_stop = result.stop_confirmation
            artifacts: dict[str, bytes | CapturedArtifact] = {
                "stdout": result.stdout, "stderr": result.stderr,
            }
            artifacts, manifest, contract_valid = await self._capture_artifacts(
                snapshot, path, result.stop_confirmation, artifacts,
            )
            status, reason = result.status, result.reason
            if result.stop_confirmation == "confirmed" and not contract_valid:
                status, reason = "failed", "output_contract_error"
            await self.store.finish(
                attempt_id, status, reason, result.stop_confirmation, result.exit_code,
                artifacts, artifact_manifest=manifest,
            )
        except asyncio.CancelledError:
            terminal = ("cancelled", "execution_cancelled", "unconfirmed", b"", {})
            raise
        except Exception as exc:
            if calls is not None:
                await calls.close()
            from .agent_deadlines import AgentStartRejected
            # Artifact 保存後処理の障害で、既に得た停止証拠を GC 済み unit の再照会で失わない。
            stop = ("confirmed" if observed_stop == "confirmed" or not launched or isinstance(exc, AgentStartRejected)
                    else await self._stop_attempt(attempt))
            reason = (exc.code if isinstance(exc, ExecutionError) else
                      "artifact_storage_error" if isinstance(exc, ArtifactStorageError) else "worker_failure")
            status = "failed"
            limits_result = {}
            if isinstance(exc, AgentStartRejected):
                from .execution_limits import AgentResourceUsage
                limits_result = {"limits_result": (AgentResourceUsage(output_bytes=0, turns=0), None)}
            if isinstance(exc, AgentStartRejected) or not launched or (snapshot["kind"] == "agent" and not runtime_entry.entered):
                agent_launch_outcome = 'not_started'
            elif snapshot["kind"] == "agent" and agent_launch_outcome is None:
                agent_launch_outcome = 'unknown'
            finished = await self.store.finish(attempt_id, status, reason, stop, None, {},
                                               launch_outcome=agent_launch_outcome, **limits_result)
            # store は取消との競合を cancelled / worker_lost へ補正する。記録もその確定値に揃える。
            if finished is not None:
                status, reason, stop = finished
            if reason == "authentication_failed":
                error_detail = str(exc) if isinstance(exc, ExecutionError) else None
                await self._observe_pool(snapshot, status, None, reason=reason, error_detail=error_detail)
            terminal = (status, reason, stop, b"", {})
            log.warning("execution.failed", attempt_id=attempt_id, reason=reason)
            return terminal
        finally:
            if calls is not None:
                await calls.close()
            from .artifact_file import ArtifactFile
            for artifact in artifacts.values():
                if isinstance(artifact, CapturedArtifact) and isinstance(artifact.content, ArtifactFile):
                    artifact.content.close()
            if interaction is not None:
                interaction.finish(*terminal)
            if warning:
                warning.cancel()
                await asyncio.gather(warning, return_exceptions=True)

    async def _capture_artifacts(
        self,
        snapshot: dict,
        workspace: Path,
        stop: str,
        artifacts: dict[str, bytes | CapturedArtifact],
    ) -> tuple[dict[str, bytes | CapturedArtifact], list[dict], bool]:
        """停止確定後だけ固定 spec を収集し、terminal transaction 用の manifest を作る。"""
        raw_spec = snapshot.get("artifact_spec")
        if stop != "confirmed" or raw_spec is None:
            return artifacts, [], True
        try:
            spec = ArtifactCaptureSpec.model_validate(raw_spec)
            result = await asyncio.to_thread(
                collect_execution_artifacts,
                workspace,
                snapshot["workspace_device"],
                snapshot["workspace_inode"],
                spec,
                reader=read_artifact_file if self.config.artifact_disk_limits else None,
            )
        except Exception:
            return artifacts, [{"name": "__capture__", "state": "error", "reason": "artifact capture failed"}], False
        if set(result.artifacts).intersection(artifacts):
            from .artifact_file import ArtifactFile
            for value in result.artifacts.values():
                if isinstance(value.content, ArtifactFile):
                    value.content.close()
            return artifacts, [
                {"name": "__capture__", "state": "error", "reason": "artifact name conflicts with execution output"}
            ], False
        artifacts.update(result.artifacts)
        return artifacts, [asdict(item) for item in result.manifest], result.contract_valid

    async def _warn(self, attempt_id: str, seconds: float) -> None:
        await asyncio.sleep(seconds)
        await self.store.append_event(attempt_id, "soft_threshold_exceeded", {"seconds": seconds})
        # 稼働中 workspace の一貫した snapshot を偽装しない。progress journal は継続保存し、
        # ファイル回収は既存の実停止確認後に行う。追加のモデル turn は発行しない。
        await self.store.append_event(attempt_id, "progress_save_requested", {
            "checkpoint": "deferred_until_stopped", "action": "continue",
        })

    async def _stop_attempt(self, attempt: dict) -> str:
        if attempt["snapshot"]["kind"] == "command":
            try:
                worker = self._worker_for(attempt["snapshot"])
            except ExecutionError:
                return "unconfirmed"
            return await worker.stop(attempt["id"], attempt["generation"], 5)
        confirmations = []
        for operation in await self.store.operations(attempt["id"]):
            if operation["kind"] in {"local_execution", "client_tool", "mcp_tool"} or operation["state"] == "confirmed":
                continue
            if operation["kind"] == "sandbox_command":
                try:
                    worker = self._worker_for(attempt["snapshot"])
                    stop = await worker.stop(operation["id"], attempt["generation"], 5)
                except ExecutionError:
                    stop = "unconfirmed"
            elif operation["kind"] == "provider_process" and operation["result"]:
                if "agent_container" in operation["result"]:
                    # 部屋 (agent container) で動いた attempt の停止証拠は container の不在だけ。jail の証拠は使わない。
                    from .agent_container import recover_recorded_container
                    stop = await recover_recorded_container(operation["result"]["agent_container"])
                    if stop == "confirmed" and attempt["snapshot"].get("provider") == "codex":
                        from .codex_egress import forget_unsettled_egress
                        forget_unsettled_egress(attempt["id"])
                elif attempt["snapshot"].get("backend") == "print_cli":
                    from .host_place import stop_recovered_group
                    stop = await stop_recovered_group(ProcessIdentity(**operation["result"]), 5)
                elif attempt["snapshot"].get("provider") == "codex":
                    from .codex_process_stop import stop_codex_process
                    if attempt["snapshot"].get("codex_execution_mode") == "host_native_coding_v1":
                        from .codex_stop_recovery import recover_codex_stop
                        stop = await recover_codex_stop(attempt, operation["result"],
                            record_namespace_stop=lambda evidence: self.store.record_codex_namespace_stop(operation["id"], evidence),
                            record_egress_stop=lambda evidence: self.store.record_codex_egress_stop(operation["id"], evidence),
                            egress_owner_absent=self._egress_owner_absent)
                        if stop == "confirmed":
                            from .codex_egress import forget_unsettled_egress
                            forget_unsettled_egress(attempt["id"])
                    else:
                        stop = await stop_codex_process(ProcessIdentity(**operation["result"]), 5)
                else:
                    stop = await stop_identified_process(ProcessIdentity(**operation["result"]), 5)
            else:
                stop = "unconfirmed"
            confirmations.append(stop)
            await self.store.resolve_operation(operation["id"], stop)
        return "confirmed" if all(stop == "confirmed" for stop in confirmations) else "unconfirmed"

    async def _sweep_agent_containers(self) -> None:
        """起動時に、この instance の container のうち生きている attempt に属さないものと、残った temp home を消す。

        停止が確認できなかった container があれば home は消さない。bind mount 中の home を
        消すと、残った container の中身だけが失われるため。失敗は記録して起動は続ける。
        """
        runtime = self.config.agent_container
        if runtime is None:
            return
        from .agent_container import DEFAULT_INSTANCE, AgentContainer, AgentContainerError, remove_leftover_homes
        live = set(self._task_jobs.values())
        container = AgentContainer(instance=DEFAULT_INSTANCE)
        try:
            removed = await container.sweep(keep=lambda job_id: job_id in live)
            remaining = [job_id for job_id in await container.owned_jobs() if job_id not in live]
        except (AgentContainerError, ValueError):
            log.error("execution.agent_container_sweep_failed")
            return
        if removed:
            log.info("execution.agent_containers_swept", count=len(removed))
        if remaining:
            # 停止が確認できなかった container が残るので、それが使う home は消さない。
            log.error("execution.agent_container_sweep_retained", count=len(remaining))
            return
        # 処理中の attempt がある間は home の所有を証明できないので消さない。起動直後は通常ここで空。
        if self._tasks:
            return
        try:
            homes = remove_leftover_homes(Path(runtime.home_root))
        except (OSError, ValueError):
            log.error("execution.agent_home_sweep_failed")
            return
        if homes:
            log.info("execution.agent_homes_swept", count=len(homes))

    def _egress_owner_absent(self, attempt: dict, ownership: dict):
        """egress だけが未確認の停止で、egress を持っていた process の不在を判断する。"""
        from .codex_egress import egress_settled_in_process
        from .codex_egress_owner import egress_owner_absent
        from .process_identity import capture_process_identity
        try:
            process = capture_process_identity(os.getpid())
        except (OSError, RuntimeError, ValueError):
            return None
        return egress_owner_absent(attempt, ownership, coordinator=getattr(self, "_coordinator", None),
                                   process=process, settled_in_process=egress_settled_in_process)

    def _worker_for(self, snapshot: dict) -> CommandWorker | None:
        from .agent_spec import admitted_execution_cell, mode_requests_room
        mode = snapshot.get("codex_execution_mode")
        if mode_requests_room(mode):
            from .codex_coding_policy import resolve_codex_sandbox, validate_coding_policy
            from .codex_provider_scope import CodexProviderExecutor, PROVIDER_WORKER_ID
            provider = snapshot.get("provider")
            cell = admitted_execution_cell(provider, mode) if isinstance(provider, str) else None
            expected = set(cell.tool_refs) if cell is not None else set()
            if provider == "claude":
                # Claude coding は Codex の sandbox / tool policy の pin を持たない。container の中で動く。
                if (snapshot.get("kind") != "agent" or snapshot.get("backend") != "native"
                        or snapshot.get("worker_id") != PROVIDER_WORKER_ID
                        or set(snapshot.get("tool_refs", [])) - TOOL_SPECS.keys() != expected
                        or snapshot.get("image_id") is not None
                        or snapshot.get("worker_capabilities") != CodexProviderExecutor.public_snapshot()):
                    raise ExecutionError("request_conflict", "Pinned host coding executor changed", 409)
                return None
            try:
                resolve_codex_sandbox(mode, snapshot.get("codex_sandbox"))
                validate_coding_policy(snapshot.get("execution_environment"), snapshot.get("codex_sandbox_policy"),
                                       snapshot.get("codex_tool_policy_digest"), mode=mode)
            except ValueError as exc:
                if str(exc) == "invalid_codex_coding_policy":
                    raise ExecutionError("request_conflict", "Pinned host coding executor changed", 409) from exc
                raise ExecutionError("request_conflict", "Pinned host coding sandbox is unsupported", 409) from exc
            if (snapshot.get("kind") != "agent" or snapshot.get("backend") != "native"
                    or provider != "codex" or snapshot.get("worker_id") != PROVIDER_WORKER_ID
                    or set(snapshot.get("tool_refs", [])) - TOOL_SPECS.keys() != expected
                    or snapshot.get("image_id") is not None
                    or snapshot.get("worker_capabilities") != CodexProviderExecutor.public_snapshot()):
                raise ExecutionError("request_conflict", "Pinned host coding executor changed", 409)
            return None
        worker_id = snapshot.get("worker_id")
        pinned = snapshot.get("worker_capabilities")
        capability = self.config.worker_capabilities.get(worker_id)
        if capability is None or not isinstance(pinned, dict):
            raise ExecutionError("unsupported_capability", "Pinned execution worker is unavailable", 501)
        # Attempt の snapshot に保存した公開 identity と現在の enrollment を完全一致させる。
        # 証明書ローテーション後の新規 dispatch や旧 Attempt の別 identity 使用を防ぐ。
        current = capability.public_snapshot()
        if json.dumps(current, sort_keys=True, separators=(",", ":")) != json.dumps(
            pinned, sort_keys=True, separators=(",", ":")
        ):
            raise ExecutionError("request_conflict", "Pinned Worker identity or binding changed", 409)
        if snapshot.get("worker_capabilities", {}).get("workspace_locality") == "remote":
            binding = next((item for item in capability.workspace_bindings
                            if item.caller_workspace_ref == snapshot.get("caller_workspace_ref")), None)
            if binding is None:
                raise ExecutionError("request_conflict", "Pinned remote workspace binding is unavailable", 409)
            return self._remote_worker_for_binding(capability, binding)
        worker = self.workers.get(worker_id)
        if worker is None:
            raise ExecutionError("unsupported_capability", "Pinned execution worker is unavailable", 501)
        return worker

    def _remote_worker_for_binding(self, capability, binding) -> RemoteWorkerClient:
        connection = capability.remote_connection
        if connection is None:
            raise ExecutionError("unsupported_capability", "Remote worker connection is unavailable", 501)
        journal = self._remote_journals.get(capability.worker_id)
        if journal is None:
            journal = RemoteWorkerJournal(connection.journal_path)
            self._remote_journals[capability.worker_id] = journal
        return RemoteWorkerClient(
            base_url=connection.endpoint,
            worker=connection.worker,
            ca_file=connection.ca_file,
            certificate_file=connection.certificate_file,
            key_file=connection.key_file,
            journal=journal,
            workspace_ref=binding.remote_workspace_ref,
            workspace_generation=binding.remote_workspace_generation,
        )

    async def _prepare_remote(self, snapshot: dict) -> None:
        if snapshot.get("worker_capabilities", {}).get("workspace_locality") != "remote":
            return
        worker = self._worker_for(snapshot)
        capability = self.config.worker_capabilities[snapshot["worker_id"]]
        binding = next(item for item in capability.workspace_bindings
                       if item.caller_workspace_ref == snapshot["caller_workspace_ref"])
        if not await worker.probe():
            raise ExecutionError("provider_failure", "Remote worker is unavailable", 503)
        if not await worker.verify_shared_binding(
            Path(snapshot["workspace_path"]), binding.remote_workspace_ref,
            binding.remote_workspace_generation, binding.challenge_relative,
        ):
            raise ExecutionError("request_conflict", "Shared workspace binding could not be verified", 409)

