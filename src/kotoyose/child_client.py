"""child から host へ張る登録と、pin したままの run。

`cli_login` や PTY は読まない。Components の adapter は SDK の import と CLI の存在を確認する。
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager, suppress
import importlib.metadata
import logging
from pathlib import Path
import shutil
import ssl

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from .child_components import bundled_cli, providers, sdk_importable, selected_cli
from .child_identity import (
    ChildPolicy,
    ChildRejectedNotEnrolled,
    HostPin,
    ensure_child_identity,
    der_to_pem,
    ensure_default_policy,
    load_host_pin,
    load_policy,
    parse_host_address,
    pem_fingerprint,
    write_host_pin,
)
from .child_ops import ChildOps
from .child_mcp import FEATURE as HOST_MCP_FEATURE, approved_versions
from .child_protocol import (
    CODEX_TURNS_FEATURE,
    LOGIN_STATUS_FEATURE,
    PROVIDER_DATA_FEATURE,
    PROVIDER_DATA_MCP_FEATURE,
    PROVIDER_DATA_UNRESTRICTED_FEATURE,
    PROTOCOL_VERSION,
    PROMPT_PREFIX_SKILLS_FEATURE, SDK_SKILLS_FEATURE, SKILL_FOLDERS_FEATURE, MAX_RECORD, MAX_SKILL_TURN_RECORD,
    LOCAL_LLM_FEATURE, REPOSITORY_SKILLS_FEATURE, WORKSPACE_POLICY_FEATURE,
    ChildRejected,
    HostFingerprintMismatch,
    StreamMux,
    current_package_version,
    decode_frame,
    encode_frame,
    enrollment_parts,
    rejection,
)
from .child_turn import ChildTurns, supports_component_turn
from .child_update import FEATURE as APP_UPDATE_FEATURE
from .child_workspace import parse_workspace_policy_report
from .cli_install_spec import platform_key
from .remote_worker_identity import certificate_fingerprint

_SKILL_FOLDER_PLATFORMS = frozenset({"linux-x64", "linux-arm64", "windows-x64"})


def _hello_features(platform: str, app_update: bool = False) -> list[str]:
    from .provider_data import UNRESTRICTED_PLATFORM
    from .provider_data_runtime import LANDLOCK_PLATFORMS

    features = [
        CODEX_TURNS_FEATURE, LOGIN_STATUS_FEATURE, PROVIDER_DATA_FEATURE, WORKSPACE_POLICY_FEATURE,
        PROMPT_PREFIX_SKILLS_FEATURE,
    ]
    if app_update:
        features.append(APP_UPDATE_FEATURE)
    if platform in _SKILL_FOLDER_PLATFORMS:
        features.extend((SKILL_FOLDERS_FEATURE, REPOSITORY_SKILLS_FEATURE))
    features.append(LOCAL_LLM_FEATURE)
    if platform == UNRESTRICTED_PLATFORM:
        features.append(PROVIDER_DATA_UNRESTRICTED_FEATURE)
    elif platform in LANDLOCK_PLATFORMS:
        features.append(PROVIDER_DATA_MCP_FEATURE)
    if platform.startswith("linux-") or platform == UNRESTRICTED_PLATFORM:
        # Windows too: the legacy skill text lacks frontmatter, so Claude did not register it.
        features.append(SDK_SKILLS_FEATURE)
    if platform.startswith("linux-"):
        features.append(HOST_MCP_FEATURE)
    return features


_ADAPTER_PROBES: dict[str, tuple[str | None, tuple[str, ...]]] = {
    "cursor.sdk": ("cursor-sdk", ("cursor-agent",)),
    "antigravity.sdk": ("google-antigravity", ("agy",)),
    "cli.acp": (None, ("agy",)),
    "cli.print": ("kotoyose", ()),
}


def _quiet_logger() -> logging.Logger:
    logger = logging.getLogger("kotoyose.child.websocket")
    logger.setLevel(logging.CRITICAL)
    logger.propagate = False
    return logger


def adapter_installed(name: str, home: Path | None = None) -> bool:
    row = next((row for row in providers() if row["packages"] and row["driver"] == name), None)
    if row:
        cli = bundled_cli(row) if home is None else selected_cli(home, row)
        return supports_component_turn(name) and cli is not None and sdk_importable(row)
    spec = _ADAPTER_PROBES.get(name)
    if spec is None:
        return False
    distribution, binaries = spec
    if distribution is not None:
        try:
            importlib.metadata.distribution(distribution)
            return True
        except importlib.metadata.PackageNotFoundError:
            pass
    return any(shutil.which(binary) for binary in binaries)


def available_adapters(names: tuple[str, ...] | list[str], home: Path | None = None) -> list[str]:
    return [name for name in names if adapter_installed(name, home)]


def mcp_catalog(policy: ChildPolicy) -> list[dict]:
    """hello に載せるのは名前と説明だけ。起動定義と env は送らない。"""
    items = []
    for name, spec in policy.mcp.items():
        item = {"name": name}
        description = spec.get("description") if isinstance(spec, dict) else None
        if isinstance(description, str):
            item["description"] = description
        items.append(item)
    return items


def _client_context(*, cert_file=None, key_file=None, host_pem: str | None = None) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = False
    if host_pem:
        context.verify_mode = ssl.CERT_REQUIRED
        context.load_verify_locations(cadata=host_pem)
    else:
        context.verify_mode = ssl.CERT_NONE
    if cert_file is not None and key_file is not None:
        context.load_cert_chain(certfile=str(cert_file), keyfile=str(key_file))
    return context


def _pinned_connection_class(expected_fingerprint: str, peer_box: list[str], mismatch: list[bool]):
    """TLS のあと、WebSocket の要求を書く前に host fingerprint を見る ClientConnection。

    websockets の公開の口 (`connect(create_connection=...)`) だけを使う。17 で `connect.create_connection`
    の上書きの口が無くなり、それに頼っていた照合が呼ばれなくなったため (Windows で uv tool install が 17 を入れて発覚)。
    """

    class PinnedConnection(ClientConnection):
        def connection_made(self, transport) -> None:
            ssl_object = transport.get_extra_info("ssl_object")
            der = ssl_object.getpeercert(binary_form=True) if ssl_object is not None else None
            try:
                actual = certificate_fingerprint(der) if der else ""
            except ValueError:
                actual = ""
            if actual != expected_fingerprint:
                mismatch.append(True)
                transport.abort()
                return
            peer_box.append(der_to_pem(der))
            super().connection_made(transport)

    return PinnedConnection


async def _connect_pinned(address: str, fingerprint: str, context: ssl.SSLContext, *, max_size: int = MAX_RECORD):
    host, port = parse_host_address(address)
    peer: list[str] = []
    mismatch: list[bool] = []
    link = connect(
        f"wss://{host}:{port}/",
        ssl=context,
        server_hostname=host,
        proxy=None,
        compression=None,
        max_size=max_size,
        ping_interval=None,
        open_timeout=5,
        logger=_quiet_logger(),
        create_connection=_pinned_connection_class(fingerprint, peer, mismatch),
    )
    try:
        websocket = await link
    except Exception:
        if mismatch:
            raise HostFingerprintMismatch("host certificate fingerprint mismatch") from None
        raise
    pem = peer[0] if peer else ""
    # 照合の口が呼ばれなかったときも、ここで止める (コードや証明書はまだ送っていない)。
    if not pem or pem_fingerprint(pem) != fingerprint:
        await websocket.close()
        raise HostFingerprintMismatch("host certificate fingerprint mismatch")
    return websocket, pem


async def _close_quietly(websocket) -> None:
    try:
        await websocket.close()
    except ConnectionClosed:
        pass


async def enroll_child(code: str, home, *, name: str) -> None:
    """host fingerprint を確かめてから、コードと自分の証明書を送る。"""
    claims, _, _ = enrollment_parts(code)
    identity = ensure_child_identity(home)
    ensure_default_policy(home)
    context = _client_context()
    websocket, peer_pem = await _connect_pinned(claims["host"], claims["fp"], context)
    try:
        await websocket.send(encode_frame(0, "enroll", {
            "cert_pem": identity.cert_pem,
            "code": code,
            "fingerprint": identity.fingerprint,
            "name": name,
        }))
        raw = await asyncio.wait_for(websocket.recv(), timeout=10)
        frame = decode_frame(raw)
        if frame.kind == "reject":
            raise rejection(str(frame.payload.get("reason") or "rejected"))
        if frame.kind != "enroll_ok":
            raise ChildRejected("unexpected enrollment response")
        write_host_pin(home, HostPin(
            address=claims["host"],
            fingerprint=claims["fp"],
            name=name,
            host_cert_pem=peer_pem,
        ))
    finally:
        await _close_quietly(websocket)


class ChildSession:
    def __init__(self, *, received_turns: list[dict] | None = None):
        self.ready = asyncio.Event()
        self.generation = 0
        self._received_turns = received_turns
        self.turns: ChildTurns | None = None
        self.ops: ChildOps | None = None
        self._write_lock = asyncio.Lock()
        self._done = asyncio.Event()
        self._error: ChildRejected | None = None
        self._send = True
        self._closed = False
        self._interval = 20.0
        self._ws = None
        self._mux: StreamMux | None = None
        self._reader: asyncio.Task | None = None
        self._sender: asyncio.Task | None = None
        self._watch: asyncio.Task | None = None
        self._home = None
        self._fingerprint = ""
        self.control_kinds: list[str] = []
        self.catalog_received = asyncio.Event()

    def pause_heartbeats(self) -> None:
        self._send = False

    async def until_closed(self) -> None:
        await self._done.wait()
        if self._error is not None:
            raise self._error

    async def open(
        self, home, *, heartbeat_interval: float, package_version: str | None,
        features: list[str] | None = None,
    ) -> None:
        try:
            pin = load_host_pin(home)
        except ChildRejectedNotEnrolled as exc:
            raise ChildRejected(str(exc)) from exc
        try:
            policy = load_policy(home / "policy.json")
        except ValueError as exc:
            raise ChildRejected(str(exc)) from exc
        self._home = home
        self._fingerprint = pin.fingerprint
        identity = ensure_child_identity(home)
        context = _client_context(
            cert_file=identity.certificate_file,
            key_file=identity.key_file,
            host_pem=pin.host_cert_pem,
        )
        self._ws, _peer = await _connect_pinned(pin.address, pin.fingerprint, context, max_size=MAX_SKILL_TURN_RECORD)
        self._mux = StreamMux()
        self.turns = ChildTurns(home, self.send_frame, self._received_turns, host_fingerprint=pin.fingerprint)
        self.turns.start()
        self.ops = ChildOps(home, self.send_frame)
        self._reader = asyncio.create_task(self._read())
        try:
            version = package_version or current_package_version()
            platform = platform_key()
            advertised_features = features if features is not None else _hello_features(platform)
            hello = {
                "adapters": available_adapters(policy.adapters, home),
                "features": advertised_features,
                "platform": platform,
                "mcp": mcp_catalog(policy),
                "quota_providers": list(policy.logins),
                "protocol": PROTOCOL_VERSION,
                "version": version,
            }
            self._host_mcp = HOST_MCP_FEATURE in advertised_features
            if LOCAL_LLM_FEATURE in advertised_features:
                hello["local_llm"] = {name: entry.models for name, entry in policy.local_llm.items()}
            if self._host_mcp:
                hello["mcp_approvals"] = approved_versions(home, pin.fingerprint)
            if PROVIDER_DATA_FEATURE in advertised_features:
                from .provider_data_child import advertised_providers

                hello["providers"] = [
                    row for row in advertised_providers(home)
                    if row["provider"] in policy.adapters or row["profile"] in policy.adapters
                ]
            if WORKSPACE_POLICY_FEATURE in advertised_features:
                report = parse_workspace_policy_report({
                    "workspace_roots": [str(Path(root)) for root in policy.workspace_roots],
                    "codex_sandbox_max": policy.codex_sandbox_max,
                })
                if report is not None:
                    hello["workspace_policy"] = report
            await self._ws.send(encode_frame(0, "hello", hello))
            assert self._mux is not None
            while True:
                frame = await asyncio.wait_for(self._mux.control.get(), timeout=10)
                if frame is None:
                    raise ChildRejected("connection closed before hello")
                if frame.kind == "heartbeat":
                    continue
                if frame.kind == "reject":
                    raise rejection(str(frame.payload.get("reason") or "rejected"))
                if frame.kind == "hello_ok":
                    self.generation = int(frame.payload.get("generation") or 0)
                    advertised = float(frame.payload.get("heartbeat_interval") or heartbeat_interval)
                    self._interval = min(heartbeat_interval, advertised)
                    break
                raise ChildRejected("unexpected frame")
            self._sender = asyncio.create_task(self._send_heartbeats(home / "policy.json", policy.logins))
            self._watch = asyncio.create_task(self._watch_control())
            self.ready.set()
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._send = False
        if self.turns is not None:
            self.turns.begin_shutdown()
        if self.ops is not None:
            self.ops.begin_shutdown()
        tasks = [task for task in (self._sender, self._watch, self._reader) if task is not None]
        for task in tasks:
            task.cancel()
        if self._ws is not None:
            await _close_quietly(self._ws)
        for task in tasks:
            with suppress(asyncio.CancelledError, Exception):
                await task
        if self.ops is not None:
            await self.ops.finish()
        if self.turns is not None:
            await self.turns.finish()
        self._done.set()

    async def _read(self) -> None:
        assert self._ws is not None and self._mux is not None
        accepted = False
        try:
            async for raw in self._ws:
                try:
                    frame = decode_frame(raw)
                except ValueError:
                    continue
                if len(raw.encode("utf-8") if isinstance(raw, str) else raw) > MAX_RECORD and (
                    frame.kind != "turn" or not isinstance(frame.payload.get("skill_folders"), list)
                ):
                    continue
                if frame.stream == 0 and frame.kind == "hello_ok":
                    accepted = True
                if frame.stream == 0 and frame.kind == "reject":
                    self._error = rejection(str(frame.payload.get("reason") or "rejected"))
                    if self.ready.is_set():
                        self._done.set()
                if frame.stream and accepted and frame.kind in {"turn", "cancel", "release", "mcp_response"} and self.turns is not None:
                    self.turns.handle(frame)
                    continue
                if self.ops is not None and frame.stream and accepted and frame.kind in {
                    "login", "login_input", "login_cancel", "quota", "quota_cancel",
                    "status", "status_cancel",
                }:
                    await self.ops.handle(frame)
                    continue
                self._mux.dispatch(frame)
        except ConnectionClosed:
            pass
        finally:
            if self.turns is not None:
                if not self._closed:
                    await self.turns.link_lost()
                self.turns.begin_shutdown()
            if self.ops is not None:
                self.ops.begin_shutdown()
            self._mux.close()

    async def _watch_control(self) -> None:
        assert self._mux is not None
        try:
            while True:
                frame = await self._mux.control.get()
                if frame is None:
                    return
                self.control_kinds.append(frame.kind)
                if frame.kind == "provider_catalog" and self._home is not None:
                    from .provider_data_child import store_offers

                    try:
                        store_offers(self._home, self._fingerprint, frame.payload.get("offers"))
                    except ValueError:
                        continue
                    self.catalog_received.set()
                    continue
                if frame.kind == "reject":
                    self._error = rejection(str(frame.payload.get("reason") or "rejected"))
                    return
        finally:
            self._done.set()

    async def send_frame(self, stream: int, kind: str, payload: dict) -> None:
        """heartbeat と turn が同じ socket を交互に書いても frame が混ざらないようにする。"""
        if self._ws is None or self._closed:
            if kind == "mcp_request":
                raise OSError("child link is closed")
            return
        try:
            async with self._write_lock:
                await self._ws.send(encode_frame(stream, kind, payload))
        except ConnectionClosed as exc:
            if kind == "mcp_request":
                raise OSError("child link is closed") from exc
            return

    async def _send_heartbeats(self, policy_path: Path, quota_providers: tuple[str, ...]) -> None:
        assert self._ws is not None
        while True:
            if self._send:
                try:
                    current = load_policy(policy_path).logins
                except ValueError:
                    current = ()
                payload = {"quota_providers": list(current)} if current != quota_providers else {}
                if self._host_mcp:
                    payload["mcp_approvals"] = approved_versions(policy_path.parent, self._fingerprint)
                await self.send_frame(0, "heartbeat", payload)
                quota_providers = current
            await asyncio.sleep(self._interval)


@asynccontextmanager
async def child_session(
    home, *, heartbeat_interval: float = 20.0, package_version: str | None = None,
    received_turns: list[dict] | None = None, features: list[str] | None = None,
):
    """child のリンクを開く。受信 turn の採取はテスト用のリストを渡した場合に限る。"""
    session = ChildSession(received_turns=received_turns)
    try:
        await session.open(
            home, heartbeat_interval=heartbeat_interval, package_version=package_version, features=features,
        )
        yield session
    finally:
        await session.close()


async def run_child(
    home,
    *,
    stop: asyncio.Event,
    heartbeat_interval: float = 20.0,
    backoff: float = 0.5,
    package_version: str | None = None,
) -> None:
    """切れたら間隔を広げて繋ぎ直す。版の不一致と revoke では戻る。"""
    delay = backoff
    last_status = ""

    def status(text: str) -> None:
        # Report each transition once. Exception bodies can contain sensitive data.
        nonlocal last_status
        if text != last_status:
            print(text, file=sys.stderr, flush=True)
            last_status = text

    while not stop.is_set():
        try:
            async with child_session(
                home, heartbeat_interval=heartbeat_interval, package_version=package_version,
            ) as session:
                delay = backoff
                status("connected to host")
                closed = asyncio.create_task(session.until_closed())
                stopped = asyncio.create_task(stop.wait())
                done, pending = await asyncio.wait({closed, stopped}, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                for task in pending:
                    with suppress(asyncio.CancelledError):
                        await task
                if closed in done:
                    closed.result()
            status("disconnected from host; reconnecting")
        except ChildRejected as exc:
            if not exc.retryable:
                raise
            status("host rejected the connection; retrying")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            status(f"cannot reach host: {type(exc).__name__}; retrying")
        if stop.is_set():
            return
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
            return
        except TimeoutError:
            pass
        delay = min(delay * 2, 30.0)
