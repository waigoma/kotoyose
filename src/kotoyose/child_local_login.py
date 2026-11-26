"""Local settings login. Only public sign-in prompts and status leave this helper."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import nullcontext, suppress
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
from threading import Thread

from .child_components import providers, selected_cli
from .child_process_tree import kill_pids, release_windows_job, windows_job_scope
from .child_pinned import PinnedCommandRejected, login_spec, status_spec
from .provider_spec import sdk_provider


_BROWSER_ENV = (
    "DISPLAY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR",
    "XDG_CURRENT_DESKTOP", "XAUTHORITY", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
)
_OUTPUT_LIMIT = 16 * 1024
_STATUS_TIMEOUT = 8
_CODE_LINE_LIMIT = 514


class _ParentClosed(Exception):
    pass


class _Parent:
    def __init__(self):
        self.closed = asyncio.Event()
        self.codes = asyncio.Queue(maxsize=1)

    def watch(self, stream):
        """Watch the link's stdin pipe until EOF, even when no code is expected."""
        loop = asyncio.get_running_loop()
        if sys.platform == "linux":
            loop.add_signal_handler(signal.SIGTERM, self.closed.set)

        def read():
            # FileIO avoids a buffered stdin lock during interpreter shutdown on either OS.
            with suppress(OSError, RuntimeError):
                while line := stream.readline(_CODE_LINE_LIMIT):
                    if len(line) == _CODE_LINE_LIMIT and not line.endswith(b"\n"):
                        while line and not line.endswith(b"\n"):
                            line = stream.readline(_CODE_LINE_LIMIT)
                    else:
                        loop.call_soon_threadsafe(self._queue_code, line)
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(self.closed.set)

        Thread(target=read, daemon=True).start()

    def _queue_code(self, line):
        with suppress(asyncio.QueueFull):
            self.codes.put_nowait(line)

    def ensure_open(self):
        if self.closed.is_set():
            raise _ParentClosed

    async def wait(self, operation, seconds):
        work = asyncio.create_task(operation)
        closed = asyncio.create_task(self.closed.wait())
        try:
            async with asyncio.timeout(seconds):
                await asyncio.wait((work, closed), return_when=asyncio.FIRST_COMPLETED)
            self.ensure_open()
            return work.result()
        finally:
            work.cancel()
            closed.cancel()
            await asyncio.gather(work, closed, return_exceptions=True)


def _local_login(row, platform):
    transport = (row.get("login") or {}).get("transports", {}).get(platform)
    if transport is None:
        raise PinnedCommandRejected("No local login on this OS")
    provider = row.get("provider", row["id"])
    spec, flow = login_spec(provider, row.get("profile"))
    return spec, flow, transport


def _cli_prefix(home, row):
    if sdk := sdk_provider(row["id"]):
        from .child_launch import sdk_cli_prefix

        return sdk_cli_prefix(home, sdk.child_turn.launch)
    if binary := selected_cli(home, row):
        return (str(binary),)
    raise FileNotFoundError(row["id"])


def _emit(state, **fields):
    print(json.dumps({"state": state, **fields}), flush=True)


async def _capture_status(proc):
    capture = bytearray()
    while chunk := await proc.stdout.read(4096):
        capture.extend(chunk[:_OUTPUT_LIMIT - len(capture)])
    return await proc.wait(), capture.decode("utf-8", "replace")


async def _login_status(prefix, spec, platform, cwd, env, parent, timeout, *, declared=False):
    parent.ensure_open()
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if platform == "win32" else {"start_new_session": True}
    with windows_job_scope() if declared and platform == "win32" else nullcontext() as pids:
        proc = await asyncio.create_subprocess_exec(
            *prefix, *spec.status_command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **options,
        )
        try:
            return await parent.wait(_capture_status(proc), timeout)
        finally:
            if pids is not None:
                kill_pids(pids)
            await _stop(proc, platform)


async def _stop(proc, platform):
    if platform == "linux":
        from .child_process_tree import kill_process_group

        # The group can outlive its leader (for example, a child still holding stdout).
        kill_process_group(proc.pid)
    elif proc.returncode is None:
        with suppress(ProcessLookupError):
            proc.kill()
    await proc.wait()


def _public_prompt(text, spec, flow):
    from .cli_login import _extract_official_url, strip_ansi

    url = _extract_official_url(text, spec.url_pattern, spec.url_hosts, require_terminated=True)
    if not url or len(url) > 4096:
        return None
    if flow == "paste_code":
        return {"url": url, "needs_code": True}
    if flow == "device_code" and spec.user_code_pattern:
        # Remove URLs so OAuth state cannot be mistaken for a device code.
        message = re.sub(r"""https://[^\s<>"']+""", "", strip_ansi(text))
        match = re.search(spec.user_code_pattern, message)
        if not match or match.end() == len(message):
            return None
        return {"url": url, "device_code": match.group(1)}
    return {"url": url}


async def _interact(session, spec, flow, parent):
    from .cli_login import validate_login_code, write_pty_line

    while not (prompt := _public_prompt(session._output_buf, spec, flow)):
        if session._eof_event.is_set():
            return
        session._data_event.clear()
        await session._data_event.wait()
    _emit("running", **prompt)
    if flow != "paste_code":
        return
    while True:
        line = await parent.codes.get()
        try:
            code = validate_login_code(line.decode("ascii").rstrip("\n"))
        except ValueError:
            continue
        try:
            await write_pty_line(session, code)
        except OSError:
            return
        # The CLI owns validation. If it keeps running after a submission, allow another.
        if session._process.returncode is None:
            _emit("running", **prompt)


