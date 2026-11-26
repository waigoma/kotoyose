"""In-memory authority and runtime configuration for alias delegation."""

import asyncio
import hashlib
import hmac
import os
import re
import secrets
import time
from collections import deque
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import uuid4

if TYPE_CHECKING:
    from .runtime.base import RuntimeSession

MCP_NAME = "kotoyose"
MCP_PATH = "/v1/mcp"
KEY_PREFIX = "delegate:"
MAX_CONCURRENT = 4
MAX_PER_HOUR = 32
TOKEN_PATTERN = r"kyd_[A-Za-z0-9_-]{43}"
_token_pattern = re.compile(TOKEN_PATTERN)
_runtime_token: ContextVar[str | None] = ContextVar("delegation_runtime_token", default=None)


def redact_token(value: str) -> str:
    return _token_pattern.sub("[redacted delegation token]", value)


def fingerprint(value: str) -> bytes:
    return hashlib.sha256(value.encode()).digest()


@dataclass(frozen=True, slots=True)
class DelegationParent:
    client_id: str
    api_key_name: str | None
    allowed_aliases: frozenset[str]


@dataclass(slots=True)
class DelegationGrant:
    client_id: str
    parent_alias: str
    parent_conversation_key: str
    allowed_aliases: frozenset[str]
    expires_at: float
    scope: str
    api_key_name: str | None = None
    depth: int = field(default=1, init=False)
    active: int = 0
    calls: deque[float] = field(default_factory=deque)
    runtime: "RuntimeSession | None" = field(default=None, repr=False)
    revoked: bool = False

    def acquire_call(self) -> None:
        """同時 4・直近 1 時間 32 の枠を 1 件取る。非同期の依頼は終了まで離さない。"""
        now = time.monotonic()
        while self.calls and self.calls[0] <= now - 3600:
            self.calls.popleft()
        if self.revoked or self.expires_at <= now:
            raise ValueError("Delegation token expired")
        if self.active >= MAX_CONCURRENT or len(self.calls) >= MAX_PER_HOUR:
            raise ValueError("Delegation call limit reached")
        self.calls.append(now)
        self.active += 1

    def release_call(self) -> None:
        self.active -= 1

    @contextmanager
    def reserve_call(self):
        self.acquire_call()
        try:
            yield
        finally:
            self.release_call()


_grants: dict[bytes, DelegationGrant] = {}


def issue(
    client_id: str, parent_alias: str, conversation_key: str | None,
    allowed_aliases: frozenset[str], *, parent_depth: int, ttl: float, api_key_name: str | None = None,
    scope: str | None = None,
) -> tuple[str, DelegationGrant]:
    """Issue a fresh grant. A host-owned scope can preserve delegated conversations across grants."""
    if parent_depth >= 1:
        raise ValueError("Delegated turns cannot issue delegation tokens")
    now = time.monotonic()
    prune_expired(now)
    token = "kyd_" + secrets.token_urlsafe(32)
    scope = scope or uuid4().hex
    grant = DelegationGrant(
        client_id, parent_alias, conversation_key or scope, allowed_aliases - {parent_alias},
        now + ttl, scope, api_key_name,
    )
    _grants[fingerprint(token)] = grant
    return token, grant


def prune_expired(now: float) -> None:
    for digest, grant in list(_grants.items()):
        if grant.expires_at <= now:
            del _grants[digest]
            grant.revoked = True
            cancel_grant_tasks(grant)


def authenticate(token: str) -> DelegationGrant | None:
    if not _token_pattern.fullmatch(token):
        return None
    digest = fingerprint(token)
    # Only digests are kept in the registry. Compare the verifier in constant time.
    for verifier, grant in _grants.items():
        if hmac.compare_digest(verifier, digest):
            if (
                not grant.revoked and grant.expires_at > time.monotonic()
                and getattr(grant.runtime, "invalidated", False) is not True
            ):
                return grant
            return None
    return None


