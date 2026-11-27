"""host 側の child 待ち受け。公開 API とは別の TLS WebSocket。

1 本の port で登録と、登録後の接続を受ける。登録はクライアント証明書を出さないので
verify_mode は CERT_OPTIONAL。登録後は、保存した自己署名証明書を trust anchor にして
照合し、fingerprint が台帳と違えば拒否する。CA は作らない。
"""

from __future__ import annotations

import asyncio
from collections.abc import Collection, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
import logging
import math
import os
from pathlib import Path
import ssl
import time
from typing import Any

import structlog
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from .child_identity import (
    default_host_child_home,
    ensure_host_identity,
    parse_host_address,
    pem_fingerprint,
)
from .child_pinned import adapter_matches_provider, normalize_child_provider
from .child_mcp import FEATURE as HOST_MCP_FEATURE, versions as mcp_versions
from .child_mcp_host import publication_catalog, reap_recorded_stdio_groups
from .child_local_llm import parse_local_llm_catalog
from .child_protocol import (
    SESSION_COLLECT_FEATURE, SESSION_COLLECT_POLICY_REFUSAL,
    LOCAL_LLM_FEATURE,
    PROTOCOL_VERSION,
    PROVIDER_DATA_FEATURE,
    REPOSITORY_SKILLS_FEATURE,
    WORKSPACE_POLICY_FEATURE,
    Frame,
    StreamMux,
    current_package_version,
    decode_frame,
    encode_frame,
)
from .child_registry import ENROLLMENT_ATTEMPTS, ENROLLMENT_TTL_SECONDS, valid_child_name, ChildRegistry
from . import child_update
from .child_update import FEATURE as APP_UPDATE_FEATURE
from .child_workspace import WorkspacePolicyReport, parse_workspace_policy_report
from .remote_worker_identity import certificate_fingerprint

log = structlog.get_logger()

_HEARTBEAT_INTERVAL = 20.0
_MISSED_HEARTBEATS = 3
# A mismatched package may carry the declaration catalog and stay alive. Nothing else is forwarded.
_VERSION_SKEW_ALLOW = frozenset({
    "provider_catalog", "heartbeat", "cancel", "app_update_chunk", "app_update_end", "app_update_error",
})
_VERSION_SKEW_ERROR = {"turn": "error", "login": "login_error", "status": "status_error", "quota": "quota_error", "session_collect": "session_collect_error"}


def child_listen_config(environ: Mapping[str, str] | None = None) -> tuple[str, int] | None:
    """`KOTOYOSE_CHILD_PORT` が空なら待ち受けない。bind は `KOTOYOSE_CHILD_BIND`。"""
    env = os.environ if environ is None else environ
    raw = (env.get("KOTOYOSE_CHILD_PORT") or "").strip()
    if not raw:
        return None
    try:
        port = int(raw)
    except ValueError:
        raise ValueError("KOTOYOSE_CHILD_PORT is invalid") from None
    if not 1 <= port <= 65535:
        raise ValueError("KOTOYOSE_CHILD_PORT is invalid")
    bind = (env.get("KOTOYOSE_CHILD_BIND") or "0.0.0.0").strip() or "0.0.0.0"
    return bind, port


def _server_context(certificate_file, key_file) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = False
    context.verify_mode = ssl.CERT_OPTIONAL
    context.load_cert_chain(certfile=str(certificate_file), keyfile=str(key_file))
    return context


def _quiet_logger() -> logging.Logger:
    logger = logging.getLogger("kotoyose.child.websocket")
    logger.setLevel(logging.CRITICAL)
    logger.propagate = False
    return logger


class _HostMux(StreamMux):
    def __init__(self) -> None:
        super().__init__()
        self.update_requests: asyncio.Queue[dict] = asyncio.Queue(maxsize=1)
        self.update_busy = False


