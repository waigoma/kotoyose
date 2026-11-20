import asyncio
import hashlib
import os
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
import structlog
from httpx import ASGITransport, AsyncClient

from tests.fixtures.api_keys import TEST_API_HEADERS

from kotoyose import activity, agent_interactions, interactions
from kotoyose.app import app
from kotoyose.artifact_file import ArtifactFile
from kotoyose.compatibility_execution import CompatibilityExecution
from kotoyose.compatibility_http import _stream as native_chat_stream
from kotoyose.config import HarnessConfig
from kotoyose.credential_leak_guard import Finding, find_credential_shapes
from kotoyose.execution_agent import _ProgressWriter
from kotoyose.execution_artifacts import CapturedArtifact
from kotoyose.execution_output_guard import check_artifacts, check_workspace, workspace_state
from kotoyose.execution_resources import ExecutionError
from kotoyose.execution_service import ExecutionService
from kotoyose.execution_store import ExecutionStore
from kotoyose.native_claude import NativeAgentResult
from kotoyose.runtime.base import RuntimeEvent


def fake_key():
    return "sk-proj-" + "FakeOnly0_" * 6


@pytest.mark.parametrize(
    ("kind", "value"),
    [
        ("jwt", "eyJ" + "FAKE" * 6 + ".eyJ" + "Claims" * 4 + "." + "Signature" * 6),
        ("anthropic_oauth_access", "sk-ant-oat01-" + "FakeOnly0_" * 6),
        ("anthropic_oauth_refresh", "sk-ant-ort01-" + "FakeOnly0_" * 6),
        ("anthropic_api_key", "sk-ant-api03-" + "FakeOnly0_" * 6),
        ("openai_project_key", fake_key()),
        ("openai_api_key", "sk-" + "FakeOnly0" * 6),
        ("google_oauth_access", "ya29." + "FakeOnly0_" * 6),
        ("google_oauth_refresh", "1//0" + "FakeOnly0_" * 6),
        ("cursor_api_key", "crsr_" + "FakeOnly0_" * 6),
        ("xai_api_key", "xai-" + "FakeOnly0_" * 6),
        ("pem_private_key", "-----\x42EGIN OPENSSH PRIVATE KEY-----"),
        ("pem_private_key", "-----\x42EGIN PRIVATE KEY-----"),
    ],
)
def test_shapes_return_only_kind_and_character_offsets(kind, value):
    assert find_credential_shapes(f"値: {value}!") == [Finding(kind, 3, 3 + len(value))]


@pytest.mark.parametrize("text", [
    hashlib.sha256(b"example").hexdigest(),
    "task-" + hashlib.sha256(b"example").hexdigest(),
    str(UUID(int=1)),
    "eyJ0eXAiOiJKV1QifQ.eyJ9.U2hvcnQ",
    "key = 'sk-proj-example'; token = 'ya29.fake'",
    "-----\x42EGIN PUBLIC KEY-----",
    "def configure_task(work_queue: list[str]) -> None: ...",
])
def test_ordinary_text_is_not_a_credential(text):
    assert find_credential_shapes(text) == []


@pytest.fixture
def ledger(monkeypatch):
    pool = SimpleNamespace(fetchrow=AsyncMock(return_value={"id": 42}), execute=AsyncMock())
    monkeypatch.setattr(interactions, "_get_pool", lambda: pool)
    return pool


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.asyncio
@pytest.mark.usefixtures("configured_chat_alias")
@pytest.mark.usefixtures("client_api_key", "configured_chat_alias")
async def test_chat_blocks_output_and_redacts_activity(monkeypatch, ledger, stream):
    value = fake_key()
    pieces = [value[:5], value[5:25], value[25:]]
    runtime_closed = asyncio.Event()

    async def ask(client, prompt):
        activity.emit_runtime_event(RuntimeEvent(kind="tool_result", summary=value, payload={"key": value}))
        return value, None, None

    async def stream_runtime(client, prompt):
        try:
            yield RuntimeEvent(kind="tool_result", summary=value, payload={"key": value})
            for piece in pieces:
                yield piece
            pytest.fail("runtime was pulled after detecting a credential")
        finally:
            runtime_closed.set()

    monkeypatch.setattr("kotoyose.auth.API_KEY", None)
    monkeypatch.setattr("kotoyose.routes.chat.get_or_resume_client", AsyncMock(return_value=(AsyncMock(), False)))
    monkeypatch.setattr("kotoyose.routes.chat.ask_claude", ask)
    monkeypatch.setattr("kotoyose.routes.chat.stream_claude", stream_runtime)
    with structlog.testing.capture_logs() as logs:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=TEST_API_HEADERS) as client:
            response = await client.post("/v1/chat/completions", json={
                "model": "kotoyose-chat", "messages": [{"role": "user", "content": "hello"}], "stream": stream,
            })
        await asyncio.gather(*activity._event_writer_tasks)
    assert response.status_code == (200 if stream else 502)
    assert "credential_leak_suspected" in response.text
    assert value not in response.text
    assert all(piece not in response.text for piece in pieces)
    assert value not in repr(logs)
    if stream:
        assert runtime_closed.is_set()
        assert '"finish_reason":"stop"' not in response.text
    writes = ledger.execute.await_args_list
    assert any("status = 'failed'" in call.args[0] for call in writes)
    assert all("status = 'completed'" not in call.args[0] for call in writes)
    assert any("INSERT INTO kotoyose.interaction_events" in call.args[0] for call in writes)
    assert "[redacted: suspected credential]" in repr(writes)
    assert value not in repr(writes)


