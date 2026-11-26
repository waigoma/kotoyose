"""Shared IPC fixtures and Python runner bootstrap checks."""

import asyncio
import base64
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from kotoyose.child_identity import parse_policy_text
from kotoyose.child_protocol import current_package_version, decode_frame, encode_frame
from kotoyose.child_runner import MAX_RECORD, pack_record, read_record

FIXTURES = Path(__file__).parent / "fixtures" / "child_frames"


def _cases(name):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", _cases("frames"), ids=lambda case: case["name"])
def test_shared_frame(case):
    body = case["raw"].replace("~repeat~", "x" * case["repeat"]).encode("utf-8")
    if not case["python"]:
        with pytest.raises(ValueError):
            decode_frame(body)
        return
    frame = decode_frame(body)
    value = json.loads(body)
    assert (frame.stream, frame.kind, frame.payload) == (value["stream"], value["kind"], value.get("payload", {}))


class Fragmented(io.BytesIO):
    def read(self, size=-1):
        return super().read(min(size, 137 if len(self.getbuffer()) > 100 else 1))


@pytest.mark.parametrize("case", _cases("records"), ids=lambda case: case["name"])
def test_shared_stdio_record(case):
    raw = base64.b64decode(case["bytes"]) + b" " * case["spaces"]
    pipe = Fragmented(raw)
    outcome = case["outcome"]
    if outcome == "error":
        with pytest.raises(ValueError):
            read_record(pipe)
        return
    body = read_record(pipe)
    if outcome == "eof":
        assert body is None
    elif outcome == "invalid_json":
        with pytest.raises(ValueError):
            decode_frame(body)
    else:
        assert decode_frame(body).payload == {"text": "応答"}
        assert pack_record(body) == raw
        assert read_record(pipe) is None


@pytest.mark.parametrize(("raw", "expected"), _cases("policy"))
def test_shared_policy(raw, expected):
    if expected is None:
        with pytest.raises(json.JSONDecodeError):
            parse_policy_text(raw)
    else:
        assert parse_policy_text(raw)["workspace_roots"] == [expected]


def test_runner_size_cap_matches_websockets():
    from websockets.asyncio.client import connect
    from websockets.asyncio.server import serve
    from inspect import signature

    assert signature(connect).parameters["max_size"].default == MAX_RECORD
    assert signature(serve).parameters["max_size"].default == MAX_RECORD
    with pytest.raises(ValueError):
        pack_record(b"x" * (MAX_RECORD + 1))


@pytest.mark.parametrize("with_claude", [False, True])
def test_runner_imports_with_only_the_installer_adapters(tmp_path, with_claude):
    probe = """
import importlib.abc
import io
from pathlib import Path
import sys

missing = {'openai_codex', 'cursor_sdk'}
if sys.argv[1] == 'False':
    missing.add('claude_agent_sdk')

class BlockUninstalledDrivers(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.partition('.')[0] in missing:
            raise ModuleNotFoundError(fullname, name=fullname)

sys.meta_path.insert(0, BlockUninstalledDrivers())
from kotoyose.child_runner import Runner
Runner(Path.cwd(), io.BytesIO(), 0)
if sys.argv[1] == 'True':
    from kotoyose.session import _build_claude_runtime_session
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", probe, str(with_claude)],
        cwd=tmp_path, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("mode", ["shutdown", "eof", "bad_version", "invalid_start"])
async def test_runner_entry_lifecycle(tmp_path, mode):
    version = current_package_version()
    init = {
        "ipc": 1, "version": "not-current" if mode == "bad_version" else version, "runner_idle_seconds": 0,
    }
    packet = pack_record(encode_frame(0, "runner_init", init).encode())
    if mode == "shutdown":
        packet += pack_record(encode_frame(0, "runner_shutdown").encode())
    if mode == "invalid_start":
        packet += pack_record(encode_frame(1, "quota", {"command": ["x" * 129]}).encode())
    host_config = tmp_path / "host-settings"
    host_config.write_text("invalid: [")
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-I", "-m", "kotoyose.child_runner", "--home", str(tmp_path),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "KOTOYOSE_CONFIG": str(host_config)},
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(packet), 8)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    records = io.BytesIO(stdout)
    frames = [decode_frame(raw) for raw in iter(lambda: read_record(records), None)]
    if mode == "bad_version":
        assert proc.returncode == 1 and not frames
        assert b"runner stopped" in stderr
        return
    assert proc.returncode == 0, stderr.decode()
    assert (frames[0].stream, frames[0].kind, frames[0].payload) == (
        0, "runner_ready", {"ipc": 1, "version": version},
    )
    expected = {
        "eof": [], "shutdown": ["runner_stopped"], "invalid_start": ["quota_error", "runner_stream_done"],
    }
    assert [frame.kind for frame in frames[1:]] == expected[mode]


def test_first_acp_turn_can_build_with_the_base_runner_dependencies(tmp_path):
    script = """
