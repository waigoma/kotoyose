"""画像の exact wire、入力専用予算、隔離と失敗閉鎖を fake provider で検証する。"""

import asyncio
import base64
import contextlib
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import random
import struct
import zlib
from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest

from kotoyose.codex_images import image_data_uris
from kotoyose.codex_egress import CodexEgress, parse_request
from kotoyose.codex_model_policy import model_catalog
from kotoyose.execution_images import MAX_IMAGE_TOTAL_BYTES, NativeImageInput
from kotoyose.execution_jobs import WorkerResult
from kotoyose.native_codex import NativeCodexWorker, _Protocol
from kotoyose import codex_egress
from tests.codex_fake_api import COMMAND, fake_api
from tests.codex_subscription_fixture import subscription_job
from tests.test_codex_egress import header
from tests.test_native_codex import job, provider  # noqa: F401
from tests.codex_runtime_fixture import codex_rootfs  # noqa: F401
from tests import codex_fake_api


BINARY = os.environ.get("KOTOYOSE_CODEX_CONFORMANCE_BINARY")


def png(*, large=False):
    """CLI decoder も通る完全な fixture PNG。大画像は圧縮されにくい固定 pixels。"""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    width, height = (1024, 512) if large else (1, 1)
    pixels = random.Random(7).randbytes(width * height * 3) if large else b"\x00\x00\x00"
    rows = b"".join(b"\x00" + pixels[y * width * 3:(y + 1) * width * 3] for y in range(height))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) \
        + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


def image(content=b"\x89PNG\r\n\x1a\nimage", media_type="image/png"):
    digest = hashlib.sha256(content).hexdigest()
    return NativeImageInput("artifact:fixture:image:" + digest, digest, media_type, content)


def payload(uris):
    return {"model": "fixed", "stream": True, "store": False, "input": [
        {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "Describe"},
            *({"type": "input_image", "image_url": uri, "detail": "high"} for uri in uris)]}]}


@pytest.mark.parametrize("content,media", [(b"\x89PNG\r\n\x1a\nx", "image/png"),
    (b"\xff\xd8\xffx", "image/jpeg"), (b"GIF89ax", "image/gif"), (b"RIFF1234WEBPx", "image/webp")])
def test_image_codec_uses_only_validated_bytes(content, media):
    uri, = image_data_uris((image(content, media),))
    prefix, encoded = uri.split(",", 1)
    assert prefix == "data:" + media + ";base64" and base64.b64decode(encoded) == content


