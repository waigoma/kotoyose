"""Authenticated Streamable HTTP transport for alias delegation."""

import asyncio
import hmac
import json
import secrets
import time
from contextlib import nullcontext
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import anyio
import structlog
from fastapi import HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.security.utils import get_authorization_scheme_param
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import delegation
from .child_registry import child_on_name
from .config import get_config
from .execution_http import authorize_execution
from .execution_resources import ExecutionError, agent_profile_allowed, authenticate_client
from .models import ChatCompletionResponse
from .routes.chat import delegated_chat_turn

_SESSION_HEADER = "mcp-session-id"
_IDLE_SECONDS = 1800
log = structlog.get_logger()


def _candidates(request: Request) -> list[dict]:
    cfg = get_config()
    grant = request.state.delegation_grant
    policy = request.state.execution_policy
    settings = getattr(getattr(request.app.state, "execution_service", None), "config", None)
    if grant:
        parent = cfg.models.get(grant.parent_alias)
        if parent is None or not parent.has_delegation_mcp:
            return []
        targets = grant.allowed_aliases.intersection(parent.delegates)
    else:
        targets = cfg.models
    agents = []
    for alias, preset in cfg.models.items():
        if alias not in targets or not agent_profile_allowed(alias, policy, settings, config=cfg):
            continue
        if child_on_name(preset.on) is not None or not preset.delegable or not preset.is_direct_host_chat:
            continue
        effective = cfg.preset_for(alias)
        if effective.provider == "print_cli":
            continue
        agents.append({
            "alias": alias, "description": preset.description,
            "model": effective.model, "place": "host",
        })
    return agents


def _child_request(request: Request) -> Request:
    # The MCP credential authenticates this transport only.
    scope = dict(request.scope)
    scope["headers"] = [
        (name, value) for name, value in scope["headers"]
        if name != b"authorization" and delegation.redact_token(value.decode("latin1")) == value.decode("latin1")
    ]
    return Request(scope)


async def _watch_disconnect(request: Request) -> None:
    while (await request.receive())["type"] != "http.disconnect":
        pass


async def _run_until_disconnect(request: Request, turn):
    turn_task = asyncio.create_task(turn)
    watcher = asyncio.create_task(_watch_disconnect(request))
    try:
        done, _ = await asyncio.wait({turn_task, watcher}, return_when=asyncio.FIRST_COMPLETED)
        if turn_task in done:
            return turn_task.result()
        raise ToolError("Delegation request disconnected")
    finally:
        turn_task.cancel()
        watcher.cancel()
        with anyio.CancelScope(shield=True):
            await asyncio.gather(turn_task, watcher, return_exceptions=True)


def _progress_event(kind: str, child_id: str, **fields) -> dict:
    data = {"object": "kotoyose.event", "kind": kind, "tool_call_id": f"delegate:{child_id}"}
    data.update({key: value for key, value in fields.items() if value is not None})
    return data


def _caller_scope(request: Request) -> str:
    grant = request.state.delegation_grant
    if grant is not None:
        return grant.scope
    return delegation.fingerprint(request.state.execution_principal).hex()


def _failure_text(exc: Exception) -> str:
    if isinstance(exc, TimeoutError):
        return "Delegated turn timed out"
    if isinstance(exc, (ExecutionError, ValueError, ToolError)):
        return delegation.redact_token(str(exc))
    return "Delegated chat failed"


@dataclass(frozen=True, slots=True)
class _ChildCall:
    request: Request
    alias: str
    task: str
    child_id: str
    key: str
    metadata: dict
    timeout: float | None
    grant: delegation.DelegationGrant | None
    scope: str