def revoke(grant: DelegationGrant) -> None:
    grant.revoked = True
    for digest, entry in _grants.items():
        if entry is grant:
            del _grants[digest]
            break
    cancel_grant_tasks(grant)


@contextmanager
def runtime_credentials(token: str | None):
    binding = _runtime_token.set(token)
    try:
        yield
    finally:
        _runtime_token.reset(binding)


def inject_delegation_server(servers: dict[str, dict]) -> None:
    servers.pop(MCP_NAME, None)
    if token := _runtime_token.get():
        base = (os.environ.get("KOTOYOSE_SELF_URL") or "http://127.0.0.1:8000").rstrip("/")
        servers[MCP_NAME] = {
            "type": "http",
            "url": base + MCP_PATH,
            "headers": {"Authorization": f"Bearer {token}"},
        }


@dataclass(slots=True)
class DelegationRuntime:
    runtime: "RuntimeSession" = field(repr=False)
    grant: DelegationGrant

    @property
    def provider(self):
        return self.runtime.provider

    async def ask(self, prompt):
        return await self.runtime.ask(prompt)

    def stream(self, prompt):
        return self.runtime.stream(prompt)

    @property
    def invalidated(self) -> bool:
        return (
            self.grant.revoked or self.grant.expires_at <= time.monotonic()
            or getattr(self.runtime, "invalidated", False) is True
        )

    async def disconnect(self) -> None:
        revoke(self.grant)
        await self.runtime.disconnect()


# Concurrent calls may target the same conversation. Keep its lock while it has users.
_conversations: dict[str, tuple[asyncio.Lock, int]] = {}


@asynccontextmanager
async def serialize_conversation(key: str | None):
    if key is None:
        yield
        return
    entry = _conversations.get(key)
    lock, count = entry if entry else (asyncio.Lock(), 0)
    _conversations[key] = lock, count + 1
    try:
        async with lock:
            yield
    finally:
        _, count = _conversations[key]
        if count == 1:
            del _conversations[key]
        else:
            _conversations[key] = lock, count - 1


# 親が X-Kotoyose-Events: 1 で配信中のときだけ、子の道具進捗をその SSE へ載せる。
@dataclass(frozen=True, slots=True)
class ParentStreamEvent:
    """親ストリームへ差し込む kotoyose.event。子の本文は入れない。"""

    data: dict


_parent_streams: dict[str, asyncio.Queue] = {}
_progress_listener: ContextVar = ContextVar("delegation_progress_listener", default=None)
_TOOL_ACTIVITY = frozenset({"tool_call", "tool_call_update", "tool_use", "tool_result"})
_TOOL_STATUS = frozenset({"pending", "in_progress", "completed", "failed"})
RETENTION_SECONDS = 3600


def parent_stream_key(client) -> str | None:
    """Grant が束縛している親会話キー。トークンとその会話は同じ grant を指す。"""
    grant = getattr(client, "grant", None)
    key = getattr(grant, "parent_conversation_key", None)
    return key if isinstance(key, str) and key else None


def register_parent_stream(key: str | None, queue: asyncio.Queue) -> None:
    if key:
        _parent_streams[key] = queue


def unregister_parent_stream(key: str | None, queue: asyncio.Queue) -> None:
    if key and _parent_streams.get(key) is queue:
        del _parent_streams[key]


def publish_parent_event(key: str | None, data: dict) -> None:
    if not key:
        return
    queue = _parent_streams.get(key)
    if queue is None:
        return
    try:
        queue.put_nowait(ParentStreamEvent(data))
    except Exception:
        return


def bind_progress_listener(listener):
    return _progress_listener.set(listener)


def reset_progress_listener(token) -> None:
    _progress_listener.reset(token)


def notify_progress(event) -> None:
    listener = _progress_listener.get()
    if listener is None:
        return
    try:
        listener(event)
    except Exception:
        return