import asyncio
import sys
from pathlib import Path
sys.modules["claude_agent_sdk"] = None
sys.modules["openai_codex"] = None

from kotoyose.child_turn import _build_runtime
from kotoyose.config import HarnessConfig
from kotoyose.runtime import acp

root = Path(sys.argv[1])
acp.resolve_print_cli_binary = lambda *_: root / "fake"
config = HarnessConfig.model_validate({
    "connections": {"c": {
        "kind": "subscription", "provider": "grok", "profile": "grok",
        "config_dir": str(root), "data_policy": "no_training",
    }},
    "routes": {"r": {"driver": "cli.acp", "connection": "c", "profile": "grok", "model": "fake-model"}},
    "models": {"acp": {"route": "r"}},
})

async def main():
    runtime = await _build_runtime({"alias": "acp", "config": config, "cwd": str(root)})
    assert isinstance(runtime, acp.AcpRuntimeSession)
    assert "kotoyose.session" not in sys.modules
    await runtime.disconnect()

asyncio.run(main())
"""
    result = subprocess.run([sys.executable, "-I", "-c", script, str(tmp_path)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


async def test_system_claude_cli_choice_reaches_the_sdk(monkeypatch, tmp_path):
    import json
    from pathlib import Path

    from kotoyose import claude_cli, session
    from kotoyose.child_client import available_adapters
    from kotoyose.child_components import probe
    from kotoyose.child_turn import _build_runtime, _prepare

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    cli = tmp_path / "claude"
    cli.write_text('#!/bin/sh\n[ "$*" = "--version" ] || exit 2\nprintf "Claude fixture 1.2\\n"\n')
    cli.chmod(0o700)
    (tmp_path / "policy.json").write_text('{"adapters":["claude.agent-sdk"],"workspace_roots":[],"mcp":{}}')
    (tmp_path / "app.json").write_text('{"claude_cli":"system"}')
    monkeypatch.setattr(claude_cli, "bundled_claude_cli", lambda: pytest.fail("Must use the system CLI"))
    choices = []

    class Client:
        def __init__(self, *, options):
            choices.append(Path(options.cli_path))

        async def connect(self):
            pass

    monkeypatch.setattr(session, "ClaudeSDKClient", Client)
    monkeypatch.delitem(session.RUNTIME_PROVIDER_REGISTRY, "claude", raising=False)
    prepared = _prepare(tmp_path, {
        "alias": "test", "route": {"driver": "claude.agent-sdk", "model": "claude-sonnet-4-5"},
        "connection": {"provider": "claude", "kind": "subscription", "data_policy": "no_training"},
    })
    try:
        await _build_runtime(prepared)
    finally:
        prepared["cleanup"]()
    assert choices == [cli]
    assert claude_cli.claude_cli_override.get() is None
    monkeypatch.setattr(
        "kotoyose.child_client.sdk_importable",
        lambda row: row["driver"] == "claude.agent-sdk",
    )
    assert available_adapters(["claude.agent-sdk", "codex.app-server"], tmp_path) == ["claude.agent-sdk"]
    rows = {row["id"]: row for row in probe(tmp_path, versions=True)}
    assert rows["claude"]["system_version"] == "Claude fixture 1.2"
    assert rows["codex"]["turns_supported"]
    cli.unlink()
    assert available_adapters(["claude.agent-sdk"], tmp_path) == []
