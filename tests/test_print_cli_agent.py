"""Subprocess worker tests for `cli.print` using fake CLI scripts only."""

from __future__ import annotations

import asyncio
import builtins
import json
import os
from pathlib import Path
import sys
import textwrap

import pytest

from kotoyose.agent_deadlines import AgentStartRejected
from kotoyose.print_cli_agent import _HOME_SEMAPHORES, PrintCliJob, PrintCliWorker, has_print_cli_home_capacity


def _write_fake_agy(bin_dir: Path, body: str) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "agy"
    script.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body), encoding="utf-8")
    script.chmod(0o755)
    return script


def _make_job(
    *,
    cli_binary: Path,
    workspace: Path,
    login_home: Path,
    surface: str = "read",
    prompt: str = "hello",
    output_contract: str = "text",
    output_schema: dict | None = None,
    output_limit_bytes: int = 64 * 1024,
    timeout_seconds: float = 5.0,
    grace_seconds: float = 0.2,
) -> PrintCliJob:
    return PrintCliJob(
        profile_id="agy",
        cli_binary=cli_binary,
        model="gemini-4-hydrogen-high",
        workspace=workspace,
        login_home=login_home,
        surface=surface,
        instructions="Follow instructions.",
        prompt=prompt,
        output_contract=output_contract,
        timeout_seconds=timeout_seconds,
        grace_seconds=grace_seconds,
        output_limit_bytes=output_limit_bytes,
        memory_bytes=512 * 1024 * 1024,
        pids_limit=64,
        cpus=2.0,
        output_schema=output_schema,
    )


async def test_worker_env_allowlist_no_workspace_overlays_and_never_opens_login_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _HOME_SEMAPHORES.clear()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    login_home = tmp_path / "agy-login-home"
    login_home.mkdir()
    secret_file = login_home / "secret.json"
    secret_file.write_text('{"token":"fake"}', encoding="utf-8")
    capture_file = tmp_path / "capture.json"

    fake_agy = _write_fake_agy(
        tmp_path / "bin",
        f"""
        import json, os, sys
        from pathlib import Path
        entries = sorted(p.name for p in Path.cwd().iterdir())
        Path({str(capture_file)!r}).write_text(
            json.dumps({{"env": dict(os.environ), "argv": sys.argv[1:], "cwd": os.getcwd(), "entries": entries}}),
            encoding="utf-8",
        )
        print(json.dumps({{"event": "init", "conversation_id": "c1", "init": {{"model": "gemini-4-hydrogen-high"}}}}))
        print(json.dumps({{"event": "step_update", "step_update": {{"step_index": 0, "state": "DONE", "step_type": "agent_response", "text_delta": "ok", "usage": {{"input_tokens": 5, "output_tokens": 2}}}}}}))
        print(json.dumps({{"event": "result", "result": {{"conversation_id": "c1", "status": "SUCCESS", "response": "done", "num_turns": 1, "usage": {{"input_tokens": 5, "output_tokens": 2}}}}}}))
        """,
    )

    monkeypatch.setenv("KOTOYOSE_SENTINEL_SECRET", "must-not-leak")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk\x2dant-must-not-leak")
    monkeypatch.setenv("LANG", "C.UTF-8")

    opened_under_login_home: list[str] = []
    login_home_prefix = os.path.abspath(str(login_home))
    real_open = builtins.open
    real_os_open = os.open

    def spy_open(file, *args, **kwargs):
        if isinstance(file, (str, bytes, os.PathLike)):
            resolved = os.path.abspath(os.fspath(file))
            if resolved == login_home_prefix or resolved.startswith(login_home_prefix + os.sep):
                opened_under_login_home.append(resolved)
        return real_open(file, *args, **kwargs)

    def spy_os_open(path, *args, **kwargs):
        if isinstance(path, (str, bytes, os.PathLike)):
            resolved = os.path.abspath(os.fspath(path))
            if resolved == login_home_prefix or resolved.startswith(login_home_prefix + os.sep):
                opened_under_login_home.append(resolved)
        return real_os_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", spy_open)
    monkeypatch.setattr(os, "open", spy_os_open)

    events: list[tuple[str, dict]] = []

    async def on_started(pid: int) -> None:
        assert pid > 0

    async def on_event(kind: str, payload: dict) -> None:
        events.append((kind, payload))

    worker = PrintCliWorker()
    result = await worker.execute(
        _make_job(cli_binary=fake_agy, workspace=workspace, login_home=login_home, surface="write"),
        asyncio.Event(),
        on_process_started=on_started,
        on_event=on_event,
    )

    assert result.status == "succeeded"
    assert result.reason == "completed"
    assert result.content == b"done"
    assert result.turns == 1
    assert result.provider_session_id is None
    assert result.usage == {"source": "reported", "cost": None, "input_tokens": 5, "output_tokens": 2}
    assert result.stop_confirmation == "confirmed"
    assert result.launch_outcome == "started"

    assert opened_under_login_home == []
    assert list(workspace.iterdir()) == []  # Zero overlay/settings files written into workspace.

    captured = json.loads(capture_file.read_text(encoding="utf-8"))
    child_env = captured["env"]
    assert child_env["HOME"] == str(login_home)
    assert "KOTOYOSE_SENTINEL_SECRET" not in child_env
    assert "ANTHROPIC_API_KEY" not in child_env
    assert set(child_env) <= {"PATH", "LANG", "TERM", "HOME"}
    assert captured["cwd"] == str(workspace)
    assert captured["entries"] == []
    assert "--settings" in captured["argv"]
    settings_path = captured["argv"][captured["argv"].index("--settings") + 1]
    assert not settings_path.startswith(str(workspace))
    assert not Path(settings_path).exists()  # Cleaned up after execution.
    assert any(kind == "provider_started" for kind, _ in events)
    assert any(kind == "provider_finished" for kind, _ in events)


