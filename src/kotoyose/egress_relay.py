"""container 内専用 relay。127.0.0.1:18080 の byte を broker の UNIX socket へ渡し、CLI を子として実行する。

標準ライブラリだけを使う。image の python3 から次の形で起動する:
    python3 /opt/agent/egress_relay.py <socket> -- <cli> <args...>
CLI の終了コードをそのまま返す。PID 1 として動いても SIGTERM/SIGINT を CLI へ渡す。
終了コードの規約: 0-255 は CLI の結果 (signal 終了は 128+n)、64 は usage、75 は listener の bind 失敗。
"""

import asyncio
import contextlib
import os
import signal
import sys

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 18080
MAX_CLIENTS = 32
_CHUNK = 16384
_USAGE = 64
_BIND_FAILED = 75


async def _pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    while data := await reader.read(_CHUNK):
        writer.write(data)
        await writer.drain()


async def _bridge(socket_path: str, client_reader, client_writer) -> None:
    remote_writer = None
    try:
        remote_reader, remote_writer = await asyncio.open_unix_connection(socket_path)
        # broker は応答を完結させてから閉じるため、どちらかの終了で接続全体を閉じてよい。
        # 片方だけ残すと keep-alive の client 側接続が broker 終了後も居残る。
        tasks = [asyncio.create_task(_pump(client_reader, remote_writer)),
                 asyncio.create_task(_pump(remote_reader, client_writer))]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    except OSError:
        pass
    finally:
        for stream in (client_writer, remote_writer):
            if stream is not None:
                stream.close()
                with contextlib.suppress(Exception):
                    await stream.wait_closed()


async def _forward_stop(process, stop: asyncio.Event) -> None:
    """停止要求を CLI へ渡す。要求が CLI の起動前に届いていても、起動直後に渡す。"""
    await stop.wait()
    with contextlib.suppress(ProcessLookupError):
        process.send_signal(signal.SIGTERM)


def _kill_session_helpers(session_id: int) -> None:
    # Helpers can change process groups while retaining the attached stdout and stderr.
    for name in os.listdir("/proc"):
        if name.isdigit():
            pid = int(name)
            with contextlib.suppress(ProcessLookupError, PermissionError):
                if os.getsid(pid) == session_id:
                    os.kill(pid, signal.SIGKILL)


async def _run(socket_path: str, cli: list[str]) -> int:
    # CLI を起動する前に停止を受け取れるようにする。
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)

    active = 0

    async def handle(reader, writer):
        nonlocal active
        if active >= MAX_CLIENTS:
            # 上限超過の接続は応答せず閉じる。CLI 側は接続失敗として扱う。
            writer.close()
            return
        active += 1
        try:
            await _bridge(socket_path, reader, writer)
        finally:
            active -= 1

    try:
        server = await asyncio.start_server(handle, LISTEN_HOST, LISTEN_PORT)
    except OSError as exc:
        print(f"egress_relay: cannot bind {LISTEN_HOST}:{LISTEN_PORT}: {exc.strerror}", file=sys.stderr)
        return _BIND_FAILED
    process = await asyncio.create_subprocess_exec(*cli, env=dict(os.environ), start_new_session=True)
    forwarder = asyncio.create_task(_forward_stop(process, stop))
    try:
        # The CLI inherits the relay's descriptors, so this wait tracks only the leader.
        code = await process.wait()
        _kill_session_helpers(process.pid)
    finally:
        forwarder.cancel()
        # 残存する keep-alive 接続を待つと CLI 終了後も居残るため、listener だけ閉じる。
        server.close()
    return code if code >= 0 else 128 - code


def main(argv: list[str]) -> int:
    if len(argv) < 4 or argv[2] != "--":
        print("usage: egress_relay.py <socket> -- <cli> [args...]", file=sys.stderr)
        return _USAGE
    return asyncio.run(_run(argv[1], argv[3:]))


if __name__ == "__main__":
    sys.exit(main(sys.argv))
