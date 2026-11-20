"""画像 Artifact の新世代 admission と durable resume を owned PG/fake API で検証する。"""

import asyncio
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from uuid import uuid4

import pytest

from kotoyose.execution_artifacts import CapturedArtifact
from kotoyose.execution_contract import ExecutionInput, ResumeRequest, RuntimeAuthenticationResolution
from kotoyose.execution_resources import AgentDefinition, ExecutionError, content_digest
from kotoyose import codex_egress
from tests.codex_fake_api import fake_api
from tests.codex_subscription_fixture import subscription_auth
from tests.test_codex_images import png
from tests.test_native_codex_postgres import (  # noqa: F401
    DATABASE_URL, IMAGE_ID, _install, _use_subscription, codex_service, terminal,
)
from tests.test_native_codex import provider  # noqa: F401
from tests.codex_runtime_fixture import codex_rootfs  # noqa: F401
from tests.fixtures.clients import clients, set_client


pytestmark = pytest.mark.skipif(not DATABASE_URL or not IMAGE_ID or not os.environ.get("KOTOYOSE_CODEX_CONFORMANCE_BINARY"),
    reason="Owned Codex PG, reviewed Docker image and pinned CLI with fake API required")


async def image_request(service, request, workspace, *, authentication="api"):
    source = workspace.parent / "image-source"
    source.mkdir()
    registration = await service.store.register_workspace("caller-a", "image-source", source)
    source_ref = f"workspace:{registration['id']}:0"
    grant = await service.store.resource("caller-a", request.execution_grant_ref)
    source_grant = grant.model_copy(update={"grant_id": "image-source", "version": 1,
        "workspace_ref": source_ref, "caller_run_ref": "image-source"})
    source_grant_ref = await service.store.put_resource("caller-a", source_grant)
    attempt, _ = await service.store.admit("caller-a", {
        "idempotency_key": "image-source", "caller_run_ref": "image-source", "workspace_ref": source_ref,
        "execution_grant_ref": source_grant_ref},
        {"workspace_id": registration["id"], "generation": 0, "workspace_path": str(source)}, 2)
    await service.store.finish(attempt["id"], "completed", "ok", "confirmed", 0,
        {"image": CapturedArtifact(png(), "image/png")})
    ref = (await service.store.attempt("caller-a", attempt["id"]))["artifacts"][0]["ref"]
    definition = await service.store.put_resource("caller-a", AgentDefinition(schema_version="1", kind="agent_definition",
        instructions="Describe the supplied fixture image.", output_contract="text", max_turns=1,
        timeout_policy_ref=(await service.store.resource("caller-a", request.agent_definition_ref)).timeout_policy_ref))
    updated = grant.model_copy(update={"version": grant.version + 1, "resource_refs": [*grant.resource_refs, ref, definition]})
    request = request.model_copy(update={"agent_definition_ref": definition, "tool_refs": [],
        "execution_grant_ref": await service.store.put_resource("caller-a", updated),
        "inputs": [ExecutionInput(kind="image", provenance="project", content_ref=ref)]})
    _install(service).cli_binary = Path(os.environ["KOTOYOSE_CODEX_CONFORMANCE_BINARY"])
    if authentication == "subscription":
        path, _ = subscription_auth(workspace.parent)
        _use_subscription(service, path)
    if authentication == "subscription":
        binding = service.resolve_runtime_authentication(clients()["caller-a"],
            RuntimeAuthenticationResolution(schema_version="1", model="automatic"))
        request = request.model_copy(update={"required_authentication_mode": "chatgpt_subscription",
            "expected_authentication_binding_digest": binding["runtime_authentication"]["binding_digest"]})
    return request, ref