async def _run_pty_login(prefix, spec, flow, cwd, env, parent):
    from .cli_login import PtyLoginSession, attach_pty_reader, close_master_fd, spawn_pty_process

    binary = Path(prefix[0])
    session = PtyLoginSession(
        "local", "local", "", datetime.now(timezone.utc), binary, _max_output_buffer_chars=_OUTPUT_LIMIT,
    )
    parent.ensure_open()
    env.setdefault("TERM", "xterm-256color")
    proc, fd = await spawn_pty_process(binary, (*prefix[1:], *spec.command), env=env, cwd=Path(cwd))
    session._process = proc
    try:
        attach_pty_reader(session, fd)
        _emit("running")
        interaction = asyncio.create_task(_interact(session, spec, flow, parent))
        try:
            returncode = await parent.wait(proc.wait(), spec.cli_timeout_seconds)
        finally:
            interaction.cancel()
            await asyncio.gather(interaction, return_exceptions=True)
    finally:
        close_master_fd(session)
        await _stop(proc, "linux")
    return returncode, session._output_buf


async def _run_console_login(prefix, spec, cwd, env, parent, *, declared=False):
    parent.ensure_open()
    proc = None
    try:
        with windows_job_scope() if declared else nullcontext() as pids:
            proc = await asyncio.create_subprocess_exec(
                *prefix, *spec.command, cwd=cwd, env=env, creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
            _emit("running")
            returncode = await parent.wait(proc.wait(), spec.cli_timeout_seconds)
            if pids is not None:
                release_windows_job(pids)
    except BaseException:
        if proc is not None:
            await _stop(proc, "win32")
        raise
    return returncode, ""


async def run(home, provider, mode, platform, parent):
    """Run a pinned login or status check while the link's stdin remains open."""
    from .provider_data_child import use_approved

    with use_approved(home):
        await _run(home, provider, mode, platform, parent)


async def _run(home, provider, mode, platform, parent):
    from .child_ops import _login_state, child_cli_env, resolve_child_binary
    from .cli_login import _check_login_success
    from .print_cli_profiles import get_print_cli_profile_for_provider

    row = next((row for row in providers() if row["id"] == provider), None)
    profile = get_print_cli_profile_for_provider(provider) if row is None and platform in {"linux", "win32"} else None
    declared = profile.data_declaration if profile is not None else None
    if row is None and declared is None:
        _emit("unsupported")
        return
    if declared is not None:
        profile_id = profile.id
        spec, flow = declared.login, declared.login_flow
        transport = "console" if platform == "win32" else "pty"
    else:
        profile_id = row.get("profile")
        provider = row.get("provider", provider)
        try:
            spec, flow, transport = _local_login(row, platform)
        except PinnedCommandRejected:
            _emit("unsupported")
            return
    status = status_spec(provider, profile_id)
    if mode == "status" and status is None:
        _emit("unsupported")
        return
    try:
        prefix = (str(resolve_child_binary(provider, profile_id, home)),) if declared else _cli_prefix(home, row)
    except (OSError, ValueError):
        _emit("unavailable")
        return
    if not Path(prefix[0]).is_absolute() or (platform == "win32" and Path(prefix[0]).suffix.lower() != ".exe"):
        _emit("unavailable")
        return
    env = child_cli_env(provider, profile_id)
    if declared is None:
        env.update({key: os.environ[key] for key in _BROWSER_ENV if key in os.environ})
    try:
        # A process that opened the browser can still have the temporary directory open.
        with tempfile.TemporaryDirectory(prefix="kotoyose-local-login-", ignore_cleanup_errors=True) as cwd:
            if mode == "start":
                login_env = env
                if profile_id:
                    from .child_pinned import pinned_profile

                    login_env = {**env, **pinned_profile(provider, profile_id).login_env}
                if transport == "pty":
                    result = await _run_pty_login(prefix, spec, flow, cwd, login_env, parent)
                else:
                    result = await _run_console_login(prefix, spec, cwd, login_env, parent, declared=declared is not None)
            if status is None:
                logged_in = _check_login_success(spec, *result)
            else:
                if declared is not None and mode == "start":
                    prefix = (str(resolve_child_binary(provider, profile_id, home)),)
                code, output = await _login_status(
                    prefix, status, platform, cwd, env, parent, declared.status_timeout if declared else _STATUS_TIMEOUT,
                    declared=declared is not None,
                )
                if declared is not None:
                    _emit(_login_state(provider, status, output.encode(), b"", code))
                    return
                logged_in = _check_login_success(status, code, output)
    except _ParentClosed:
        return
    except (OSError, ValueError, TimeoutError):
        _emit("failed")
    else:
        _emit("logged_in" if logged_in else "logged_out")


async def _serve(args):
    parent = _Parent()
    parent.watch(sys.stdin.buffer.raw)
    await run(args.home, args.provider, args.mode, sys.platform, parent)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("provider")
    parser.add_argument("mode", choices=("status", "start"))
    args = parser.parse_args()
    if not args.home.is_absolute():
        parser.error("--home must be absolute")
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    os.environ["KOTOYOSE_CONFIG"] = os.devnull
    asyncio.run(_serve(args))


if __name__ == "__main__":
    main()
