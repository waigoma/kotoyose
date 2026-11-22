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

    @contextmanager
    def reserve_call(self):
        now = time.monotonic()
        while self.calls and self.calls[0] <= now - 3600:
            self.calls.popleft()
        if self.revoked or self.expires_at <= now:
            raise ValueError("Delegation token expired")
        if self.active >= MAX_CONCURRENT or len(self.calls) >= MAX_PER_HOUR:
            raise ValueError("Delegation call limit reached")
        self.calls.append(now)
        self.active += 1
        try:
            yield
        finally:
            self.active -= 1


_grants: dict[bytes, DelegationGrant] = {}


def issue(
    client_id: str, parent_alias: str, conversation_key: str | None,
    allowed_aliases: frozenset[str], *, parent_depth: int, ttl: int, api_key_name: str | None = None,
) -> tuple[str, DelegationGrant]:
    if parent_depth >= 1:
        raise ValueError("Delegated turns cannot issue delegation tokens")
    now = time.monotonic()
    prune_expired(now)
    token = "kyd_" + secrets.token_urlsafe(32)
    scope = uuid4().hex
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