@dataclass
class _Live:
    ws: object
    lock: asyncio.Lock
    mux: StreamMux
    name: str
    generation: int
    features: frozenset[str]
    build_id: str | None = None
    update: child_update.Publication | None = None
    update_retry_at: float = 0.0
    package_match: bool = True
    quota_providers: frozenset[str] | None = None
    providers: tuple[dict, ...] = ()
    local_llm: dict[str, tuple[str, ...]] = field(default_factory=dict)
    session_import_sources: tuple[str, ...] = ()
    session_import_origin_hint: str | None = None
    platform: str | None = None
    mcp_approvals: dict[str, str] = field(default_factory=dict)
    mcp_active: int = 0
    stdio_live: int = 0
    connected: bool = True
    last_mono: float = 0.0


@dataclass(frozen=True)
class ChildConnectionStatus:
    state: str
    last_heartbeat: str | None = None
    quota_shared: bool = False
    enrolled_at: str | None = None
    workspace_policy: WorkspacePolicyReport | None = None


_active_hub: "ChildHub | None" = None


def active_child_hub() -> "ChildHub | None":
    """この process で待ち受けている hub。起動前は None。"""
    return _active_hub


def _child_state(record: dict, online: bool) -> str:
    if record.get("revoked"):
        return "revoked"
    if online:
        return "online"
    return "disconnected" if record.get("last_heartbeat") else "waiting"


def child_generation(name: str) -> int | None:
    """台帳上の generation。未登録なら None。offline でも revoke 後の値を返す。"""
    hub = _active_hub
    if hub is None:
        return None
    record = hub.registry.get(name)
    if record is None:
        return None
    return int(record.get("generation") or 0)


