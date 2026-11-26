"""Child installer publication and transfer over the mutual TLS link."""

import asyncio
import base64
from contextlib import asynccontextmanager
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import threading

import pytest
from websockets.exceptions import ConnectionClosed

from kotoyose import child_update as update
from kotoyose.child_client import _client_context, _connect_pinned, enroll_child
from kotoyose.child_identity import ensure_child_identity, load_host_pin
from kotoyose.child_protocol import (
    PROTOCOL_VERSION,
    PROVIDER_DATA_FEATURE,
    current_package_version,
    decode_frame,
    encode_frame,
)
from kotoyose.child_server import ChildHub

BUILD = "a" * 40
OTHER = "b" * 40
VERSION = "1.2.0"
INSTALLER = f"KotoyoseChild-Setup-{VERSION}.exe"


def staged(path, payload=b"installer", *, platform="windows-x64", version=VERSION, build=BUILD):
    path.mkdir(exist_ok=True)
    name = update.package_name(version, platform)
    (path / name).write_bytes(payload)
    metadata = {"version": version, "build_id": build, "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest()}
    (path / f"{name}.json").write_text(json.dumps(metadata), encoding="utf-8")
    return metadata


@pytest.mark.parametrize("change", [
    {"version": "../1.2.0"},
    {"version": "1.2.1"},
    {"size": update.MAX_SIZE + 1},
    {"size": 10},
    {"size": True},
    {"sha256": "f" * 64},
    {"build_id": "unknown"},
    {"build_id": f"{BUILD}-dirty"},
])
def test_rejected_publication_preserves_current_pair(tmp_path, change):
    root, dist = tmp_path / "host", tmp_path / "dist"
    metadata = staged(dist)
    update.publish(dist, root, VERSION)
    before = update.Publication.current(root)
    assert before.offer.payload() == metadata
    sidecar = dist / f"{INSTALLER}.json"
    sidecar.write_text(json.dumps(metadata | change), encoding="utf-8")
    with pytest.raises(ValueError):
        update.publish(dist, root, VERSION)
    assert update.Publication.current(root) == before


def test_publication_replaces_both_files_for_same_version(tmp_path):
    root, dist = tmp_path / "host", tmp_path / "dist"
    staged(dist)
    update.publish(dist, root, VERSION)
    first = update.Publication.current(root)
    metadata = staged(dist, b"new bytes")
    update.publish(dist, root, VERSION)
    newest = update.Publication.current(root)
    assert newest.offer.payload() == metadata
    assert not first.path.parent.exists()
    assert newest.path.read_bytes() == b"new bytes"


def test_dirty_publication_needs_operator_opt_in(tmp_path):
    root, dist = tmp_path / "host", tmp_path / "dist"
    staged(dist)
    staged(dist, platform="linux-x64", build=f"{BUILD}-dirty-0123456789ab")
    with pytest.raises(ValueError, match="--allow-dirty"):
        list(update.publish_available(dist, root, VERSION))
    assert update.Publication.current(root) is None
    assert len(list(update.publish_available(dist, root, VERSION, allow_dirty=True))) == 2


def test_dirty_fingerprint_changes_when_only_a_packaged_wheel_changes(tmp_path):
    helper = Path(__file__).resolve().parents[1] / "child-app/installer/payload_fingerprint.py"
    provisional = f"{BUILD}-dirty-000000000000"
    root = tmp_path / "stage"
    parts = root / "bin/parts"
    parts.mkdir(parents=True)
    exe = root / "bin/kotoyose-child-app"
    image = b"fixture KOTOYOSE_CHILD_BUILD:" + provisional.encode() + b"\0"
    wheel = parts / "fixture.whl"

    def fingerprint(content, sidecar):
        exe.write_bytes(image)
        wheel.write_bytes(content)
        (root / "publication.json").write_text(sidecar)
        build = subprocess.check_output([sys.executable, helper, root, provisional, "bin"], text=True).strip()
        assert build.encode() + b"\0" in exe.read_bytes()
        return build

    original = fingerprint(b"wheel A", "first sidecar")
    assert fingerprint(b"wheel A", "second sidecar") == original
    assert fingerprint(b"wheel B", "first sidecar") != original


@asynccontextmanager
async def linked(tmp_path, *, features=(update.FEATURE,), platform="windows-x64", build=OTHER,
                 publications=("windows-x64", "linux-x64")):
    dist, root = tmp_path / "dist", tmp_path / "updates"
    artifacts = {}
    for key in publications:
        content = key.encode() * 4500 + bytes(range(251)) * 800
        artifacts[key] = content, staged(dist, content, platform=key)
    assert len(list(update.publish_available(dist, root, VERSION))) == len(publications)
    content, metadata = artifacts.get(platform, next(iter(artifacts.values())))
    host = ChildHub(tmp_path / "host", heartbeat_interval=0.15, missed_heartbeats=10, update_dir=root)
    await host.start("127.0.0.1", 0)
    home = tmp_path / "child"
    try:
        await enroll_child(host.issue_enrollment(f"127.0.0.1:{host.port}")["code"], home, name="dev")
        pin = load_host_pin(home)
        identity = ensure_child_identity(home)
        context = _client_context(cert_file=identity.certificate_file, key_file=identity.key_file, host_pem=pin.host_cert_pem)
        ws, _ = await _connect_pinned(pin.address, pin.fingerprint, context)
        try:
            await ws.send(encode_frame(0, "hello", {
                "protocol": PROTOCOL_VERSION, "version": "0.9.0" if features else current_package_version(),
                "features": list(features), "platform": platform, "build_id": build,
            }))
            assert decode_frame(await ws.recv()).kind == "hello_ok"
            yield host, ws, content, metadata
        finally:
            await ws.close()
    finally:
        await host.stop()


async def app_frame(ws):
    async with asyncio.timeout(3):
        while True:
            frame = decode_frame(await ws.recv())
            if frame.kind != "heartbeat":
                return frame


async def test_host_skips_matching_build(tmp_path):
    async with linked(tmp_path, build=BUILD) as (host, ws, _, metadata):
        row = host.list_children()[0]
        assert row["build_id"] == BUILD
        assert row["update_offer"] is None
        await ws.send(encode_frame(0, "app_update_get", {"sha256": metadata["sha256"]}))
        assert (await app_frame(ws)).kind == "app_update_error"


async def test_source_checkout_with_build_id_but_without_update_feature_cannot_download(tmp_path):
    async with linked(tmp_path, features=(PROVIDER_DATA_FEATURE,), build=OTHER) as (host, ws, _, metadata):
        assert (await app_frame(ws)).kind == "provider_catalog"
        assert host.list_children()[0]["build_id"] is None
        assert host.list_children()[0]["update_offer"] is None
        await ws.send(encode_frame(0, "app_update_get", {"sha256": metadata["sha256"]}))
        assert decode_frame(await asyncio.wait_for(ws.recv(), 1)).kind == "heartbeat"
        assert host._update_downloads == 0


@pytest.mark.parametrize(("platform", "publications"), [
    ("linux-arm64", ("windows-x64", "linux-x64")),
    ("linux-x64", ("windows-x64",)),
    ("windows-x64", ("linux-x64",)),
])
async def test_child_without_matching_publication_can_neither_see_nor_download_another(tmp_path, platform, publications):
    async with linked(tmp_path, platform=platform, publications=publications) as (host, ws, _, metadata):
        assert host.list_children()[0]["update_offer"] is None
        await ws.send(encode_frame(0, "app_update_get", {"sha256": metadata["sha256"]}))
        assert (await app_frame(ws)).kind == "app_update_error"


@pytest.mark.parametrize("platform", ["windows-x64", "linux-x64", "linux-arm64"])
async def test_child_transfers_only_its_platform_in_order_even_with_package_skew(tmp_path, platform):
    publications = ("windows-x64", "linux-x64", "linux-arm64")
    async with linked(tmp_path, platform=platform, publications=publications) as (host, ws, content, metadata):
        assert (await app_frame(ws)).payload == metadata
        assert host.list_children()[0]["build_id"] == OTHER
        await ws.send(encode_frame(0, "app_update_get", {"sha256": metadata["sha256"]}))
        received = bytearray()
        while (frame := await app_frame(ws)).kind == "app_update_chunk":
            assert frame.payload["offset"] == len(received)
            chunk = base64.b64decode(frame.payload["data"], validate=True)
            assert 0 < len(chunk) <= update.CHUNK_SIZE
            received.extend(chunk)
        assert frame.kind == "app_update_end"
        assert received == content


@pytest.mark.parametrize("change", ["hash", "truncated", "wrong_request"])
async def test_host_rejects_changed_contents_before_sending_chunks(tmp_path, change):
    async with linked(tmp_path) as (host, ws, content, metadata):
        assert (await app_frame(ws)).kind == "app_update_offer"
        published = update.Publication.current(host.update_dir)
        request = {"sha256": metadata["sha256"]}
        if change == "hash":
            published.path.write_bytes(b"x" * len(content))
        elif change == "truncated":
            published.path.write_bytes(content[:-1])
        else:
            request["path"] = INSTALLER
        await ws.send(encode_frame(0, "app_update_get", request))
        assert (await app_frame(ws)).kind == "app_update_error"


async def test_heartbeat_updates_offers_on_an_existing_link(tmp_path):
    async with linked(tmp_path, publications=("linux-x64",)) as (host, ws, _, _):
        assert host.list_children()[0]["update_offer"] is None
        dist = tmp_path / "dist"
        first = staged(dist)
        update.publish(dist, host.update_dir, VERSION)
        assert (await app_frame(ws)).payload == first
        await ws.send(encode_frame(0, "app_update_get", {"sha256": "0" * 64}))
        assert (await app_frame(ws)).kind == "app_update_error"
        publication = update.Publication.current(host.update_dir)
        revised = first | {"build_id": "c" * 40}
        publication.path.with_name("offer.json").write_text(json.dumps(revised), encoding="utf-8")
        assert (await app_frame(ws)).payload == revised
        second = staged(dist, b"replacement")
        update.publish(dist, host.update_dir, VERSION)
        assert (await app_frame(ws)).payload == second
        (host.update_dir / "windows-x64" / "current").unlink()
        offer = await app_frame(ws)
        assert offer.kind == "app_update_offer" and offer.payload == {}


async def test_verified_hash_is_cached_until_the_package_changes(tmp_path, monkeypatch):
    root, dist = tmp_path / "host", tmp_path / "dist"
    staged(dist)
    update.publish(dist, root, VERSION)
    publication = await update.Publications().get(root, "windows-x64")
    sha256 = hashlib.sha256
    hash_threads = []

    def count_hashes():
        hash_threads.append(threading.get_ident())
        return sha256()

    async def send(_kind, _payload):
        return True

    monkeypatch.setattr(update.hashlib, "sha256", count_hashes)
    assert await publication.stream(send)
    assert await publication.stream(send)
    assert len(hash_threads) == 1 and hash_threads[0] != threading.get_ident()
    publication.path.write_bytes(b"x" * publication.offer.size)
    assert not await publication.stream(send)
    assert len(hash_threads) == 2


@pytest.mark.parametrize("reason", ["cancel", "timeout"])
@pytest.mark.parametrize("during", ["open", "read"])
async def test_stream_interrupted_during_io_closes_file_after_worker(tmp_path, monkeypatch, reason, during):
    root, dist = tmp_path / "host", tmp_path / "dist"
    staged(dist)
    update.publish(dist, root, VERSION)
    publication = update.Publication.current(root)
    opened = publication._open_verified()
    loop = asyncio.get_running_loop()
    started, closed = asyncio.Event(), asyncio.Event()
    resume = threading.Event()

    def block():
        loop.call_soon_threadsafe(started.set)
        assert resume.wait(5)

    class Reader:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            opened.close()
            loop.call_soon_threadsafe(closed.set)

        def read(self, size):
            if during == "read":
                block()
            return opened.read(size)

    def open_verified(_publication):
        if during == "open":
            block()
        return Reader()

    async def send(_kind, _payload):
        pytest.fail("interrupted transfer sent a frame")

    monkeypatch.setattr(update.Publication, "_open_verified", open_verified)
    monkeypatch.setattr(update, "TRANSFER_TIMEOUT", 0.15 if reason == "timeout" else 5)
    transfer = asyncio.create_task(publication.stream(send))
    try:
        await asyncio.wait_for(started.wait(), 2)
        if reason == "cancel":
            transfer.cancel()
            with pytest.raises(asyncio.CancelledError):
                await transfer
        else:
            assert not await transfer
        assert not opened.closed
    finally:
        resume.set()
    await asyncio.wait_for(closed.wait(), 2)
    assert opened.closed


async def test_duplicate_active_transfer_is_ignored_then_cooldown_reoffers(tmp_path, monkeypatch):
    release = asyncio.Event()

    async def stalled(_publication, send):
        await send("app_update_chunk", {"offset": 0, "data": "eA=="})
        await release.wait()
        return await send("app_update_end", {})

    monkeypatch.setattr(update.Publication, "stream", stalled)
    monkeypatch.setattr(update, "DOWNLOAD_COOLDOWN", 0.5)
    async with linked(tmp_path) as (host, ws, _, metadata):
        assert (await app_frame(ws)).kind == "app_update_offer"
        request = encode_frame(0, "app_update_get", {"sha256": metadata["sha256"]})
        await ws.send(request)
        assert (await app_frame(ws)).kind == "app_update_chunk"
        await ws.send(request)
        # A host heartbeat gives the duplicate time to reach the queue while the transfer is active.
        assert decode_frame(await asyncio.wait_for(ws.recv(), 1)).kind == "heartbeat"
        assert host._update_downloads == 1
        release.set()
        assert (await app_frame(ws)).kind == "app_update_end"
        assert decode_frame(await asyncio.wait_for(ws.recv(), 1)).kind == "heartbeat"
        await ws.send(request)
        retry = await app_frame(ws)
        assert retry.kind == "app_update_error" and retry.payload == {"code": "retry", "retry_after": 1}
        offer = await app_frame(ws)
        assert offer.kind == "app_update_offer" and offer.payload == metadata
        await ws.send(request)
        assert (await app_frame(ws)).kind == "app_update_chunk"


async def test_busy_retry_does_not_resend_a_withdrawn_offer(tmp_path, monkeypatch):
    monkeypatch.setattr(update, "MAX_DOWNLOADS", 0)
    monkeypatch.setattr(update, "DOWNLOAD_COOLDOWN", 0.5)
    async with linked(tmp_path) as (host, ws, _, metadata):
        assert (await app_frame(ws)).kind == "app_update_offer"
        await ws.send(encode_frame(0, "app_update_get", {"sha256": metadata["sha256"]}))
        retry = await app_frame(ws)
        assert retry.kind == "app_update_error" and retry.payload == {"code": "retry", "retry_after": 1}
        (host.update_dir / "windows-x64" / "current").unlink()
        withdrawn = await app_frame(ws)
        assert withdrawn.kind == "app_update_offer" and withdrawn.payload == {}
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(app_frame(ws), 0.65)


@pytest.mark.parametrize("terminal_available", [True, False])
async def test_failed_chunk_send_attempts_terminal_error(tmp_path, monkeypatch, terminal_available):
    original = ChildHub.send_frame

    async def fail_chunk(self, name, stream, kind, payload, *, link=None):
        if kind == "app_update_chunk":
            return False
        return await original(self, name, stream, kind, payload, link=link)

    monkeypatch.setattr(ChildHub, "send_frame", fail_chunk)
    async with linked(tmp_path) as (host, ws, _, metadata):
        assert (await app_frame(ws)).kind == "app_update_offer"
        if not terminal_available:
            server_link = host.live_for("dev").ws
            send_frame = server_link.send

            async def fail_terminal(frame):
                if decode_frame(frame).kind == "app_update_error":
                    raise OSError("terminal send failed")
                await send_frame(frame)

            monkeypatch.setattr(server_link, "send", fail_terminal)
        await ws.send(encode_frame(0, "app_update_get", {"sha256": metadata["sha256"]}))
        if terminal_available:
            assert (await app_frame(ws)).kind == "app_update_error"
        else:
            with pytest.raises(ConnectionClosed):
                await app_frame(ws)


async def test_disconnect_cancels_transfer_and_returns_host_slot(tmp_path, monkeypatch):
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def stalled(_publication, send):
        await send("app_update_chunk", {"offset": 0, "data": "eA=="})
        started.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    monkeypatch.setattr(update.Publication, "stream", stalled)
    async with linked(tmp_path) as (host, ws, _, metadata):
        assert (await app_frame(ws)).kind == "app_update_offer"
        await ws.send(encode_frame(0, "app_update_get", {"sha256": metadata["sha256"]}))
        assert (await app_frame(ws)).kind == "app_update_chunk"
        await started.wait()
        assert host._update_downloads == 1
        await ws.close()
        with pytest.raises(ConnectionClosed):
            await ws.recv()
        await asyncio.wait_for(cancelled.wait(), 1)
        assert host._update_downloads == 0