async def test_worker_exit_3_quota_non_success_and_missing_completion(tmp_path: Path) -> None:
    _HOME_SEMAPHORES.clear()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    login_home = tmp_path / "home"
    login_home.mkdir()

    async def noop_started(pid: int) -> None:
        pass

    async def noop_event(kind: str, payload: dict) -> None:
        pass

    worker = PrintCliWorker()

    # Exit code 3 -> timeout
    exit3_bin = _write_fake_agy(
        tmp_path / "bin3",
        """
        import json, sys
        print(json.dumps({"event": "step_update", "step_update": {"step_index": 0, "step_type": "agent_response", "text_delta": "partial", "usage": {"input_tokens": 3, "output_tokens": 1}}}))
        sys.stderr.write('AGY_ERROR: {"status": "DEADLINE_EXCEEDED"}\\n')
        sys.exit(3)
        """,
    )
    res_timeout = await worker.execute(
        _make_job(cli_binary=exit3_bin, workspace=workspace, login_home=login_home),
        asyncio.Event(),
        on_process_started=noop_started,
        on_event=noop_event,
    )
    assert res_timeout.status == "timeout"
    assert res_timeout.reason == "timeout"

    # Quota error -> provider_limit (Hub waits instead of failing)
    quota_bin = _write_fake_agy(
        tmp_path / "binq",
        """
        import sys
        sys.stderr.write('AGY_ERROR: {"status": "RESOURCE_EXHAUSTED", "code": 429}\\n')
        sys.stderr.write('MODEL_QUOTA_EXHAUSTED: quota will reset later\\n')
        sys.exit(1)
        """,
    )
    res_quota = await worker.execute(
        _make_job(cli_binary=quota_bin, workspace=workspace, login_home=login_home),
        asyncio.Event(),
        on_process_started=noop_started,
        on_event=noop_event,
    )
    assert res_quota.status == "failed"
    assert res_quota.reason == "provider_usage_limit"
    assert res_quota.provider_limit is not None
    assert res_quota.provider_limit.provider == "print_cli"

    # Missing result event -> missing_completion with lower-bound usage
    missing_bin = _write_fake_agy(
        tmp_path / "binm",
        """
        import json
        print(json.dumps({"event": "step_update", "step_update": {"step_index": 0, "step_type": "agent_response", "text_delta": "half", "usage": {"input_tokens": 11, "output_tokens": 4}}}))
        """,
    )
    res_missing = await worker.execute(
        _make_job(cli_binary=missing_bin, workspace=workspace, login_home=login_home),
        asyncio.Event(),
        on_process_started=noop_started,
        on_event=noop_event,
    )
    assert res_missing.status == "failed"
    assert res_missing.reason == "missing_completion"
    assert res_missing.usage == {"source": "reported", "cost": None, "input_tokens": 11, "output_tokens": 4}

    # Non-SUCCESS result -> provider_error
    fail_bin = _write_fake_agy(
        tmp_path / "binf",
        """
        import json
        print(json.dumps({"event": "result", "result": {"status": "ERROR", "response": "", "num_turns": 1}}))
        """,
    )
    res_fail = await worker.execute(
        _make_job(cli_binary=fail_bin, workspace=workspace, login_home=login_home),
        asyncio.Event(),
        on_process_started=noop_started,
        on_event=noop_event,
    )
    assert res_fail.status == "failed"
    assert res_fail.reason == "provider_error"


