"""ホストチャットの ACP セッションとプロセス寿命。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import ExitStack, suppress
import hashlib
import json
from pathlib import Path
import tempfile

from anyio import CancelScope
import structlog

from ..activity import emit_runtime_event
from ..acp_client import AcpClient, AcpError, GrokMcpGuardError, _finish_cleanup
from ..acp_turn import AcpPermissionPolicy, AcpTurn, acp_mcp_servers, acp_usage_parser
from ..chat_approvals import current_turn_approvals
from ..config import HarnessConfig, ModelPreset, get_config
from ..execution_resources import NativeExecutionConfig
from ..grok_skills import grok_skill_env, uses_grok_native_skills
from ..print_cli_agent import _HOME_SEMAPHORES, compose_print_cli_prompt
from ..print_cli_home import PrintCliHomeBusyError, use_login_home
from ..print_cli_profiles import (
    PrintCliProfile, build_print_cli_env, get_print_cli_profile, resolve_print_cli_binary,
)
from ..provider_data import UNRESTRICTED
from ..provider_data_runtime import DeclarationRejected
from ..repository_skills import RepositorySkills
from .base import RuntimeEvent, RuntimePrompt, RuntimeResult, RuntimeTurnError, SDKConnectionError, prompt_has_images, prompt_text
from .skill_resources import CliInstructions, resolve_cli_resources

log = structlog.get_logger()
IDLE_SECONDS = 120.0
MAX_LIVE_PROCESSES = 16
_LIVE: set[AcpRuntimeSession] = set()
_END = object()


class AcpPrompt(str):
    """最新発言と、agent に履歴がない場合のチャット履歴を保持する。"""

    def __new__(cls, text: str, history: str):
        instance = super().__new__(cls, text)
        instance.history = history
        return instance


class AcpRuntimeSession:
    """1 会話の ACP 接続。turn を直列化し、idle 後は session ID から復帰する。"""

    provider = "cli_acp"

    def __init__(
        self, profile: PrintCliProfile, binary: Path, login_home: Path, cwd: Path | None, model: str,
        timeout: float, instructions: str, mcp_servers: list[dict], policy: AcpPermissionPolicy,
        session_id: str | None, *, idle_seconds: float = IDLE_SECONDS,
        resources: CliInstructions | None = None, output_schema: dict | None = None,
        repository_skills: RepositorySkills | None = None,
    ) -> None:
        self.profile = profile
        self.binary = binary
        self.home = login_home
        # None: host chat gets a private temporary cwd per connection, like cli.print chat.
        self._fixed_cwd = cwd
        self.cwd = cwd
        self.model = model
        self.timeout = timeout
        self.instructions = instructions
        self._resources = resources
        self._output_schema = output_schema
        self._repository_skills = repository_skills
        self._effective_instructions = instructions
        self.mcp_servers = mcp_servers
        self.policy = policy
        self.session_id = session_id
        self.idle_seconds = idle_seconds
        self._lock = asyncio.Lock()
        self._process: AcpClient | None = None
        self._approval_watch: asyncio.Task | None = None
        self._leases = ExitStack()
        self._idle: asyncio.TimerHandle | None = None
        self.invalidated = False

    async def disconnect(self) -> None:
        """進行中の turn を待ち、接続を閉じる。"""
        async with self._lock:
            self.invalidated = True
            await _finish_cleanup(asyncio.create_task(self._close()))

    async def _close(self) -> None:
        if self._idle is not None:
            self._idle.cancel()
            self._idle = None
        try:
            if self._process is not None and (declaration := self.profile.data_declaration) is not None:
                from ..provider_data_runtime import stop_process

                if pid := self._process.process.pid:
                    stop_process(declaration.id, pid)
            if self._approval_watch is not None:
                self._approval_watch.cancel()
                await asyncio.gather(self._approval_watch, return_exceptions=True)
                self._approval_watch = None
            if self._process is not None:
                stop = await self._process.close()
                if stop != "confirmed":
                    log.warning("acp.stop_unconfirmed", profile=self.profile.id)
        finally:
            self._process = None
            self._leases.close()
            _LIVE.discard(self)

    async def _reap_idle(self) -> None:
        # turn が先に開始した場合は、その turn が次の idle deadline を設定する。
        if self._lock.locked():
            return
        async with self._lock:
            await _finish_cleanup(asyncio.create_task(self._close()))

    async def _connect(self) -> tuple[AcpClient, bool]:
        if self._process is not None and self._process.process.returncode is None:
            return self._process, True
        if self._process is not None:
            await _finish_cleanup(asyncio.create_task(self._close()))
        sem = _HOME_SEMAPHORES.get(self.profile.id, self.home, self.profile.max_concurrent_per_home)
        # 同じ home の idle process を先に閉じ、別の会話でもログイン済み home を使えるようにする。
        if sem.locked():
            idle = next((item for item in _LIVE if item._idle is not None
                         and item.profile.id == self.profile.id and item.home == self.home), None)
            if idle is not None:
                await idle._reap_idle()
        if len(_LIVE) >= MAX_LIVE_PROCESSES:
            idle = next((item for item in _LIVE if item._idle is not None), None)
            if idle is not None:
                await idle._reap_idle()
        if len(_LIVE) >= MAX_LIVE_PROCESSES or sem.locked():
            raise AcpError("capacity_exhausted")
        _LIVE.add(self)
        try:
            self._leases.enter_context(use_login_home(self.home))
        except PrintCliHomeBusyError:
            raise AcpError("login_in_progress") from None
        await sem.acquire()
        self._leases.callback(sem.release)
        spec = self.profile.acp
        assert spec is not None
        if self._fixed_cwd is None:
            self.cwd = Path(self._leases.enter_context(tempfile.TemporaryDirectory(prefix="kotoyose-acp-chat-")))
        declaration = self.profile.data_declaration
        mcp_servers = self.mcp_servers
        child_home = None
        approval = None
        capability = "write" if self.policy.sandboxed_child_write else "read"
        require_empty_grok_mcp = False
        if declaration is not None:
            from ..provider_data import GROK_MCP_GUARD, reject_option_value
            from ..provider_data_child import approved_for, execution_home, watch_approval
            from ..provider_data_runtime import (
                acp_data_env, checked_write_cwd, managed_command, register_declared_process, require_acp_landlock,
            )

            if self.policy.unrestricted_child:
                from ..child_process_tree import bound_windows_job
                from ..cli_install_spec import platform_key

                target = declaration.platform(platform_key())
                if (target is None or not target.supports(UNRESTRICTED)
                        or self._fixed_cwd is None or declaration.acp_unrestricted is None):
                    raise AcpError("unsupported_acp_write")
                bound_windows_job()
                capability = UNRESTRICTED
                spec = declaration.acp_unrestricted
            elif self.policy.mcp_names:
                capability = "mcp"
            require_empty_grok_mcp = capability == "mcp" or (
                capability == UNRESTRICTED and declaration.mcp_guard == GROK_MCP_GUARD
            )
            if self.policy.mcp_names and declaration.mcp_guard != GROK_MCP_GUARD:
                raise AcpError("unsupported_mcp_guard")
            if not self.policy.mcp_names:
                mcp_servers = []
            try:
                reject_option_value(self.model, "model")
            except ValueError as exc:
                raise AcpError("unsupported_acp_model") from exc
            child_home = execution_home()
            if child_home is not None:
                approval = approved_for(child_home, declaration, capability)
            if capability in ("write", "mcp", UNRESTRICTED):
                if child_home is None or self._fixed_cwd is None:
                    raise AcpError("unsupported_acp_write")
                self.cwd = checked_write_cwd(self._fixed_cwd, child_home, declaration=declaration)
            if capability in ("write", "mcp"):
                if declaration.acp_write is None:
                    raise AcpError("unsupported_acp_write")
                spec = declaration.acp_write
            require_acp_landlock(spec)
            env = acp_data_env(declaration, child_home, self._leases)
            env.update(spec.env)
            command = managed_command(declaration, child_home)
            self.binary = Path(command[0])
        else:
            command = (str(self.binary),)
            env = build_print_cli_env(self.profile, self.home, minimal=True)
            env.update(spec.env)
        self._place_instructions(env)
        self._process = await AcpClient.spawn(
            [*command, *spec.arguments(self.model, mcp_servers=mcp_servers)],
            cwd=self.cwd, env=env,
        )
        if declaration is not None and (pid := self._process.process.pid):
            try:
                register_declared_process(declaration.id, pid)
            except Exception:
                await _finish_cleanup(asyncio.create_task(self._close()))
                raise
            if child_home is not None and approval is not None:
                # The approval can change while the subprocess is starting.
                approved_for(child_home, declaration, capability, generation=approval.generation)
                self._approval_watch = asyncio.create_task(watch_approval(child_home, approval, pid))
        resumed = await self._process.start_session(
            spec, cwd=str(self.cwd), mcp_servers=mcp_servers,
            session_id=self.session_id, model=self.model, require_empty_grok_mcp=require_empty_grok_mcp,
        )
        self.session_id = self._process.session_id
        return self._process, resumed

    def _place_instructions(self, env: dict[str, str]) -> None:
        resources = self._resources
        if uses_grok_native_skills(self.profile, self.binary):
            if self.profile.data_declaration is not None:
                home = Path(env["HOME"])
            else:
                home = Path(self._leases.enter_context(tempfile.TemporaryDirectory(prefix="kotoyose-acp-home-")))
            grok_skill_env(env, home, self._repository_skills, self.profile)
            if resources is None:
                return
            try:
                text = resources.place_grok(home)
            except ValueError as exc:
                raise DeclarationRejected(str(exc)) from None
        elif self._repository_skills is not None:
            raise DeclarationRejected("repository_skills requires the pinned Linux x64 Grok 1.0.89 binary")
        elif resources is not None and resources.folders:
            root = Path(self._leases.enter_context(tempfile.TemporaryDirectory(prefix="kotoyose-acp-skills-")))
            text = resources.place(root / "skills")
        else:
            return
        self._effective_instructions = _with_output_schema(text, self._output_schema)

    async def _run(self, prompt: RuntimePrompt, turn: AcpTurn) -> RuntimeResult:
        if self._idle is not None:
            self._idle.cancel()
            self._idle = None
        prompt_task: asyncio.Task | None = None
        try:
            if self.invalidated:
                raise AcpError("session_closed")
            if prompt_has_images(prompt):
                raise AcpError("unsupported_input")
            deadline = asyncio.get_running_loop().time() + self.timeout
            async with asyncio.timeout_at(deadline):
                client, resumed = await self._connect()
                user_text = prompt.history if isinstance(prompt, AcpPrompt) and not resumed else prompt_text(prompt)
                text = compose_print_cli_prompt(self._effective_instructions, user_text)
                if len(text.encode()) > self.profile.max_prompt_bytes:
                    raise AcpError("unsupported_input")
                # Keep the prompt reader alive for session/cancel during cleanup.
                prompt_task = asyncio.create_task(turn.run(client, text, timeout=None, turn_deadline=deadline))
                await asyncio.shield(prompt_task)
            self._idle = asyncio.get_running_loop().call_later(
                self.idle_seconds, lambda: asyncio.create_task(self._reap_idle()),
            )
            return self._result(turn)
        except GrokMcpGuardError:
            await _finish_cleanup(asyncio.create_task(self._close()))
            raise
        except BaseException:
            async def cleanup() -> None:
                try:
                    if self._process is not None:
                        await self._process.cancel(prompt_task)
                    if prompt_task is not None:
                        prompt_task.cancel()
                        await asyncio.gather(prompt_task, return_exceptions=True)
                finally:
                    await self._close()

            await _finish_cleanup(asyncio.create_task(cleanup()))
            raise

    def _result(self, turn: AcpTurn) -> RuntimeResult:
        usage = {key: value for key, value in turn.usage.items() if value is not None}
        return RuntimeResult(bytes(turn.content).decode("utf-8", errors="replace"),
                             usage=usage, runtime_session_id=self.session_id, provider=self.provider)

    async def _run_turn(self, prompt: RuntimePrompt, turn: AcpTurn) -> RuntimeResult:
        async with self._lock:
            try:
                return await self._run(prompt, turn)
            except DeclarationRejected as exc:
                raise RuntimeTurnError(str(exc), partial_result=self._result(turn), retryable=False) from None
            except GrokMcpGuardError as exc:
                raise RuntimeTurnError(
                    exc.user_message, partial_result=self._result(turn), retryable=False,
                    diagnostics={"terminal_reason": exc.reason},
                ) from None
            except (AcpError, TimeoutError, OSError) as exc:
                reason = exc.reason if isinstance(exc, AcpError) else (
                    "timeout" if isinstance(exc, TimeoutError) else "provider_error"
                )
                log.warning(
                    "acp.turn_failed",
                    profile=self.profile.id,
                    reason=reason,
                    code=exc.code if isinstance(exc, AcpError) else None,
                    agent_message=(exc.agent_message[:200] if isinstance(exc, AcpError) and exc.agent_message else None),
                )
                raise RuntimeTurnError(
                    f"ACP turn failed ({reason})",
                    partial_result=self._result(turn), diagnostics={"terminal_reason": reason},
                    retryable=False if reason in {"unsupported_input", "unsupported_acp_model",
                                                   "authentication_failed", "unsupported_mcp_transport"} else None,
                ) from None

    async def ask(self, prompt: RuntimePrompt) -> RuntimeResult:
        """非ストリーミングで応答を返す。許可判定はログに記録する。"""
        async def emit(item: str | RuntimeEvent) -> None:
            self._audit_permission(item)
            if isinstance(item, RuntimeEvent):
                emit_runtime_event(item)

        return await self._run_turn(prompt, AcpTurn(
            self.policy, 1024 * 1024, emit, usage_parser=acp_usage_parser(self.profile),
            approvals=current_turn_approvals.get(),
        ))

    @staticmethod
    def _audit_permission(item: str | RuntimeEvent) -> None:
        if isinstance(item, RuntimeEvent) and item.kind == "permission":
            log.info("acp.permission", decision=item.summary, tool_kind=item.tool_name, **(item.payload or {}))

    async def stream(self, prompt: RuntimePrompt) -> AsyncIterator[str | RuntimeEvent | RuntimeResult]:
        """テキストとイベントを順に送る。購読中断時は turn とプロセスを回収する。"""
        queue: asyncio.Queue = asyncio.Queue(maxsize=32)

        async def emit(item: str | RuntimeEvent) -> None:
            self._audit_permission(item)
            await queue.put(item)

        async def run():
            try:
                return await self._run_turn(prompt, AcpTurn(
                    self.policy, 1024 * 1024, emit, usage_parser=acp_usage_parser(self.profile),
                    approvals=current_turn_approvals.get(),
                ))
            finally:
                # 購読中断で queue が満杯なら、呼び出し側が task を取り消して回収する。
                await queue.put(_END)

        task = asyncio.create_task(run())
        try:
            while (item := await queue.get()) is not _END:
                yield item
            result = await task
        finally:
            if not task.done():
                task.cancel()
                with CancelScope(shield=True):
                    # queue が満杯でも producer の終了通知を塞がない。
                    while not queue.empty():
                        queue.get_nowait()
                    with suppress(asyncio.CancelledError, RuntimeTurnError):
                        await _finish_cleanup(task)
            elif not task.cancelled():
                task.exception()
        yield result


def acp_binding(
    config: HarnessConfig, preset: ModelPreset, cwd_override: str | None, *, instructions: CliInstructions | None = None,
) -> str:
    """会話再利用を route、login home、MCP、ツール方針、cwd と解決済みの指示文に束縛する。"""
    route = config.routes[preset.route]
    conn = config.connections[route.connection]
    _, _, servers = config.resolve_resources(preset)
    profile = get_print_cli_profile(route.profile)
    shape = {
        "route": preset.route, "connection": route.connection, "profile": profile.profile_digest(acp=True),
        "home": str(conn.config_dir.expanduser().resolve()) if conn.config_dir else None, "cwd": str(Path(cwd_override or preset.cwd).resolve()),
        "agent": preset.agent.model_dump() if preset.agent is not None else None,
        "tools": preset.tools, "deny": preset.deny, "mcp": servers,
        "prompt_files": config.resolve_prompt_files(preset),
        "timeout": preset.timeout_seconds,
    }
    if instructions is not None:
        shape["instructions"] = instructions.binding
    return hashlib.sha256(json.dumps(shape, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def build_acp_session(
    preset: ModelPreset, cwd_override: str | None, session_id: str | None, headers: dict[str, str] | None,
    *, config: HarnessConfig | None = None, settings: NativeExecutionConfig | None = None,
    child_data_capability: str | None = None, instructions: CliInstructions | None = None,
) -> AcpRuntimeSession:
    """route と alias から、まだプロセスを持たないホスト用セッションを作る。"""
    from ..config import _inject_mcp_request_headers, resolve_mcp_secrets

    config = config or get_config()
    route = config.routes.get(preset.route or "")
    if route is None or route.driver != "cli.acp" or route.profile is None:
        raise SDKConnectionError("ACP chat requires a configured cli.acp route")
    if preset.place == "jail" or (preset.agent is not None and preset.agent.place != "host"):
        raise SDKConnectionError("ACP chat requires host placement")
    conn = config.connections[route.connection]
    profile = get_print_cli_profile(route.profile)
    declaration = profile.data_declaration
    if declaration is not None:
        if conn.config_dir is not None or conn.kind != "subscription":
            raise SDKConnectionError("data providers use the normal OS login location")
        login_home = Path.home()
    elif conn.config_dir is None or conn.kind != "subscription":
        raise SDKConnectionError("ACP chat requires a CLI login home")
    else:
        login_home = conn.config_dir.expanduser().resolve()
    binary = resolve_print_cli_binary(profile, settings)
    if binary is None:
        raise SDKConnectionError(f"Set drivers.cli.print.profile_binaries.{profile.id} to an available executable")
    if declaration is not None:
        from ..cli_install_spec import platform_key
        from ..provider_data import child_capability, require_unrestricted_alias
        from ..provider_data_child import execution_home
        from ..provider_data_runtime import read_permission_policy

        servers = {}
        if child_data_capability is not None:
            target = declaration.platform(platform_key())
            surface = preset.agent.surface if preset.agent is not None else "read"
            if (target is None or not target.supports(child_data_capability)
                    or execution_home() is None or not cwd_override or surface != "write"):
                raise SDKConnectionError("data provider write requires an approved child workspace")
            if child_data_capability == UNRESTRICTED:
                require_unrestricted_alias(
                    preset.on, surface, preset.allow is not None and preset.allow.full, route.driver,
                    tools=preset.tools, deny=preset.deny,
                )
            elif child_capability(surface, has_mcp=bool(preset.mcp_refs)) != child_data_capability:
                raise SDKConnectionError("data provider write requires an approved child workspace")
            if child_data_capability in ("mcp", UNRESTRICTED):
                # The child builder gets its catalog from _prepare's policy projection.
                servers = {name: config.mcps[name].server for name in preset.mcp_refs}
    else:
        _, _, servers = config.resolve_resources(preset)
        servers = _inject_mcp_request_headers(
            resolve_mcp_secrets(servers), headers or {},
            builtin_delegation=not config._child_mcp_projection and preset.has_delegation_mcp,
        )
    resources = instructions or resolve_cli_resources(config, preset)
    text = _with_output_schema(resources.text(), preset.output_schema)
    if declaration is not None and child_data_capability is None:
        policy = read_permission_policy()
    elif child_data_capability == UNRESTRICTED:
        policy = AcpPermissionPolicy("write", "host", unrestricted_child=True, mcp_names=tuple(servers))
    else:
        policy = AcpPermissionPolicy(
            preset.agent.surface if preset.agent is not None else "read", "host",
            tuple(preset.tools) if preset.tools is not None else None, tuple(preset.deny),
            sandboxed_child_write=declaration is not None and child_data_capability is not None,
            mcp_names=tuple(servers) if child_data_capability == "mcp" else (),
        )
    cwd = cwd_override or (preset.cwd if preset.repository_skills == "include" else None)
    return AcpRuntimeSession(
        profile, binary.absolute(), login_home,
        Path(cwd).expanduser().resolve() if cwd else None, route.model, preset.timeout_seconds or 600.0,
        text, acp_mcp_servers(servers), policy, session_id, resources=resources, output_schema=preset.output_schema,
        repository_skills=preset.repository_skills,
    )


def _with_output_schema(instructions: str, schema: dict | None) -> str:
    if schema is None:
        return instructions
    return compose_print_cli_prompt(instructions, "", output_contract="json", output_schema=schema)
