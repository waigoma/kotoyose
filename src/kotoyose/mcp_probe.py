"""Explicit, read-only admin MCP probes, isolated from agent execution and config writes."""
from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import time
from urllib.parse import urlsplit

from pydantic import ValidationError

from . import process_env

PROBE_TIMEOUT_SECONDS = 15
MAX_CONFIG_BYTES = 65536
MAX_METADATA_BYTES = 1048576
MAX_TOOLS = 256
MAX_PAGES = 8
_active_probes = 0


class ProbeValidationError(ValueError):
    """Only constant, credential-free messages may cross the admin API boundary."""


def validate_probe_server(server: object) -> dict:
    from .config import McpDefinition, _MCP_REQUEST_HEADER_TEMPLATE, parse_mcp_secret_value
    try:
        server = McpDefinition(server=server).server
    except (ValidationError, ValueError, TypeError):
        raise ProbeValidationError("type と command、または type と url を確認してください") from None
    kind = server.get('type', 'stdio')
    allowed = {'type', 'command', 'args', 'env'} if kind == 'stdio' else {'type', 'url', 'headers'}
    if set(server) - allowed:
        raise ProbeValidationError("この疎通確認では type / command / args / env または type / url / headers のみ指定できます")
    # Request-scoped templates are deliberately not resolved from the admin's Basic auth.
    if isinstance(server.get('headers'), dict) and any(
        isinstance(value, str) and _MCP_REQUEST_HEADER_TEMPLATE.search(value) for value in server['headers'].values()
    ):
        raise ProbeValidationError("request header テンプレートは疎通確認で解決できません。一時的な入力値に置き換えて確認してください（保存は不要です）")
    if kind == 'stdio':
        if os.name != 'posix':
            raise ProbeValidationError("stdio の疎通確認は POSIX ホストでのみ利用できます")
        if not isinstance(server.get('command'), str) or not server['command'].strip() or '\x00' in server['command']:
            raise ProbeValidationError("stdio の command には空でない文字列が必要です")
        args = server.get('args', [])
        if not isinstance(args, list) or any(not isinstance(arg, str) or '\x00' in arg for arg in args):
            raise ProbeValidationError("args は文字列の配列で指定してください")
        values = server.get('env', {})
    else:
        try:
            url = urlsplit(server.get('url', ''))
            if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.fragment:
                raise ValueError()
            url.port
        except (TypeError, ValueError, AttributeError):
            raise ProbeValidationError("url には認証情報や fragment を含まない http(s) URL が必要です") from None
        values = server.get('headers', {})
    if not isinstance(values, dict):
        raise ProbeValidationError("env / headers は文字列のキーと値（または secret 参照）で指定してください")
    for k, v in values.items():
        if not isinstance(k, str) or not k or '\x00' in k:
            raise ProbeValidationError("env / headers は文字列のキーと値（または secret 参照）で指定してください")
        if isinstance(v, dict):
            try:
                if parse_mcp_secret_value(v) is None:
                    raise ProbeValidationError("env / headers は文字列のキーと値（または secret 参照）で指定してください")
            except ValueError:
                raise ProbeValidationError("env / headers の secret 参照が不正です") from None
        elif not isinstance(v, str) or '\x00' in v:
            raise ProbeValidationError("env / headers は文字列のキーと値（または secret 参照）で指定してください")
    if kind == 'stdio' and any('=' in key for key in values):
        raise ProbeValidationError("env のキーに = は使えません")
    if kind != 'stdio' and any('\n' in k or '\r' in k or (isinstance(v, str) and ('\n' in v or '\r' in v)) for k, v in values.items()):
        raise ProbeValidationError("headers に改行は使えません")
    if len(json.dumps(server).encode()) > MAX_CONFIG_BYTES:
        raise ProbeValidationError("MCP 設定がサイズ上限を超えています")
    return server


def probe_error(stage: str, code: str, message: str) -> dict:
    return {'status': 'error', 'stage': stage, 'error_code': code, 'message': message, 'tools': [], 'tool_count': None}


async def _stop_worker(process: asyncio.subprocess.Process) -> None:
    """Own the session/process group, including stdio children that ignore EOF or TERM."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
        if sig == signal.SIGTERM:
            await asyncio.sleep(0.15)
    await process.wait()
    if process.stdin:
        process.stdin.close()


async def run_mcp_probe(server: dict, *, disconnected=None, timeout_seconds: float = PROBE_TIMEOUT_SECONDS) -> dict:
    global _active_probes
    started = time.monotonic()
    stage = 'validation'
    process = None
    if _active_probes >= 4:
        return {**probe_error(stage, 'busy', '疎通確認が混み合っています。少し待って再試行してください'), 'elapsed_ms': 0}
    _active_probes += 1
    try:
        server = validate_probe_server(server)
        from .config import resolve_mcp_secrets_async
        from .secret_store import SecretStoreError
        try:
            server = (await resolve_mcp_secrets_async({"probe": server}))["probe"]
        except SecretStoreError as exc:
            raise ProbeValidationError(f"Secret の解決に失敗しました: {exc}") from exc
        server = validate_probe_server(server)
        stage = 'connect'
        if os.name != 'posix':
            raise ProbeValidationError('疎通確認は POSIX ホストでのみ利用できます')
        # Input is on stdin, never in argv, env overrides, files, access logs or exceptions.
        async with asyncio.timeout(timeout_seconds):
            process = await asyncio.create_subprocess_exec(
                sys.executable, '-m', 'kotoyose.mcp_probe_worker', stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True, limit=MAX_METADATA_BYTES,
                env=process_env.build_minimal_env(include_python=True),
            )
            process.stdin.write((json.dumps(server) + '\n').encode())
            await process.stdin.drain()

            async def receive():
                nonlocal stage
                while True:
                    line = await process.stdout.readline()
                    if not line:
                        return probe_error(stage, 'connection_failed', '接続を完了できませんでした。接続先と設定を確認してください')
                    event = json.loads(line)
                    if event.get('stage') in ('connect', 'initialize', 'tools_list', 'cleanup'):
                        stage = event['stage']
                    if 'status' in event:
                        return event

            async def wait_disconnect():
                while True:
                    if disconnected is not None and await disconnected():
                        return
                    await asyncio.sleep(0.1)

            result_task = asyncio.create_task(receive())
            disconnect_task = asyncio.create_task(wait_disconnect())
            try:
                done, _ = await asyncio.wait((result_task, disconnect_task), return_when=asyncio.FIRST_COMPLETED)
                if disconnect_task in done:
                    result = probe_error(stage, 'cancelled', '疎通確認を中止しました')
                else:
                    result = await result_task
            finally:
                result_task.cancel()
                disconnect_task.cancel()
                await asyncio.gather(result_task, disconnect_task, return_exceptions=True)
    except ProbeValidationError as exc:
        result = probe_error('validation', 'invalid_config', str(exc))
    except TimeoutError:
        result = probe_error(stage, 'timeout', '疎通確認がタイムアウトしました。接続先・認証・応答時間を確認してください')
    except Exception:
        # Child output, URL, command args and protocol exceptions are never logged/returned.
        result = probe_error(stage, 'connection_failed', '接続を完了できませんでした。接続先と設定を確認してください')
    finally:
        try:
            if process is not None:
                cleanup = asyncio.create_task(_stop_worker(process))
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    await cleanup
                    raise
        finally:
            _active_probes -= 1
    return {**result, 'elapsed_ms': round((time.monotonic() - started) * 1000)}