def public_progress_text(value: str) -> str:
    """イベントに出してよい短い文字列。委任トークンと既存の資格情報の形を隠す。"""
    from .output_guard import redact_event

    text = redact_event(redact_token(value))
    return text[:200] if isinstance(text, str) else "[redacted: suspected credential]"


def child_tool_summary(event) -> str | None:
    """子の道具呼び出しの題と状態だけを返す。結果本文と assistant の文は捨てる。"""
    kind = getattr(event, "kind", None)
    if kind not in _TOOL_ACTIVITY:
        return None
    payload = getattr(event, "payload", None) or {}
    if not isinstance(payload, dict):
        payload = {}
    tool_name = getattr(event, "tool_name", None) or "tool"
    summary = getattr(event, "summary", None)
    if kind in ("tool_call", "tool_call_update"):
        title = payload.get("title") if isinstance(payload.get("title"), str) else None
        status = summary if isinstance(summary, str) and summary in _TOOL_STATUS else None
        if kind == "tool_call" and status is None:
            status = "in_progress"
        if title and status:
            text = f"{title} ({status})"
        elif title:
            text = title
        elif status:
            text = status
        else:
            text = tool_name
    elif kind == "tool_use":
        detail = " ".join(summary.split())[:80] if isinstance(summary, str) and summary else ""
        text = f"{tool_name}: {detail} (in_progress)" if detail else f"{tool_name} (in_progress)"
    else:
        status = "failed" if payload.get("is_error") else "completed"
        text = f"{tool_name} ({status})"
    return public_progress_text(text)


@dataclass(slots=True)
class DelegationTask:
    """呼び出し元の scope に閉じた非同期委譲。終了後 1 時間だけ結果を残す。"""

    scope: str
    conversation_id: str
    state: str = "running"
    text: str | None = None
    error: str | None = None
    usage: dict | None = None
    finished_at: float | None = None
    job: asyncio.Task | None = field(default=None, repr=False)
    grant: DelegationGrant | None = field(default=None, repr=False)
    released: bool = False

    def finish(self, state: str, *, text: str | None = None, error: str | None = None, usage: dict | None = None) -> bool:
        if self.state != "running":
            return False
        self.state = state
        self.text = text
        self.error = error
        self.usage = usage
        self.finished_at = time.monotonic()
        return True

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        if self.grant is not None:
            self.grant.release_call()

    def public(self) -> dict:
        body: dict = {"state": self.state}
        if self.state == "completed":
            body["text"] = self.text or ""
            if self.usage is not None:
                body["usage"] = self.usage
        elif self.state == "failed" and self.error:
            body["error"] = self.error
        return body


_tasks: dict[str, DelegationTask] = {}


def remember_task(task_id: str, task: DelegationTask) -> None:
    _prune_tasks(time.monotonic())
    _tasks[task_id] = task


def take_task(scope: str, task_id: str) -> DelegationTask | None:
    """scope が一致し、保持期限内のタスクだけ返す。他人の ID は存在しないものとして扱う。"""
    now = time.monotonic()
    _prune_tasks(now)
    task = _tasks.get(task_id)
    if task is None or len(task.scope) != len(scope) or not hmac.compare_digest(task.scope, scope):
        return None
    if task.finished_at is not None and now >= task.finished_at + RETENTION_SECONDS:
        _tasks.pop(task_id, None)
        return None
    return task


def _prune_tasks(now: float) -> None:
    for task_id, task in list(_tasks.items()):
        if task.finished_at is not None and now >= task.finished_at + RETENTION_SECONDS:
            del _tasks[task_id]


def cancel_grant_tasks(grant: DelegationGrant) -> None:
    """親セッションの失効で、その grant が始めた実行中の非同期の子を止める。"""
    for task in _tasks.values():
        if task.grant is grant and task.state == "running" and task.job is not None and not task.job.done():
            task.job.cancel()
