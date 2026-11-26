"""Host-side MCP relay. HTTP credentials and stdio processes stay on the host."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import signal
import tempfile
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from functools import partial
from pathlib import Path
from urllib.parse import parse_qsl, quote

import httpx

from .child_mcp import (
    FEATURE,
    MAX_BODY,
    MAX_CALLS,
    MAX_CONCURRENT,
    MAX_PER_LINK,
    RELAY_WRITE_TIMEOUT,
    UPSTREAM_TIMEOUT,
    lost_result,
    rpc_body,
    rpc_error,
    valid_rpc_id,
    validate_request,
)
from .child_protocol import Frame
from .child_registry import child_on_name
from .config import _MCP_REQUEST_HEADER_TEMPLATE, _inject_mcp_request_headers, get_config, resolve_mcp_secrets_async
from .delegation import MCP_NAME

DELEGATION_DESCRIPTION = "Kotoyose delegation"

_MAX_ACTIVE = 64
_MAX_DELEGATION_ACTIVE = 8
_MAX_STDIO = 8
_MAX_STDIO_PER_LINK = 2
_GROUP_GRACE_SECONDS = 0.2
_STDIO_GROUP_DIR = "stdio-groups"
_active = 0
_delegation_active = 0
_stdio_live = 0


def _server_kind(server: dict) -> str | None:
    kind = server.get("type")
    if kind in {"http", "sse", "stdio"}:
        return kind
    if "command" in server:
        return "stdio"
    if "url" in server:
        return "http"
    return None


def _stdio_material(name: str, definition) -> list:
    """Public stdio contract. Environment values are secrets and stay out of the digest."""
    server = definition.server
    env = server.get("env") if isinstance(server.get("env"), dict) else {}
    bindings = sorted(
        f"{key}:{value['secret']}"
        for key, value in env.items()
        if isinstance(key, str) and isinstance(value, dict) and set(value) == {"secret"} and isinstance(value.get("secret"), str)
    )
    return [
        name,
        {
            "description": definition.description,
            "publication_revision": definition.publication_revision,
            "server": {
                "type": "stdio",
                "command": server.get("command"),
                "args": server.get("args") or [],
                "cwd": server.get("cwd"),
                "env_names": sorted(key for key in env if isinstance(key, str)),
                "secret_env": bindings,
                "enabled_tools": server.get("enabled_tools"),
                "disabled_tools": server.get("disabled_tools"),
            },
        },
    ]


def delegation_rows(config) -> list[dict]:
    """Child aliases that reference the built-in MCP, and the host aliases they may call."""
    if config is None:
        return []
    rows = []
    for alias, preset in sorted(config.models.items()):
        if child_on_name(preset.on) and f"host:{MCP_NAME}" in preset.mcp_refs:
            rows.append({"alias": alias, "on": preset.on, "delegates": sorted(preset.delegates)})
    return rows


def delegation_publication(secret: bytes, config) -> dict | None:
    """Owner-facing offer. Moving a child alias or changing its delegates requires a new approval."""
    rows = delegation_rows(config)
    if not rows:
        return None
    targets = sorted({target for row in rows for target in row["delegates"]})
    data = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
    version = hmac.new(secret, b"host-builtin-delegation/1\0" + data, hashlib.sha256).hexdigest()
    return {
        "name": MCP_NAME,
        "version": version,
        "description": DELEGATION_DESCRIPTION,
        "tools": targets,
        "all_tools": False,
    }


def publication_version(secret: bytes, name: str, definition) -> str:
    if _server_kind(definition.server) == "stdio":
        payload, prefix = _stdio_material(name, definition), b"host-stdio-mcp/1\0"
    else:
        payload, prefix = [name, definition.model_dump(mode="json")], b"host-http-mcp/1\0"
    data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(secret, prefix + data, hashlib.sha256).hexdigest()


def publication_catalog(secret: bytes) -> list[dict]:
    rows = []
    for name, definition in sorted(get_config().mcps.items()):
        if not definition.publish_to_children:
            continue
        enabled = definition.server.get("enabled_tools")
        listed = isinstance(enabled, list)
        row = {
            "name": name,
            "version": publication_version(secret, name, definition),
            "description": definition.description[:500],
            "tools": [tool[:48] for tool in enabled[:12] if isinstance(tool, str)] if listed else [],
            "all_tools": not listed,
        }
        command = definition.server.get("command")
        if _server_kind(definition.server) == "stdio" and isinstance(command, str):
            row["command"] = command[:4096]
            args = definition.server.get("args") or []
            if isinstance(args, list):
                shown_args = [arg[:4096] for arg in args if isinstance(arg, str)]
                if shown_args:
                    row["args"] = shown_args[:64]
            cwd = definition.server.get("cwd")
            if isinstance(cwd, str) and cwd:
                row["cwd"] = cwd[:4096]
            env = definition.server.get("env") if isinstance(definition.server.get("env"), dict) else {}
            names = sorted(key for key in env if isinstance(key, str))
            if names:
                row["env_names"] = names[:64]
        rows.append(row)
    if offer := delegation_publication(secret, get_config()):
        rows.append(offer)
    return rows


def select_publications(config, names: list[str], secret: bytes, live) -> dict[str, str]:
    publications = {}
    for name in names:
        if FEATURE not in live.features or not live.package_match:
            raise ValueError(f"host MCP '{name}' requires a compatible Linux child")
        if name == MCP_NAME:
            offer = delegation_publication(secret, config)
            if offer is None:
                raise ValueError(f"host MCP '{name}' is not published")
            if live.mcp_approvals.get(name) != offer["version"]:
                raise ValueError(f"host MCP '{name}' requires child owner approval for the current published version")
            publications[name] = offer["version"]
            continue
        definition = config.mcps.get(name) if config is not None else None
        if definition is None or not definition.publish_to_children:
            raise ValueError(f"host MCP '{name}' is not published")
        version = publication_version(secret, name, definition)
        if live.mcp_approvals.get(name) != version:
            raise ValueError(f"host MCP '{name}' requires child owner approval for the current published version")
        publications[name] = version
    return publications


def _response_message(raw: bytes, request_id: int | str) -> dict | None:
    body = json.loads(raw)
    if (
        isinstance(body, dict)
        and body.get("jsonrpc") == "2.0"
        and type(body.get("id")) is type(request_id)
        and body["id"] == request_id
        and ("result" in body) != ("error" in body)
    ):
        return body
    return None


_SSE_END = re.compile(rb"\r?\n\r?\n")


async def _read_response(response: httpx.Response, request_id: int | str) -> dict:
    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type not in {"application/json", "text/event-stream"}:
        raise ValueError("Unsupported MCP response type")
    raw = bytearray()
    total = scan = 0
    async for chunk in response.aiter_raw():
        total += len(chunk)
        if total > MAX_BODY:
            raise ValueError("MCP response exceeds size limit")
        raw.extend(chunk)
        if content_type == "text/event-stream":
            while end := _SSE_END.search(raw, scan):
                event = raw[: end.start()]
                del raw[: end.end()]
                scan = 0
                lines = (line[5:].removeprefix(b" ") for line in event.splitlines() if line.startswith(b"data:"))
                if (data := b"\n".join(lines)) and (body := _response_message(data, request_id)) is not None:
                    return body
            scan = max(0, len(raw) - 3)
    if content_type == "application/json" and (body := _response_message(raw, request_id)) is not None:
        return body
    raise ValueError("MCP response has no matching JSON-RPC ID")


def _credential_needles(values: tuple[str, ...]) -> tuple[str, ...]:
    """Raw, percent-encoded, and base64 forms. Values shorter than 8 characters are ignored."""
    needles: list[str] = []
    seen: set[str] = set()

    def add(text: str) -> None:
        if text and text not in seen:
            seen.add(text)
            needles.append(text)

    for value in values:
        if not isinstance(value, str) or len(value) < 8:
            continue
        add(value)
        encoded = quote(value, safe="")
        if encoded != value:
            add(encoded)
            if encoded.lower() != encoded:
                add(encoded.lower())
        raw = value.encode()
        for blob in (base64.b64encode(raw), base64.urlsafe_b64encode(raw)):
            text = blob.decode("ascii")
            add(text)
            add(text.rstrip("="))
    return tuple(needles)


def _contains_material(value: object, credentials: tuple[str, ...]) -> bool:
    if isinstance(value, str):
        return any(credential in value for credential in credentials)
    if isinstance(value, list):
        return any(_contains_material(item, credentials) for item in value)
    if isinstance(value, dict):
        return any(_contains_material(key, credentials) or _contains_material(item, credentials) for key, item in value.items())
    return False


@dataclass
class _UpstreamSession:
    session_id: str | None = None
    protocol: str | None = None
    initializing: bool = False
    ready: bool = False
    endpoint: httpx.URL | None = None
    close_headers: dict[str, str] = field(default_factory=dict)

    async def close(self) -> None:
        """Best-effort session termination. A hung upstream must not block turn cleanup."""
        if not self.session_id or self.endpoint is None:
            return
        headers = dict(self.close_headers)
        headers["Mcp-Session-Id"] = self.session_id
        if self.protocol:
            headers["MCP-Protocol-Version"] = self.protocol
        try:
            async with asyncio.timeout(2):
                async with httpx.AsyncClient(follow_redirects=False, trust_env=False, timeout=2) as client:
                    await client.delete(self.endpoint, headers=headers)
        except Exception:
            return

    async def call(
        self,
        server: dict,
        request: dict,
        client: httpx.AsyncClient,
        tool_allowed,
        caller_values: tuple[str, ...],
    ) -> dict | None:
        method = request["method"]
        if method == "initialize":
            if self.initializing or self.protocol is not None:
                return rpc_error(request, "MCP relay already initialized")
            self.initializing = True
        elif not self.ready and (method != "notifications/initialized" or self.protocol is None):
            return rpc_error(request, "MCP relay must be initialized")
        try:
            endpoint = httpx.URL(server["url"])
            headers = httpx.Headers(server.get("headers") or {})
            self.endpoint = endpoint
            self.close_headers = {
                key: value
                for key, value in headers.items()
                if key.lower() not in {"accept", "content-type", "accept-encoding", "mcp-session-id", "mcp-protocol-version"}
            }
            credentials = [*headers.values(), *caller_values, endpoint.username, endpoint.password]
            credentials.extend(value for _, value in parse_qsl(endpoint.query.decode("ascii")))
            if authorization := headers.get("Authorization"):
                credentials.append(authorization.partition(" ")[2])
            # Only the host constructs the transport and session headers.
            for name in ("content-length", "transfer-encoding", "host", "connection", "mcp-session-id", "mcp-protocol-version"):
                headers.pop(name, None)
            headers.update(
                {
                    "Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json",
                    "Accept-Encoding": "identity",
                }
            )
            if self.session_id:
                headers["Mcp-Session-Id"] = self.session_id
                credentials.append(self.session_id)
            if self.protocol:
                headers["MCP-Protocol-Version"] = self.protocol
            async with client.stream("POST", endpoint, content=rpc_body(request), headers=headers) as response:
                response.raise_for_status()
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise ValueError("Compressed MCP reply")
                if method == "notifications/initialized":
                    self.ready = True
                    return None
                body = await _read_response(response, request["id"])
                session_id = response.headers.get("Mcp-Session-Id")
                if session_id:
                    credentials.append(session_id)
                if method == "initialize" and isinstance(result := body.get("result"), dict):
                    protocol = result.get("protocolVersion")
                    if not isinstance(protocol, str) or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", protocol) is None:
                        raise ValueError("Invalid MCP protocol version")
                    self.protocol, self.session_id = protocol, session_id
                if method == "tools/list" and isinstance(result := body.get("result"), dict) and isinstance(result.get("tools"), list):
                    result["tools"] = [tool for tool in result["tools"] if isinstance(tool, dict) and tool_allowed(tool.get("name"))]
                if _contains_material(body, _credential_needles(tuple(value for value in credentials if isinstance(value, str)))):
                    raise ValueError("MCP response contains host credential material")
                rpc_body(body)
                return body
        finally:
            if method == "initialize":
                self.initializing = False


def _tool_allowed(server: dict, preset, server_name: str, acp: bool, name: object) -> bool:
    if not isinstance(name, str) or not name:
        return False
    enabled = server.get("enabled_tools")
    disabled = server.get("disabled_tools")
    if enabled is not None and (not isinstance(enabled, list) or name not in enabled) or isinstance(disabled, list) and name in disabled:
        return False
    if acp:
        identifiers = {"other", f"{server_name}__{name}"}
        return identifiers.isdisjoint(preset.deny) and (preset.tools is None or not identifiers.isdisjoint(preset.tools))
    prefix = f"mcp__{server_name}"
    return not any(pattern == prefix or fnmatchcase(f"{prefix}__{name}", pattern) for pattern in preset.deny)


@dataclass
class _CallState:
    tool_sent: bool = False
    completed: bool = False
    dispatched: bool = False


def _stdio_environment(server: dict, home: str) -> dict[str, str]:
    """Safe inherited defaults plus declared keys. HOME is the session directory."""
    from mcp.client.stdio import get_default_environment

    env = {key: value for key, value in get_default_environment().items() if not key.startswith("KOTOYOSE_")}
    for key, value in (server.get("env") or {}).items():
        if isinstance(key, str) and isinstance(value, str) and key != "HOME" and not key.startswith("KOTOYOSE_"):
            env[key] = value
    env["HOME"] = home
    return env


def _stdio_group_directory(home: Path) -> Path:
    return Path(home) / _STDIO_GROUP_DIR


def _write_group_record(home: Path, pgid: int) -> Path:
    directory = _stdio_group_directory(home)
    directory.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        directory.chmod(0o700)
    path = directory / str(pgid)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(f"{pgid}\n")
    return path


async def _signal_group(pgid: int | None) -> None:
    """SIGTERM a process group, then SIGKILL it even when the leader has already exited."""
    if pgid is None or pgid <= 1 or pgid in {os.getpid(), os.getpgrp()}:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            pass
        except PermissionError:
            return
        if sig == signal.SIGTERM:
            await asyncio.sleep(_GROUP_GRACE_SECONDS)


async def reap_recorded_stdio_groups(home: Path) -> None:
    """Stop stdio groups recorded for this host and not removed before the process exited."""
    directory = _stdio_group_directory(home)
    if not directory.is_dir():
        return
    for record in list(directory.iterdir()):
        if record.is_symlink() or not record.is_file():
            continue
        try:
            pgid = int(record.name)
        except ValueError:
            record.unlink(missing_ok=True)
            continue
        await _signal_group(pgid)
        record.unlink(missing_ok=True)


class _StdioSession:
    """One host stdio process for one published server during one child turn."""

    def __init__(self, state_dir: Path | None = None, link: object | None = None) -> None:
        self.state_dir = state_dir
        self.link = link
        self.process: asyncio.subprocess.Process | None = None
        self.pgid: int | None = None
        self.record: Path | None = None
        self.workdir: tempfile.TemporaryDirectory[str] | None = None
        self.pending: dict[int | str, asyncio.Future] = {}
        self.reader: asyncio.Task | None = None
        self.write_lock = asyncio.Lock()
        self.started = False
        self.frozen = False
        self.held = False
        self.link_held = False
        self.killed = False
        self.initializing = False
        self.ready = False
        self.protocol: str | None = None

    def _release(self) -> None:
        global _stdio_live
        if self.held:
            self.held = False
            _stdio_live -= 1
        if self.link_held and self.link is not None:
            self.link_held = False
            self.link.stdio_live -= 1

    def _drop_record(self) -> None:
        record = self.record
        self.record = None
        if record is not None:
            record.unlink(missing_ok=True)

    def _cleanup_directory(self) -> None:
        workdir = self.workdir
        self.workdir = None
        if workdir is not None:
            workdir.cleanup()

    def _session_cwd(self, server: dict) -> str:
        configured = server.get("cwd")
        if isinstance(configured, str) and Path(configured).is_absolute():
            return configured
        self.workdir = tempfile.TemporaryDirectory(prefix="kotoyose-host-stdio-", ignore_cleanup_errors=True)
        return self.workdir.name

    async def fail(self) -> None:
        """The process may have run a tool. Do not start another one for this turn."""
        self.frozen = True
        await self._kill()

    async def close(self) -> None:
        self.frozen = True
        await self._kill()

    async def _kill(self) -> None:
        if self.killed:
            return
        self.killed = True
        self.frozen = True
        self._release()
        self._drop_record()
        reader = self.reader
        if reader is not None and reader is not asyncio.current_task():
            reader.cancel()
        await _signal_group(self.pgid)
        process = self.process
        if process is not None and process.stdin is not None:
            process.stdin.close()
        if reader is not None and reader is not asyncio.current_task():
            await asyncio.gather(reader, return_exceptions=True)
        if process is not None:
            await process.wait()
        for future in self.pending.values():
            if not future.done():
                future.set_exception(ConnectionError("stdio MCP process closed"))
        self.pending.clear()
        self._cleanup_directory()

    async def _ensure(self, server: dict) -> None:
        global _stdio_live
        if self.frozen:
            raise ValueError("stdio MCP process is closed")
        if self.process is not None and self.process.returncode is None:
            return
        if self.started:
            raise ValueError("stdio MCP process is closed")
        link_full = self.link is not None and self.link.stdio_live >= _MAX_STDIO_PER_LINK
        if _stdio_live >= _MAX_STDIO or link_full:
            raise ValueError("MCP relay concurrency limit reached")
        command = server.get("command")
        args = server.get("args") or []
        if not isinstance(command, str) or not command.strip() or "\x00" in command:
            raise ValueError("MCP stdio command is invalid")
        if not isinstance(args, list) or any(not isinstance(arg, str) or "\x00" in arg for arg in args):
            raise ValueError("MCP stdio args are invalid")
        env = server.get("env") or {}
        if any(not isinstance(key, str) or "=" in key or "\x00" in key for key in env):
            raise ValueError("MCP stdio environment is invalid")
        configured_cwd = server.get("cwd")
        if configured_cwd is not None and (not isinstance(configured_cwd, str) or "\x00" in configured_cwd):
            raise ValueError("MCP stdio cwd is invalid")
        _stdio_live += 1
        self.held = True
        if self.link is not None:
            self.link.stdio_live += 1
            self.link_held = True
        self.started = True
        cwd = self._session_cwd(server)
        try:
            self.process = await asyncio.create_subprocess_exec(
                command,
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=_stdio_environment(server, cwd),
                cwd=cwd,
                start_new_session=True,
                limit=MAX_BODY,
            )
            self.pgid = self.process.pid
            if self.state_dir is not None and self.pgid:
                self.record = _write_group_record(self.state_dir, self.pgid)
        except Exception:
            self.frozen = True
            self._release()
            self._drop_record()
            self._cleanup_directory()
            raise ValueError("MCP stdio server failed to start") from None
        self.reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        try:
            while not self.killed:
                try:
                    line = await self.process.stdout.readline()
                    if not line or len(line) > MAX_BODY:
                        break
                    body = json.loads(line)
                    if not isinstance(body, dict):
                        break
                    if "id" in body and not valid_rpc_id(body.get("id")):
                        break
                    future = self.pending.get(body.get("id"))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    break
                else:
                    if future is not None and not future.done():
                        future.set_result(body)
        finally:
            if not self.killed:
                await self.fail()

    async def call(self, server: dict, request: dict, tool_allowed, caller_values: tuple[str, ...], state: _CallState):
        method = request["method"]
        if self.frozen:
            raise ValueError("stdio MCP process is closed")
        if method == "initialize":
            if self.initializing or self.protocol is not None:
                return rpc_error(request, "MCP relay already initialized")
            self.initializing = True
        elif not self.ready and (method != "notifications/initialized" or self.protocol is None):
            return rpc_error(request, "MCP relay must be initialized")
        future = None
        try:
            await self._ensure(server)
            assert self.process is not None and self.process.stdin is not None
            if method != "notifications/initialized":
                request_id = request["id"]
                if request_id in self.pending:
                    raise ValueError("MCP request id is already in use")
                future = asyncio.get_running_loop().create_future()
                self.pending[request_id] = future
            if method == "tools/call":
                state.tool_sent = True
            try:
                async with self.write_lock:
                    self.process.stdin.write(rpc_body(request) + b"\n")
                    await self.process.stdin.drain()
            except (TimeoutError, asyncio.CancelledError):
                await asyncio.shield(self.fail())
                raise
            if method == "notifications/initialized":
                self.ready = True
                return None
            assert future is not None
            body = await asyncio.wait_for(future, UPSTREAM_TIMEOUT)
            state.completed = True
            self.pending.pop(request["id"], None)
            if not isinstance(body, dict) or ("result" in body) == ("error" in body):
                raise ValueError("MCP response has no matching JSON-RPC ID")
            if method == "initialize" and isinstance(result := body.get("result"), dict):
                protocol = result.get("protocolVersion")
                if not isinstance(protocol, str) or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", protocol) is None:
                    raise ValueError("Invalid MCP protocol version")
                self.protocol = protocol
            if method == "tools/list" and isinstance(result := body.get("result"), dict) and isinstance(result.get("tools"), list):
                result["tools"] = [tool for tool in result["tools"] if isinstance(tool, dict) and tool_allowed(tool.get("name"))]
            credentials = [value for value in caller_values if isinstance(value, str)]
            credentials.extend(arg for arg in (server.get("args") or []) if isinstance(arg, str))
            credentials.extend(value for value in (server.get("env") or {}).values() if isinstance(value, str))
            if _contains_material(body, _credential_needles(tuple(credentials))):
                raise ValueError("MCP response contains host credential material")
            rpc_body(body)
            return body
        except (TimeoutError, asyncio.CancelledError):
            await asyncio.shield(self.fail())
            raise
        finally:
            if method == "initialize":
                self.initializing = False
            if future is not None:
                self.pending.pop(request.get("id"), None)


class HostMcpTurn:
    def __init__(
        self,
        hub,
        live,
        stream: int,
        alias: str,
        publications: dict[str, str],
        headers: dict[str, str],
        timeout: float,
        delegation_grant=None,
        delegation_app=None,
    ):
        self.hub, self.live, self.stream, self.alias = hub, live, stream, alias
        self.publications = publications
        self.headers = {key.lower(): value for key, value in headers.items()}
        self.deadline = asyncio.get_running_loop().time() + timeout
        self.grant = secrets.token_hex(32)
        self.delegation_grant = delegation_grant
        self.delegation_app = delegation_app
        self._builtin_protocol: str | None = None
        self._builtin_ready = False
        self.busy_reply: asyncio.Task | None = None
        self.sessions: dict[str, _UpstreamSession | _StdioSession] = {}
        self.tasks: set[asyncio.Task] = set()
        self.seen: set[int] = set()
        self.sent_tool_call = False
        self.closed = False
        self.reply_failed = False

    def _definition(self, name: str, version: str):
        cfg = get_config()
        preset = cfg.models.get(self.alias)
        definition = cfg.mcps.get(name)
        if (
            asyncio.get_running_loop().time() >= self.deadline
            or self.hub.live_for(self.live.name) is not self.live
            or preset is None
            or preset.on != f"child:{self.live.name}"
            or f"host:{name}" not in preset.mcp_refs
            or self.publications.get(name) != version
            or self.live.mcp_approvals.get(name) != version
            or definition is None
            or not definition.publish_to_children
            or publication_version(self.hub.identity.secret, name, definition) != version
        ):
            raise ValueError("MCP relay authorization is unavailable or changed")
        route = cfg.routes.get(preset.route)
        return definition, preset, route is not None and route.driver == "cli.acp"

    def accept(self, payload: dict) -> None:
        global _active, _delegation_active
        call_id, name, version = (payload.get(key) for key in ("call", "name", "version"))
        if (
            self.closed
            or type(call_id) is not int
            or not 1 <= call_id <= MAX_CALLS
            or call_id in self.seen
            or not isinstance(name, str)
            or not isinstance(version, str)
            or payload.get("grant") != self.grant
        ):
            return
        self.seen.add(call_id)
        builtin = name == MCP_NAME
        active, limit = (_delegation_active, _MAX_DELEGATION_ACTIVE) if builtin else (_active, _MAX_ACTIVE)
        if len(self.tasks) >= MAX_CONCURRENT or self.live.mcp_active >= MAX_PER_LINK or active >= limit:
            if self.busy_reply is None or self.busy_reply.done():
                body = rpc_error(payload.get("request"), "MCP relay concurrency limit reached")
                self.busy_reply = asyncio.create_task(self._reply(call_id, body))
            else:
                self._fail_reply()
            return
        self.live.mcp_active += 1
        if builtin:
            _delegation_active += 1
        else:
            _active += 1
        task = asyncio.create_task(self._serve(call_id, name, version, payload.get("request")))
        self.tasks.add(task)
        task.add_done_callback(partial(self._finished, name))

    def _finished(self, name: str, task: asyncio.Task) -> None:
        global _active, _delegation_active
        self.tasks.discard(task)
        self.live.mcp_active -= 1
        if name == MCP_NAME:
            _delegation_active -= 1
        else:
            _active -= 1

    def _delegation_allowed(self, version: str) -> bool:
        if (
            asyncio.get_running_loop().time() >= self.deadline
            or self.hub.live_for(self.live.name) is not self.live
            or self.publications.get(MCP_NAME) != version
            or self.live.mcp_approvals.get(MCP_NAME) != version
            or self.delegation_grant is None
            or self.delegation_grant.revoked
        ):
            return False
        cfg = get_config()
        preset = cfg.models.get(self.alias)
        offer = delegation_publication(self.hub.identity.secret, cfg)
        return bool(
            preset is not None
            and preset.on == f"child:{self.live.name}"
            and f"host:{MCP_NAME}" in preset.mcp_refs
            and offer is not None
            and offer["version"] == version
        )

    async def _builtin_exchange(self, request: dict, version: str, state: _CallState) -> dict | None:
        from . import delegation as delegation_api
        from .delegation_mcp import builtin_tools, relay_builtin_tool

        if not self._delegation_allowed(version):
            raise ValueError("MCP relay authorization is unavailable or changed")
        method = request["method"]
        if method == "initialize":
            if self._builtin_protocol is not None:
                return rpc_error(request, "MCP relay already initialized")
            protocol = (request.get("params") or {}).get("protocolVersion")
            if not isinstance(protocol, str) or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", protocol) is None:
                return rpc_error(request, "Invalid MCP protocol version")
            self._builtin_protocol = protocol
            result = {
                "protocolVersion": protocol, "capabilities": {"tools": {}},
                "serverInfo": {"name": DELEGATION_DESCRIPTION, "version": "1"},
            }
            body: dict | None = {"jsonrpc": "2.0", "id": request["id"], "result": result}
        elif not self._builtin_ready and (method != "notifications/initialized" or self._builtin_protocol is None):
            return rpc_error(request, "MCP relay must be initialized")
        elif method == "notifications/initialized":
            self._builtin_ready = True
            body = None
        elif method == "ping":
            body = {"jsonrpc": "2.0", "id": request["id"], "result": {}}
        elif method == "tools/list":
            body = {"jsonrpc": "2.0", "id": request["id"], "result": {"tools": builtin_tools()}}
        elif method == "tools/call":
            params = request.get("params") or {}
            if params.get("name") == "ask_agent":
                state.tool_sent = True
                state.dispatched = self.sent_tool_call = True
            result = await relay_builtin_tool(
                self.delegation_app, self.delegation_grant, params.get("name"), params.get("arguments") or {},
            )
            state.completed = state.tool_sent
            body = {"jsonrpc": "2.0", "id": request["id"], "result": result}
        else:
            return rpc_error(request, "MCP method is unavailable through the child relay")
        if body is not None:
            try:
                encoded = rpc_body(body).decode("ascii")
            except ValueError:
                message = "MCP response exceeds the size limit"
                if state.completed:
                    message = "Delegated call completed but its response exceeds the MCP size limit. Do not retry."
                return {
                    "jsonrpc": "2.0", "id": request["id"],
                    "result": {"content": [{"type": "text", "text": message}], "isError": True},
                }
            if delegation_api.redact_token(encoded) != encoded:
                return rpc_error(request, "MCP response contains host credential material")
        # The caller must see a completed ask_agent outcome even if approval changed while it ran.
        if not state.completed and not self._delegation_allowed(version):
            raise ValueError("MCP relay alias changed during the request")
        return body

    async def _upstream(self, name: str, version: str, request: dict, state: _CallState) -> dict | None:
        definition, _, _ = self._definition(name, version)
        method = request["method"]
        tool = (request.get("params") or {}).get("name")
        servers = await resolve_mcp_secrets_async({name: definition.server})
        if _server_kind(servers[name]) == "stdio":
            server = servers[name]
            caller_values = tuple(value for value in (server.get("env") or {}).values() if isinstance(value, str))
        else:
            template_headers = servers[name].get("headers") or {}
            caller_values = tuple(
                self.headers[header.lower()]
                for value in template_headers.values()
                for header in _MCP_REQUEST_HEADER_TEMPLATE.findall(value)
                if header.lower() in self.headers
            )
            server = _inject_mcp_request_headers(servers, self.headers)[name]
        definition, preset, acp = self._definition(name, version)

        def tool_allowed(tool_name: object) -> bool:
            return _tool_allowed(definition.server, preset, name, acp, tool_name)

        if method == "tools/call" and not tool_allowed(tool):
            raise ValueError("MCP tool is outside the published configuration")
        if _server_kind(server) == "stdio":
            existing = self.sessions.get(name)
            if not isinstance(existing, _StdioSession):
                existing = self.sessions[name] = _StdioSession(self.hub.home, self.live)
            if method == "tools/call" and existing.ready:
                state.dispatched = self.sent_tool_call = True
            body = await existing.call(server, request, tool_allowed, caller_values, state)
        else:
            http_session = self.sessions.get(name)
            if not isinstance(http_session, _UpstreamSession):
                http_session = self.sessions[name] = _UpstreamSession()
            async with httpx.AsyncClient(follow_redirects=False, trust_env=False, timeout=UPSTREAM_TIMEOUT) as http:
                if method == "tools/call" and http_session.ready:
                    state.dispatched = self.sent_tool_call = True
                body = await http_session.call(server, request, http, tool_allowed, caller_values)
        if state.tool_sent:
            state.dispatched = self.sent_tool_call = True
        if self._definition(name, version) != (definition, preset, acp):
            raise ValueError("MCP relay alias changed during the request")
        return body

    async def _serve(self, call_id: int, name: str, version: str, raw: object) -> None:
        request = None
        state = _CallState()
        upstream_cap = asyncio.get_running_loop().time() + UPSTREAM_TIMEOUT
        deadline = self.deadline if name == MCP_NAME else min(self.deadline, upstream_cap)
        try:
            async with asyncio.timeout_at(deadline):
                request = validate_request(raw)
                if name == MCP_NAME:
                    body = await self._builtin_exchange(request, version, state)
                else:
                    body = await self._upstream(name, version, request, state)
        except asyncio.CancelledError:
            session = self.sessions.get(name)
            if isinstance(session, _StdioSession) and state.tool_sent and not state.completed:
                await asyncio.shield(session.fail())
            if state.tool_sent and request is not None:
                self.sent_tool_call = True
                await asyncio.shield(self._reply(call_id, rpc_error(request, lost_result(request))))
            raise
        except Exception as exc:
            session = self.sessions.get(name)
            if isinstance(session, _StdioSession) and state.tool_sent and not state.completed:
                await session.fail()
            ran = state.tool_sent or state.dispatched
            if ran:
                self.sent_tool_call = True
            if not ran and str(exc) == "MCP relay concurrency limit reached":
                message = str(exc)
            else:
                message = lost_result(request) if ran else "MCP relay request refused or upstream unavailable"
            body = rpc_error(raw, message)
        await self._reply(call_id, body)

    async def _reply(self, call_id: int, body: dict | None) -> None:
        try:
            async with asyncio.timeout(RELAY_WRITE_TIMEOUT):
                sent = await self.hub.send_frame(
                    self.live.name,
                    self.stream,
                    "mcp_response",
                    {"call": call_id, "response": body},
                    link=self.live,
                )
        except Exception:
            sent = False
        if not sent:
            self._fail_reply()

    def _fail_reply(self) -> None:
        if self.closed:
            return
        self.reply_failed = True
        message = "MCP relay could not deliver the response"
        if self.sent_tool_call:
            message += ". MCP tool execution result is unknown"
        self.live.mux.dispatch(Frame(self.stream, "error", {
            "message": message, "diagnostics": {"terminal_reason": "mcp_reply_failed"}, "retryable": False,
        }))

    def cancel(self) -> None:
        self.closed = True
        for task in self.tasks:
            task.cancel()
        if self.busy_reply is not None:
            self.busy_reply.cancel()

    async def close(self) -> None:
        self.cancel()
        reaping = asyncio.create_task(self._reap())
        try:
            await asyncio.shield(reaping)
        except asyncio.CancelledError:
            await reaping
            raise

    async def _reap(self) -> None:
        tasks = list(self.tasks)
        if self.busy_reply is not None:
            tasks.append(self.busy_reply)
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*(session.close() for session in self.sessions.values()), return_exceptions=True)
