"""Authenticated Streamable HTTP transport for alias delegation."""

import asyncio
import hmac
import time
from contextlib import nullcontext
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
        if parent is None or delegation.MCP_NAME not in parent.mcp_refs:
            return []
        targets = grant.allowed_aliases.intersection(parent.delegates)
    else:
        targets = cfg.models
    agents = []
    for alias, preset in cfg.models.items():
        if alias not in targets or not agent_profile_allowed(alias, policy, settings, config=cfg):
            continue
        if not preset.delegable or not preset.is_direct_host_chat:
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
        self.transport = self.server.streamable_http_app()
        self.manager = self.server.session_manager
        self.manager.session_idle_timeout = _IDLE_SECONDS
        self.owners: dict[str, tuple[bytes, float]] = {}

    async def list_agents(self, ctx: Context) -> list[dict]:
        """List the chat aliases available for delegation."""
        return _candidates(ctx.request_context.request)

    async def ask_agent(
        self, alias: str, task: str, ctx: Context, conversation_id: str | None = None,
    ) -> dict[str, Any]:
        """Give another alias a self-contained task, optionally continuing its conversation."""
        request = ctx.request_context.request
        if not any(agent["alias"] == alias for agent in _candidates(request)):
            raise ToolError("Alias is outside the delegation scope")
        if not task.strip() or delegation.redact_token(task) != task:
            raise ToolError("A non-empty task without delegation credentials is required")
        try:
            child_id = UUID(conversation_id).hex if conversation_id is not None else uuid4().hex
        except ValueError:
            raise ToolError("Invalid conversation_id") from None
        grant = request.state.delegation_grant
        scope = grant.scope if grant else delegation.fingerprint(request.state.execution_principal).hex()
        key = f"{delegation.KEY_PREFIX}{scope}:{alias}:{child_id}"
        metadata = (
            {"delegated_by": grant.parent_alias, "depth": grant.depth} if grant else {"delegated_by": "client"}
        )
        timeout = get_config().preset_for(alias).timeout_seconds
        child_request = _child_request(request)
        try:
            async def turn():
                async with delegation.serialize_conversation(key), asyncio.timeout(timeout):
                    return await delegated_chat_turn(child_request, alias, task, key, metadata)

            with (grant.reserve_call() if grant else nullcontext()), structlog.contextvars.bound_contextvars(
                request_id=request.state.request_id,
            ):
                result = await _run_until_disconnect(request, turn())
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
        if not isinstance(result, ChatCompletionResponse):
            raise ToolError(f"Delegated chat failed (HTTP {result.status_code})")
        reply = {"text": result.choices[0].message.text_content(), "conversation_id": child_id}
        reply["usage"] = result.usage.model_dump(exclude_none=True)
        return reply

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


def delegation_route() -> Route:
    async def handle(scope, receive, send):
        await scope["app"].state.delegation_mcp(scope, receive, send)

    # An ASGI endpoint keeps the HTTP body and disconnect handling in the MCP SDK.
    class Endpoint:
        __call__ = staticmethod(handle)

    return Route(delegation.MCP_PATH, Endpoint(), methods=["POST", "GET", "DELETE"])
