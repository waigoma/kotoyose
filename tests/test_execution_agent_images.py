"""ArtifactRef image input の admission と execute 時再照合を検証する。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
from urllib.parse import unquote, urlparse

import asyncpg
import pytest
import pytest_asyncio

from kotoyose.capabilities import CapabilitySupport, capability_manifest
from kotoyose.execution_admission import ExecutionAdmission
from kotoyose.execution_agent import AgentExecutor
from kotoyose.execution_contract import AgentExecution, ExecutionInput
from kotoyose.execution_resources import (
    AgentDefinition, ExecutionBudget, ExecutionError, ExecutionGrant,
    NativeExecutionConfig, TimeoutPolicy,
)
from kotoyose.execution_store import ExecutionStore
from kotoyose.execution_artifacts import CapturedArtifact
from kotoyose.native_claude import NativeAgentResult
from kotoyose.runtime.base import RuntimeEvent
from tests.codex_runtime_fixture import codex_rootfs  # noqa: F401
from tests.codex_fake_api import fake_api
from tests.test_codex_images import png
from tests.fixtures.clients import clients


DATABASE_URL = os.environ.get("KOTOYOSE_EXECUTION_AGENT_IMAGE_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="requires an explicitly isolated image-agent PostgreSQL database"
)


def _database_name(url: str) -> str:
    return unquote(urlparse(url).path.lstrip("/")).split("?", 1)[0]


@pytest_asyncio.fixture
async def store() -> ExecutionStore:
    assert DATABASE_URL is not None
    assert _database_name(DATABASE_URL).startswith("ahs_execution_test_agent_images")
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=4, command_timeout=15)
    try:
        await pool.execute("DROP SCHEMA IF EXISTS kotoyose CASCADE; CREATE SCHEMA kotoyose")
        result = ExecutionStore(pool)
        await result.initialize()
        yield result
    finally:
        await pool.execute("DROP SCHEMA IF EXISTS kotoyose CASCADE")
        await pool.close()


def _config(root: Path, *, provider="claude", rootfs=None, cli_binary=Path("/usr/bin/sh")) -> NativeExecutionConfig:
    from kotoyose import config as config_module
    from kotoyose.config import AgentRoute, AliasAgent, Client, Connection, HarnessConfig, ModelPreset

    model = "gpt-6-luna" if provider == "codex" else "claude-sonnet-4-5"
    driver = "codex.app-server" if provider == "codex" else "claude.agent-sdk"
    surface = "exec" if provider == "codex" else "none"
    policy = Client(
        workspace_roots=[root], models=["profile"],
        places=["isolated"], max_surface=surface,
        allowed_env_vars=[], allowed_provenance=["project", "agent_generated", "mixed"],
        max_run_seconds=30, max_memory_bytes=128 * 1024 * 1024, max_processes=16, max_cpus=1,
        max_concurrent_runs=2,
    )
    config_module._config = HarnessConfig(
        connections={"img-conn": Connection(provider=provider, kind="api_key", data_policy="no_training", secret="env:KOTOYOSE_IMAGE_PROVIDER_TOKEN")},
        routes={"profile": AgentRoute(driver=driver, model=model, connection="img-conn")},
        models={"profile": ModelPreset(
            route="profile",
            agent=AliasAgent(place="isolated", surface=surface),
        )},
        clients={"caller-a": policy},
    )
    install = {"cli_binary": str(cli_binary)}
    if rootfs:
        install["cells"] = {"standard": {"rootfs": str(rootfs[0]), "rootfs_digest": rootfs[1]}}
    return NativeExecutionConfig(
        schema_version="1", image_id="sha256:" + "a" * 64,
        drivers={driver: install},
    )


def _manifest():
    manifest = capability_manifest()
    for name in ("native_execution", "sandbox"):
        manifest.capabilities[name] = CapabilitySupport(status="supported", reason="test")
    return manifest


async def _case(store: ExecutionStore, root: Path, *, content=None):
    now = datetime.now(timezone.utc)
    source = root / "source"
    target = root / "target"
    source.mkdir()
    target.mkdir()
    source_registration = await store.register_workspace("caller-a", "source", source)
    source_ref = f"workspace:{source_registration['id']}:0"
    source_grant = ExecutionGrant(
        schema_version="1", kind="grant", grant_id="source-grant", version=1,
        caller_run_ref="source-run", workspace_ref=source_ref, isolation="sandbox", network="none",
        policy_ref="policy", not_before=now - timedelta(minutes=1), expires_at=now + timedelta(minutes=5),
        max_seconds=20, delegation="none",
    )
    source_grant_ref = await store.put_resource("caller-a", source_grant)
    source_attempt, _ = await store.admit(
        "caller-a",
        {"idempotency_key": "source", "caller_run_ref": "source-run", "workspace_ref": source_ref,
         "execution_grant_ref": source_grant_ref},
        {"workspace_id": source_registration["id"], "generation": 0, "workspace_path": str(source)},
        2,
    )
    image_content = content if content is not None else b"\x89PNG\r\n\x1a\nimage"
    await store.finish(
        source_attempt["id"], "completed", "ok", "confirmed", 0,
        {"image": CapturedArtifact(image_content, "image/png")},
    )
    image_ref = (await store.attempt("caller-a", source_attempt["id"]))["artifacts"][0]["ref"]

    registration = await store.register_workspace("caller-a", "target", target)
    timeout_ref = await store.put_resource(
        "caller-a", TimeoutPolicy(schema_version="1", kind="timeout_policy", soft_seconds=1, hard_seconds=10, grace_seconds=1)
    )
    budget_ref = await store.put_resource(
        "caller-a", ExecutionBudget(
            schema_version="1", kind="budget", reservation_ref="image", mode="observed",
            deadline=now + timedelta(minutes=5), memory_bytes=64 * 1024 * 1024,
            pids_limit=16, cpus=1, output_limit_bytes=4096,
        )
    )
    definition_ref = await store.put_resource(
        "caller-a", AgentDefinition(
            schema_version="1", kind="agent_definition", instructions="Describe the image.",
            output_contract="text", max_turns=1, timeout_policy_ref=timeout_ref,
        )
    )
    grant = ExecutionGrant(
        schema_version="1", kind="grant", grant_id="image-grant", version=1,
        caller_run_ref="image-run", workspace_ref=f"workspace:{registration['id']}:0",
        isolation="sandbox", network="none", agent_models=["profile"],
        resource_refs=[definition_ref, timeout_ref, budget_ref, image_ref], policy_ref="policy",
        not_before=now - timedelta(minutes=1), expires_at=now + timedelta(minutes=5),
        max_seconds=20, delegation="none",
    )
    grant_ref = await store.put_resource("caller-a", grant)
    request = AgentExecution(
        schema_version="1", kind="agent", idempotency_key="image-run", caller_run_ref="image-run",
        workspace_ref=f"workspace:{registration['id']}:0", execution_grant_ref=grant_ref,
        budgets_ref=budget_ref, agent_definition_ref=definition_ref, model="profile",
        inputs=[ExecutionInput(kind="image", provenance="project", content_ref=image_ref)],
    )
    return registration, image_ref, request, grant_ref, target


class _CapturingRuntime:
    def __init__(self):
        self.job = None
        self.calls = 0

    async def execute(self, job, cancel, execute_tool, *, on_process_started):
        self.calls += 1
        self.job = job
        process = await asyncio.create_subprocess_exec("sleep", "30")
        try:
            await on_process_started(process.pid)
            if job.activity_event is not None:
                job.activity_event(RuntimeEvent(kind="text", summary="ok"))
            return NativeAgentResult(
                status="succeeded", reason="completed", content=b"ok", stop_confirmation="confirmed",
                usage={"source": "unknown", "cost": None, "input_tokens": None, "output_tokens": None},
            )
        finally:
            process.terminate()
            await process.wait()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["claude", "codex"])
async def test_image_artifact_flows_to_native_agent_job_without_snapshot_bytes(store, tmp_path, monkeypatch, codex_rootfs, provider):
    monkeypatch.setenv("KOTOYOSE_IMAGE_PROVIDER_TOKEN", "provider-secret")
    config = _config(tmp_path, provider=provider, rootfs=codex_rootfs if provider == "codex" else None)
    registration, image_ref, request, grant_ref, target = await _case(store, tmp_path)
    admission = ExecutionAdmission(store, _manifest(), config.image_id, settings=config)
    snapshot = await admission.resolve("caller-a", clients()["caller-a"], request)
    image = snapshot["inputs"][0]
    assert image == {
        "kind": "image", "provenance": "project", "artifact_ref": image_ref,
        "digest": image["digest"], "media_type": "image/png", "size": len(b"\x89PNG\r\n\x1a\nimage"),
    }
    assert "content" not in image and "base64" not in str(snapshot)
    attempt, _ = await store.admit("caller-a", request.model_dump(mode="json"), snapshot, 2)
    await store.mark_running(attempt["id"])
    runtime = _CapturingRuntime()
    executor = AgentExecutor(config, store, object())
    if provider == "codex": executor.codex_runtime = runtime
    else: executor.runtime = runtime
    result = await executor.execute(attempt, asyncio.Event())
    assert result.status == "succeeded"
    assert runtime.calls == 1
    assert runtime.job.image_inputs[0].content.startswith(b"\x89PNG")
    assert runtime.job.image_inputs[0].artifact_ref == image_ref


@pytest.mark.asyncio
async def test_native_executor_forwards_fake_worker_activity(store, tmp_path, monkeypatch):
    monkeypatch.setenv("KOTOYOSE_IMAGE_PROVIDER_TOKEN", "provider-secret")
    config = _config(tmp_path)
    _, _, request, _, _ = await _case(store, tmp_path)
    admission = ExecutionAdmission(store, _manifest(), config.image_id, settings=config)
    snapshot = await admission.resolve("caller-a", clients()["caller-a"], request)
    attempt, _ = await store.admit("caller-a", request.model_dump(mode="json"), snapshot, 2)
    await store.mark_running(attempt["id"])
    runtime = _CapturingRuntime()
    executor = AgentExecutor(config, store, object())
    executor.runtime = runtime
    events = []
    interaction = type("Interaction", (), {"emit": lambda self, event: events.append(event)})()
    result = await executor.execute(attempt, asyncio.Event(), interaction=interaction)
    assert result.status == "succeeded"
    assert [(event.kind, event.summary) for event in events] == [("text", "ok")]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("change", ["digest", "mime", "magic", "size", "count", "total"])
async def test_image_digest_drift_is_rejected_before_provider_query(store, tmp_path, monkeypatch, codex_rootfs, provider, change):
    monkeypatch.setenv("KOTOYOSE_IMAGE_PROVIDER_TOKEN", "provider-secret")
    config = _config(tmp_path, provider=provider, rootfs=codex_rootfs if provider == "codex" else None)
    registration, image_ref, request, grant_ref, target = await _case(store, tmp_path)
    admission = ExecutionAdmission(store, _manifest(), config.image_id, settings=config)
    snapshot = await admission.resolve("caller-a", clients()["caller-a"], request)
    attempt, _ = await store.admit("caller-a", request.model_dump(mode="json"), snapshot, 2)
    await store.mark_running(attempt["id"])
    if change in {"digest", "magic"}:
        content = b"\x89PNG\r\n\x1a\nchanged" if change == "digest" else b"changed"
        digest = hashlib.sha256(content).hexdigest()
        await store.pool.execute("UPDATE kotoyose.execution_artifacts SET content=$2,digest=$3 WHERE ref=$1", image_ref, content, digest)
    elif change == "mime":
        await store.pool.execute("UPDATE kotoyose.execution_artifacts SET media_type='image/jpeg' WHERE ref=$1", image_ref)
    elif change == "size": attempt["snapshot"]["inputs"][0]["size"] += 1
    elif change == "count": attempt["snapshot"]["inputs"] *= 11
    elif change == "total":
        from kotoyose import execution_agent
        monkeypatch.setattr(execution_agent, "MAX_IMAGE_TOTAL_BYTES", 1)
    runtime = _CapturingRuntime()
    executor = AgentExecutor(config, store, object())
    if provider == "codex": executor.codex_runtime = runtime
    else: executor.runtime = runtime
    with pytest.raises(ExecutionError):
        await executor.execute(attempt, asyncio.Event())
    assert runtime.calls == 0


@pytest.mark.skipif(not os.environ.get("KOTOYOSE_CODEX_CONFORMANCE_BINARY"), reason="Explicit pinned CLI with fake API required")
@pytest.mark.parametrize("large", [False, True])
async def test_codex_artifact_bytes_reach_actual_cli_fake_api_without_snapshot_or_events(store, tmp_path, monkeypatch, codex_rootfs, large):
    monkeypatch.setenv("KOTOYOSE_IMAGE_PROVIDER_TOKEN", "fake-image-provider-secret")
    config = _config(tmp_path, provider="codex", rootfs=codex_rootfs,
        cli_binary=Path(os.environ["KOTOYOSE_CODEX_CONFORMANCE_BINARY"]))
    _, image_ref, request, _, _ = await _case(store, tmp_path, content=png(large=large))
    admission = ExecutionAdmission(store, _manifest(), config.image_id, settings=config)
    snapshot = await admission.resolve("caller-a", clients()["caller-a"], request)
    assert "base64" not in str(snapshot) and "data:image" not in str(snapshot)
    attempt, _ = await store.admit("caller-a", request.model_dump(mode="json"), snapshot, 2)
    await store.mark_running(attempt["id"])
    async with fake_api(monkeypatch, tool=False) as calls:
        result = await AgentExecutor(config, store, object()).execute(attempt, asyncio.Event())
    assert (result.status, result.content, result.stop_confirmation) == ("succeeded", b"fixture answer", "confirmed"), result
    assert len(calls) == 1
    image_parts = [part for message in calls[0][1]["input"] if message["type"] == "message"
        for part in message["content"] if part["type"] == "input_image"]
    import base64
    assert len(image_parts) == 1 and base64.b64decode(image_parts[0]["image_url"].split(",", 1)[1]) == png(large=large)
    public = await store.attempt("caller-a", attempt["id"])
    assert "data:image" not in str(public) and "base64" not in str(public)
    assert image_ref in str(public["snapshot"]["inputs"])
