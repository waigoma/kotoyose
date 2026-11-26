"""Local login launches only the selected, pinned CLI and publishes filtered output."""

import asyncio
from contextlib import contextmanager
import json
from pathlib import Path
import signal
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kotoyose import child_local_login, child_pinned


@pytest.mark.parametrize(("provider", "login", "status", "output"), [
    ("claude", ("auth", "login", "--claudeai"), ("auth", "status", "--json"), b'{"loggedIn":true}'),
    ("codex", ("login", "--device-auth"), ("login", "status"), b"Logged in using ChatGPT"),
])
@pytest.mark.parametrize("platform", ["linux", "win32"])
async def test_pinned_argv_and_console_options(monkeypatch, tmp_path, capsys, provider, login, status, output, platform):
    from types import SimpleNamespace

    monkeypatch.setattr(subprocess, "CREATE_NEW_CONSOLE", 0x10, raising=False)
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    binary = tmp_path / (provider + (".exe" if platform == "win32" else ""))
    prefix = (str(binary),)
    monkeypatch.setattr(child_local_login, "_cli_prefix", lambda home, provider: prefix)
    runner = SimpleNamespace(wait=AsyncMock(return_value=0), returncode=0)
    probe = SimpleNamespace(
        stdout=SimpleNamespace(read=AsyncMock(side_effect=[output, b""])),
        wait=AsyncMock(return_value=0), returncode=0,
    )
    execute = AsyncMock(side_effect=[runner, probe] if platform == "win32" else [probe])
    monkeypatch.setattr(child_local_login.asyncio, "create_subprocess_exec", execute)
    if platform == "linux":
        def attach(session, fd):
            session._eof_event = asyncio.Event()
            session._eof_event.set()

        pty = AsyncMock(return_value=(runner, 44))
        monkeypatch.setattr("kotoyose.cli_login.spawn_pty_process", pty)
        monkeypatch.setattr("kotoyose.cli_login.attach_pty_reader", attach)
        monkeypatch.setattr("kotoyose.child_process_tree.kill_process_group", lambda _pid: None)
        runner.pid = 42
        probe.pid = 43

    await child_local_login.run(tmp_path, provider, "start", platform, child_local_login._Parent())

    check = execute.await_args
    assert check.args == (*prefix, *status) and not check.kwargs.get("shell")
    assert check.kwargs["cwd"] != str(tmp_path)
    if platform == "win32":
        launch = execute.await_args_list[0]
        assert launch.args == (*prefix, *login) and not launch.kwargs.get("shell")
        assert launch.kwargs["creationflags"] == subprocess.CREATE_NEW_CONSOLE
        assert {"stdin", "stdout", "stderr"}.isdisjoint(launch.kwargs)
        assert check.kwargs["creationflags"] == subprocess.CREATE_NO_WINDOW
    else:
        assert pty.await_args.args == (binary, login)
        assert pty.await_args.kwargs["cwd"] == Path(check.kwargs["cwd"])
        assert check.kwargs["start_new_session"]
    assert [json.loads(line) for line in capsys.readouterr().out.splitlines()] == [
        {"state": "running"}, {"state": "logged_in"},
    ]


@pytest.mark.parametrize(("provider", "returncode", "output", "expected"), [
    ("claude", 0, b'notice\n\x1b[2m{"loggedIn":true,"account":"private"}\x1b[0m', "logged_in"),
    ("claude", 0, b'{"loggedIn":false}', "logged_out"),
    ("claude", 0, b"garbled", "logged_out"),
    ("claude", 1, b'{"loggedIn":true}', "logged_out"),
    ("codex", 0, b"Logged in using ChatGPT", "logged_in"),
    ("codex", 1, b"Not logged in", "logged_out"),
])
async def test_login_status_only_publishes_boolean_state(monkeypatch, tmp_path, capsys, provider, returncode, output, expected):
    from types import SimpleNamespace

    proc = SimpleNamespace(
        stdout=SimpleNamespace(read=AsyncMock(side_effect=[output, b""])),
        wait=AsyncMock(return_value=returncode), returncode=returncode, pid=42,
    )
    monkeypatch.setattr("kotoyose.child_process_tree.kill_process_group", lambda _pid: None)
    monkeypatch.setattr(child_local_login.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))
    monkeypatch.setattr(child_local_login, "_cli_prefix", lambda home, provider: (str(tmp_path / "fake-cli"),))
    await child_local_login.run(tmp_path, provider, "status", "linux", child_local_login._Parent())
    assert json.loads(capsys.readouterr().out) == {"state": expected}


def test_only_the_pinned_device_url_and_code_are_published():
    spec, flow = child_pinned.login_spec("codex", None)
    assert child_local_login._public_prompt("https://auth.openai.com@other.example/device\nABCD-12345\n", spec, flow) is None
    output = (
        "https://auth.openai.com@other.example/device\n"
        "private diagnostic\nhttps://auth.openai.com/codex/device?state=WXYZ-9999\n\x1b[1mABCD-12345\x1b[0m\n"
    )
    assert child_local_login._public_prompt(output, spec, flow) == {
        "url": "https://auth.openai.com/codex/device?state=WXYZ-9999", "device_code": "ABCD-12345",
    }


