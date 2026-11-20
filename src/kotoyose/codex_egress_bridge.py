"""jail 内専用 bridge。host TCP へ接続せず固定 UNIX broker に byte を渡す。"""

import asyncio
import contextlib
import os
import sys


async def forward(reader, writer):
    while data := await reader.read(16384):
        writer.write(data)
        await writer.drain()


async def client(reader, writer):
    remote_writer = None
    try:
        remote, remote_writer = await asyncio.open_unix_connection("/runtime/egress.sock")
        tasks = [asyncio.create_task(forward(reader, remote_writer)),
                 asyncio.create_task(forward(remote, writer))]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        for stream in (writer, remote_writer):
            if stream is not None:
                stream.close()
                with contextlib.suppress(Exception):
                    await stream.wait_closed()


async def main():
    # Model を開始せず実 binary の version を照合する。本文を controller log に出さない。
    check = await asyncio.create_subprocess_exec("/runtime/codex", "--version",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=1024)
    try:
        version = await asyncio.wait_for(check.stdout.readline(), timeout=3)
        await asyncio.wait_for(check.wait(), timeout=3)
        if check.returncode != 0 or version.strip() != b"codex-cli 0.170.0":
            return 78
    finally:
        if check.returncode is None:
            check.kill()
            await check.wait()
    server = await asyncio.start_server(client, "127.0.0.1", 18080)
    environment = dict(os.environ)
    process = await asyncio.create_subprocess_exec("/runtime/codex", *sys.argv[1:], env=environment)
    try:
        await process.wait()
    finally:
        server.close()
        await server.wait_closed()
    return process.returncode


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