@pytest.mark.parametrize("changed", ["digest", "mime", "magic", "ref", "count", "size", "aggregate", "type"])
def test_direct_adapter_image_validation_is_closed(changed):
    item = image()
    images = (item,)
    if changed == "digest": images = (replace(item, digest="0" * 64),)
    elif changed == "mime": images = (replace(item, media_type="image/jpeg"),)
    elif changed == "magic": images = (image(b"not an image"),)
    elif changed == "ref": images = (replace(item, artifact_ref="artifact:fixture:image:" + "0" * 64),)
    elif changed == "count": images = (item,) * 11
    elif changed == "size": images = (image(b"\x89PNG\r\n\x1a\n" + b"x" * MAX_IMAGE_TOTAL_BYTES),)
    elif changed == "aggregate": images = (image(b"\x89PNG\r\n\x1a\n" + b"x" * (MAX_IMAGE_TOTAL_BYTES // 2)),) * 2
    elif changed == "type": images = [item]
    with pytest.raises(ValueError, match="invalid_job"):
        image_data_uris(images)


def test_exact_upper_byte_and_count_bounds_and_text_catalog():
    assert len(image_data_uris((image(),) * 10)) == 10
    assert len(image_data_uris((image(b"\x89PNG\r\n\x1a\n" + b"x" * (MAX_IMAGE_TOTAL_BYTES - 8)),))) == 1
    text = model_catalog("gpt-6-luna")["models"][0]
    visual = model_catalog("gpt-6-luna", images=True)["models"][0]
    assert text["input_modalities"] == ["text"] and visual["input_modalities"] == ["text", "image"]
    assert {**text, "input_modalities": visual["input_modalities"]} == visual


@pytest.mark.parametrize("change", ["none", "drop", "replace", "duplicate", "order", "remote", "path",
    "file", "assistant", "developer", "tool", "reasoning", "extra", "detail"])
def test_broker_allows_only_exact_ordered_user_images(change):
    uris = image_data_uris((image(), image(b"GIF89ax", "image/gif")))
    broker = CodexEgress(1, 65536, "fixed", "fake", None, image_uris=uris)
    value = payload(uris)
    content = value["input"][0]["content"]
    part = content[1]
    if change == "drop": content.pop()
    elif change == "replace": part["image_url"] = image_data_uris((image(b"\x89PNG\r\n\x1a\nother"),))[0]
    elif change == "duplicate": content.append(dict(part))
    elif change == "order": content[1:] = content[:0:-1]
    elif change == "remote": part["image_url"] = "https://external.invalid/image.png"
    elif change == "path": part["image_url"] = "file:///tmp/image.png"
    elif change == "file": part.update(file_id="arbitrary")
    elif change in {"assistant", "developer"}: value["input"][0]["role"] = change
    elif change == "tool": value["input"] = [{"type": "function_call_output", "call_id": "x", "output": content}]
    elif change == "reasoning": value["input"] = [{"type": "reasoning", "content": content}]
    elif change == "extra": part["local_path"] = "/tmp/image.png"
    elif change == "detail": part["detail"] = "original"
    body = json.dumps(value).encode()
    request, _ = parse_request(header(length=len(body)))
    try:
        if change == "none": assert json.loads(broker.validate_request(request, body)) == value
        else:
            with pytest.raises(ValueError, match="egress_denied"): broker.validate_request(request, body)
    finally:
        broker.directory.cleanup()


async def test_fake_protocol_final_without_image_delivery_is_failed(provider, tmp_path):
    result = await provider.execute(job(tmp_path, image_inputs=(image(),)), asyncio.Event(), AsyncMock(), on_process_started=AsyncMock())
    assert (result.status, result.reason, result.stop_confirmation) == ("failed", "image_delivery_missing", "confirmed")


def echo(uris, method="item/started"):
    return {"method": method, "emittedAtMs": 1, "params": {"threadId": "thread", "turnId": "turn",
        "startedAtMs" if method == "item/started" else "completedAtMs": 1,
        "item": {"type": "userMessage", "id": "user", "clientId": None, "content": [
            {"type": "text", "text": "prompt", "text_elements": []},
            *({"type": "image", "url": uri, "detail": None} for uri in uris)]}}}


async def test_only_two_exact_image_echoes_are_input_and_are_removed_from_queue():
    uris = image_data_uris((image(png(large=True)),))
    reader = AsyncMock()
    reader.readuntil.side_effect = [(json.dumps(echo(uris, method)) + "\n").encode()
        for method in ("item/started", "item/completed", "item/completed")]
    rpc = _Protocol(SimpleNamespace(stdout=reader), 2048, uris, "prompt")
    rpc.thread_id = "thread"
    for _ in range(2):
        value = await rpc.read()
        assert value["params"]["item"]["content"][1:] == [{"type": "image"}]
        assert "data:image" not in str(value)
    assert rpc.bytes < 2048
    with pytest.raises(ValueError, match="invalid_protocol"):
        await rpc.read()


@pytest.mark.parametrize("change", ["thread", "turn", "unknown", "id", "client", "role", "drop", "replace",
    "timestamp", "detail", "text", "assistant", "tool"])
async def test_echo_cannot_expand_original_output_or_authority(change):
    uris = image_data_uris((image(png(large=True)),))
    value = echo(uris)
    rpc = _Protocol(None, 1024, uris, "prompt")
    rpc.thread_id, rpc.turn_id, rpc.echo_item_id = "thread", "turn", "user"
    params, item = value["params"], value["params"]["item"]
    if change == "thread": params["threadId"] = "other"
    elif change == "turn": params["turnId"] = "other"
    elif change == "unknown": value["permission"] = True
    elif change == "id": item["id"] = "other"
    elif change == "client": item["clientId"] = "other"
    elif change == "role": item["role"] = "user"
    elif change == "drop": item["content"].pop()
    elif change == "replace": item["content"][1]["url"] += "x"
    elif change == "timestamp": value["emittedAtMs"] = True
    elif change == "detail": item["content"][1]["detail"] = "original"
    elif change == "text": item["content"][0]["text"] += "changed"
    elif change in {"assistant", "tool"}: item["type"] = "agentMessage" if change == "assistant" else "dynamicToolCall"
    rpc.process = SimpleNamespace(stdout=SimpleNamespace(readuntil=AsyncMock(return_value=(json.dumps(value) + "\n").encode())))
    with pytest.raises(ValueError): await rpc.read()


async def test_image_text_on_stderr_is_discarded_without_an_input_echo_exception():
    uri = image_data_uris((image(png(large=True)),))[0]
    reader = SimpleNamespace(read=AsyncMock(side_effect=[uri.encode(), b""]))
    rpc = _Protocol(SimpleNamespace(stderr=reader), 1024)
    await rpc.drain_stderr()
    assert rpc.bytes == len(uri) > rpc.limit
    assert not rpc.pending and reader.read.await_count == 2


async def send(broker, body):
    reader, writer = await asyncio.open_unix_connection(broker.path)
    try:
        writer.write(header(length=len(body)) + body)
        await writer.drain()
        return await reader.read()
    except (ConnectionResetError, BrokenPipeError):
        return b"closed"
    finally:
        writer.close()
        with contextlib.suppress(ConnectionResetError, BrokenPipeError):
            await writer.wait_closed()


@pytest.mark.parametrize("change", ["drop", "unknown", "duplicate", "duplicate_json", "remote"])
async def test_image_denial_is_sticky_for_api_before_upstream(monkeypatch, change):
    uris = image_data_uris((image(),))
    broker = CodexEgress(1, 65536, "fixed", "fake", None, image_uris=uris)
    connect = AsyncMock()
    monkeypatch.setattr(codex_egress, "connect_endpoint", connect)
    value = payload(uris)
    content = value["input"][0]["content"]
    if change == "drop": content.pop()
    elif change == "unknown": content[1]["unknown"] = True
    elif change == "duplicate": content.append(content[1])
    elif change == "remote": content[1]["image_url"] = "https://external.invalid/image"
    body = json.dumps(value).encode()
    if change == "duplicate_json": body = body.replace(b'"detail": "high"', b'"detail":"high","detail":"high"')
    await broker.start()
    try:
        assert b"502" in await send(broker, body)
        assert broker.failure_reason == "egress_denied" and broker.image_requests_completed == 0
        denied = await send(broker, json.dumps(payload(uris)).encode())
        assert b"502" in denied or denied == b"closed"
        connect.assert_not_awaited()
    finally:
        assert await broker.close(1) == "confirmed"


@pytest.mark.parametrize("mode", ["normal", "request_limit", "text", "response"])
async def test_image_input_allowance_does_not_expand_text_response_or_retry_caps(monkeypatch, mode):
    uris = image_data_uris((image(png(large=True)),))
    broker = CodexEgress(2, 65536, "fixed", "fake", None, image_uris=uris, image_request_limit=1)
    value = payload(uris)
    if mode == "text": value["input"][0]["content"][0]["text"] = "x" * 65536
    body = json.dumps(value).encode()
    with pytest.raises(ValueError): parse_request(header(length=len(body)))
    assert parse_request(header(length=len(body)), image_bytes=broker.image_bytes)[1] == len(body)
    async with fake_api(monkeypatch, tool=False, reflected_secret="x" * 65536 if mode == "response" else None) as calls:
        await broker.start()
        try:
            response = await send(broker, body)
            if mode in {"normal", "request_limit"}:
                assert b"200 OK" in response and broker.image_requests_completed == 1
                assert broker.bytes < broker.byte_limit and broker.image_input_bytes == broker.image_bytes
                assert len(calls) == 1
                if mode == "request_limit":
                    denied = await send(broker, body)
                    assert b"502" in denied or denied == b"closed"
                    assert broker.failure_reason == "egress_denied" and len(calls) == 1
            else:
                assert b"502" in response and broker.failure_reason == "egress_limit_exceeded"
                assert broker.image_requests_completed == 0
                assert len(calls) == (0 if mode == "text" else 1)
        finally:
            assert await broker.close(1) == "confirmed"


@pytest.mark.skipif(not BINARY, reason="Explicit reviewed CLI and fake API required")
async def test_actual_cli_invalid_png_text_substitution_never_completes_visual(provider, tmp_path, monkeypatch):
    async with fake_api(monkeypatch, tool=False) as calls:
        result = await NativeCodexWorker().execute(job(tmp_path, cli_binary=Path(BINARY), image_inputs=(image(),),
            timeout_seconds=8, output_limit_bytes=65536), asyncio.Event(), AsyncMock(), on_process_started=AsyncMock())
    assert (result.status, result.reason, result.stop_confirmation) == ("failed", "egress_denied", "confirmed")
    assert not calls


@pytest.mark.skipif(not BINARY, reason="Explicit reviewed CLI and fake API required")
@pytest.mark.parametrize("authentication", ["api", "subscription"])
async def test_actual_image_cancel_converges_pending_connection(provider, tmp_path, monkeypatch, authentication):
    entered, never, cancel = asyncio.Event(), asyncio.Event(), asyncio.Event()
    credentials = subscription_job(tmp_path) if authentication == "subscription" else {"credential_value": "fake"}
    async def blocked():
        entered.set()
        await never.wait()
    monkeypatch.setattr(codex_egress, "connect_endpoint", blocked)
    monkeypatch.setattr(codex_egress, "connect_subscription_endpoint", blocked)
    operation = asyncio.create_task(NativeCodexWorker().execute(job(tmp_path, cli_binary=Path(BINARY),
        **credentials, image_inputs=(image(png(large=True)),), timeout_seconds=10, output_limit_bytes=65536),
        cancel, AsyncMock(), on_process_started=AsyncMock()))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        cancel.set()
        # Namespace init/pidfd stop has its own bounded grace+6 convergence window.
        result = await asyncio.wait_for(asyncio.shield(operation), 15)
        assert (result.status, result.reason, result.stop_confirmation) == ("cancelled", "cancelled", "confirmed"), result
    finally:
        cancel.set()
        await operation


@pytest.mark.parametrize("mode", ["empty", "incomplete", "wrong_id", "failed", "duplicate_json", "after_complete"])
async def test_http_200_without_valid_model_sse_completion_is_not_delivery(monkeypatch, mode):
    original = codex_fake_api.response(1, False)
    records = original.split(b"\n\n")
    if mode == "empty": bad = b""
    elif mode == "incomplete": bad = b"\n\n".join(records[:-2]) + b"\n\n"
    elif mode == "wrong_id": bad = original.replace(b'"id": "resp_fixture_1", "status": "completed"', b'"id":"other","status":"completed"')
    elif mode == "failed": bad = original.replace(b'"type": "response.completed"', b'"type":"response.failed"')
    elif mode == "duplicate_json": bad = original.replace(b'"status": "completed", "output": []', b'"status":"completed","status":"completed","output":[]')
    else: bad = original + records[1] + b"\n\n"
    monkeypatch.setattr(codex_fake_api, "response", lambda *args, **kwargs: bad)
    uris = image_data_uris((image(png()),))
    broker = CodexEgress(2, 65536, "fixed", "fake", None, image_uris=uris)
    async with fake_api(monkeypatch, tool=False) as calls:
        await broker.start()
        try:
            assert b"502" in await send(broker, json.dumps(payload(uris)).encode())
            assert len(calls) == 1 and broker.image_requests_completed == 0 and broker.failure_reason is not None
        finally:
            assert await broker.close(1) == "confirmed"


async def test_fake_appserver_cannot_succeed_after_empty_http_200_image_response(provider, tmp_path, monkeypatch):
    launcher = tmp_path / "shortcut-provider"
    launcher.write_text('''#!/usr/bin/python3
import json,os,socket,sys
def emit(v): print(json.dumps(v),flush=True)
for line in sys.stdin:
 m=json.loads(line);method=m.get('method');p=m.get('params',{})
 if method=='initialize':emit({'id':m['id'],'result':{}})
 elif method=='thread/start':
  model=p['model'];emit({'id':m['id'],'result':{'thread':{'id':'thread'},'model':model,'modelProvider':'kotoyose_native'}})
 elif method=='turn/start':
  emit({'id':m['id'],'result':{'turn':{'id':'turn'}}})
  body=json.dumps({'model':model,'stream':True,'store':False,'input':[{'type':'message','role':'user','content':[
   {'type':'input_image','image_url':v['url'],'detail':'high'} for v in p['input'] if v['type']=='image']}]}).encode()
  s=socket.socket(socket.AF_UNIX);s.connect(os.environ['KOTOYOSE_NATIVE_CODEX_EGRESS_SOCKET'])
  s.sendall(('POST /v1/responses HTTP/1.1\\r\\nHost: 127.0.0.1:18080\\r\\nAuthorization: Bearer '+os.environ['OPENAI_API_KEY']+'\\r\\nContent-Length: '+str(len(body))+'\\r\\n\\r\\n').encode()+body)
  while s.recv(16384):pass
  s.close()
  emit({'method':'item/completed','params':{'threadId':'thread','turnId':'turn','item':{'type':'agentMessage','id':'a','phase':'final_answer','text':'shortcut'}}})
  emit({'method':'turn/completed','params':{'threadId':'thread','turn':{'id':'turn','status':'completed'}}})
''')
    launcher.chmod(0o700)
    monkeypatch.setattr(codex_fake_api, "response", lambda *args, **kwargs: b"")
    async with fake_api(monkeypatch, tool=False) as calls:
        result = await NativeCodexWorker(launcher).execute(job(tmp_path, image_inputs=(image(png()),),
            timeout_seconds=5), asyncio.Event(), AsyncMock(), on_process_started=AsyncMock())
    assert (result.status, result.reason, result.stop_confirmation) == ("failed", "provider_error", "confirmed"), result
    assert len(calls) == 1


@contextlib.asynccontextmanager
async def pending_image_api(monkeypatch, marker, mode):
    """最初の SSE は完了し、2 要求目だけを接続待ちまたは HTTP body 待ちに固定する。"""
    entered, never = asyncio.Event(), asyncio.Event()
    calls, tasks, writers = [], set(), set()
    connections = 0

    async def serve(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        writers.add(writer)
        try:
            raw = await reader.readuntil(b"\r\n\r\n")
            headers = dict(line.split(b": ", 1) for line in raw.split(b"\r\n")[1:] if line)
            calls.append(json.loads(await reader.readexactly(int(headers[b"content-length"]))))
            body = codex_fake_api.response(len(calls), False)
            incomplete = len(calls) == 2
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\nContent-Length: "
                + str(len(body)).encode() + b"\r\n\r\n" + (body[:32] if incomplete else body))
            await writer.drain()
            if incomplete:
                marker.touch()
                entered.set()
                await never.wait()
        finally:
            writer.close()
            with contextlib.suppress(ConnectionResetError, BrokenPipeError):
                await writer.wait_closed()
            writers.discard(writer)
            tasks.discard(task)

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async def connect():
        nonlocal connections
        connections += 1
        if mode == "blocked" and connections == 2:
            marker.touch()
            entered.set()
            await never.wait()
        return await asyncio.open_connection("127.0.0.1", port)
    monkeypatch.setattr(codex_egress, "connect_endpoint", connect)
    monkeypatch.setattr(codex_egress, "connect_subscription_endpoint", connect)
    try:
        yield entered, calls
    finally:
        never.set()
        server.close()
        await server.wait_closed()
        for writer in tuple(writers):
            writer.close()
        for task in tuple(tasks):
            task.cancel()
        if tasks:
            _, pending = await asyncio.wait(tuple(tasks), timeout=2)
            assert not pending, "Owned fake upstream did not stop"


@pytest.mark.parametrize("mode", ["blocked", "incomplete"])
@pytest.mark.parametrize("authentication", ["api", "subscription"])
@pytest.mark.parametrize("completion", ["final", "cancel", "timeout"])
async def test_every_image_request_must_complete_before_success(provider, tmp_path, monkeypatch, mode, authentication, completion):
    from kotoyose import native_codex
    marker = tmp_path / "second-image-request-started"
    launcher = tmp_path / "pending-image-provider"
    launcher.write_text('''#!/usr/bin/python3
import json,os,socket,sys,time
def emit(v): print(json.dumps(v),flush=True)
for line in sys.stdin:
 m=json.loads(line);method=m.get('method');p=m.get('params',{})
 if method=='initialize':emit({'id':m['id'],'result':{}})
 elif method=='thread/start':
  model=p['model'];completion=p['developerInstructions'];emit({'id':m['id'],'result':{'thread':{'id':'thread'},'model':model,'modelProvider':'kotoyose_native'}})
 elif method=='turn/start':
  emit({'id':m['id'],'result':{'turn':{'id':'turn'}}})
  marker=p['input'][0]['text']
  body=json.dumps({'model':model,'stream':True,'store':False,'input':[{'type':'message','role':'user','content':[
   {'type':'input_image','image_url':v['url'],'detail':'high'} for v in p['input'] if v['type']=='image']}]}).encode()
  credential=os.environ.get('OPENAI_API_KEY') or os.environ['KOTOYOSE_CODEX_BROKER_CAPABILITY']
  header=('POST /v1/responses HTTP/1.1\\r\\nHost: 127.0.0.1:18080\\r\\nAuthorization: Bearer '+credential+'\\r\\nContent-Length: '+str(len(body))+'\\r\\n\\r\\n').encode()
  s=socket.socket(socket.AF_UNIX);s.connect(os.environ['KOTOYOSE_NATIVE_CODEX_EGRESS_SOCKET']);s.sendall(header+body)
  response=b''
  while chunk:=s.recv(16384):response+=chunk
  assert b'200 OK' in response and b'response.completed' in response
  s.close()
  s=socket.socket(socket.AF_UNIX);s.connect(os.environ['KOTOYOSE_NATIVE_CODEX_EGRESS_SOCKET']);s.sendall(header+body)
  deadline=time.monotonic()+5
  while not os.path.exists(marker):
   assert time.monotonic()<deadline
   time.sleep(.01)
  if completion=='final':
   emit({'method':'item/completed','params':{'threadId':'thread','turnId':'turn','item':{'type':'agentMessage','id':'a','phase':'final_answer','text':'shortcut'}}})
   emit({'method':'turn/completed','params':{'threadId':'thread','turn':{'id':'turn','status':'completed'}}})
  while True:time.sleep(1)
''')
    launcher.chmod(0o700)
    brokers = []
    def broker(*args, **kwargs):
        value = CodexEgress(*args, **kwargs)
        brokers.append(value)
        return value
    monkeypatch.setattr(native_codex, "CodexEgress", broker)
    credentials = subscription_job(tmp_path) if authentication == "subscription" else {"credential_value": "fake"}
    cancel = asyncio.Event()
    async with pending_image_api(monkeypatch, marker, mode) as (entered, calls):
        operation = asyncio.create_task(NativeCodexWorker(launcher).execute(job(tmp_path, image_inputs=(image(png()),),
            prompt=str(marker), instructions=completion, timeout_seconds=30 if completion == "final" else 2, output_limit_bytes=65536,
            **credentials), cancel, AsyncMock(), on_process_started=AsyncMock()))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            if completion == "cancel": cancel.set()
            result = await asyncio.wait_for(asyncio.shield(operation), 10)
        finally:
            cancel.set()
            await operation
    assert len(brokers) == 1 and brokers[0].connections == 2 and brokers[0].image_requests_completed == 1
    assert brokers[0].closed and not brokers[0].tasks and not brokers[0].writers
    assert len(calls) == (1 if mode == "blocked" else 2)
    expected = {"final": ("failed", "image_delivery_incomplete"), "cancel": ("cancelled", "cancelled"),
        "timeout": ("timeout", "timeout")}[completion]
    assert (result.status, result.reason, result.stop_confirmation) == (*expected, "confirmed"), result


@pytest.mark.parametrize("waiter", ["server", "handler"])
async def test_broker_shutdown_requires_server_and_handler_convergence(waiter):
    broker = CodexEgress(2, 65536, "fixed", "fake", None, image_uris=image_data_uris((image(),)))
    release, cancelled = asyncio.Event(), asyncio.Event()
    task = None
    async def blocked():
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            if waiter == "handler": await release.wait()
            else: raise
    if waiter == "server": broker.server = SimpleNamespace(close=lambda: None, wait_closed=blocked)
    else:
        task = asyncio.create_task(blocked())
        broker.tasks.add(task)
        await asyncio.sleep(0)
    try:
        assert await asyncio.wait_for(broker.close(.01), 1) == "unconfirmed"
        assert cancelled.is_set() and broker.closed
    finally:
        release.set()
        if task is not None: await asyncio.wait_for(task, 1)
        broker.directory.cleanup()


@pytest.mark.skipif(not BINARY, reason="Explicit reviewed CLI and fake API required")
async def test_actual_cli_images_json_review_contract_and_count_ten(provider, tmp_path, monkeypatch):
    images = (image(png()),) * 10
    expected = {"passed": True, "summary": "fixture transport only", "checks": []}
    async with fake_api(monkeypatch, tool=False, answer=json.dumps(expected)) as calls:
        result = await NativeCodexWorker().execute(job(tmp_path, cli_binary=Path(BINARY), image_inputs=images,
            output_contract="json", timeout_seconds=8, output_limit_bytes=65536),
            asyncio.Event(), AsyncMock(), on_process_started=AsyncMock())
    assert result.status == "succeeded" and result.stop_confirmation == "confirmed", result
    assert json.loads(result.content) == expected
    assert len(calls) == 1
    observed = [part["image_url"] for message in calls[0][1]["input"] if message["type"] == "message"
        for part in message["content"] if part["type"] == "input_image"]
    assert tuple(observed) == image_data_uris(images)


@pytest.mark.skipif(not BINARY, reason="Explicit reviewed CLI and fake API required")
async def test_image_success_never_overwrites_unknown_stop(provider, tmp_path, monkeypatch):
    from kotoyose import native_codex
    original = native_codex.stop_codex_process
    async def stopped_but_proof_unknown(*args):
        assert await original(*args) == "confirmed"  # Cleanup is real; only its evidence is fault-injected.
        return "unconfirmed"
    monkeypatch.setattr(native_codex, "stop_codex_process", stopped_but_proof_unknown)
    async with fake_api(monkeypatch, tool=False) as calls:
        result = await NativeCodexWorker().execute(job(tmp_path, cli_binary=Path(BINARY), image_inputs=(image(png()),),
            timeout_seconds=8, output_limit_bytes=65536), asyncio.Event(), AsyncMock(), on_process_started=AsyncMock())
    assert (result.status, result.reason, result.stop_confirmation) == ("worker_lost", "worker_lost", "unconfirmed"), result
    assert len(calls) == 1


@pytest.mark.skipif(not BINARY, reason="Explicit reviewed CLI and fake API required")
async def test_image_cancel_preserves_unknown_broker_stop(provider, tmp_path, monkeypatch):
    entered, never, cancel = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = CodexEgress.close
    async def closed_but_proof_unknown(broker, grace):
        assert await original(broker, grace) == "confirmed"
        return "unconfirmed"
    async def blocked():
        entered.set()
        await never.wait()
    monkeypatch.setattr(CodexEgress, "close", closed_but_proof_unknown)
    monkeypatch.setattr(codex_egress, "connect_endpoint", blocked)
    operation = asyncio.create_task(NativeCodexWorker().execute(job(tmp_path, cli_binary=Path(BINARY),
        image_inputs=(image(png()),), timeout_seconds=8, output_limit_bytes=65536),
        cancel, AsyncMock(), on_process_started=AsyncMock()))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        cancel.set()
        result = await asyncio.wait_for(asyncio.shield(operation), 15)
        assert (result.status, result.reason, result.stop_confirmation) == ("cancelled", "cancelled", "unconfirmed"), result
    finally:
        cancel.set()
        await operation


@pytest.mark.skipif(not BINARY, reason="Explicit reviewed 0.170.0 CLI with fake API required")
@pytest.mark.parametrize("tool", [False, True])
@pytest.mark.parametrize("authentication", ["subscription", "api"])
@pytest.mark.parametrize("large", [False, True])
async def test_real_cli_forwards_exact_image_with_original_output_cap(provider, tmp_path, monkeypatch, tool, authentication, large):
    item = image(png(large=large))
    credentials = subscription_job(tmp_path) if authentication == "subscription" else {"credential_value": "fake-provider-key"}
    callback = AsyncMock(return_value=WorkerResult("succeeded", 0, b"ok", b"", "confirmed", "completed"))
    async with fake_api(monkeypatch, tool=tool) as calls:
        result = await NativeCodexWorker().execute(job(tmp_path, cli_binary=Path(BINARY), **credentials,
            image_inputs=(item,), tool_refs=("builtin:sandbox-exec@1",) if tool else (),
            max_turns=4, timeout_seconds=12, output_limit_bytes=65536),
            asyncio.Event(), callback, on_process_started=AsyncMock())
    assert (result.status, result.content, result.stop_confirmation) == ("succeeded", b"fixture answer", "confirmed"), result
    assert len(calls) == (2 if tool else 1)
    for _, body in calls:
        actual = [part for message in body["input"] if message["type"] == "message"
            for part in message["content"] if part["type"] == "input_image"]
        assert actual == [{"type": "input_image", "image_url": image_data_uris((item,))[0], "detail": "high"}]
        assert [t["name"] for t in body["tools"]] == (["sandbox_exec"] if tool else [])
    if tool: callback.assert_awaited_once_with(tuple(COMMAND), ".")
    else: callback.assert_not_awaited()
