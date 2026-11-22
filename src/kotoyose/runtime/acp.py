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
from ..acp_client import AcpClient, AcpError, _finish_cleanup
from ..acp_turn import AcpPermissionPolicy, AcpTurn, acp_mcp_servers, acp_usage_parser
from ..config import HarnessConfig, ModelPreset, get_config
from ..execution_resources import NativeExecutionConfig
from ..print_cli_agent import _HOME_SEMAPHORES, compose_print_cli_prompt
from ..print_cli_home import PrintCliHomeBusyError, use_login_home
from ..print_cli_profiles import (
    PrintCliProfile, build_print_cli_env, get_print_cli_profile, resolve_print_cli_binary,
)
from .base import RuntimeEvent, RuntimePrompt, RuntimeResult, RuntimeTurnError, prompt_has_images, prompt_text

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
        self.mcp_servers = mcp_servers
        self.policy = policy
        self.session_id = session_id
        self.idle_seconds = idle_seconds
        self._lock = asyncio.Lock()
        self._process: AcpClient | None = None
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
        env = build_print_cli_env(self.profile, self.home, minimal=True)
        env.update(spec.env)
        self._process = await AcpClient.spawn(
            [str(self.binary), *spec.arguments(self.model, mcp_servers=self.mcp_servers)],
            cwd=self.cwd, env=env,
        )
        resumed = await self._process.start_session(
            spec, cwd=str(self.cwd), mcp_servers=self.mcp_servers,
            session_id=self.session_id, model=self.model,
        )
        self.session_id = self._process.session_id
        return self._process, resumed

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
            async with asyncio.timeout(self.timeout):
                client, resumed = await self._connect()
                user_text = prompt.history if isinstance(prompt, AcpPrompt) and not resumed else prompt_text(prompt)
                text = compose_print_cli_prompt(self.instructions, user_text)
                if len(text.encode()) > self.profile.max_prompt_bytes:
                    raise AcpError("unsupported_input")
                prompt_task = asyncio.create_task(turn.run(client, text, timeout=self.timeout))
                await asyncio.shield(prompt_task)
            self._idle = asyncio.get_running_loop().call_later(
                self.idle_seconds, lambda: asyncio.create_task(self._reap_idle()),
            )
            return self._result(turn)
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
            except (AcpError, TimeoutError, OSError) as exc:
                reason = exc.reason if isinstance(exc, AcpError) else (
                    "timeout" if isinstance(exc, TimeoutError) else "provider_error"
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

        return await self._run_turn(prompt, AcpTurn(self.policy, 1024 * 1024, emit, usage_parser=acp_usage_parser(self.profile)))

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
                return await self._run_turn(prompt, AcpTurn(self.policy, 1024 * 1024, emit, usage_parser=acp_usage_parser(self.profile)))
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


def acp_binding(config: HarnessConfig, preset: ModelPreset, cwd_override: str | None) -> str:
    """会話再利用を route、login home、MCP 設定、ツール方針と cwd に束縛する。"""
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
    return hashlib.sha256(json.dumps(shape, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def build_acp_session(
    preset: ModelPreset, cwd_override: str | None, session_id: str | None, headers: dict[str, str] | None,
    *, config: HarnessConfig | None = None, settings: NativeExecutionConfig | None = None,
) -> AcpRuntimeSession:
    """route と alias から、まだプロセスを持たないホスト用セッションを作る。"""
    from ..config import _inject_mcp_request_headers, resolve_mcp_secrets, resolve_prompt_texts
    from ..delegation import MCP_NAME
    from ..session import SDKConnectionError

    config = config or get_config()
    route = config.routes.get(preset.route or "")
    if route is None or route.driver != "cli.acp" or route.profile is None:
        raise SDKConnectionError("ACP chat requires a configured cli.acp route")
    if preset.place == "jail" or (preset.agent is not None and preset.agent.place != "host"):
        raise SDKConnectionError("ACP chat requires host placement")
    conn = config.connections[route.connection]
    if conn.config_dir is None or conn.kind != "subscription":
        raise SDKConnectionError("ACP chat requires a CLI login home")
    profile = get_print_cli_profile(route.profile)
    binary = resolve_print_cli_binary(profile, settings)
    if binary is None:
        raise SDKConnectionError(f"Set drivers.cli.print.profile_binaries.{profile.id} to an available executable")
    _, _, servers = config.resolve_resources(preset)
    servers = _inject_mcp_request_headers(
        resolve_mcp_secrets(servers), headers or {}, builtin_delegation=MCP_NAME in preset.mcp_refs,
    )
    instructions = "\n".join(resolve_prompt_texts(config.resolve_prompt_files(preset)))
    if preset.output_schema is not None:
        instructions = compose_print_cli_prompt(instructions, "", output_contract="json", output_schema=preset.output_schema)
    policy = AcpPermissionPolicy(
        preset.agent.surface if preset.agent is not None else "read", "host",
        tuple(preset.tools) if preset.tools is not None else None, tuple(preset.deny),
    )
    return AcpRuntimeSession(
        profile, binary.absolute(), conn.config_dir.expanduser().resolve(),
        # Like cli.print chat: a private temporary cwd, never the alias cwd on the host.
        Path(cwd_override).resolve() if cwd_override else None, route.model, preset.timeout_seconds or 600.0,
        instructions, acp_mcp_servers(servers), policy, session_id,
    )
