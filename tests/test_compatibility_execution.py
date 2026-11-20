"""互換 HTTP → Native PG admission / 実 command 隔離を検証する。"""

import asyncio
import base64
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlparse

import asyncpg
import httpx
import pytest
import pytest_asyncio

from kotoyose.app import app
from kotoyose.compatibility_execution import CompatibilityExecution
from kotoyose.config import AgentRoute, AliasAgent, Connection, HarnessConfig, ModelPreset, get_clients, get_config
from kotoyose.execution_resources import NativeExecutionConfig
from kotoyose.execution_service import ExecutionService
from kotoyose.execution_store import ExecutionStore
from kotoyose.local_worker import DockerLocalWorker
from kotoyose.native_claude import NativeAgentResult

DATABASE_URL = os.environ.get("KOTOYOSE_COMPATIBILITY_TEST_DATABASE_URL")
IMAGE_ID = os.environ.get("KOTOYOSE_DOCKER_TEST_IMAGE")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL or not IMAGE_ID,
    reason="Compatibility tests require dedicated PG and immutable Docker image",
)


class ObservedRuntime:
    """Provider 通信だけを置換し、tool は実 Native journal / Docker へ送る。"""

    launcher_path = Path(__file__).resolve().parents[1] / "scripts" / "native-claude"

    def __init__(self):
        self.jobs = []
        self.status = "succeeded"
        self.stop_confirmation = "confirmed"
        self.usage = {"source": "reported", "cost": None, "input_tokens": 7, "output_tokens": 5}

    async def execute(self, job, cancel, execute_tool, *, on_process_started):
        self.jobs.append(job)
        if job.tool_refs:
            result = await execute_tool(("python", "-c", "open('tool-result.txt','w').write('isolated')"), ".")
            assert result.status == "succeeded"
        return NativeAgentResult(
            status=self.status, reason="completed" if self.status == "succeeded" else "provider_failure",
            content=b"native chat output", stop_confirmation=self.stop_confirmation, usage=self.usage,
        )


