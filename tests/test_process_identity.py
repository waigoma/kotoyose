"""provider process identity の procfs parsing と pidfd recovery を検証する。"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from kotoyose.process_identity import (
    ProcessIdentity,
    _pidfd_backend,
    capture_process_identity,
    stop_identified_process,
)


def _child(*code: str) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", *code])


def _permission_denied(_pid: int, _flags: int) -> int:
    raise PermissionError


def test_capture_parses_comm_with_spaces_and_parentheses(monkeypatch, tmp_path):
    """proc stat の comm 終端を最後の ')' で求め、空白や括弧を壊さない。"""
    # field 3 comm = ``worker ) with spaces``; field 22 は 20 番目の remainder。
    remainder = "S " + " ".join(str(i) for i in range(4, 23))
    boot = tmp_path / "boot_id"
    boot.write_text("boot-test\n")
    real_read_text = Path.read_text
    real_stat = Path.stat
    with (
        patch("kotoyose.process_identity._BOOT_ID", boot),
        patch.object(Path, "read_text", autospec=True) as read_text,
        patch.object(Path, "stat", autospec=True) as stat,
    ):
        read_text.side_effect = lambda self, **kwargs: (
            f"123 (worker ) with spaces) {remainder}\n"
            if str(self) == "/proc/123/stat" else real_read_text(self, **kwargs)
        )
        stat.side_effect = lambda self: (
            os.stat_result((0, 0, 0, 0, 1000, 0, 0, 0, 0, 0))
            if str(self) == "/proc/123" else real_stat(self)
        )
        identity = capture_process_identity(123)
    assert identity == ProcessIdentity(pid=123, start_ticks=22, boot_id="boot-test", uid=1000)


@pytest.mark.asyncio
async def test_live_child_is_stopped_and_confirmed_when_pidfd_is_available():
    """live child は identity 再照合後 TERM で停止し、終了を pidfd で確認する。"""
    if any(item is None for item in _pidfd_backend()):
        pytest.skip("this runtime has no pidfd backend")
    child = _child("import time; time.sleep(30)")
    try:
        identity = capture_process_identity(child.pid)
        assert await stop_identified_process(identity, 1.0) == "confirmed"
        child.wait(timeout=2)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


@pytest.mark.asyncio
async def test_already_exited_process_is_confirmed_without_signal():
    """保存済み PID が消滅していれば kill せず終了確認とする。"""
    child = _child("import time; time.sleep(0.02)")
    identity = capture_process_identity(child.pid)
    child.wait(timeout=2)
    assert await stop_identified_process(identity, 0.05) == "confirmed"


@pytest.mark.asyncio
async def test_forged_starttime_does_not_signal_live_pid():
    """starttime の違う同一 PID は再利用とみなし、別 process を kill しない。"""
    child = _child("import time; time.sleep(30)")
    try:
        identity = capture_process_identity(child.pid)
        forged = replace(identity, start_ticks=identity.start_ticks + 1)
        with patch("kotoyose.process_identity.os.kill", side_effect=AssertionError("os.kill is forbidden")):
            assert await stop_identified_process(forged, 0.05) == "confirmed"
        assert child.poll() is None
    finally:
        child.kill()
        child.wait()


@pytest.mark.asyncio
async def test_boot_mismatch_does_not_signal_live_pid():
    """boot ID が違う保存 identity は別 boot のものとして停止対象にしない。"""
    child = _child("import time; time.sleep(30)")
    try:
        identity = capture_process_identity(child.pid)
        with patch("kotoyose.process_identity.os.kill", side_effect=AssertionError("os.kill is forbidden")):
            assert await stop_identified_process(replace(identity, boot_id="other-boot"), 0.05) == "confirmed"
        assert child.poll() is None
    finally:
        child.kill()
        child.wait()


@pytest.mark.asyncio
async def test_uid_mismatch_is_unconfirmed_without_signal():
    """UID が caller の現在 UID と一致しない identity は停止確認不能とする。"""
    child = _child("import time; time.sleep(30)")
    try:
        identity = capture_process_identity(child.pid)
        with patch("kotoyose.process_identity.os.kill", side_effect=AssertionError("os.kill is forbidden")):
            assert await stop_identified_process(replace(identity, uid=identity.uid + 1), 0.05) == "unconfirmed"
        assert child.poll() is None
    finally:
        child.kill()
        child.wait()


@pytest.mark.asyncio
async def test_pidfd_permission_failure_is_unconfirmed_without_kill():
    """pidfd_open が権限不足なら危険な kill fallback をせず unconfirmed とする。"""
    child = _child("import time; time.sleep(30)")
    try:
        identity = capture_process_identity(child.pid)
        with patch(
            "kotoyose.process_identity._pidfd_backend",
            return_value=(_permission_denied, lambda _fd, _sig: None),
        ):
            assert await stop_identified_process(identity, 0.05) == "unconfirmed"
        assert child.poll() is None
    finally:
        child.kill()
        child.wait()


@pytest.mark.asyncio
async def test_pidfd_api_absence_is_unconfirmed_without_kill():
    """pidfd API 非対応 runtime は unsupported として unconfirmed を返す。"""
    child = _child("import time; time.sleep(30)")
    try:
        identity = capture_process_identity(child.pid)
        with patch("kotoyose.process_identity._pidfd_backend", return_value=(None, None)):
            assert await stop_identified_process(identity, 0.05) == "unconfirmed"
        assert child.poll() is None
    finally:
        child.kill()
        child.wait()


@pytest.mark.asyncio
async def test_sigterm_ignored_child_is_killed_with_bounded_recovery():
    """SIGTERM 無視時は grace 後に pidfd SIGKILL を送り、bounded に停止確認する。"""
    if any(item is None for item in _pidfd_backend()):
        pytest.skip("this runtime has no pidfd backend")
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "print('ready', flush=True); time.sleep(30)",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "ready"
        identity = capture_process_identity(child.pid)
        assert await asyncio.wait_for(stop_identified_process(identity, 0.05), timeout=2) == "confirmed"
        child.wait(timeout=2)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


def test_capture_errors_do_not_include_proc_contents_or_credentials(monkeypatch):
    """procfs parse error は安全な定型文だけを返し、credential 値を漏らさない。"""
    with patch("kotoyose.process_identity._read_identity", side_effect=ValueError("provider-secret")):
        with pytest.raises(ValueError) as error:
            capture_process_identity(123)
    assert "provider-secret" not in str(error.value)