class DelegationMCP:
    def __init__(self):
        # Public and reverse-proxied hosts are supported. Every request requires a bearer.
        self.server = FastMCP(
            "Kotoyose delegation",
            streamable_http_path=delegation.MCP_PATH,
            json_response=True,
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        )
        self.server.add_tool(self.list_agents)
        self.server.add_tool(self.ask_agent)
        self.server.add_tool(self.start_agent)
        self.server.add_tool(self.agent_status)
        self.server.add_tool(self.cancel_agent)
        self.transport = self.server.streamable_http_app()
        self.manager = self.server.session_manager
        self.manager.session_idle_timeout = _IDLE_SECONDS
        self.owners: dict[str, tuple[bytes, float]] = {}

    async def list_agents(self, ctx: Context) -> list[dict]:
        """List the chat aliases available for delegation."""
        return _candidates(ctx.request_context.request)

    def _prepare(self, request: Request, alias: str, task: str, conversation_id: str | None) -> _ChildCall:
        if not any(agent["alias"] == alias for agent in _candidates(request)):
            raise ToolError("Alias is outside the delegation scope")
        if not task.strip() or delegation.redact_token(task) != task:
            raise ToolError("A non-empty task without delegation credentials is required")
        try:
            child_id = UUID(conversation_id).hex if conversation_id is not None else uuid4().hex
        except ValueError:
            raise ToolError("Invalid conversation_id") from None
        grant = request.state.delegation_grant
        scope = _caller_scope(request)
        key = f"{delegation.KEY_PREFIX}{scope}:{alias}:{child_id}"
        metadata = (
            {"delegated_by": grant.parent_alias, "depth": grant.depth} if grant else {"delegated_by": "client"}
        )
        timeout = get_config().preset_for(alias).timeout_seconds
        return _ChildCall(
            _child_request(request), alias, task, child_id, key, metadata, timeout, grant, scope,
        )

    async def _turn(self, call: _ChildCall) -> dict[str, Any]:
        """子を 1 turn 走らせ、親ストリームが開いていれば道具の進捗をそこに載せる。"""
        stream_key = call.grant.parent_conversation_key if call.grant is not None else None
        delegation.publish_parent_event(stream_key, _progress_event(
            "tool_call", call.child_id,
            title=delegation.public_progress_text(f"{call.alias}: {call.task[:80]}"),
            tool_kind="other", status="in_progress",
        ))

        def listener(event) -> None:
            summary = delegation.child_tool_summary(event)
            if summary:
                delegation.publish_parent_event(stream_key, _progress_event(
                    "tool_call_update", call.child_id, summary=summary, status="in_progress",
                ))

        token = delegation.bind_progress_listener(listener)
        status, summary = "failed", "failed"
        try:
            async with delegation.serialize_conversation(call.key), asyncio.timeout(call.timeout):
                result = await delegated_chat_turn(
                    call.request, call.alias, call.task, call.key, call.metadata,
                )
            if not isinstance(result, ChatCompletionResponse):
                raise ToolError(f"Delegated chat failed (HTTP {result.status_code})")
            status, summary = "completed", "completed"
            reply = {"text": result.choices[0].message.text_content(), "conversation_id": call.child_id}
            reply["usage"] = result.usage.model_dump(exclude_none=True)
            return reply
        except asyncio.CancelledError:
            summary = "cancelled"
            raise
        finally:
            delegation.reset_progress_listener(token)
            delegation.publish_parent_event(stream_key, _progress_event(
                "tool_call_update", call.child_id, summary=summary, status=status,
            ))

    async def ask_agent(
        self, alias: str, task: str, ctx: Context, conversation_id: str | None = None,
    ) -> dict[str, Any]:
        """Give another alias a self-contained task, optionally continuing its conversation."""
        request = ctx.request_context.request
        call = self._prepare(request, alias, task, conversation_id)
        try:
            with (call.grant.reserve_call() if call.grant else nullcontext()), structlog.contextvars.bound_contextvars(
                request_id=request.state.request_id,
            ):
                return await _run_until_disconnect(request, self._turn(call))
        except TimeoutError:
            raise ToolError("Delegated turn timed out") from None
        except ExecutionError as exc:
            raise ToolError(delegation.redact_token(str(exc))) from None
        except ValueError as exc:
            raise ToolError(delegation.redact_token(str(exc))) from None
        except ToolError:
            raise
        except Exception as exc:
            log.error("delegation.turn_failed", error_type=type(exc).__name__)
            raise ToolError("Delegated chat failed") from None

    async def start_agent(
        self, alias: str, task: str, ctx: Context, conversation_id: str | None = None,
    ) -> dict[str, str]:
        """Start another alias on a self-contained task and return without waiting.

        Use this to fan out independent tasks, then poll agent_status sparingly.
        The call counts toward the same concurrency and hourly limits as ask_agent.
        """
        request = ctx.request_context.request
        call = self._prepare(request, alias, task, conversation_id)
        if call.grant is not None:
            try:
                call.grant.acquire_call()
            except ValueError as exc:
                raise ToolError(delegation.redact_token(str(exc))) from None
        task_id = secrets.token_urlsafe(16)
        record = delegation.DelegationTask(call.scope, call.child_id, grant=call.grant)
        delegation.remember_task(task_id, record)

        async def run() -> None:
            try:
                reply = await self._turn(call)
            except asyncio.CancelledError:
                record.finish("cancelled")
                raise
            except Exception as exc:
                if not isinstance(exc, (ToolError, TimeoutError, ExecutionError, ValueError)):
                    log.error("delegation.turn_failed", error_type=type(exc).__name__)
                record.finish("failed", error=_failure_text(exc))
            else:
                record.finish("completed", text=reply["text"], usage=reply.get("usage"))

        try:
            record.job = asyncio.create_task(run())
        except BaseException:
            record.release()
            delegation._tasks.pop(task_id, None)
            raise

        def finished(job: asyncio.Task) -> None:
            if record.state == "running":
                failed = None if job.cancelled() else "Delegated chat failed"
                record.finish("cancelled" if job.cancelled() else "failed", error=failed)
            record.release()
            if not job.cancelled():
                job.exception()

        record.job.add_done_callback(finished)
        return {"task_id": task_id, "conversation_id": call.child_id}

    async def agent_status(self, task_id: str, ctx: Context) -> dict[str, Any]:
        """Return whether a task from start_agent is running, completed, failed, or cancelled."""
        record = delegation.take_task(_caller_scope(ctx.request_context.request), task_id)
        if record is None:
            raise ToolError("Delegation task not found")
        return record.public()

    async def cancel_agent(self, task_id: str, ctx: Context) -> dict[str, Any]:
        """Cancel a running task started by this caller."""
        record = delegation.take_task(_caller_scope(ctx.request_context.request), task_id)
        if record is None:
            raise ToolError("Delegation task not found")
        if record.state == "running" and record.job is not None and not record.job.done():
            record.job.cancel()
            with anyio.CancelScope(shield=True):
                await asyncio.gather(record.job, return_exceptions=True)
        return record.public()

    async def __call__(self, scope, receive, send):
        request = Request(scope, receive=receive)
        scheme, token = get_authorization_scheme_param(request.headers.get("authorization"))
        try:
            await self._authorize(request, token if scheme.lower() == "bearer" else "")
        except (HTTPException, ExecutionError) as exc:
            status = exc.status_code if isinstance(exc, HTTPException) else exc.status
            response = JSONResponse(
                {"detail": "MCP authentication rejected"}, status_code=status,
                headers={"WWW-Authenticate": "Bearer"} if status == 401 else None,
            )
            await response(scope, receive, send)
            return

        owner = delegation.fingerprint(token)
        session_id = request.headers.get(_SESSION_HEADER)
        now = time.monotonic()
        if session_id:
            binding = self.owners.get(session_id)
            if not binding or binding[1] <= now or not hmac.compare_digest(binding[0], owner):
                await JSONResponse({"detail": "Unknown MCP session"}, status_code=404)(scope, receive, send)
                return
            self.owners[session_id] = owner, now + _IDLE_SECONDS
        else:
            self.owners = {key: value for key, value in self.owners.items() if value[1] > now}

        async def bind_session(message):
            if message["type"] == "http.response.start":
                new_session_id = Headers(raw=message["headers"]).get(_SESSION_HEADER)
                if new_session_id and session_id is None:
                    self.owners[new_session_id] = owner, time.monotonic() + _IDLE_SECONDS
                if request.method == "DELETE" and session_id and message["status"] < 300:
                    self.owners.pop(session_id, None)
            await send(message)

        await self.transport(scope, receive, bind_session)

    @staticmethod
    async def _authorize(request: Request, token: str) -> None:
        request.state.delegation_grant = None
        if token.startswith("kyd_"):
            grant = delegation.authenticate(token)
            if grant is None:
                raise HTTPException(401)
            principal, policy = authenticate_client(grant.client_id)
            request.state.execution_principal = principal
            request.state.execution_policy = policy
            request.state.delegation_grant = grant
            request.state.api_key_name = grant.api_key_name
        else:
            credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token) if token else None
            await authorize_execution(request, credentials)


