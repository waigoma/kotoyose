"""Antigravity CLI の version 診断境界を検証する。"""

from __future__ import annotations

import asyncio

import pytest

from kotoyose.runtime.antigravity_probe import (
    MIN_ANTIGRAVITY_CLI_VERSION,
    probe_antigravity_cli,
)


class _ProbeProcess:
    """version probe に必要な subprocess 契約だけを提供する。"""

    def __init__(self, stdout: bytes, stderr: bytes = b"", returncode: int = 0):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode


@pytest.mark.parametrize(
    ("version", "compatible"),
    [
        (b"agy 1.1.23\n", False),
        (b"agy 1.1.24\n", True),
        (b"agy 1.1.25\n", True),
    ],
)
async def test_probe_enforces_minimum_version_boundary(
    monkeypatch, version: bytes, compatible: bool
):
    """既知の pipe 修正を含む 1.1.24 を互換性の境界とする。"""
    monkeypatch.setattr(
        "kotoyose.runtime.antigravity_probe.shutil.which", lambda _: "/usr/bin/agy"
    )

    async def fake_create(*args, **kwargs):
        return _ProbeProcess(version)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    result = await probe_antigravity_cli()

    assert MIN_ANTIGRAVITY_CLI_VERSION == (1, 1, 24)
    assert result.executable == "/usr/bin/agy"
    assert result.detected_version == tuple(
        int(part) for part in version.decode().split()[1].split(".")
    )
    assert result.compatible is compatible
    assert result.error_code == (None if compatible else "version_too_old")


async def test_probe_reports_missing_command(monkeypatch):
    """CLI が未導入なら例外ではなく構造化診断を返す。"""
    monkeypatch.setattr("kotoyose.runtime.antigravity_probe.shutil.which", lambda _: None)

    result = await probe_antigravity_cli()

    assert result.executable is None
    assert result.detected_version is None
    assert result.compatible is False
    assert result.error_code == "command_not_found"


async def test_probe_rejects_nonzero_and_invalid_utf8_output(monkeypatch):
    """秘密を含み得る stderr は返さず、失敗種別だけを公開する。"""
    monkeypatch.setattr(
        "kotoyose.runtime.antigravity_probe.shutil.which", lambda _: "/usr/bin/agy"
    )

    async def fake_create(*args, **kwargs):
        return _ProbeProcess(b"agy \xff\n", b"secret-token", returncode=2)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    result = await probe_antigravity_cli()

    assert result.detected_version is None
    assert result.compatible is False
    assert result.error_code == "nonzero_exit"
    assert "secret-token" not in str(result)


async def test_probe_reports_unknown_version(monkeypatch):
    """正常終了でも version を解析できなければ互換とは判定しない。"""
    monkeypatch.setattr(
        "kotoyose.runtime.antigravity_probe.shutil.which", lambda _: "/usr/bin/agy"
    )

    async def fake_create(*args, **kwargs):
        return _ProbeProcess(b"agy development build\n")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    result = await probe_antigravity_cli()

    assert result.compatible is False
    assert result.error_code == "version_unknown"


async def test_probe_kills_process_on_timeout(monkeypatch):
    """応答しない version probe を停止し、管理 API に timeout を返す。"""
    monkeypatch.setattr(
        "kotoyose.runtime.antigravity_probe.shutil.which", lambda _: "/usr/bin/agy"
    )
    monkeypatch.setattr(
        "kotoyose.runtime.antigravity_probe._VERSION_PROBE_TIMEOUT_SECONDS", 0.001
    )
    process = _ProbeProcess(b"")

    async def communicate_forever():
        await asyncio.Event().wait()
        return b"", b""

    process.communicate = communicate_forever

    async def fake_create(*args, **kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    result = await probe_antigravity_cli()

    assert process.killed is True
    assert result.compatible is False
    assert result.error_code == "timeout"


async def test_probe_reaps_process_when_caller_is_cancelled(monkeypatch):
    """Client 切断で診断が中断されても child process を残さない。"""
    monkeypatch.setattr(
        "kotoyose.runtime.antigravity_probe.shutil.which", lambda _: "/usr/bin/agy"
    )
    process = _ProbeProcess(b"")

    async def communicate_forever():
        await asyncio.Event().wait()
        return b"", b""

    process.communicate = communicate_forever

    async def fake_create(*args, **kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    task = asyncio.create_task(probe_antigravity_cli())
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.killed is True