def _clean_mcp(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    cleaned = []
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            continue
        kept = {"name": item["name"]}
        description = item.get("description")
        if isinstance(description, str):
            kept["description"] = description
        cleaned.append(kept)
    return cleaned


def _clean_names(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _quota_providers(value: object) -> frozenset[str] | None:
    if not isinstance(value, list) or any(not isinstance(name, str) for name in value):
        return None
    return frozenset(normalize_child_provider(name) for name in value)


def _data_providers(value: object) -> tuple[dict, ...]:
    from .provider_data import CAPABILITIES, DATA_DRIVERS, MAX_OFFERS, digest, identifier

    if not isinstance(value, list) or len(value) > MAX_OFFERS:
        return ()
    rows = []
    for row in value:
        if not isinstance(row, dict):
            continue
        driver, version = row.get("driver"), row.get("contract_version")
        capability = row.get("capability")
        if (not isinstance(driver, str) or type(version) is not int or DATA_DRIVERS.get(driver) != version
                or capability not in CAPABILITIES or (capability != "read" and driver != "cli.acp")):
            continue
        try:
            rows.append({
                "provider": identifier(row.get("provider")), "profile": identifier(row.get("profile")),
                "sha256": digest(row.get("sha256")), "capability": capability,
                "driver": driver, "contract_version": version,
            })
        except ValueError:
            continue
    return tuple(rows)


class ChildHub:
    """台帳と TLS 待ち受け。port を渡して start するまで接続は受けない。"""

    def __init__(
        self, home, heartbeat_interval: float = _HEARTBEAT_INTERVAL, missed_heartbeats: int = _MISSED_HEARTBEATS,
        *, update_dir: Path | None = None,
    ):
        self.home = home
        self.update_dir = update_dir if update_dir is not None else child_update.directory()
        self._publications = child_update.Publications()
        self._update_downloads = 0
        self._updating_children: set[str] = set()
        self._update_cooldown: dict[str, float] = {}
        self.heartbeat_interval = heartbeat_interval
        self.missed_heartbeats = missed_heartbeats
        self.identity = ensure_host_identity(home)
        self.registry = ChildRegistry(home / "children.json", self.identity.secret)
        self.port: int | None = None
        self.enrollment_messages = 0
        self.hello_count = 0
        self.hello_payloads: list[dict] = []
        self._ssl = _server_context(self.identity.certificate_file, self.identity.key_file)
        for pem in self.registry.certificate_pems():
            self._ssl.load_verify_locations(cadata=pem)
        self._live: dict[str, _Live] = {}
        self._server = None
        self._listening = False
        self._next_stream = 1

    @property
    def host_fingerprint(self) -> str:
        return self.identity.fingerprint

    @property
    def listening(self) -> bool:
        return self._listening

    async def start(self, host: str, port: int) -> None:
        await reap_recorded_stdio_groups(self.home)
        if self._server is not None:
            raise RuntimeError("child listener is already running")
        self._server = await serve(
            self._handle,
            host,
            port,
            ssl=self._ssl,
            compression=None,
            ping_interval=None,
            open_timeout=5,
            logger=_quiet_logger(),
        )
        sockets = self._server.sockets
        if not sockets:
            raise RuntimeError("child listener did not bind")
        self.port = int(sockets[0].getsockname()[1])
        self._listening = True
        global _active_hub
        _active_hub = self
        log.info("child.listener.started", port=self.port)

    async def stop(self) -> None:
        server = self._server
        self._server = None
        self._listening = False
        global _active_hub
        if _active_hub is self:
            _active_hub = None
        if server is None:
            return
        server.close()
        await server.wait_closed()

    def issue_enrollment(self, address: str, ttl_seconds: int = ENROLLMENT_TTL_SECONDS) -> dict:
        parse_host_address(address)
        code, expires_at = self.registry.issue(
            address=address,
            fingerprint=self.host_fingerprint,
            ttl_seconds=ttl_seconds,
            attempts=ENROLLMENT_ATTEMPTS,
        )
        return {
            "address": address,
            "code": code,
            "expires_at": expires_at,
            "fingerprint": self.host_fingerprint,
        }

    def issue_enrollment_code(self, address: str, ttl_seconds: int = ENROLLMENT_TTL_SECONDS) -> str:
        return self.issue_enrollment(address, ttl_seconds=ttl_seconds)["code"]

    def list_children(self) -> list[dict]:
        rows = []
        for record in self.registry.children():
            live = self._live_for_record(record)
            online = live is not None
            catalog = record.get("mcp") or []
            names = [
                item["name"] for item in catalog
                if isinstance(item, dict) and isinstance(item.get("name"), str)
            ] if isinstance(catalog, list) else []
            rows.append({
                "adapters": list(record.get("adapters") or []),
                "child_state": _child_state(record, online),
                "enrolled_at": record.get("enrolled_at"),
                "features": sorted(live.features) if live is not None else [],
                "platform": live.platform if live is not None else None,
                "fingerprint": record.get("fingerprint"),
                "generation": record.get("generation"),
                "last_heartbeat": record.get("last_heartbeat"),
                "mcp": names,
                "name": record.get("name"),
                "online": online,
                "protocol": record.get("protocol"),
                "providers": list(live.providers) if live is not None else [],
                "local_llm": {name: list(models) for name, models in live.local_llm.items()} if live is not None else {},
                "revoked": bool(record.get("revoked")),
                "version": record.get("version"),
                "build_id": record.get("build_id"),
                "update_offer": live.update.offer.payload() if live is not None and live.update else None,
                "workspace_policy": record.get("workspace_policy"),
            })
        return rows

    def session_collect_children(self) -> list[dict[str, Any]]:
        """失効していない登録済み child の収集可否と統合済み分類を返す。"""
        from .session_import.remote import resolve_effective_origin_hint

        rows = []
        for record in self.registry.children():
            if record.get("revoked"):
                continue
            live = self.live_for(record["name"])
            child_hint = live.session_import_origin_hint if live else None
            host_hint = record.get("origin_hint")
            rows.append(dict(
                name=record["name"], connected=live is not None, platform=live.platform if live else None,
                supports_session_collect=bool(live and SESSION_COLLECT_FEATURE in live.features),
                session_import_sources=list(live.session_import_sources) if live else [],
                child_origin_hint=child_hint, host_origin_hint=host_hint,
                effective_origin_hint=resolve_effective_origin_hint(child_hint, host_hint),
            ))
        return rows

    async def collect_sessions(
        self, name: str, *, source_kind: str = "all", since: str | None = None,
        max_files: int = 200, max_turns: int = 2000,
        known_files: Mapping[str, tuple[int, int]] | None = None, timeout: float = 120.0,
    ) -> tuple[list[dict[str, Any]], dict[str, Any], str | None]:
        """接続中の child から収集する。拒否は PermissionError、切断・非対応は RuntimeError。"""
        from .session_import.remote import resolve_effective_origin_hint

        live = self.live_for(name)
        if live is None:
            raise RuntimeError(f"child {name} is not connected")
        if SESSION_COLLECT_FEATURE not in live.features:
            raise RuntimeError(f"child {name} does not support session collection")
        stream = self.allocate_stream()
        queue = live.mux.queue(stream)
        batches = []
        try:
            async with asyncio.timeout(timeout):
                if not await self.send_frame(name, stream, "session_collect", dict(
                    source_kind=source_kind, since=since, max_files=max_files, max_turns=max_turns,
                    known_files=dict(known_files) if known_files is not None else None,
                ), link=live):
                    raise RuntimeError("child disconnected during session collection")
                while True:
                    frame = await queue.get()
                    if frame.kind == "session_collect_batch":
                        batches.append(frame.payload)
                    elif frame.kind == "session_collect_done":
                        record = self.registry.get(name) or {}
                        hint = resolve_effective_origin_hint(
                            live.session_import_origin_hint, frame.payload.get("child_origin_hint"), record.get("origin_hint"),
                        )
                        return batches, frame.payload, hint
                    else:
                        message = str(frame.payload.get("error") or frame.payload.get("message") or "child disconnected")
                        if message == SESSION_COLLECT_POLICY_REFUSAL:
                            raise PermissionError(message)
                        raise RuntimeError(message)
        except TimeoutError as exc:
            raise RuntimeError("session collection timed out") from exc
        finally:
            live.mux.discard(stream)

    async def revoke(self, name: str) -> dict:
        record = self.registry.revoke(name)
        live = self._live.get(record["fingerprint"])
        if live is not None:
            await self._reject(live, "child revoked")
        log.info("child.revoked", name=name, generation=record["generation"])
        return {"name": record["name"], "generation": record["generation"], "revoked": True}

    def live_for(self, name: str) -> _Live | None:
        """heartbeat が切れていない接続。offline なら None。"""
        return self._live_for_record(self.registry.get(name))

    def _live_for_record(self, record: dict | None) -> _Live | None:
        if record is None or record.get("revoked"):
            return None
        live = self._live.get(record.get("fingerprint") or "")
        if live is None or not live.connected or live.generation != record.get("generation"):
            return None
        limit = self.heartbeat_interval * self.missed_heartbeats
        if (time.monotonic() - live.last_mono) >= limit:
            return None
        return live

    def connection_status(self, name: str, provider: str, *, local_llm: str | None = None) -> ChildConnectionStatus:
        record = self.registry.get(name)
        if record is None:
            return ChildConnectionStatus("not_enrolled")
        live = self._live_for_record(record)
        heartbeat = record.get("last_heartbeat")
        workspace_policy = record.get("workspace_policy")
        if live is None:
            return ChildConnectionStatus(_child_state(record, False), heartbeat, workspace_policy=workspace_policy)
        if local_llm is not None:
            state = "online" if local_llm in live.local_llm else "not_enabled"
            return ChildConnectionStatus(state, heartbeat, workspace_policy=workspace_policy)
        normalized = normalize_child_provider(provider)
        enabled = any(
            normalize_child_provider(adapter) == normalized or adapter_matches_provider(adapter, normalized)
            for adapter in record.get("adapters") or []
        )
        if not enabled:
            return ChildConnectionStatus("not_enabled", heartbeat, workspace_policy=workspace_policy)
        return ChildConnectionStatus(
            "online", heartbeat, live.quota_providers is None or normalized in live.quota_providers,
            record.get("enrolled_at"), workspace_policy,
        )

    def allocate_stream(self) -> int:
        """turn 用の stream id。0 は制御のまま残す。"""
        stream = self._next_stream
        self._next_stream += 1
        if self._next_stream > 1_000_000_000:
            self._next_stream = 1
        return stream

    async def send_frame(self, name: str, stream: int, kind: str, payload: dict, *, link: _Live | None = None) -> bool:
        """live な child へ 1 frame 送る。link を渡した場合は、その接続に限る。"""
        live = self.live_for(name)
        if live is None or (link is not None and live is not link):
            return False
        if not live.package_match and kind not in _VERSION_SKEW_ALLOW:
            refusal = _VERSION_SKEW_ERROR.get(kind, "error")
            live.mux.queue(stream).put_nowait(Frame(stream, refusal, {
                "message": "package version mismatch", "retryable": False,
            } if kind == "turn" else {"message": "package version mismatch"}))
            return True
        try:
            async with live.lock:
                if not live.connected:
                    return False
                await live.ws.send(encode_frame(stream, kind, payload))
        except ConnectionClosed:
            return False
        return True

    async def broadcast_catalog(self) -> None:
        """Send the current declaration snapshot to children that negotiated it."""
        current = {"offers": _provider_offers((REPOSITORY_SKILLS_FEATURE,))}
        legacy = {"offers": _provider_offers(())}
        for live in list(self._live.values()):
            if not live.connected or PROVIDER_DATA_FEATURE not in live.features:
                continue
            payload = current if REPOSITORY_SKILLS_FEATURE in live.features else legacy
            await self.send_frame(live.name, 0, "provider_catalog", payload, link=live)

    async def disconnect(self, name: str) -> None:
        record = self.registry.get(name)
        if record is None:
            raise KeyError(name)
        live = self._live.get(record["fingerprint"])
        if live is not None:
            await live.ws.close()

    async def _handle(self, ws) -> None:
        mux = _HostMux()
        reader = asyncio.create_task(self._read(ws, mux))
        try:
            try:
                first = await asyncio.wait_for(mux.control.get(), timeout=10)
            except TimeoutError:
                await ws.close()
                return
            if first is None:
                return
            if first.stream == 0 and first.kind == "enroll":
                await self._on_enroll(ws, first.payload)
                return
            if first.stream == 0 and first.kind == "hello":
                await self._on_hello(ws, mux, first.payload)
                return
            await ws.send(encode_frame(0, "reject", {"reason": "expected enroll or hello"}))
            await ws.close()
        finally:
            reader.cancel()
            with suppress(asyncio.CancelledError):
                await reader

    async def _read(self, ws, mux: _HostMux) -> None:
        try:
            async for raw in ws:
                try:
                    frame = decode_frame(raw)
                except ValueError:
                    continue
                if frame.kind.startswith("app_update_"):
                    if (frame.kind == "app_update_get" and frame.stream == 0 and len(raw) <= 256
                            and not mux.update_busy and not mux.update_requests.full()):
                        mux.update_requests.put_nowait(frame.payload)
                else:
                    mux.dispatch(frame)
        except ConnectionClosed:
            pass
        finally:
            mux.close()

    async def _on_enroll(self, ws, payload: dict) -> None:
        self.enrollment_messages += 1
        try:
            name = payload.get("name")
            fingerprint = payload.get("fingerprint")
            cert_pem = payload.get("cert_pem")
            code = payload.get("code")
            if not isinstance(name, str) or not valid_child_name(name):
                raise ValueError("child name is invalid")
            if not isinstance(fingerprint, str) or not isinstance(cert_pem, str) or not isinstance(code, str):
                raise ValueError("enrollment is incomplete")
            if pem_fingerprint(cert_pem) != fingerprint:
                raise ValueError("child certificate fingerprint mismatch")
            existing = self.registry.get(name)
            if existing is not None and not existing.get("revoked"):
                raise ValueError("child name already enrolled")
            claims = self.registry.consume(code)
            if claims.get("fp") != self.host_fingerprint:
                raise ValueError("enrollment code fingerprint mismatch")
            record = self.registry.enroll(name=name, fingerprint=fingerprint, cert_pem=cert_pem)
            self._ssl.load_verify_locations(cadata=cert_pem)
        except Exception as exc:
            reason = str(exc) or "enrollment code rejected"
            try:
                await ws.send(encode_frame(0, "reject", {"reason": reason}))
            except ConnectionClosed:
                return
            await ws.close()
            return
        log.info("child.enrolled", name=record["name"], generation=record["generation"])
        try:
            await ws.send(encode_frame(0, "enroll_ok", {"generation": record["generation"], "name": record["name"]}))
        except ConnectionClosed:
            return
        await ws.close()

    async def _on_hello(self, ws, mux: _HostMux, payload: dict) -> None:
        fingerprint = self._peer_fingerprint(ws)
        if fingerprint is None:
            await ws.send(encode_frame(0, "reject", {"reason": "client certificate required"}))
            await ws.close()
            return
        record = self.registry.by_fingerprint(fingerprint)
        if record is None:
            await ws.send(encode_frame(0, "reject", {"reason": "unknown child"}))
            await ws.close()
            return
        if record.get("revoked"):
            await ws.send(encode_frame(0, "reject", {"reason": "child revoked"}))
            await ws.close()
            return
        protocol = payload.get("protocol")
        if protocol != PROTOCOL_VERSION:
            reason = f"protocol mismatch: child {protocol} != host {PROTOCOL_VERSION}"
            await ws.send(encode_frame(0, "reject", {"reason": reason}))
            await ws.close()
            return
        theirs = payload.get("version")
        ours = current_package_version()
        features = frozenset(_clean_names(payload.get("features")))
        package_match = theirs == ours
        if not package_match and features.isdisjoint({PROVIDER_DATA_FEATURE, APP_UPDATE_FEATURE}):
            reason = f"package version mismatch: child {theirs} != host {ours}"
            await ws.send(encode_frame(0, "reject", {"reason": reason}))
            await ws.close()
            return
        build_id = child_update.build_id(payload.get("build_id")) if APP_UPDATE_FEATURE in features else None
        local_llm = parse_local_llm_catalog(payload.get("local_llm")) if LOCAL_LLM_FEATURE in features else {}
        self.hello_payloads.append({
            "adapters": payload.get("adapters"),
            "features": payload.get("features"),
            "mcp": payload.get("mcp"),
            "quota_providers": payload.get("quota_providers"),
            "protocol": protocol,
            "version": theirs,
            **({"build_id": build_id} if build_id is not None else {}),
            **({"local_llm": local_llm} if LOCAL_LLM_FEATURE in features else {}),
        })
        self.hello_count += 1
        adapters = _clean_names(payload.get("adapters"))
        catalog = _clean_mcp(payload.get("mcp"))
        self.registry.record_hello(
            record["name"],
            protocol=PROTOCOL_VERSION,
            package_version=str(theirs),
            build_id=build_id,
            adapters=adapters,
            mcp=catalog,
            workspace_policy=parse_workspace_policy_report(payload.get("workspace_policy"))
            if WORKSPACE_POLICY_FEATURE in features else None,
        )
        platform = payload.get("platform")
        platform = platform if isinstance(platform, str) else None
        live = _Live(
            ws=ws, lock=asyncio.Lock(), mux=mux, name=record["name"],
            features=features, package_match=package_match,
            platform=platform, build_id=build_id,
            session_import_sources=tuple(
                item for item in _clean_names(payload.get("session_import_sources"))
                if item in ("all", "claude", "codex", "antigravity", "cursor", "grok")
            ) if SESSION_COLLECT_FEATURE in features else (),
            session_import_origin_hint=(
                payload.get("session_import_origin_hint")
                if SESSION_COLLECT_FEATURE in features and payload.get("session_import_origin_hint") in ("personal", "work")
                else None
            ),
            quota_providers=_quota_providers(payload.get("quota_providers")),
            providers=_data_providers(payload.get("providers")) if PROVIDER_DATA_FEATURE in features else (),
            local_llm=local_llm,
            generation=int(record.get("generation") or 0), last_mono=time.monotonic(),
            mcp_approvals=mcp_versions(payload.get("mcp_approvals")) if HOST_MCP_FEATURE in features else {},
        )
        publication = await self._publication_for(live)
        previous = self._live.get(fingerprint)
        self._live[fingerprint] = live
        if previous is not None and previous is not live:
            previous.connected = False
            await previous.ws.close()
        catalog = publication_catalog(self.identity.secret) if HOST_MCP_FEATURE in live.features else None
        if not mux.update_requests.empty():
            mux.update_requests.get_nowait()
        try:
            async with live.lock:
                await ws.send(encode_frame(0, "hello_ok", {
                    "generation": record["generation"],
                    "heartbeat_interval": self.heartbeat_interval,
                }))
                if PROVIDER_DATA_FEATURE in live.features:
                    await ws.send(encode_frame(0, "provider_catalog", {"offers": _provider_offers(live.features)}))
                if catalog is not None:
                    await ws.send(encode_frame(0, "host_mcp_catalog", {"offers": catalog}))
                await self._refresh_update(live, publication)
            sender = asyncio.create_task(self._beat(live, catalog))
            download = (
                asyncio.create_task(self._serve_updates(live, mux))
                if APP_UPDATE_FEATURE in live.features and live.build_id is not None else None
            )
            try:
                while True:
                    frame = await mux.control.get()
                    if frame is None:
                        break
                    if frame.kind == "heartbeat":
                        live.last_mono = time.monotonic()
                        if "quota_providers" in frame.payload:
                            live.quota_providers = _quota_providers(frame.payload["quota_providers"])
                        if HOST_MCP_FEATURE in live.features and "mcp_approvals" in frame.payload:
                            live.mcp_approvals = mcp_versions(frame.payload["mcp_approvals"])
                        self.registry.touch(record["name"])
            finally:
                sender.cancel()
                if download is not None:
                    download.cancel()
                    with suppress(asyncio.CancelledError):
                        await download
                with suppress(asyncio.CancelledError):
                    await sender
        finally:
            current = self._live.get(fingerprint)
            if current is live:
                current.connected = False
                self._live.pop(fingerprint, None)

    async def _publication_for(self, live: _Live) -> child_update.Publication | None:
        if APP_UPDATE_FEATURE not in live.features or live.build_id is None:
            return None
        publication = await self._publications.get(self.update_dir, live.platform)
        return publication if publication is not None and publication.offer.build_id != live.build_id else None

    @staticmethod
    async def _refresh_update(live: _Live, publication: child_update.Publication | None, *, resend=False) -> None:
        if publication is not live.update or (resend and publication is not None):
            payload = publication.offer.payload() if publication is not None else {}
            await live.ws.send(encode_frame(0, "app_update_offer", payload))
            live.update = publication

    async def _serve_updates(self, live: _Live, mux: _HostMux) -> None:
        if APP_UPDATE_FEATURE not in live.features or live.build_id is None:
            return
        while live.connected:
            if live.update_retry_at and time.monotonic() >= live.update_retry_at:
                live.update_retry_at = 0.0
                publication = await self._publication_for(live)
                try:
                    async with live.lock:
                        await self._refresh_update(live, publication, resend=True)
                except ConnectionClosed:
                    return
            remaining = live.update_retry_at - time.monotonic() if live.update_retry_at else None
            try:
                async with asyncio.timeout(remaining):
                    request = await mux.update_requests.get()
            except TimeoutError:
                continue
            mux.update_busy = True
            try:
                await self._send_update(live, request)
            finally:
                mux.update_busy = False

    async def _send_update(self, live: _Live, request: dict) -> None:
        async def send(kind: str, payload: dict) -> bool:
            return await self.send_frame(live.name, 0, kind, payload, link=live)

        publication = live.update
        now = time.monotonic()
        if publication is None or request != {"sha256": publication.offer.sha256}:
            await self._update_error(live)
            return
        retry_at = self._update_cooldown.get(live.name, 0)
        if live.name in self._updating_children or self._update_downloads >= child_update.MAX_DOWNLOADS:
            retry_at = max(retry_at, now + child_update.DOWNLOAD_COOLDOWN)
        if retry_at > now:
            live.update_retry_at = retry_at
            await self._update_error(live, {"code": "retry", "retry_after": math.ceil(retry_at - now)})
            return
        live.update_retry_at = 0.0
        self._update_cooldown = {
            name: until for name, until in self._update_cooldown.items() if until > now
        }
        self._updating_children.add(live.name)
        self._update_downloads += 1
        try:
            if publication is await self._publication_for(live) and await publication.stream(send):
                return
            await self._update_error(live)
        finally:
            self._update_downloads -= 1
            self._updating_children.remove(live.name)
            self._update_cooldown[live.name] = time.monotonic() + child_update.DOWNLOAD_COOLDOWN

    @staticmethod
    async def _update_error(live: _Live, payload: dict | None = None) -> None:
        if not live.connected:
            return
        try:
            async with asyncio.timeout(5), live.lock:
                await live.ws.send(encode_frame(0, "app_update_error", payload or {}))
        except (ConnectionClosed, OSError, TimeoutError):
            try:
                await live.ws.close(code=1011)
            except (ConnectionClosed, OSError):
                pass

    async def _beat(self, live: _Live, catalog: list[dict] | None) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_interval)
            try:
                publication = await self._publication_for(live)
                async with live.lock:
                    await live.ws.send(encode_frame(0, "heartbeat", {}))
                    if live.name not in self._updating_children:
                        await self._refresh_update(live, publication)
                    if catalog is not None:
                        current = publication_catalog(self.identity.secret)
                        if current != catalog:
                            await live.ws.send(encode_frame(0, "host_mcp_catalog", {"offers": current}))
                            catalog = current
            except ConnectionClosed:
                return

    async def _reject(self, live: _Live, reason: str) -> None:
        try:
            async with live.lock:
                await live.ws.send(encode_frame(0, "reject", {"reason": reason}))
        except ConnectionClosed:
            pass
        try:
            await live.ws.close()
        except ConnectionClosed:
            pass

    @staticmethod
    def _peer_fingerprint(ws) -> str | None:
        ssl_object = ws.transport.get_extra_info("ssl_object")
        if ssl_object is None:
            return None
        der = ssl_object.getpeercert(binary_form=True)
        if not der:
            return None
        return certificate_fingerprint(der)


def _provider_offers(features: Collection[str]) -> list[dict]:
    from .provider_data_host import host_catalog

    return [row.offer() for row in host_catalog().rows
            if row.native_skill_layout is None or REPOSITORY_SKILLS_FEATURE in features]


def open_host_hub(environ: Mapping[str, str] | None = None, **kwargs) -> ChildHub:
    kwargs.setdefault("update_dir", child_update.directory(environ))
    return ChildHub(default_host_child_home(environ), **kwargs)