_BUILTIN_TOOLS = (
    {
        "name": "list_agents",
        "description": "List the chat aliases available for delegation.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "ask_agent",
        "description": "Give another alias a self-contained task, optionally continuing its conversation.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "alias": {"type": "string"},
                "task": {"type": "string"},
                "conversation_id": {"type": "string"},
            },
            "required": ["alias", "task"],
        },
    },
)
_relay_server: DelegationMCP | None = None


def builtin_tools() -> list[dict]:
    """Tools a child relay may call. Async delegation stays on the host HTTP endpoint."""
    return [dict(tool) for tool in _BUILTIN_TOOLS]


def _tool_result(value: object) -> dict:
    structured = {"result": value} if isinstance(value, list) else value
    return {
        "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=True)}],
        "structuredContent": structured,
    }


def _tool_error(message: str) -> dict:
    return {"content": [{"type": "text", "text": delegation.redact_token(message)}], "isError": True}


def _relay_request(app) -> Request:
    """In-process request. Disconnect is the parent task being cancelled, not an HTTP socket."""
    if app is None:
        app = SimpleNamespace(state=SimpleNamespace())

    async def receive():
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "POST", "scheme": "http", "path": delegation.MCP_PATH,
        "raw_path": delegation.MCP_PATH.encode(), "query_string": b"", "headers": [],
        "client": ("127.0.0.1", 0), "server": ("127.0.0.1", 80), "app": app,
    }
    return Request(scope, receive)


