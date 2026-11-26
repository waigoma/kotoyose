"""`kotoyose-child enroll` と `kotoyose-child run`。"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
from pathlib import Path

from .child_client import ChildRejected, HostFingerprintMismatch, enroll_child, run_child
from .child_identity import default_child_home
from .child_lock import child_lock
from .child_windows import (
    child_run_argv,
    logon_task_create_argv,
    logon_task_delete_argv,
    logon_task_xml,
    windows_logon_user_id,
    write_logon_task_xml,
)


def _default_name() -> str:
    host = socket.gethostname().split(".")[0]
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "-", host).strip("-._")[:64]
    if not cleaned or not cleaned[0].isalnum():
        return "child"
    return cleaned


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kotoyose-child")
    sub = parser.add_subparsers(dest="command", required=True)
    enroll = sub.add_parser("enroll", help="register this machine with a one-time code")
    enroll.add_argument("code")
    enroll.add_argument("--name", default=None)
    enroll.add_argument("--home", default=None)
    run = sub.add_parser("run", help="keep a pinned connection to the host")
    run.add_argument("--home", default=None)
    sub.add_parser("install-task", help="register a logon task that runs kotoyose-child (Windows)")
    sub.add_parser("uninstall-task", help="remove the logon task (Windows)")
    return parser


def _home(value: str | None) -> Path:
    return Path(value).expanduser() if value else default_child_home()


def arm_stop(stop: asyncio.Event, *, loop=None, signal_module=None) -> None:
    """止める合図を loop に載せる。Windows の loop は add_signal_handler を実装しない。

    そのときは Ctrl+C (SIGINT) と Ctrl+Break (SIGBREAK) と SIGTERM で stop を立てる。
    run は stop を見ると、生きている turn を消し、そのプロセスの木を殺し、リンクを閉じる。
    """
    signals = signal if signal_module is None else signal_module
    if loop is None:
        loop = asyncio.get_running_loop()
    try:
        for sig in (signals.SIGINT, signals.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
    except NotImplementedError:
        _arm_console_stop(stop, loop=loop, signal_module=signals)


def _arm_console_stop(stop: asyncio.Event, *, loop, signal_module) -> None:
    def handler(_signum, _frame) -> None:
        try:
            loop.call_soon_threadsafe(stop.set)
        except RuntimeError:
            # loop はもう閉じている。止める相手がいない。
            return

    for name in ("SIGINT", "SIGBREAK", "SIGTERM"):
        sig = getattr(signal_module, name, None)
        if sig is not None:
            signal_module.signal(sig, handler)


def install_logon_task(*, user_id: str, which, executable: str, runner, home: Path) -> int:
    """schtasks に、対話ログオンで `kotoyose-child run` を動かす task を登録する。

    XML は child の home に BOM 付き UTF-16 で書き、登録が終わったら消す。
    """
    xml = logon_task_xml(user_id=user_id, run_argv=child_run_argv(which=which, executable=executable))
    path = write_logon_task_xml(home, xml)
    try:
        argv = logon_task_create_argv(xml_path=str(path))
        completed = runner(argv, check=False, shell=False)
        return int(completed.returncode)
    finally:
        path.unlink(missing_ok=True)


async def _run(home: Path) -> None:
    stop = asyncio.Event()
    arm_stop(stop)
    await run_child(home, stop=stop)


def _windows_task(command: str) -> int:
    if sys.platform != "win32":
        print("install-task and uninstall-task are Windows-only", file=sys.stderr)
        return 2
    if command == "install-task":
        try:
            user_id = windows_logon_user_id(os.environ.get("USERDOMAIN") or "", os.environ.get("USERNAME") or "")
            return install_logon_task(
                user_id=user_id,
                which=shutil.which,
                executable=sys.executable,
                runner=subprocess.run,
                home=default_child_home(),
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    completed = subprocess.run(logon_task_delete_argv(), check=False, shell=False)
    return int(completed.returncode)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command in {"install-task", "uninstall-task"}:
        return _windows_task(args.command)
    home = _home(args.home)
    try:
        with child_lock(home):
            if args.command == "enroll":
                name = args.name or _default_name()
                asyncio.run(enroll_child(args.code, home, name=name))
                print(f"enrolled {name}")
                return 0
            asyncio.run(_run(home))
            return 0
    except (ChildRejected, HostFingerprintMismatch, ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