@pytest.mark.parametrize("authentication", ["api", "subscription"])
@pytest.mark.parametrize("change", ["none", "revoked_grant", "digest", "mime", "unconfirmed"])
async def test_image_resume_readmits_bytes_and_requires_new_grant_stop_and_generation(codex_service, monkeypatch, authentication, change):
    service, policy, request, workspace = codex_service
    request, image_ref = await image_request(service, request, workspace, authentication=authentication)
    async with fake_api(monkeypatch, tool=False) as calls:
        old_id = await service.dispatch("caller-a", policy, request)
        old = await terminal(service, old_id)
        assert (old["status"], old["stop_confirmation"]) == ("succeeded", "confirmed"), old
        assert len(calls) == 1
        description = await service.describe("caller-a", old_id)
        grant = await service.store.resource("caller-a", request.execution_grant_ref)
        fresh_ref = await service.store.put_resource("caller-a", grant.model_copy(update={
            "version": grant.version + 1, "workspace_ref": description["workspace_ref"]}))
        continuation = request.model_copy(update={"idempotency_key": str(uuid4()),
            "workspace_ref": description["workspace_ref"], "execution_grant_ref": fresh_ref})
        resume = ResumeRequest(schema_version="1", predecessor_snapshot_digest=content_digest(old["snapshot"]), execution=continuation)
        if change == "revoked_grant": await service.store.revoke_grant("caller-a", fresh_ref)
        elif change == "digest":
            await service.store.pool.execute("UPDATE kotoyose.execution_artifacts SET content=$2 WHERE ref=$1", image_ref, b"changed")
        elif change == "mime":
            await service.store.pool.execute("UPDATE kotoyose.execution_artifacts SET media_type='image/jpeg' WHERE ref=$1", image_ref)
        elif change == "unconfirmed":
            await service.store.pool.execute("UPDATE kotoyose.execution_attempts SET stop_confirmation='unconfirmed' WHERE id=$1", old_id)
        before = await service.store.pool.fetchval("SELECT count(*) FROM kotoyose.execution_attempts")
        if change == "none":
            ids = await asyncio.gather(*(service.resume("caller-a", policy, old_id, resume) for _ in range(4)))
            assert len(set(ids)) == 1
            current = await terminal(service, ids[0])
            assert (current["status"], current["stop_confirmation"]) == ("succeeded", "confirmed"), current
            assert current["generation"] == old["generation"] + 1 and current["snapshot"]["resume_mode"] == "durable_state"
            assert len(calls) == 2  # job-B proves its own image delivery, never reuses job-A evidence.
            assert current["snapshot"]["inputs"] == old["snapshot"]["inputs"]
            assert "data:image" not in str(current) and "base64" not in str(current)
            stale = resume.model_copy(update={"execution": continuation.model_copy(update={"idempotency_key": str(uuid4())})})
            with pytest.raises(ExecutionError): await service.resume("caller-a", policy, old_id, stale)
        else:
            with pytest.raises(ExecutionError): await service.resume("caller-a", policy, old_id, resume)
            assert len(calls) == 1
            assert await service.store.pool.fetchval("SELECT count(*) FROM kotoyose.execution_attempts") == before
            assert (await service.store.attempt("caller-a", old_id))["resumed_by"] is None


@pytest.mark.parametrize("action", ["cancel", "revoke"])
async def test_pending_image_send_is_fenced_and_cancelled_attempt_readmits_on_resume(codex_service, monkeypatch, action):
    service, policy, request, workspace = codex_service
    request, _ = await image_request(service, request, workspace)
    entered, release = asyncio.Event(), asyncio.Event()
    async with fake_api(monkeypatch, tool=False) as calls:
        connect = codex_egress.connect_endpoint
        async def deferred():
            entered.set()
            await release.wait()
            return await connect()
        monkeypatch.setattr(codex_egress, "connect_endpoint", deferred)
        attempt_id = await service.dispatch("caller-a", policy, request)
        await asyncio.wait_for(entered.wait(), 5)
        if action == "cancel":
            await service.cancel("caller-a", attempt_id)
        else:
            await service.store.revoke_grant("caller-a", request.execution_grant_ref)
            release.set()
        old = await terminal(service, attempt_id)
        # Grant 撤回も durable cancelling を通り、確認済み停止後は既存契約の cancelled になる。
        assert (old["status"], old["reason"]) == (
            "cancelled", "user_cancelled" if action == "cancel" else "policy_revoked"), old
        assert old["stop_confirmation"] == "confirmed" and old["side_effect_resolution"] == "resolved" and not calls
        assert all(op["state"] == "confirmed" for op in await service.store.operations(attempt_id))
        if action == "cancel":
            monkeypatch.setattr(codex_egress, "connect_endpoint", connect)
            description = await service.describe("caller-a", attempt_id)
            grant = await service.store.resource("caller-a", request.execution_grant_ref)
            fresh_ref = await service.store.put_resource("caller-a", grant.model_copy(update={
                "version": grant.version + 1, "workspace_ref": description["workspace_ref"]}))
            continuation = request.model_copy(update={"idempotency_key": str(uuid4()),
                "workspace_ref": description["workspace_ref"], "execution_grant_ref": fresh_ref})
            resume = ResumeRequest(schema_version="1", predecessor_snapshot_digest=content_digest(old["snapshot"]), execution=continuation)
            current_id = await service.resume("caller-a", policy, attempt_id, resume)
            current = await terminal(service, current_id)
            assert current["status"] == "succeeded" and current["stop_confirmation"] == "confirmed", current
            assert current["generation"] == old["generation"] + 1 and len(calls) == 1