@pytest.mark.parametrize("surface", ["answer", "artifact", "workspace", "events", "disabled"])
@pytest.mark.asyncio
async def test_jail_output_is_checked_before_persistence(tmp_path, monkeypatch, ledger, surface):
    value = fake_key()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    identity = workspace.stat()
    snapshot = {
        "kind": "agent", "backend": "print_cli", "execution_environment": "isolated",
        "model": "fake", "surface": "write", "workspace_path": str(workspace),
        "workspace_device": identity.st_dev, "workspace_inode": identity.st_ino,
        "deadline": (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat(),
        "timeout": {"soft_seconds": 60}, "budget": {},
    }
    if surface == "artifact":
        snapshot["surface"] = "read"
        snapshot["artifact_spec"] = {
            "kind": "artifact_spec", "schema_version": "1", "max_total_bytes": 1024,
            "entries": [{"name": "evidence", "path": "evidence.txt", "media_type": "text/plain",
                         "max_bytes": 1024, "required": True}],
        }
    attempt = {"id": "attempt", "principal": "test", "generation": 0,
               "request": {"execution_grant_ref": "grant"}, "snapshot": snapshot}
    row = dict(
        attempt, finished_at=None, limits_state=None, status="running", workspace_id="workspace",
        request=json.dumps(dict(attempt["request"], workspace_ref="workspace:workspace:0")),
    )
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=row)
    conn.execute = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchval = AsyncMock(side_effect=[1, False, 2])
    pool = MagicMock()
    pool.acquire.return_value.__aenter__.return_value = conn
    pool.fetchrow = AsyncMock(return_value=row)
    store = ExecutionStore(pool)
    store.resource = AsyncMock()
    store.mark_running = AsyncMock(return_value=True)
    store.operations = AsyncMock(return_value=[])
    store.client_tools.cancel_attempt = AsyncMock()
    store.mcp_calls.cancel_in_transaction = AsyncMock()

    async def execute(attempt, cancel, *, worker, interaction, runtime_entry):
        runtime_entry.entered = True
        event = RuntimeEvent(kind="tool_result", summary=value, payload={"nested": {"output": value}})
        progress = _ProgressWriter(store, attempt["id"])
        progress.emit(event)
        interaction.emit(event)
        await progress.finish()
        if surface in {"artifact", "workspace"}:
            (workspace / "evidence.txt").write_text(value)
        output = value if surface in {"answer", "disabled"} else "ordinary answer"
        return NativeAgentResult("succeeded", "completed", output.encode(), "confirmed", {},
                                 launch_outcome="started")

    service = object.__new__(ExecutionService)
    service._accepting = True
    service.agent_executor = SimpleNamespace(execute=execute)
    service.store = store
    service.config = SimpleNamespace(artifact_disk_limits=None)
    if surface == "disabled":
        monkeypatch.setattr("kotoyose.config._config", HarnessConfig(credential_leak_guard=False))

    with structlog.testing.capture_logs() as logs:
        await service._execute(attempt, asyncio.Event())
        await asyncio.gather(*agent_interactions._tasks)
        await asyncio.gather(*activity._event_writer_tasks)

    writes = [call.args for call in conn.execute.await_args_list]
    finish = next(args for args in writes if "SET status=$2,reason=$3" in args[0])
    artifacts = [args for args in writes if "INSERT INTO kotoyose.execution_artifacts" in args[0]]
    if surface in {"events", "disabled"}:
        assert finish[2:5] == ("succeeded", "completed", "confirmed")
        assert len(artifacts) == 1
        assert artifacts[0][5] == (value if surface == "disabled" else "ordinary answer").encode()
    else:
        assert finish[2:5] == ("failed", "credential_leak_suspected", "confirmed")
        assert artifacts == []
        findings = [log for log in logs if log["event"] == "credential_leak_suspected"]
        assert findings
        assert all(log["counts"] == {"openai_project_key": 1} for log in findings)
        assert findings[0]["path"] == ("output" if surface == "answer" else "evidence.txt")
    if surface != "disabled":
        assert value not in repr((logs, writes, ledger.execute.await_args_list))
        progress = next(args for args in writes if "provider_progress" in args)
        assert "[redacted: suspected credential]" in progress[-1]


