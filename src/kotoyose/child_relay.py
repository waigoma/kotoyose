"""Host-side streams for child login, status and quota."""

from __future__ import annotations

import asyncio

from websockets.exceptions import ConnectionClosed

from .child_pinned import PinnedCommandRejected, normalize_child_provider, quota_command
from .child_protocol import LOGIN_STATUS_FEATURE, QUOTA_POLICY_REFUSAL, encode_frame
from .child_server import active_child_hub


class ChildStreamError(RuntimeError):
    """A child operation ended without its expected response."""


class ChildFeatureUnsupported(ChildStreamError):
    """The child did not advertise a required link feature."""


class ChildQuotaUnavailable(ChildStreamError):
    """The child policy or pinned commands do not provide quota for this connection."""


class ChildStream:
    def __init__(self, child: str, *, feature: str | None = None, quota_provider: str | None = None):
        hub = active_child_hub()
        live = hub.live_for(child) if hub is not None else None
        if live is None:
            raise ChildStreamError(f"child {child} is offline")
        if feature is not None and feature not in live.features:
            raise ChildFeatureUnsupported(f"child does not support {feature}")
        if quota_provider is not None and live.quota_providers is not None:
            if normalize_child_provider(quota_provider) not in live.quota_providers:
                raise ChildQuotaUnavailable(QUOTA_POLICY_REFUSAL)
        self._live = live
        self._stream = hub.allocate_stream()
        self._queue = live.mux.queue(self._stream)
        self.finished = False

    async def send(self, kind: str, payload: dict) -> None:
        try:
            async with self._live.lock:
                await self._live.ws.send(encode_frame(self._stream, kind, payload))
        except ConnectionClosed as exc:
            raise ChildStreamError("child link closed") from exc

    async def receive(self, *expected: str):
        frame = await self._queue.get()
        if frame.kind in {"login_error", "quota_error", "status_error"}:
            self.finished = True
            message = frame.payload["message"]
            if frame.kind == "quota_error" and message == QUOTA_POLICY_REFUSAL:
                raise ChildQuotaUnavailable(message)
            raise ChildStreamError(message)
        if frame.kind == "closed":
            self.finished = True
            raise ChildStreamError("child link closed")
        if frame.kind not in expected:
            raise ChildStreamError("unexpected child response")
        if frame.kind in {"login_exit", "quota_result", "status_result"}:
            self.finished = True
        return frame

    async def close(self, cancel_kind: str) -> None:
        self._live.mux.discard(self._stream)
        if not self.finished:
            self.finished = True
            try:
                async with asyncio.timeout(1):
                    await self.send(cancel_kind, {})
            except (ChildStreamError, TimeoutError):
                pass


async def fetch_child_quota(child: str, provider: str, profile: str | None, timeout: float) -> dict:
    try:
        command = quota_command(provider, profile)
    except PinnedCommandRejected as exc:
        raise ChildQuotaUnavailable("child quota is unavailable for this provider") from exc
    stream = ChildStream(child, quota_provider=provider)
    try:
        async with asyncio.timeout(timeout):
            await stream.send("quota", {"provider": provider, "profile": profile, "command": list(command)})
            return (await stream.receive("quota_result")).payload
    finally:
        await stream.close("quota_cancel")


async def fetch_child_login_status(
    child: str, provider: str, profile: str | None, timeout: float, *, require_feature: bool = False,
) -> str:
    try:
        stream = ChildStream(child, feature=LOGIN_STATUS_FEATURE)
    except ChildFeatureUnsupported:
        if require_feature:
            raise
        return "unknown"
    try:
        async with asyncio.timeout(timeout):
            await stream.send("status", {"provider": provider, "profile": profile})
            return (await stream.receive("status_result")).payload["state"]
    finally:
        await stream.close("status_cancel")
