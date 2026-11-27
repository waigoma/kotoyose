"""Stdio entry point for the child link app."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from .child_protocol import MAX_RECORD as MAX_RECORD, MAX_SKILL_TURN_RECORD

IPC_VERSION = 1
STARTS = {"turn", "login", "quota", "status", "session_collect"}
TURN_KINDS = {"turn", "cancel", "release", "mcp_response"}
OPS_KINDS = {
    "login", "login_input", "login_cancel", "quota", "quota_cancel",
    "status", "status_cancel", "session_collect", "session_collect_cancel",
}
TERMINALS = {
    "result", "error", "login_exit", "login_error", "quota_result", "quota_error",
    "status_result", "status_error", "session_collect_done", "session_collect_error",
}


def _read_exact(handle: BinaryIO, size: int, *, allow_eof: bool = False) -> bytes | None:
    data = bytearray()
    while len(data) < size:
        chunk = handle.read(size - len(data))
        if not chunk:
            if allow_eof and not data:
                return None
            raise ValueError("truncated runner record")
        data.extend(chunk)
    return bytes(data)


def read_record(handle: BinaryIO, limit: int = MAX_RECORD) -> bytes | None:
    prefix = _read_exact(handle, 4, allow_eof=True)
    if prefix is None:
        return None
    size = int.from_bytes(prefix, "big")
    if not 1 <= size <= limit:
        raise ValueError("invalid runner record length")
    return _read_exact(handle, size)


def pack_record(body: bytes) -> bytes:
    if not 1 <= len(body) <= MAX_RECORD:
        raise ValueError("invalid runner record length")
    return len(body).to_bytes(4, "big") + body


def _redirect_stdio() -> tuple[BinaryIO, BinaryIO]:
    # os.dup produces non-inheritable descriptors on both platforms.
    source = os.fdopen(os.dup(0), "rb")
    sink = os.fdopen(os.dup(1), "wb")
    sys.stdout.flush()
    with open(os.devnull, "rb") as null:
        os.dup2(null.fileno(), 0)
    os.dup2(2, 1)
    if sys.platform == "win32":
        import ctypes
        import msvcrt

        for handle in (source, sink):
            msvcrt.setmode(handle.fileno(), os.O_BINARY)
        set_handle = ctypes.WinDLL("kernel32", use_last_error=True).SetStdHandle
        set_handle.argtypes = (ctypes.c_uint32, ctypes.c_void_p)
        for fd, identifier in ((0, -10), (1, -11)):
            if not set_handle(identifier, msvcrt.get_osfhandle(fd)):
                raise ctypes.WinError(ctypes.get_last_error())
    sys.stdout = sys.stderr
    return source, sink


@dataclass
class _Work:
    kind: str
    task: asyncio.Task | None = None
    release: asyncio.Task | None = None
    terminal: bool = False
    cancelled: bool = False


class Runner:
    def __init__(self, home: Path, sink: BinaryIO, idle: int, *, host_fingerprint: str | None = None) -> None:
        from .child_ops import ChildOps
        from .child_turn import ChildTurns
        from .config import SESSION_TTL_SECONDS

        self.sink = sink
        self.write_lock = asyncio.Lock()
        self.work: dict[int, _Work] = {}
        self.watchers: set[asyncio.Task] = set()
        self.turns = ChildTurns(
            home, self.send, None, session_ttl=min(SESSION_TTL_SECONDS, idle), host_fingerprint=host_fingerprint,
        )
        self.ops = ChildOps(home, self.send)

    def _write(self, record: bytes) -> None:
        self.sink.write(record)
        self.sink.flush()

    async def send(self, stream: int, kind: str, payload: dict) -> None:
        from .child_protocol import encode_frame

        work = self.work.get(stream)
        if work is not None and (work.cancelled or work.terminal):
            return
        oversized = False
        try:
            record = pack_record(encode_frame(stream, kind, payload).encode("utf-8"))
        except ValueError:
            if work is None:
                raise
            oversized = True
            kind, payload = self.error(work.kind, "runner frame exceeds size limit")
            record = pack_record(encode_frame(stream, kind, payload).encode("utf-8"))
        async with self.write_lock:
            # Cancellation of a stream must leave a complete record on the pipe.
            writing = asyncio.create_task(asyncio.to_thread(self._write, record))
            try:
                await asyncio.shield(writing)
            except asyncio.CancelledError:
                await writing
                raise
        if work is not None and kind in TERMINALS:
            work.terminal = True
        if oversized:
            raise ValueError("runner frame exceeds size limit")

    @staticmethod
    def error(kind: str, message: str) -> tuple[str, dict]:
        if kind == "session_collect":
            return "session_collect_error", {"error": message}
        if kind != "turn":
            return f"{kind}_error", {"message": message}
        from .child_turn import _result_payload
        from .runtime.base import RuntimeResult

        return "error", {
            "message": message, "retryable": False, "diagnostics": {},
            "partial_result": _result_payload(RuntimeResult(text="")),
        }

    async def done(self, stream: int, work: _Work) -> None:
        if work.task is not None:
            await asyncio.gather(work.task, return_exceptions=True)
        if work.release is not None:
            await asyncio.gather(work.release, return_exceptions=True)
        if not work.terminal and not work.cancelled:
            await self.send(stream, *self.error(work.kind, "runner operation ended without a result"))
        self.work.pop(stream)
        await self.send(0, "runner_stream_done", {"stream": stream})

    async def dispatch(self, raw: bytes, envelope: object) -> None:
        from .child_protocol import decode_frame

        # Read the envelope separately so a malformed operation can fail its own stream.
        if not isinstance(envelope, dict):
            raise ValueError("invalid runner frame")
        stream, kind = envelope.get("stream"), envelope.get("kind")
        if type(stream) is not int or not 1 <= stream <= 1_000_000_000:
            raise ValueError("invalid runner stream")
        if not isinstance(kind, str) or kind not in TURN_KINDS | OPS_KINDS:
            raise ValueError("invalid runner kind")
        work = self.work.get(stream)
        try:
            frame = decode_frame(raw)
        except ValueError:
            if work is not None:
                await self.send(stream, *self.error(work.kind, "invalid runner operation frame"))
                if work.task is not None and not work.task.cancelling():
                    work.task.cancel()
            elif kind in STARTS:
                await self.send(stream, *self.error(kind, "invalid runner operation frame"))
                await self.send(0, "runner_stream_done", {"stream": stream})
            return

        if kind in STARTS:
            if work is not None:
                raise ValueError("duplicate runner stream")
            work = self.work[stream] = _Work(kind)
            work.task = self.turns.handle(frame) if kind == "turn" else await self.ops.handle(frame)
            watcher = asyncio.create_task(self.done(stream, work))
            self.watchers.add(watcher)
            watcher.add_done_callback(self.watchers.discard)
        else:
            if work is not None and kind in {
                "cancel", "release", "login_cancel", "quota_cancel", "status_cancel", "session_collect_cancel",
            }:
                work.cancelled = True
            if kind in TURN_KINDS:
                release = self.turns.handle(frame)
                if work is not None and release is not None and work.release is None:
                    work.release = release
            else:
                await self.ops.handle(frame)

    async def close(self) -> None:
        self.turns.begin_shutdown()
        self.ops.begin_shutdown()
        await asyncio.gather(self.turns.finish(), self.ops.finish())
        await asyncio.gather(*self.watchers)


async def serve(home: Path, source: BinaryIO, sink: BinaryIO) -> None:
    from .child_process_tree import STRICT_WINDOWS_JOBS
    from .child_protocol import current_package_version, decode_frame, is_skill_folder_turn

    raw = await asyncio.to_thread(read_record, source)
    if raw is None:
        return
    init = decode_frame(raw)
    version = current_package_version()
    idle = init.payload.get("runner_idle_seconds")
    if (
        init.stream != 0 or init.kind != "runner_init"
        or type(init.payload.get("ipc")) is not int or init.payload["ipc"] != IPC_VERSION
        or init.payload.get("version") != version or type(idle) is not int or idle < 0
    ):
        raise ValueError("incompatible runner_init")

    strict = STRICT_WINDOWS_JOBS.set(True)
    from .child_mcp import valid_version

    fingerprint = init.payload.get("host_fingerprint")
    runner = Runner(home, sink, idle, host_fingerprint=fingerprint if valid_version(fingerprint) else None)
    runner.turns.start()
    shutdown = False
    try:
        await runner.send(0, "runner_ready", {"ipc": IPC_VERSION, "version": version})
        while (raw := await asyncio.to_thread(read_record, source, MAX_SKILL_TURN_RECORD)) is not None:
            # Only stream zero controls are decoded here. Work gets per-stream validation in dispatch.
            envelope = json.loads(raw.decode("utf-8"))
            if len(raw) > MAX_RECORD and not is_skill_folder_turn(envelope):
                raise ValueError("invalid runner record length")
            if isinstance(envelope, dict) and type(envelope.get("stream")) is int and envelope["stream"] == 0:
                frame = decode_frame(raw)
                if frame.kind == "runner_link_lost" and not frame.payload:
                    await runner.turns.link_lost()
                    continue
                if frame.kind != "runner_shutdown" or frame.payload or runner.work:
                    raise ValueError("unexpected runner control")
                shutdown = True
                break
            await runner.dispatch(raw, envelope)
    finally:
        try:
            await runner.close()
        finally:
            STRICT_WINDOWS_JOBS.reset(strict)
    if shutdown:
        await runner.send(0, "runner_stopped", {})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kotoyose-child-runner")
    parser.add_argument("--home", required=True, type=Path)
    parser.add_argument("--temp", type=Path)
    parser.add_argument("--components", choices=("availability", "status", "check"))
    args = parser.parse_args(argv)
    if not args.home.is_absolute():
        parser.error("--home must be absolute")
    if args.temp is not None:
        if not args.temp.is_absolute() or not args.temp.is_dir():
            parser.error("--temp must be an absolute directory")
        tempfile.tempdir = str(args.temp)
    # Runtime builders import the shared host configuration module.
    # Their child configuration comes from the turn and policy instead.
    os.environ["KOTOYOSE_CHILD_RUNNER"] = "1"
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    os.environ["KOTOYOSE_CONFIG"] = os.devnull
    os.environ.pop("KOTOYOSE_MASTER_KEY_FILE", None)
    os.environ.pop("CREDENTIALS_DIRECTORY", None)
    if args.components:
        import io
        from contextlib import redirect_stdout

        from .child_components import probe

        with redirect_stdout(io.StringIO()):
            rows = probe(args.home, versions=args.components == "check")
        print(json.dumps(rows))
        return 0
    source, sink = _redirect_stdio()
    with source, sink:
        try:
            asyncio.run(serve(args.home, source, sink))
        except (OSError, ValueError, RecursionError) as exc:
            print(f"runner stopped reason={type(exc).__name__}", file=sys.stderr, flush=True)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