async def test_worker_cancellation_kills_grandchild(tmp_path: Path) -> None:
    _HOME_SEMAPHORES.clear()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    login_home = tmp_path / "home"
    login_home.mkdir()
    pid_file = tmp_path / "grandchild.pid"

    fake_agy = _write_fake_agy(
        tmp_path / "binc",
        f"""
        import subprocess, sys, time
        from pathlib import Path
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        Path({str(pid_file)!r}).write_text(str(child.pid), encoding="utf-8")
        time.sleep(30)
        """,
    )

    cancel = asyncio.Event()

    async def on_started(pid: int) -> None:
        pass

    async def on_event(kind: str, payload: dict) -> None:
        pass

    worker = PrintCliWorker()
    task = asyncio.create_task(
        worker.execute(
            _make_job(cli_binary=fake_agy, workspace=workspace, login_home=login_home, grace_seconds=0.1),
            cancel,
            on_process_started=on_started,
            on_event=on_event,
        )
    )
    for _ in range(100):
        if pid_file.exists():
            break
        await asyncio.sleep(0.02)
    assert pid_file.exists()
    grandchild_pid = int(pid_file.read_text(encoding="utf-8").strip())

    cancel.set()
    result = await task
    assert result.status == "cancelled"
    assert result.reason == "cancelled"
    assert result.stop_confirmation == "confirmed"
    with pytest.raises(ProcessLookupError):
        os.kill(grandchild_pid, 0)


async def test_worker_output_limit_and_json_contract(tmp_path: Path) -> None:
    _HOME_SEMAPHORES.clear()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    login_home = tmp_path / "home"
    login_home.mkdir()
    prompt_capture = tmp_path / "prompt.txt"

    async def noop_started(pid: int) -> None:
        pass

    async def noop_event(kind: str, payload: dict) -> None:
        pass

    worker = PrintCliWorker()

    # Output limit exceeded
    big_bin = _write_fake_agy(
        tmp_path / "bin_big",
        """
        import json
        print(json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "x" * 200, "num_turns": 1}}))
        """,
    )
    over = await worker.execute(
        _make_job(cli_binary=big_bin, workspace=workspace, login_home=login_home, output_limit_bytes=50),
        asyncio.Event(),
        on_process_started=noop_started,
        on_event=noop_event,
    )
    assert over.status == "failed"
    assert over.reason == "output_limit_exceeded"
    assert len(over.content) <= 50

    # JSON schema via prompt + markdown-fenced JSON extraction
    json_bin = _write_fake_agy(
        tmp_path / "bin_json",
        f"""
        import json, sys
        from pathlib import Path
        argv = sys.argv[1:]
        prompt = argv[argv.index("-p") + 1]
        Path({str(prompt_capture)!r}).write_text(prompt, encoding="utf-8")
        print(json.dumps({{"event": "result", "result": {{"status": "SUCCESS", "response": "Sure:\\n```json\\n{{\\"ok\\": true, \\"n\\": 7}}\\n```", "num_turns": 1}}}}))
        """,
    )
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}, "n": {"type": "integer"}}, "required": ["ok", "n"]}
    res_json = await worker.execute(
        _make_job(
            cli_binary=json_bin,
            workspace=workspace,
            login_home=login_home,
            output_contract="json",
            output_schema=schema,
        ),
        asyncio.Event(),
        on_process_started=noop_started,
        on_event=noop_event,
    )
    assert res_json.status == "succeeded"
    assert json.loads(res_json.content) == {"ok": True, "n": 7}
    assert '"ok"' in prompt_capture.read_text(encoding="utf-8")