def _tool_server(app) -> DelegationMCP:
    found = getattr(getattr(app, "state", None), "delegation_mcp", None)
    if isinstance(found, DelegationMCP):
        return found
    global _relay_server
    if _relay_server is None:
        _relay_server = DelegationMCP()
    return _relay_server


async def relay_builtin_tool(app, grant: delegation.DelegationGrant | None, name: object, arguments: object) -> dict:
    """Run list_agents or ask_agent for a child relay. The delegation bearer stays on the host."""
    if not isinstance(arguments, dict):
        return _tool_error("Invalid arguments")
    if grant is None or grant.revoked or grant.expires_at <= time.monotonic():
        if name == "list_agents" and not arguments:
            return _tool_result([])
        return _tool_error("Delegation is unavailable")
    request = _relay_request(app)
    request.state.delegation_grant = grant
    request.state.request_id = "child-delegation"
    try:
        principal, policy = authenticate_client(grant.client_id)
    except ExecutionError as exc:
        return _tool_error(str(exc))
    request.state.execution_principal = principal
    request.state.execution_policy = policy
    request.state.api_key_name = grant.api_key_name
    server = _tool_server(app)
    ctx = SimpleNamespace(request_context=SimpleNamespace(request=request))
    try:
        if name == "list_agents":
            if arguments:
                return _tool_error("Unexpected arguments")
            return _tool_result(await server.list_agents(ctx))
        if name != "ask_agent":
            return _tool_error("MCP tool is outside the published configuration")
        alias, task, conversation_id = arguments.get("alias"), arguments.get("task"), arguments.get("conversation_id")
        if (
            not isinstance(alias, str) or not isinstance(task, str)
            or conversation_id is not None and not isinstance(conversation_id, str)
            or set(arguments) - {"alias", "task", "conversation_id"}
        ):
            return _tool_error("Invalid arguments")
        return _tool_result(await server.ask_agent(alias, task, ctx, conversation_id))
    except ToolError as exc:
        return _tool_error(str(exc))


def delegation_route() -> Route:
    async def handle(scope, receive, send):
        await scope["app"].state.delegation_mcp(scope, receive, send)

    # An ASGI endpoint keeps the HTTP body and disconnect handling in the MCP SDK.
    class Endpoint:
        __call__ = staticmethod(handle)

    return Route(delegation.MCP_PATH, Endpoint(), methods=["POST", "GET", "DELETE"])
