"""ACP v1 の stdio 接続。1 接続につき同時に 1 リクエストを処理する。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
import json
import os
import subprocess
from pathlib import Path
from types import MappingProxyType
from typing import Any

import structlog

from .acp_spec import AcpSpec
from .host_place import _shutdown
from .print_cli_events import read_stderr_tail

log = structlog.get_logger()

LINE_LIMIT = 1024 * 1024
CONTROL_TIMEOUT = 30.0
UpdateHandler = Callable[[dict], Awaitable[None]]
PermissionHandler = Callable[[dict], Awaitable[dict]]


class AcpError(RuntimeError):
    """外部由来のメッセージ本文を含まない ACP エラー。"""

    def __init__(self, reason: str, code: int | None = None, *, agent_message: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code
        self.agent_message = agent_message


_UNSCOPED_REPORT = object()


class GrokMcpGuardError(AcpError):
    def __init__(self, message: str) -> None:
        super().__init__("grok_mcp_guard_failed")
        self.user_message = message


def _objects(items: Any) -> list[dict]:
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _named_values(items: Any, key: str) -> dict[str, str | None]:
    return {
        item[key]: item["name"] if isinstance(item.get("name"), str) else None
        for item in _objects(items) if isinstance(item.get(key), str)
    }


def _select_values(option: dict) -> dict[str, str | None]:
    values = {}
    for item in _objects(option.get("options")):
        if isinstance(item.get("group"), str):
            values.update(_named_values(item.get("options"), "value"))
        else:
            values.update(_named_values([item], "value"))
    return values


def _has_config_value(options: list[dict], config_id: str, value: str) -> bool:
    return any(item.get("id") == config_id and item.get("currentValue") == value for item in options)


class AcpClient:
    """起動済みの stdio に接続する。close は呼び出し側が必ず実行する。"""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        from .child_process_tree import TURN_PIDS

        self.process = process
        self._windows_pids = TURN_PIDS.get() if os.name == "nt" else None
        self.capabilities: Mapping[str, Any] = MappingProxyType({})
        self.session_id: str | None = None
        self._next_id = 0
        self._request_lock = asyncio.Lock()
        self._background: list[asyncio.Task] = []
        self._stop_confirmation: str | None = None
        self._require_empty_grok_mcp = False
        self._grok_empty_report_sessions: set[str] | None = None

    @classmethod
    async def spawn(cls, argv: Sequence[str], *, cwd: Path, env: Mapping[str, str]) -> AcpClient:
        """専用プロセスセッションに起動し、stderr と終了後の子孫を回収する。"""
        from .child_process_tree import TURN_PIDS
        from .print_cli_agent import _reap_session_after_exit

        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
        spawn = asyncio.create_task(asyncio.create_subprocess_exec(
            *argv, cwd=cwd, env=env, **options,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=LINE_LIMIT,
        ))
        try:
            process = await asyncio.shield(spawn)
        except asyncio.CancelledError:
            async def reap_late() -> None:
                with suppress(OSError):
                    process = await spawn
                    await _shutdown_acp(process, 0.5, pids=TURN_PIDS.get())

            cleanup = asyncio.create_task(reap_late())
            await _finish_cleanup(cleanup)
            raise
        client = cls(process)
        assert process.stderr is not None
        client._background.append(asyncio.create_task(read_stderr_tail(process.stderr, max_bytes=32768)))
        if os.name != "nt":
            client._background.append(asyncio.create_task(_reap_session_after_exit(process)))
        return client

    async def _send(self, **message: Any) -> None:
        data = json.dumps({"jsonrpc": "2.0", **message}, ensure_ascii=False, allow_nan=False).encode()
        if len(data) > LINE_LIMIT:
            raise AcpError("output_limit_exceeded")
        assert self.process.stdin is not None
        try:
            self.process.stdin.write(data + b"\n")
            await self.process.stdin.drain()
        except (BrokenPipeError, ConnectionError):
            raise AcpError("missing_completion") from None

    async def _receive(self) -> dict:
        assert self.process.stdout is not None
        try:
            raw = await self.process.stdout.readline()
        except ValueError:
            raise AcpError("output_limit_exceeded") from None
        if not raw:
            raise AcpError("missing_completion")
        if len(raw) > LINE_LIMIT + 1:
            raise AcpError("output_limit_exceeded")
        if not raw.endswith(b"\n"):
            raise AcpError("invalid_acp_message")
        try:
            message = json.loads(raw.decode("utf-8"))
        except (ValueError, RecursionError):
            raise AcpError("invalid_acp_message") from None
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            raise AcpError("invalid_acp_message")
        return message

    async def _request(
        self, method: str, params: dict, *, timeout: float | None = CONTROL_TIMEOUT,
        update: UpdateHandler | None = None, permission: PermissionHandler | None = None,
    ) -> dict:
        async with asyncio.timeout(timeout), self._request_lock:
            self._next_id += 1
            request_id = self._next_id
            await self._send(id=request_id, method=method, params=params)
            message = await self._receive_response(update, permission)
            return self._response(message, request_id)

    @staticmethod
    def _response(message: dict, request_id: int) -> dict:
        if type(message.get("id")) is not int or message["id"] != request_id:
            raise AcpError("invalid_acp_message")
        if "error" in message and "result" not in message:
            error = message["error"]
            code = error.get("code") if isinstance(error, dict) else None
            if type(code) is not int:
                raise AcpError("invalid_acp_message")
            reason = "authentication_failed" if code == -32000 else "provider_error"
            detail = error.get("message")
            raise AcpError(reason, code, agent_message=detail if isinstance(detail, str) else None)
        result = message.get("result")
        if not isinstance(result, dict) or "error" in message:
            raise AcpError("invalid_acp_message")
        return result

    async def _receive_response(
        self, update: UpdateHandler | None, permission: PermissionHandler | None,
    ) -> dict:
        permissions: set[asyncio.Task[None]] = set()
        try:
            while True:
                message = await self._receive_with_permissions(permissions) if permissions else await self._receive()
                if "method" not in message:
                    if type(message.get("id")) is str:
                        # この client の要求 id は整数だけ。文字列 id の応答は agent 自身の内部要求への応答
                        # (Grok Build の "skills-reload" など) なので読み捨てる。
                        continue
                    return message
                await self._dispatch(message, update, permission, permissions)
        finally:
            for task in permissions:
                task.cancel()
            if permissions:
                await asyncio.gather(*permissions, return_exceptions=True)

    async def _receive_with_permissions(self, permissions: set[asyncio.Task[None]]) -> dict:
        receive = asyncio.create_task(self._receive())
        try:
            while permissions:
                await asyncio.wait({receive, *permissions}, return_when=asyncio.FIRST_COMPLETED)
                for task in tuple(permissions):
                    if task.done():
                        permissions.remove(task)
                        task.result()
                if receive.done():
                    break
            return await receive
        finally:
            if not receive.done():
                receive.cancel()
            await asyncio.gather(receive, return_exceptions=True)

    async def _reply_permission(self, request_id: str | int, params: dict, permission: PermissionHandler) -> None:
        await self._send(id=request_id, result=await permission(params))

    async def _dispatch(
        self, message: dict, update: UpdateHandler | None, permission: PermissionHandler | None,
        permissions: set[asyncio.Task[None]],
    ) -> None:
        method = message["method"]
        if not isinstance(method, str):
            raise AcpError("invalid_acp_message")
        params = message.get("params")
        if not isinstance(params, dict):
            params = {}
        if method == "_x.ai/mcp/servers_updated":
            if "id" in message:
                if type(message["id"]) not in (str, int):
                    raise AcpError("invalid_acp_message")
                await self._send(id=message["id"], result={})
            self._grok_mcp_updated(params)
            return
        matches_session = self.session_id is not None and params.get("sessionId") == self.session_id
        if "id" in message:
            request_id = message["id"]
            if type(request_id) not in (str, int):
                raise AcpError("invalid_acp_message")
            if method == "session/request_permission":
                if matches_session and permission is not None:
                    permissions.add(asyncio.create_task(self._reply_permission(request_id, params, permission)))
                else:
                    await self._send(id=request_id, result={"outcome": {"outcome": "cancelled"}})
            else:
                await self._send(id=request_id, error={"code": -32601, "message": "Method not found"})
        elif method == "session/update" and matches_session and update is not None:
            item = params.get("update")
            if isinstance(item, dict):
                await update(item)

    def _grok_mcp_updated(self, params: dict) -> None:
        servers = params.get("mcpServers")
        if servers == []:
            session_id = params.get("sessionId")
            if self._grok_empty_report_sessions is not None:
                if isinstance(session_id, str):
                    self._grok_empty_report_sessions.add(session_id)
                elif session_id is None:
                    # Grok 1.0.89 は sessionId を割り当てる前 (mcp_merge の段) にこの通知を送る。
                    # session/new の要求中に届いた id なしの通知は、その要求の分として数える
                    # (要求は _request_lock で 1 本ずつ)。
                    self._grok_empty_report_sessions.add(_UNSCOPED_REPORT)
            return
        if not isinstance(servers, list):
            if self._require_empty_grok_mcp:
                raise GrokMcpGuardError("Grok returned an invalid native MCP server list.")
            return
        names = [item.get("name") if isinstance(item, dict) else item for item in servers[:16]]
        details = ", ".join(ascii(name[:80]) for name in names if isinstance(name, str) and name)
        if not details:
            details = "unknown names"
        if len(servers) > 16:
            details += f" (+{len(servers) - 16} more)"
        if self._require_empty_grok_mcp:
            raise GrokMcpGuardError(
                f"Grok loaded native MCP servers ({details}). Disable them in Grok with `grok mcp disable <name>`."
            )
        log.warning("acp.grok_native_mcp", servers=details)

    async def start_session(
        self, spec: AcpSpec, *, cwd: str, mcp_servers: list[dict],
        session_id: str | None = None, model: str | None = None,
        require_empty_grok_mcp: bool = False,
    ) -> bool:
        """機能を交渉し、既存セッションを読み込むか作成する。load 時の履歴は破棄する。"""
        if not Path(cwd).is_absolute():
            raise AcpError("invalid_acp_cwd")
        self._require_empty_grok_mcp = require_empty_grok_mcp
        response = await self._request("initialize", {
            "protocolVersion": 1,
            "clientCapabilities": {
                "fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False,
            },
        })
        if type(response.get("protocolVersion")) is not int or response["protocolVersion"] != 1:
            raise AcpError("unsupported_acp_version")
        caps = response.get("agentCapabilities") or {}
        if not isinstance(caps, dict):
            raise AcpError("invalid_acp_message")
        self.capabilities = MappingProxyType(caps)
        methods = response.get("authMethods") or []
        if not isinstance(methods, list):
            raise AcpError("invalid_acp_message")
        advertised = [method.get("id") for method in methods if isinstance(method, dict)
                      and method.get("type") in (None, "agent")]
        auth = next((item for item in spec.home_auth_methods if item in advertised), None)

        mcp_caps = caps.get("mcpCapabilities") or {}
        for server in mcp_servers:
            transport = server.get("type")
            if transport and not (isinstance(mcp_caps, dict) and mcp_caps.get(transport) is True):
                raise AcpError("unsupported_mcp_transport")
        params = {"cwd": cwd, "mcpServers": mcp_servers}
        # The Grok extension was verified during session/new.
        resumed = bool(session_id and caps.get("loadSession") is True and not require_empty_grok_mcp)
        if resumed:
            params["sessionId"] = session_id
        method = "session/load" if resumed else "session/new"
        empty_report_sessions: set[object] = set()
        self._grok_empty_report_sessions = empty_report_sessions if require_empty_grok_mcp else None
        try:
            try:
                result = await self._request(method, params)
            except AcpError as exc:
                if exc.code != -32000 or auth is None:
                    raise
                await self._request("authenticate", {"methodId": auth})
                empty_report_sessions.clear()
                result = await self._request(method, params)
        finally:
            self._grok_empty_report_sessions = None
        sid = session_id if resumed else result.get("sessionId")
        if not isinstance(sid, str) or not sid:
            raise AcpError("invalid_acp_message")
        if require_empty_grok_mcp and sid not in empty_report_sessions and _UNSCOPED_REPORT not in empty_report_sessions:
            raise GrokMcpGuardError(
                "Grok did not report an empty native MCP server list for the new session. "
                "Check the installed Grok Build version and disable any native servers with `grok mcp disable <name>`."
            )
        self.session_id = sid
        if model is not None and not spec.model_at_launch:
            # load で選択情報が省略された場合は、保存済みモデル設定を引き継ぐ。
            if not resumed or result.get("configOptions") or result.get("models"):
                await self._select_model(result, model)
        return resumed

    async def _select_model(self, session: dict, model: str) -> None:
        models = session.get("models")
        models = models if isinstance(models, dict) else {}
        advertised = _named_values(models.get("availableModels"), "modelId")
        options = _objects(session.get("configOptions"))
        model_option = next((item for item in options if item.get("type") == "select"
                             and isinstance(item.get("id"), str)
                             and (item.get("category") == "model" or item["id"] == "model")), {})
        config_models = _select_values(model_option)
        set_model_available = True

        async def set_model(model_id: str) -> bool:
            nonlocal set_model_available
            if models.get("currentModelId") == model_id:
                return True
            if not set_model_available:
                return False
            try:
                await self._request("session/set_model", {"sessionId": self.session_id, "modelId": model_id})
            except AcpError as exc:
                if exc.code != -32601:
                    raise
                log.debug("acp_set_model_unavailable", requested_model=model, agent_message=exc.agent_message)
                set_model_available = False
                return False
            return True

        try:
            if model in advertised and await set_model(model):
                return
            if model in config_models:
                await self._set_select_option(options, model_option["id"], model)
                return

            matches = {
                model_id for choices in (advertised, config_models) for model_id, name in choices.items()
                if isinstance(name, str) and name.casefold() == model.casefold()
            }
            if len(matches) > 1:
                raise AcpError("unsupported_acp_model")
            if matches:
                model_id = matches.pop()
                if model_id in advertised and await set_model(model_id):
                    return
                if model_id in config_models:
                    await self._set_select_option(options, model_option["id"], model_id)
                    return

            base, separator, level = model.rpartition("-")
            if separator and base in config_models:
                thought = next((item for item in options if item.get("type") == "select"
                                and item.get("category") == "thought_level"
                                and isinstance(item.get("id"), str)
                                and level in _select_values(item)), None)
                if thought is not None:
                    model_id = model_option["id"]
                    options = await self._set_select_option(options, model_id, base)
                    options = await self._set_select_option(options, thought["id"], level)
                    if _has_config_value(options, model_id, base):
                        return
            raise AcpError("unsupported_acp_model")
        except AcpError as exc:
            if exc.code is None and exc.reason != "unsupported_acp_model":
                raise
            log.warning(
                "unsupported_acp_model", requested_model=model, available_models=advertised,
                available_config_models=config_models, agent_message=exc.agent_message,
            )
            raise AcpError("unsupported_acp_model") from None

    async def _set_select_option(self, options: list[dict], config_id: str, value: str) -> list[dict]:
        if _has_config_value(options, config_id, value):
            return options
        response = await self._request("session/set_config_option", {
            "sessionId": self.session_id, "configId": config_id, "value": value,
        })
        confirmed = _objects(response.get("configOptions"))
        if not _has_config_value(confirmed, config_id, value):
            raise AcpError("unsupported_acp_model")
        return confirmed

    async def prompt(
        self, text: str, *, timeout: float | None, update: UpdateHandler, permission: PermissionHandler,
    ) -> dict:
        """最後の stopReason を返すまで、承認への回答を待ちながら更新を処理する。"""
        if self.session_id is None:
            raise AcpError("missing_acp_session")
        return await self._request(
            "session/prompt", {"sessionId": self.session_id, "prompt": [{"type": "text", "text": text}]},
            timeout=timeout, update=update, permission=permission,
        )

    async def cancel(self, pending: asyncio.Task | None = None) -> None:
        """進行中の turn の取消を通知する。相手の応答待ちは上限付き。"""
        if self.session_id is not None and self.process.returncode is None:
            with suppress(AcpError, TimeoutError):
                async with asyncio.timeout(0.25):
                    await self._send(method="session/cancel", params={"sessionId": self.session_id})
                if pending is not None:
                    await asyncio.wait({pending}, timeout=0.2)

    async def close(self, *, grace_seconds: float = 0.5) -> str:
        """ホストのプロセスセッションを回収する。jail 側の削除は呼び出し側が行う。"""
        if self._stop_confirmation is not None:
            return self._stop_confirmation
        try:
            # 上限超過で stdout の読み取りを中断した場合も終了時に pipe を空にする。
            with suppress(BrokenPipeError, ConnectionError):
                if self.process.stdin is not None:
                    self.process.stdin.close()
            assert self.process.stdout is not None

            async def drain_stdout() -> None:
                while await self.process.stdout.read(65536):
                    pass

            drain = asyncio.create_task(drain_stdout())
            self._background.append(drain)
            self._stop_confirmation = await _shutdown_acp(self.process, grace_seconds, pids=self._windows_pids)
            return self._stop_confirmation
        finally:
            for task in self._background:
                task.cancel()
            await asyncio.gather(*self._background, return_exceptions=True)


async def _shutdown_acp(process: asyncio.subprocess.Process, grace: float, *, pids: set[int] | None) -> str:
    if os.name != "nt":
        return await _shutdown(process, grace)
    if pids is not None:
        from .child_process_tree import kill_pids

        with suppress(OSError):
            kill_pids(pids)
    if process.returncode is None:
        with suppress(ProcessLookupError):
            process.kill()
    try:
        await asyncio.wait_for(process.wait(), 5)
    except (OSError, TimeoutError):
        return "unconfirmed"
    return "confirmed"


async def _finish_cleanup(task: asyncio.Task) -> None:
    # 起動や cleanup の所有権が確定するまで、繰り返しの cancel でも待つ。
    interrupted = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            interrupted = True
    task.result()
    if interrupted:
        raise asyncio.CancelledError
