"""Hub が resume_unavailable の後続 Run へ渡す handoff の ArtifactRef 入力を実 row で検証する。"""

from __future__ import annotations

import os

import pytest

from kotoyose.execution_admission import ExecutionAdmission
from kotoyose.execution_contract import ExecutionInput
from kotoyose.execution_resources import ExecutionError
from tests.test_execution_agent_images import _case, _config, _manifest, store  # noqa: F401
from tests.fixtures.clients import clients, set_client

pytestmark = pytest.mark.skipif(
    not os.environ.get("KOTOYOSE_EXECUTION_AGENT_IMAGE_TEST_DATABASE_URL"),
    reason="requires an explicitly isolated image-agent PostgreSQL database",
)


async def _resolve(store, tmp_path, monkeypatch, *, content, mutate=None):
    monkeypatch.setenv("KOTOYOSE_IMAGE_PROVIDER_TOKEN", "provider-secret")
    config = _config(tmp_path)
    _, ref, request, _, _ = await _case(store, tmp_path, content=content)
    if mutate is not None:
        await mutate(ref)
    request = request.model_copy(update={"inputs": [
        ExecutionInput(kind="handoff", provenance="agent_generated", content_ref=ref),
    ]})
    admission = ExecutionAdmission(store, _manifest(), config.image_id, settings=config)
    return ref, await admission.resolve("caller-a", clients()["caller-a"], request), request, admission, config


@pytest.mark.asyncio
async def test_handoff_artifact_text_reaches_resolved_inputs(store, tmp_path, monkeypatch):
    ref, snapshot, *_ = await _resolve(store, tmp_path, monkeypatch, content="前任の成果".encode())
    assert snapshot["inputs"] == [{
        "kind": "handoff", "provenance": "agent_generated", "content_ref": ref, "text": "前任の成果",
    }]


@pytest.mark.asyncio
async def test_empty_handoff_artifact_is_omitted_not_rejected(store, tmp_path, monkeypatch):
    _, snapshot, *_ = await _resolve(store, tmp_path, monkeypatch, content=b"")
    assert snapshot["inputs"] == []


@pytest.mark.asyncio
async def test_non_utf8_handoff_artifact_is_a_clean_error(store, tmp_path, monkeypatch):
    with pytest.raises(ExecutionError) as caught:
        await _resolve(store, tmp_path, monkeypatch, content=b"\xff\xfe\x00bad")
    assert caught.value.status == 400


@pytest.mark.asyncio
async def test_oversized_handoff_artifact_is_refused(store, tmp_path, monkeypatch):
    with pytest.raises(ExecutionError) as caught:
        await _resolve(store, tmp_path, monkeypatch, content=b"a" * 262145)
    assert caught.value.status == 400


@pytest.mark.asyncio
async def test_other_principals_artifact_is_refused(store, tmp_path, monkeypatch):
    async def to_other_principal(ref):
        attempt_id = ref.split(":")[1]
        await store.pool.execute("UPDATE kotoyose.execution_attempts SET principal='caller-b' WHERE id=$1", attempt_id)
    with pytest.raises(ExecutionError) as caught:
        await _resolve(store, tmp_path, monkeypatch, content=b"secret", mutate=to_other_principal)
    assert caught.value.status in {403, 404}


@pytest.mark.asyncio
async def test_handoff_ref_outside_grant_is_refused(store, tmp_path, monkeypatch):
    ref, _, request, admission, config = await _resolve(store, tmp_path, monkeypatch, content=b"text")
    outside = ref[:-4] + "0000"
    request = request.model_copy(update={"inputs": [
        ExecutionInput(kind="handoff", provenance="agent_generated", content_ref=outside),
    ]})
    with pytest.raises(ExecutionError) as caught:
        await admission.resolve("caller-a", clients()["caller-a"], request)
    assert caught.value.status == 403