async def test_unsupported_os_never_checks_or_launches_the_cli(monkeypatch, tmp_path, capsys):
    execute = AsyncMock()
    monkeypatch.setattr(child_local_login.asyncio, "create_subprocess_exec", execute)
    await child_local_login.run(tmp_path, "claude", "start", "darwin", child_local_login._Parent())
    assert json.loads(capsys.readouterr().out) == {"state": "unsupported"}
    execute.assert_not_awaited()


@pytest.mark.parametrize("interrupt", [TimeoutError, asyncio.CancelledError, child_local_login._ParentClosed])
async def test_windows_login_stops_its_job_when_interrupted(monkeypatch, tmp_path, interrupt):
    actions = []

    @contextmanager
    def job_scope():
        try:
            yield set()
        finally:
            actions.append("kill job")

    async def interrupted(wait, seconds):
        wait.close()
        raise interrupt

    proc = SimpleNamespace(wait=AsyncMock(), returncode=None, kill=lambda: actions.append("kill process"))
    monkeypatch.setattr(subprocess, "CREATE_NEW_CONSOLE", 0x10, raising=False)
    monkeypatch.setattr(child_local_login, "windows_job_scope", job_scope)
    monkeypatch.setattr(child_local_login, "release_windows_job", lambda _: pytest.fail("released an interrupted job"))
    monkeypatch.setattr(child_local_login.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))
    parent = SimpleNamespace(ensure_open=lambda: None, wait=interrupted)
    spec = SimpleNamespace(command=("login",), cli_timeout_seconds=1)
    with pytest.raises(interrupt):
        await child_local_login._run_console_login(("fake.exe",), spec, tmp_path, {}, parent, declared=True)
    assert actions == ["kill job", "kill process"]


@pytest.mark.skipif(sys.platform != "linux", reason="uses Linux process groups and /proc")
@pytest.mark.parametrize(("provider", "mode", "stop"), [
    ("claude", "start", "eof"),
    ("codex", "start", "eof"),
    ("claude", "status", "eof"),
    ("claude", "status", "sigterm"),
    ("codex", "start", "sigterm"),
    ("claude", "status", "timeout"),
    ("codex", "start", "timeout"),
])
async def test_parent_departure_and_timeout_kill_cli_process_group(tmp_path, provider, mode, stop):
    from textwrap import dedent

    cli = tmp_path / "fake-cli"
    processes = tmp_path / "processes"
    submitted = tmp_path / "submitted"
    cli.write_text(f"#!{sys.executable}\n" + dedent(f"""
        import os
        from pathlib import Path
        import sys
        import time

        if "status" not in sys.argv:
            assert all(os.isatty(fd) for fd in (0, 1, 2))
        child = os.fork()
        if child == 0:
            time.sleep(60)
            os._exit(0)
        Path({str(processes)!r}).write_text(f"{{os.getpid()}} {{child}}")
        if sys.argv[1:] == ["auth", "login", "--claudeai"]:
            print("https://claude.ai/authorize", flush=True)
            Path({str(submitted)!r}).write_text(input())
        time.sleep(60)
        """))
    cli.chmod(0o700)
    program = dedent("""
        from dataclasses import replace
        import sys
        from kotoyose import child_local_login as helper

        cli, stop, *args = sys.argv[1:]
        helper._cli_prefix = lambda home, provider: (cli,)
        if stop == "timeout":
            login_spec = helper.login_spec
            def short_login(*args):
                spec, flow = login_spec(*args)
                return replace(spec, cli_timeout_seconds=1), flow
            helper.login_spec = short_login
            helper._STATUS_TIMEOUT = 1
        sys.argv[1:] = args
        helper.main()
        """)
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-I", "-c", program, str(cli), stop, "--home", str(tmp_path), provider, mode,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
    )

    def alive(pid):
        try:
            return Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1][0] != "Z"
        except FileNotFoundError:
            return False

    async def marker(path):
        async with asyncio.timeout(8):
            while not (value := path.read_text() if path.exists() else ""):
                await asyncio.sleep(0.01)
        return value

    try:
        cli_pid, descendant = map(int, (await marker(processes)).split())
        if provider == "claude" and mode == "start":
            async with asyncio.timeout(8):
                assert json.loads(await proc.stdout.readline()) == {"state": "running"}
                assert json.loads(await proc.stdout.readline())["needs_code"]
            proc.stdin.write(b"wrong-code\n")
            await proc.stdin.drain()
            assert await marker(submitted) == "wrong-code"
        if stop == "eof":
            proc.stdin.close()
        elif stop == "sigterm":
            proc.send_signal(signal.SIGTERM)
        assert await asyncio.wait_for(proc.wait(), 8) == 0
        async with asyncio.timeout(3):
            while alive(cli_pid) or alive(descendant):
                await asyncio.sleep(0.01)
        states = [json.loads(line)["state"] for line in (await proc.stdout.read()).splitlines()]
        assert ("failed" in states) == (stop == "timeout")
        assert not {"logged_in", "logged_out"}.intersection(states)
    finally:
        proc.stdin.close()
        if proc.returncode is None:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), 3)
