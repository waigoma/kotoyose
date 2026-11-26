"""In-memory decisions for tool permissions requested during a chat turn."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Literal
from uuid import uuid4

import structlog

from .delegation import public_progress_text

Decision = Literal["allow_once", "reject_once"]
OPTIONS = ("allow_once", "reject_once")
TOOL_KIND_BY_NAME = {
    "read": "read", "edit": "edit", "write": "edit", "multiedit": "edit", "notebookedit": "edit",
    "apply_patch": "edit", "bash": "execute", "shell": "execute", "grep": "search", "glob": "search",
    "ls": "search", "webfetch": "fetch", "websearch": "fetch",
}
OPT_IN_REQUIRED = "Tool permission denied: client approval requires streaming with both X-Kotoyose-Events: 1 and X-Kotoyose-Approvals: 1."
APPROVAL_REJECTED = "Tool permission denied: client approval was not granted."
log = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class PermissionRequest:
    request_id: str
    tool_call_id: str
    title: str
    tool_kind: str

    def wire(self) -> dict:
        return {
            "object": "kotoyose.event", "kind": "permission_request", "request_id": self.request_id,
            "tool_call_id": self.tool_call_id, "title": self.title, "tool_kind": self.tool_kind,
            "options": list(OPTIONS),
        }


@dataclass(slots=True)
class _Pending:
    future: asyncio.Future[Decision]
    deadline: float


class ChatApprovals:
    def __init__(self, *, timeout: float = 300, max_per_client: int = 64):
        self.timeout = timeout
        self.max_per_client = max_per_client
        self.pending: dict[str, dict[str, _Pending]] = {}

    def _remove(self, client_id: str, request_id: str) -> None:
        if requests := self.pending.get(client_id):
            requests.pop(request_id, None)
            if not requests:
                del self.pending[client_id]

    def decide(self, client_id: str, request_id: str, decision: Decision) -> bool:
        pending = self.pending.get(client_id, {}).get(request_id)
        if pending is None or pending.future.done() or asyncio.get_running_loop().time() >= pending.deadline:
            return False
        self._remove(client_id, request_id)
        pending.future.set_result(decision)
        return True


class TurnApprovals:
    def __init__(self, registry: ChatApprovals, client_id: str):
        self.registry = registry
        self.client_id = client_id
        self.queue: asyncio.Queue | None = None
        self.closed = False
        self._waiting: dict[str, asyncio.Future[Decision]] = {}

    def close(self) -> None:
        self.closed = True
        for request_id, future in self._waiting.items():
            self.registry._remove(self.client_id, request_id)
            if not future.done():
                future.set_result("reject_once")
        self._waiting.clear()

    async def request(
        self, tool_call_id: str, title: str, tool_kind: str, *, turn_deadline: float | None = None,
    ) -> str | None:
        """Return a denial message, or None when this one call was approved."""
        loop = asyncio.get_running_loop()
        started = loop.time()
        outcome = "reject_once"
        try:
            if self.closed or not isinstance(tool_call_id, str) or not 0 < len(tool_call_id) <= 256:
                return APPROVAL_REJECTED
            if self.queue is None:
                return OPT_IN_REQUIRED
            if len(self.registry.pending.get(self.client_id, {})) >= self.registry.max_per_client:
                return APPROVAL_REJECTED
            deadline = started + self.registry.timeout
            if turn_deadline is not None:
                # Leave time for the provider to receive the rejection and finish the turn.
                remaining = turn_deadline - started
                deadline = min(deadline, turn_deadline - min(1.0, remaining * 0.1))
            if deadline <= started:
                return APPROVAL_REJECTED
            request_id = uuid4().hex
            future = loop.create_future()
            self.registry.pending.setdefault(self.client_id, {})[request_id] = _Pending(future, deadline)
            self._waiting[request_id] = future
            try:
                self.queue.put_nowait(PermissionRequest(request_id, tool_call_id, public_progress_text(title), tool_kind))
                async with asyncio.timeout_at(deadline):
                    decision = await future
                if decision == "allow_once" and not self.closed:
                    outcome = decision
                    return None
            except TimeoutError:
                pass
            finally:
                self.registry._remove(self.client_id, request_id)
                self._waiting.pop(request_id, None)
            return APPROVAL_REJECTED
        finally:
            log.info("chat.permission", decision=outcome, elapsed_seconds=round(loop.time() - started, 3))


current_turn_approvals: ContextVar[TurnApprovals | None] = ContextVar("current_turn_approvals", default=None)


@contextmanager
def bind_turn_approvals(turn: TurnApprovals | None) -> Iterator[None]:
    token = current_turn_approvals.set(turn)
    try:
        yield
    finally:
        current_turn_approvals.reset(token)
