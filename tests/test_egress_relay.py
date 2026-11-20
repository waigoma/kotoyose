"""container 内 relay の試験。実 CLI は使わず、python の子 process を fake CLI として起動する。"""

import ast
import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

from kotoyose import egress_relay

RELAY = Path(egress_relay.__file__)

FAKE_CLI = """
import sys, urllib.request
with urllib.request.urlopen("http://127.0.0.1:18080/ping", timeout=5) as response:
    print(response.read().decode())
sys.exit(7)
"""


@pytest.fixture
def short_dir():
    path = tempfile.mkdtemp(dir="/tmp", prefix="er")
    yield Path(path)
    shutil.rmtree(path, ignore_errors=True)


async def _echo_request_server(path: Path):
    async def handle(reader, writer):
        head = await reader.readuntil(b"\r\n\r\n")
        body = head.split(b"\r\n", 1)[0]
        writer.write(b"HTTP/1.1 200 OK\r\ncontent-type: text/plain\r\ncontent-length: "
                     + str(len(body)).encode() + b"\r\nconnection: close\r\n\r\n" + body)
        await writer.drain()
        writer.close()

    return await asyncio.start_unix_server(handle, path=str(path))


async def test_relay_forwards_cli_request_to_socket_and_propagates_exit_code(short_dir):
    socket_path = short_dir / "relay.sock"
    server = await _echo_request_server(socket_path)
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable, str(RELAY), str(socket_path), "--", sys.executable, "-I", "-c", FAKE_CLI,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(process.communicate(), 20)
    finally:
        server.close()

    assert process.returncode == 7, stderr.decode()
    assert stdout.decode().strip() == "GET /ping HTTP/1.1"


async def test_relay_without_separator_exits_with_usage_error(short_dir):
    process = await asyncio.create_subprocess_exec(
        sys.executable, str(RELAY), str(short_dir / "x.sock"), sys.executable, "-c", "pass",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)

    assert await asyncio.wait_for(process.wait(), 20) == 64


def test_relay_uses_only_the_standard_library():
    tree = ast.parse(RELAY.read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= set(sys.stdlib_module_names), imported - set(sys.stdlib_module_names)


async def test_relay_exits_75_when_the_port_is_already_bound(short_dir):
    import socket

    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        holder.bind(("127.0.0.1", 18080))
        holder.listen()
        process = await asyncio.create_subprocess_exec(
            sys.executable, str(RELAY), str(short_dir / "x.sock"), "--", sys.executable, "-c", "raise SystemExit(3)",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        _, stderr = await asyncio.wait_for(process.communicate(), 20)
    finally:
        holder.close()

    # CLI は起動されず、CLI の終了コード 3 とも衝突しない 75 を返す。
    assert process.returncode == 75
    assert b"cannot bind 127.0.0.1:18080" in stderr