async def test_per_home_semaphore_raises_start_rejected_when_full(tmp_path: Path) -> None:
    _HOME_SEMAPHORES.clear()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    login_home_a = tmp_path / "home_a"
    login_home_b = tmp_path / "home_b"
    login_home_a.mkdir()
    login_home_b.mkdir()
    gate = tmp_path / "release.flag"
    started_count_file = tmp_path / "started.log"

    fake_agy = _write_fake_agy(
        tmp_path / "bins",
        f"""
        import json, time
        from pathlib import Path
        with open({str(started_count_file)!r}, "a", encoding="utf-8") as fh:
            fh.write("started\\n")
        for _ in range(100):
            if Path({str(gate)!r}).exists():
                break
            time.sleep(0.02)
        print(json.dumps({{"event": "result", "result": {{"status": "SUCCESS", "response": "done", "num_turns": 1}}}}))
        """,
    )

    started_calls = 0

    async def on_started(pid: int) -> None:
        nonlocal started_calls
        started_calls += 1

    async def noop_event(kind: str, payload: dict) -> None:
        pass

    worker = PrintCliWorker()
    t1 = asyncio.create_task(
        worker.execute(
            _make_job(cli_binary=fake_agy, workspace=workspace, login_home=login_home_a),
            asyncio.Event(),
            on_process_started=on_started,
            on_event=noop_event,
        )
    )
    t2 = asyncio.create_task(
        worker.execute(
            _make_job(cli_binary=fake_agy, workspace=workspace, login_home=login_home_a),
            asyncio.Event(),
            on_process_started=on_started,
            on_event=noop_event,
        )
    )
    for _ in range(100):
        if started_calls >= 2:
            break
        await asyncio.sleep(0.02)
    assert started_calls == 2
    assert has_print_cli_home_capacity("agy", login_home_a) is False
    assert has_print_cli_home_capacity("agy", login_home_b) is True

    # 3rd concurrent request on login_home_a must reject before spawning.
    with pytest.raises(AgentStartRejected) as exc_info:
        await worker.execute(
            _make_job(cli_binary=fake_agy, workspace=workspace, login_home=login_home_a),
            asyncio.Event(),
            on_process_started=on_started,
            on_event=noop_event,
        )
    assert exc_info.value.code == "capacity_exhausted"
    assert exc_info.value.status == 409

    # Another login home runs fine concurrently while home_a is full.
    gate.write_text("go", encoding="utf-8")
    r_other = await worker.execute(
        _make_job(cli_binary=fake_agy, workspace=workspace, login_home=login_home_b),
        asyncio.Event(),
        on_process_started=on_started,
        on_event=noop_event,
    )
    r1, r2 = await asyncio.gather(t1, t2)
    assert r1.status == "succeeded"
    assert r2.status == "succeeded"
    assert r_other.status == "succeeded"
    assert has_print_cli_home_capacity("agy", login_home_a) is True