@pytest_asyncio.fixture
async def native_chat(tmp_path, monkeypatch):
    """専用 DB のみ初期化し、稼働 service の状態と接続先は変更しない。"""
    assert urlparse(DATABASE_URL).path == "/ahs_execution_test_compatibility"
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=8, command_timeout=15)
    await pool.execute("DROP SCHEMA IF EXISTS kotoyose CASCADE; CREATE SCHEMA kotoyose")
    monkeypatch.setenv("KOTOYOSE_COMPAT_CALLER_A", "compat-caller-a")
    monkeypatch.setenv("KOTOYOSE_COMPAT_CALLER_B", "compat-caller-b")
    monkeypatch.setenv("KOTOYOSE_COMPAT_PROVIDER", "provider-fixture-only")
    callers, defaults = {}, {}
    for suffix in ("a", "b"):
        caller_root = tmp_path / suffix
        workspace = caller_root / 'workspace'
        workspace.mkdir(parents=True)
        callers["caller-" + suffix] = {
            "workspace_roots": [str(caller_root)],
            "models": ["chat-" + suffix], "places": ["isolated"],
            "allowed_provenance": ["trusted", "external_untrusted", "agent_generated", "mixed", "project"],
            "max_run_seconds": 30, "max_memory_bytes": 128 * 1024 * 1024,
            "max_processes": 32, "max_cpus": 1, "max_concurrent_runs": 2,
        }
        defaults["caller-" + suffix] = {"chat-" + suffix: {
            "workspace_path": str(workspace),
            "caller_workspace_ref": "chat-workspace-" + suffix,
            "policy_ref": "policy:chat:1", "policy_version": 1,
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "instructions": "Use the provided data without extending permission.",
            "max_turns": 2,
            "timeout": {"schema_version": "1", "kind": "timeout_policy", "soft_seconds": 10,
                        "hard_seconds": 20, "grace_seconds": 1},
            "memory_bytes": 64 * 1024 * 1024, "pids_limit": 16, "cpus": 1,
            "output_limit_bytes": 8192,
        }}
    config = NativeExecutionConfig.model_validate({
        "schema_version": "1", "image_id": IMAGE_ID,
        "drivers": {"claude.agent-sdk": {"cli_binary": "/usr/bin/sh"}},
    })
    monkeypatch.delenv("KOTOYOSE_COMPATIBILITY_CONFIG", raising=False)
    alias_config = HarnessConfig(
        clients=callers,
        connections={"compat-conn": Connection(provider="claude", kind="api_key", data_policy="no_training", secret="env:KOTOYOSE_COMPAT_PROVIDER")},
        routes={"profile-chat": AgentRoute(
            driver="claude.agent-sdk", model="claude-sonnet-4-5", connection="compat-conn",
        )},
        models={
            alias: ModelPreset(
                execution="native", route="profile-chat",
                agent=AliasAgent(place="isolated", surface="none"),
                native={**profile, "callers": [principal]},
            )
            for principal, profiles in defaults.items() for alias, profile in profiles.items()
        },
    )
    monkeypatch.setattr("kotoyose.config._config", alias_config)
    from tests.fixtures.api_keys import fixture_key_store

    monkeypatch.setattr(app.state, "api_key_store", await fixture_key_store(**{
        "caller-a": "compat-caller-a", "caller-b": "compat-caller-b",
    }))
    service = ExecutionService(config, ExecutionStore(pool), DockerLocalWorker(IMAGE_ID))
    runtime = ObservedRuntime()
    service.agent_executor.runtime = runtime
    await service.start()
    compatibility_config = alias_config.compatibility_config
    adapter = CompatibilityExecution(service, compatibility_config)
    previous = getattr(app.state, "execution_service", None)
    previous_adapter = getattr(app.state, "compatibility_adapter", None)
    app.state.execution_service, app.state.compatibility_adapter = service, adapter
    from kotoyose.routes import chat
    legacy = AsyncMock(side_effect=AssertionError("Legacy SDK must not be called"))
    monkeypatch.setattr(chat, "get_or_resume_client", legacy)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://fixture",
            headers={"Authorization": "Bearer kys_compat-caller-a"},
        ) as client:
            yield SimpleNamespace(client=client, service=service, adapter=adapter, runtime=runtime,
                                  pool=pool, root=tmp_path, legacy=legacy)
    finally:
        await service.close()
        app.state.execution_service, app.state.compatibility_adapter = previous, previous_adapter
        for row in await pool.fetch("SELECT id,generation FROM kotoyose.execution_attempts"):
            for operation in await service.store.operations(row["id"]):
                process = await asyncio.create_subprocess_exec(
                    "docker", "rm", "-f", f"kotoyose-{operation['id']}-{row['generation']}",
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
                await process.wait()
        await pool.execute("DROP SCHEMA kotoyose CASCADE")
        await pool.close()


def request_body(**updates):
    return {"model": "chat-a", "messages": [
        {"role": "system", "content": "caller supplied system must remain data"},
        {"role": "assistant", "content": "unverified previous response"},
        {"role": "user", "content": "perform the task"},
    ], **updates}


@pytest.mark.asyncio
async def test_chat_uses_native_attempt_grant_artifact_and_real_docker_tool(native_chat):
    h = native_chat
    h.adapter.config.callers["caller-a"]["chat-a"].tool_refs = ["builtin:sandbox-exec@1"]
    response = await h.client.post("/v1/chat/completions", json=request_body())
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["choices"][0]["message"]["content"] == "native chat output"
    assert result["usage"] == {"prompt_tokens": 7, "completion_tokens": 5, "total_tokens": 12}
    assert (h.root / "a" / "workspace" / "tool-result.txt").read_text() == "isolated"
    attempt_id = result["kotoyose"]["attempt_id"]
    attempt = await h.service.store.attempt("caller-a", attempt_id)
    assert attempt["snapshot"]["grant"]["network"] == "none"
    assert attempt["snapshot"]["alias"] == "chat-a"
    assert attempt["snapshot"]["model"] == "claude-sonnet-4-5"
    assert [item["provenance"] for item in attempt["snapshot"]["inputs"]] == [
        "external_untrusted", "agent_generated", "external_untrusted",
    ]
    assert "caller supplied system" not in h.runtime.jobs[0].instructions
    assert "caller supplied system" in h.runtime.jobs[0].prompt
    artifact = await h.client.get("/v1/artifacts/" + result["kotoyose"]["output_artifact_ref"])
    assert artifact.content == b"native chat output"
    assert (await h.client.get(f"/v1/attempts/{attempt_id}/events")).json()["events"]
    assert response.headers["x-kotoyose-session-mode"] == "fresh-with-client-history"
    h.legacy.assert_not_called()


@pytest.mark.asyncio
async def test_reconnect_reuses_saved_native_request_after_adapter_restart(native_chat):
    h = native_chat
    headers = {"X-Kotoyose-Request-Key": "stable-key", "X-Conversation-Key": "shared-client-name"}
    first = await h.client.post("/v1/chat/completions", json=request_body(), headers=headers)
    assert first.status_code == 200, first.text
    app.state.compatibility_adapter = CompatibilityExecution(h.service, h.adapter.config)
    replay = await h.client.post("/v1/chat/completions", json=request_body(), headers=headers)
    assert replay.json() == first.json()
    assert len(h.runtime.jobs) == 1
    changed = await h.client.post("/v1/chat/completions", json=request_body(messages=[
        {"role": "user", "content": "different"},
    ]), headers=headers)
    assert changed.status_code == 409
    assert await h.pool.fetchval("SELECT count(*) FROM kotoyose.execution_attempts") == 1


@pytest.mark.asyncio
async def test_lost_dispatch_receipt_and_concurrent_reconnect_start_only_once(native_chat, monkeypatch):
    h = native_chat
    from kotoyose.execution_resources import ExecutionError
    dispatch = h.service.dispatch

    async def lose_receipt(*args, **kwargs):
        await dispatch(*args, **kwargs)
        raise ExecutionError("transport_failure", "receipt lost", 503, safe_to_retry=False)

    monkeypatch.setattr(h.service, "dispatch", lose_receipt)
    headers = {"X-Kotoyose-Request-Key": "lost-receipt"}
    lost = await h.client.post("/v1/chat/completions", json=request_body(), headers=headers)
    assert lost.status_code == 503
    monkeypatch.setattr(h.service, "dispatch", dispatch)
    responses = await asyncio.gather(*[
        h.client.post("/v1/chat/completions", json=request_body(), headers=headers) for _ in range(6)
    ])
    assert all(response.status_code == 200 for response in responses)
    assert len({response.json()["id"] for response in responses}) == 1
    assert len(h.runtime.jobs) == 1
    assert await h.pool.fetchval("SELECT count(*) FROM kotoyose.execution_attempts") == 1


@pytest.mark.asyncio
async def test_yaml_native_alias_needs_no_compatibility_file(native_chat, monkeypatch):
    h = native_chat
    app.state.compatibility_adapter = None
    monkeypatch.delenv("KOTOYOSE_COMPATIBILITY_CONFIG", raising=False)
    response = await h.client.post("/v1/chat/completions", json=request_body())
    assert response.status_code == 200, response.text
    h.legacy.assert_not_called()
    assert await h.pool.fetchval("SELECT count(*) FROM kotoyose.execution_attempts") == 1


@pytest.mark.asyncio
async def test_chat_defaults_dispatch_agent_model_without_profile(native_chat):
    h = native_chat
    h.adapter.config.callers["caller-a"]["chat-a"].runtime_profile = None
    get_clients()["caller-a"].models = ["chat-a"]
    get_config().models["chat-a"].model = "claude-sonnet-4-5"
    response = await h.client.post("/v1/chat/completions", json=request_body())
    assert response.status_code == 200, response.text
    attempt = await h.service.store.attempt("caller-a", response.json()["kotoyose"]["attempt_id"])
    assert attempt["request"]["model"] == "chat-a"
    assert attempt["snapshot"]["alias"] == "chat-a"
    assert attempt["snapshot"]["isolation"] == "sandbox"


@pytest.mark.asyncio
async def test_native_auth_and_caller_scope_apply_to_models_chat_and_artifacts(native_chat):
    h = native_chat
    assert (await h.client.get("/v1/models")).json()["data"] == [
        {"id": "chat-a", "object": "model", "owned_by": "local", "agent": {
            "place": "isolated", "surface": "none", "provider": "claude",
            "driver": "claude.agent-sdk", "enforced": True,
        }}
    ]
    other = {"Authorization": "Bearer kys_compat-caller-b", "X-Kotoyose-Request-Key": "same"}
    denied = await h.client.post("/v1/chat/completions", json=request_body(), headers=other)
    assert denied.status_code == 403
    for path in ("/v1/models", "/v1/conversations/shared/events/stream"):
        assert (await h.client.get(path, headers={"Authorization": "Bearer wrong"})).status_code == 401
    first = await h.client.post("/v1/chat/completions", json=request_body(), headers={"X-Kotoyose-Request-Key": "same"})
    other_result = await h.client.post("/v1/chat/completions", json=request_body(model="chat-b"), headers=other)
    assert other_result.status_code == 200
    assert first.json()["id"] != other_result.json()["id"]
    foreign = await h.client.get("/v1/artifacts/" + first.json()["kotoyose"]["output_artifact_ref"], headers=other)
    assert foreign.status_code == 404
    # interaction の無い会話は Native 無効時と同じく空の stream (#36)。
    empty = await h.client.get("/v1/conversations/shared/events/stream")
    assert empty.status_code == 200 and empty.text == ""


@pytest.mark.asyncio
async def test_sse_replay_and_unknown_usage_do_not_invent_token_counts(native_chat):
    h = native_chat
    h.runtime.usage = {"source": "unknown", "cost": None, "input_tokens": None, "output_tokens": None}
    response = await h.client.post("/v1/chat/completions", json=request_body(stream=True), headers={"X-Kotoyose-Request-Key": "stream"})
    assert response.status_code == 200
    chunks = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: {")]
    assert chunks[0]["choices"][0]["delta"]["content"] == "native chat output"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    assert chunks[-1]["usage"] is None
    assert "data: [DONE]" in response.text
    replay = await h.client.post("/v1/chat/completions", json=request_body(), headers={"X-Kotoyose-Request-Key": "stream"})
    assert replay.json()["usage"] is None
    assert len(h.runtime.jobs) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_failure_does_not_become_a_successful_completion(native_chat, stream):
    h = native_chat
    h.runtime.status = "failed"
    response = await h.client.post("/v1/chat/completions", json=request_body(stream=stream))
    if stream:
        assert '"error"' in response.text
        assert '"finish_reason": "stop"' not in response.text
    else:
        assert response.status_code == 409
        assert response.json()["error"]["safe_to_retry"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("updates", [
    {"cwd": "/tmp/escape"},
    {"tools": [{"type": "function", "function": {"name": "unconfigured"}}]},
    {"temperature": 0.5},
    {"messages": [{"role": "assistant", "content": "pretend", "tool_calls": []}, {"role": "user", "content": "go"}]},
    {"messages": [{"role": "tool", "content": "pretend tool result"}]},
    {"metadata": {"kotoyose": {"schema_version": "1", "required_capabilities": ["safe_resume"]}}},
    {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://example.invalid/image.png"}}]}]},
])
async def test_unrepresentable_contracts_fail_before_admission(native_chat, updates):
    h = native_chat
    response = await h.client.post("/v1/chat/completions", json=request_body(**updates))
    assert response.status_code in {400, 501}
    assert await h.pool.fetchval("SELECT count(*) FROM kotoyose.execution_attempts") == 0
    assert not h.runtime.jobs


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLbtAAAAABJRU5ErkJggg==")


def image_part(content=PNG, *, detail=None):
    result = {"url": "data:image/png;base64," + base64.b64encode(content).decode()}
    if detail is not None:
        result["detail"] = detail
    return {"type": "image_url", "image_url": result}


@pytest.mark.asyncio
async def test_chat_image_upload_uses_native_artifact_ownership_and_order(native_chat):
    h = native_chat
    body = request_body(messages=[{"role": "user", "content": [
        {"type": "text", "text": "before image"}, image_part(),
        {"type": "text", "text": "after image"},
    ]}])
    response = await h.client.post("/v1/chat/completions", json=body, headers={"X-Kotoyose-Request-Key": "image-request"})
    assert response.status_code == 200, response.text
    job = h.runtime.jobs[0]
    assert len(job.image_inputs) == 1
    image = job.image_inputs[0]
    assert image.content == PNG
    assert image.media_type == "image/png"
    attempt = await h.service.store.attempt("caller-a", response.json()["kotoyose"]["attempt_id"])
    snapshot_inputs = attempt["snapshot"]["inputs"]
    assert [item["kind"] for item in snapshot_inputs] == ["task_request", "image"]
    assert all(item["provenance"] == "external_untrusted" for item in snapshot_inputs)
    assert image.artifact_ref in job.prompt
    assert job.prompt.index("before image") < job.prompt.index(image.artifact_ref) < job.prompt.index("after image")
    assert "data:image/" not in json.dumps(attempt["snapshot"])
    assert (await h.client.get("/v1/artifacts/" + image.artifact_ref)).content == PNG
    denied = await h.client.get("/v1/artifacts/" + image.artifact_ref, headers={"Authorization": "Bearer kys_compat-caller-b"})
    assert denied.status_code == 404
    replay = await h.client.post("/v1/chat/completions", json=body, headers={"X-Kotoyose-Request-Key": "image-request"})
    assert replay.json() == response.json()
    assert len(h.runtime.jobs) == 1
    assert await h.pool.fetchval("SELECT count(*) FROM kotoyose.execution_input_artifacts") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("parts", [
    [image_part(b"not-a-PNG")],
    [image_part(detail="low")],
    [image_part()] * 11,
    [image_part(PNG + b"x" * (11 * 1024 * 1024))] * 2,
    [{**image_part(), "unknown_native_option": True}],
])
async def test_invalid_image_contract_never_partially_uploads_or_starts(native_chat, parts):
    h = native_chat
    response = await h.client.post("/v1/chat/completions", json=request_body(messages=[{"role": "user", "content": parts}]))
    assert response.status_code in {400, 501}, response.text
    assert await h.pool.fetchval("SELECT count(*) FROM kotoyose.execution_input_artifacts") == 0
    assert await h.pool.fetchval("SELECT count(*) FROM kotoyose.execution_attempts") == 0
    assert not h.runtime.jobs


@pytest.mark.asyncio
async def test_image_only_message_is_supported(native_chat):
    h = native_chat
    response = await h.client.post("/v1/chat/completions", json=request_body(messages=[{"role": "user", "content": [image_part()]}]))
    assert response.status_code == 200, response.text
    assert h.runtime.jobs[0].image_inputs[0].content == PNG


@pytest.mark.asyncio
async def test_same_image_can_be_used_by_new_requests_without_mutating_old_ref(native_chat):
    h = native_chat
    body = request_body(messages=[{"role": "user", "content": [image_part()]}])
    first = await h.client.post("/v1/chat/completions", json=body, headers={"X-Kotoyose-Request-Key": "image-first"})
    assert first.status_code == 200, first.text
    original = dict(await h.pool.fetchrow("SELECT * FROM kotoyose.execution_input_artifacts"))
    second = await h.client.post("/v1/chat/completions", json=body, headers={"X-Kotoyose-Request-Key": "image-second"})
    assert second.status_code == 200, second.text
    assert first.headers["X-Kotoyose-Attempt-ID"] != second.headers["X-Kotoyose-Attempt-ID"]
    assert len(h.runtime.jobs) == 2
    assert h.runtime.jobs[0].image_inputs[0].artifact_ref != h.runtime.jobs[1].image_inputs[0].artifact_ref
    assert h.runtime.jobs[0].image_inputs[0].digest == h.runtime.jobs[1].image_inputs[0].digest
    assert dict(await h.pool.fetchrow("SELECT * FROM kotoyose.execution_input_artifacts WHERE ref=$1", original["ref"])) == original
    replay = await h.client.post("/v1/chat/completions", json=body, headers={"X-Kotoyose-Request-Key": "image-first"})
    assert replay.status_code == 200
    assert replay.headers["X-Kotoyose-Attempt-ID"] == first.headers["X-Kotoyose-Attempt-ID"]
    assert await h.pool.fetchval("SELECT count(*) FROM kotoyose.execution_input_artifacts") == 2