def test_workspace_only_reads_changed_text_under_its_root(tmp_path, monkeypatch):
    value = fake_key()
    unchanged = tmp_path / "prior.txt"
    changed = tmp_path / "run.txt"
    ignored = tmp_path / "ignored.txt"
    (tmp_path / ".git").mkdir()
    unchanged.write_text(value)
    changed.write_text("before the run")
    outside = tmp_path.parent / f"{tmp_path.name}-external.txt"
    outside.write_text(value)
    (tmp_path / "linked.txt").symlink_to(outside)

    baseline = workspace_state(tmp_path)
    changed.write_text(value)
    ignored.write_text(value + " " * (1024 * 1024))
    identity = tmp_path.stat()
    with structlog.testing.capture_logs() as logs:
        assert check_workspace(tmp_path, identity.st_dev, identity.st_ino, baseline)
    assert {entry["path"] for entry in logs} == {"run.txt", "ignored.txt"}
    assert value not in repr(logs)


@pytest.mark.parametrize("offset", [0, 30])
def test_streamed_artifact_is_checked_across_read_boundaries(tmp_path, monkeypatch, offset):
    monkeypatch.setattr("kotoyose.artifact_file.CHUNK_BYTES", 32)
    path = tmp_path / "artifact.txt"
    path.write_text(" " * offset + fake_key())
    with path.open("rb") as source:
        blob = ArtifactFile.capture(source.fileno(), os.path.getsize(path))
    try:
        assert check_artifacts({"evidence": CapturedArtifact(blob, "text/plain")}, None)
    finally:
        blob.close()


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.asyncio
async def test_native_chat_does_not_replay_a_previously_saved_credential(stream):
    content = fake_key().encode()
    attempt = {
        "status": "succeeded", "stop_confirmation": "confirmed", "side_effect_resolution": "resolved",
        "finished_at": "2026-01-01T00:00:00+00:00",
        "artifacts": [{"name": "output", "ref": "ref", "digest": hashlib.sha256(content).hexdigest()}],
    }
    store = SimpleNamespace(artifact=AsyncMock(return_value={"content": content}))
    service = SimpleNamespace(describe=AsyncMock(return_value=attempt), store=store)
    adapter = object.__new__(CompatibilityExecution)
    adapter.service = service

    with structlog.testing.capture_logs() as logs:
        if stream:
            response = "".join([part async for part in native_chat_stream(adapter, "caller", "attempt", "model")])
            assert "credential_leak_suspected" in response
            assert fake_key() not in response
        else:
            with pytest.raises(ExecutionError) as failure:
                await adapter.completion("caller", "attempt", "model")
            assert failure.value.code == "credential_leak_suspected"
            assert fake_key() not in str(failure.value)
    assert fake_key() not in repr(logs)


@pytest.mark.asyncio
@pytest.mark.usefixtures("configured_chat_alias")
@pytest.mark.usefixtures("client_api_key", "configured_chat_alias")
async def test_chat_stream_keeps_streaming_with_only_a_bounded_holdback(monkeypatch, ledger):
    # Ordinary long answers must not be buffered until the end (chat clients stream them).
    pieces = [("word " * 400) for _ in range(12)]  # 24 000 chars in 12 deltas

    async def stream_runtime(client, prompt):
        for piece in pieces:
            yield piece

    monkeypatch.setattr("kotoyose.auth.API_KEY", None)
    monkeypatch.setattr("kotoyose.routes.chat.get_or_resume_client", AsyncMock(return_value=(AsyncMock(), False)))
    monkeypatch.setattr("kotoyose.routes.chat.stream_claude", stream_runtime)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=TEST_API_HEADERS) as client:
        response = await client.post("/v1/chat/completions", json={
            "model": "kotoyose-chat", "messages": [{"role": "user", "content": "hello"}], "stream": True,
        })
    contents = [
        json.loads(line[6:])["choices"][0]["delta"].get("content")
        for line in response.text.splitlines()
        if line.startswith("data: {") and '"choices"' in line
    ]
    texts = [text for text in contents if text]
    assert "".join(texts) == "".join(pieces)
    # More than the final flush: text before the held-back tail was released early.
    assert len(texts) > 2
    assert all(len(text) <= 8192 + len(pieces[0]) for text in texts)


def test_workspace_scan_never_runs_git(tmp_path, monkeypatch):
    # The workspace is agent-writable; host-side git would honour its config/attributes.
    import subprocess

    from kotoyose import execution_output_guard

    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text('[filter "x"]\n\tclean = touch pwned\n')
    (tmp_path / "a.txt").write_text("hello")

    def forbidden(*args, **kwargs):
        raise AssertionError("git must not run against the workspace")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    state = execution_output_guard.workspace_state(tmp_path)
    assert "a.txt" in state
    assert not any(path.startswith(".git") for path in state)
