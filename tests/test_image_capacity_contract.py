"""Finite ten-image contract, tested without providers, subprocesses or database writes."""

import asyncio
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock
import zlib

from openai_codex import ImageInput
from openai_codex.generated.v2_all import TurnStartParams
import pytest

from kotoyose import config as config_module
from kotoyose.config import AgentRoute, AliasAgent, Connection, HarnessConfig, ModelPreset
from kotoyose.codex_egress import CodexEgress, parse_request
from kotoyose.codex_images import image_data_uris
from kotoyose.compatibility_inputs import validate_chat_images
from kotoyose.execution_admission import ExecutionAdmission
from kotoyose.execution_agent import AgentExecutor
from kotoyose.execution_contract import AgentExecution, ExecutionInput
from kotoyose.execution_images import MAX_IMAGE_BYTES, MAX_IMAGE_COUNT, MAX_IMAGE_TOTAL_BYTES
from kotoyose.execution_resources import (
    AgentDefinition, DriverInstall, ExecutionError, NativeExecutionConfig,
)
from kotoyose.input_artifacts import InputArtifactLimits
from kotoyose.models import ChatCompletionRequest
from kotoyose.native_codex import NativeCodexWorker, _CONFIG, _Protocol
from kotoyose.runtime.base import RuntimeImageInput, RuntimeTextInput
from kotoyose.runtime.codex import _codex_turn_input
from tests.test_codex_egress import header
from tests.test_codex_images import image, payload
from tests.test_execution_resources import _MemoryStore, _manifest, _policy, _resources
from tests.test_native_claude import _job


def png(index):
    """Distinct, complete one-pixel PNGs make dropped/duplicated/reordered images observable."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes([0, index, index + 1, index + 2]))) + chunk(b"IEND", b""))


class ImageStore(_MemoryStore):
    async def artifact(self, principal, ref, *, attempt_id=None):
        assert principal == "caller-a"
        assert attempt_id in (None, "attempt-1")
        return self.artifacts[ref]


def case(tmp_path, images):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    resources = _resources()
    resources["definition"] = AgentDefinition(schema_version="1", kind="agent_definition",
        instructions="Review every supplied image.", output_contract="json", max_turns=1, timeout_policy_ref="timeout")
    resources["grant"] = resources["grant"].model_copy(update={
        "agent_models": ["profile"],
        "resource_refs": ["budget", "timeout", "definition", *(item.artifact_ref for item in images)],
    })
    from kotoyose.config import Client
    config_module._config = HarnessConfig(
        connections={"unused-conn": Connection(provider="codex", kind="api_key", data_policy="no_training", secret="env:UNUSED_FIXTURE_KEY")},
        routes={"profile": AgentRoute(
            driver="codex.app-server", model="gpt-6-luna", connection="unused-conn",
        )},
        models={"profile": ModelPreset(
            route="profile",
            agent=AliasAgent(place="isolated", surface="exec"),
        )},
        clients={"caller-a": Client(
            workspace_roots=[tmp_path], models=["profile"],
        )},
    )
    policy = _policy(tmp_path, agent_models=["profile"])
    settings = NativeExecutionConfig(
        schema_version="1", image_id="sha256:" + "a" * 64,
        drivers={"codex.app-server": DriverInstall(cli_binary=Path("/usr/bin/sh"))},
    )
    store = ImageStore(resources, {"id": "workspace-id", "path": str(workspace), "generation": 0,
        "device": workspace.stat().st_dev, "inode": workspace.stat().st_ino})
    store.artifacts = {item.artifact_ref: {"content": item.content, "media_type": item.media_type,
                                         "digest": item.digest} for item in images}
    admission = ExecutionAdmission(store, _manifest(), settings.image_id, settings=settings)
    request = AgentExecution(schema_version="1", kind="agent", idempotency_key="key-1", caller_run_ref="run-1",
        workspace_ref="workspace-1", execution_grant_ref="grant", budgets_ref="budget",
        agent_definition_ref="definition", model="profile", inputs=[
            ExecutionInput(kind="image", provenance="project", content_ref=item.artifact_ref) for item in images])
    # Rendering has no runtime/config dependency and must not initialize or launch one.
    executor = object.__new__(AgentExecutor)
    executor.store = store
    return admission, policy, request, executor


def snapshot(images):
    return {"definition": {"instructions": "Review every supplied image."}, "inputs": [
        {"kind": "image", "provenance": "project", "artifact_ref": item.artifact_ref,
         "digest": item.digest, "media_type": item.media_type, "size": len(item.content)} for item in images]}


@pytest.mark.parametrize("count", [8, 10])
async def test_images_reach_renderer_and_codex_wire_in_exact_order(tmp_path, monkeypatch, count):
    images = tuple(image(png(index)) for index in range(count))
    admission, policy, request, executor = case(tmp_path, images)
    # Only the unrelated reviewed-rootfs availability check is replaced; no CLI is launched.
    monkeypatch.setattr("kotoyose.codex_rootfs.validate_rootfs", lambda *args, **kwargs: tmp_path)
    plan = await admission.resolve("caller-a", policy, request)
    assert plan["provider"] == "codex" and plan["isolation"] == "sandbox"
    assert plan["definition"]["max_turns"] == 1
    assert plan["budget"] == admission.store.resources["budget"].model_dump(mode="json")
    assert plan["timeout"] == admission.store.resources["timeout"].model_dump(mode="json")
    assert plan["deadline"] == min(admission.store.resources["budget"].deadline,
                                    admission.store.resources["grant"].expires_at).isoformat()
    instructions, prompt, rendered = await executor._render_inputs(
        {"id": "attempt-1", "principal": "caller-a", "snapshot": plan})
    assert tuple(rendered) == images
    data = json.loads(prompt.split("\n", 1)[1])
    assert [item["image_index"] for item in data] == list(range(count))
    assert [item["artifact_ref"] for item in data] == [item.artifact_ref for item in images]
    assert "base64" not in str(plan) and "data:image" not in str(plan)
    uris = image_data_uris(tuple(rendered))
    sdk_input = _codex_turn_input([RuntimeTextInput(prompt), *(RuntimeImageInput(uri) for uri in uris)])
    assert all(isinstance(part, ImageInput) for part in sdk_input[1:])
    assert tuple(part.url for part in sdk_input[1:]) == uris
    answer = '{"passed":true,"summary":"offline transport fixture","checks":[]}'
    responses = [
        {"id": "kotoyose-1", "result": {}},
        {"id": "kotoyose-2", "result": {"thread": {"id": "thread"}, "model": "fixture-model", "modelProvider": "kotoyose_native"}},
        {"id": "kotoyose-3", "result": {"turn": {"id": "turn"}}},
        {"method": "item/completed", "params": {"threadId": "thread", "turnId": "turn",
            "item": {"type": "agentMessage", "id": "answer", "phase": "final_answer", "text": answer}}},
        {"method": "turn/completed", "params": {"threadId": "thread", "turn": {"id": "turn", "status": "completed"}}},
    ]
    writes = []
    process = SimpleNamespace(stdin=SimpleNamespace(write=writes.append, drain=AsyncMock()),
        stdout=SimpleNamespace(readuntil=AsyncMock(side_effect=[(json.dumps(item) + "\n").encode() for item in responses])))
    rpc = _Protocol(process, 65536, uris, prompt)
    job = _job(tmp_path, model="fixture-model", instructions=instructions, prompt=prompt,
        output_contract="json", image_inputs=tuple(rendered), max_turns=1)
    execute_tool = AsyncMock(side_effect=AssertionError("No tool execution is allowed"))
    assert await NativeCodexWorker()._run(rpc, job, asyncio.Event(), execute_tool, {}, {}, _CONFIG) == answer.encode()
    wire = [json.loads(item) for item in writes]
    assert [item.get("method") for item in wire].count("thread/start") == 1
    turns = [item["params"] for item in wire if item.get("method") == "turn/start"]
    assert len(turns) == 1
    # Validate/serialize the actual adapter wire with the installed pinned SDK model.
    serialized = TurnStartParams.model_validate(turns[0]).model_dump(mode="json", by_alias=True, exclude_none=True)
    observed = [part["url"] for part in serialized["input"] if part["type"] == "image"]
    assert tuple(observed) == uris
    assert [hashlib.sha256(base64.b64decode(uri.split(",", 1)[1])).hexdigest() for uri in observed] == [
        item.digest for item in images]
    execute_tool.assert_not_awaited()
    broker = CodexEgress(1, 65536, "fixed", "fake", None, image_uris=uris)
    body = json.dumps(payload(observed)).encode()
    parsed, _ = parse_request(header(length=len(body)))
    try:
        assert json.loads(broker.validate_request(parsed, body)) == payload(uris)
    finally:
        broker.directory.cleanup()


@pytest.mark.parametrize("change", ["count", "aggregate", "per_image", "unsupported_mime", "bad_signature", "digest"])
async def test_invalid_images_fail_admission_renderer_and_adapter(tmp_path, change):
    images = (image(png(1)),)
    if change == "count": images *= 11
    elif change == "aggregate": images = (image(b"\x89PNG\r\n\x1a\n" + b"x" * (MAX_IMAGE_TOTAL_BYTES // 2 - 7)),) * 2
    elif change == "per_image": images = (image(b"\x89PNG\r\n\x1a\n" + b"x" * (MAX_IMAGE_BYTES - 7)),)
    elif change == "unsupported_mime": images = (image(b"<svg/>", "image/svg+xml"),)
    elif change == "bad_signature": images = (image(b"not an image"),)
    admission, policy, request, executor = case(tmp_path, images)
    if change == "digest": executor.store.artifacts[images[0].artifact_ref]["content"] = png(2)
    expected = {"count": "aggregate exceeds", "aggregate": "aggregate exceeds",
        "per_image": "size limit", "unsupported_mime": "media type is unsupported",
        "bad_signature": "signature does not match", "digest": "digest changed"}[change]
    with pytest.raises(ExecutionError, match=expected):
        await admission.resolve("caller-a", policy, request)
    with pytest.raises(ExecutionError):
        await executor._render_inputs({"id": "attempt-1", "principal": "caller-a", "snapshot": snapshot(images)})
    if change == "digest":
        from dataclasses import replace
        images = (replace(images[0], content=png(2)),)
    with pytest.raises(ValueError, match="invalid_job"):
        image_data_uris(images)


@pytest.mark.parametrize("count", [8, 10, 11])
def test_compatibility_request_has_same_finite_count_limit(count):
    parts = [{"type": "image_url", "image_url": {"url": uri}} for uri in image_data_uris((image(png(1)),)) * count]
    request = ChatCompletionRequest(model="fixture", messages=[{"role": "user", "content": parts}])
    kwargs = {"expires_at": datetime.now(timezone.utc) + timedelta(minutes=1), "limits": InputArtifactLimits()}
    if count > 10:
        with pytest.raises(ExecutionError, match="count limit"):
            validate_chat_images(request, **kwargs)
    else:
        assert len(validate_chat_images(request, **kwargs)) == count


def test_local_count_and_original_byte_ceilings_are_explicit():
    assert MAX_IMAGE_COUNT == 10
    assert MAX_IMAGE_BYTES == MAX_IMAGE_TOTAL_BYTES == 20 * 1024 * 1024


def test_direct_codex_broker_rejects_eleven_images():
    uri, = image_data_uris((image(png(1)),))
    with pytest.raises(ValueError, match="invalid_job"):
        CodexEgress(1, 65536, "fixed", "fake", None, image_uris=(uri,) * 11)


@pytest.mark.parametrize("change", ["role", "remote", "detail", "signature", "aggregate"])
def test_compatibility_unsupported_images_fail_closed(change):
    uri, = image_data_uris((image(png(1)),))
    part = {"type": "image_url", "image_url": {"url": uri}}
    role = "user"
    if change == "role": role = "assistant"
    elif change == "remote": part["image_url"]["url"] = "https://example.invalid/image.png"
    elif change == "detail": part["image_url"]["detail"] = "high"
    elif change == "signature": part["image_url"]["url"] = "data:image/png;base64,bm90IGFuIGltYWdl"
    parts = [part]
    if change == "aggregate":
        content = b"\x89PNG\r\n\x1a\n" + b"x" * (MAX_IMAGE_TOTAL_BYTES // 2 - 7)
        parts = [{"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(content).decode()}}] * 2
    request = ChatCompletionRequest(model="fixture", messages=[{"role": role, "content": parts}])
    with pytest.raises(ExecutionError):
        validate_chat_images(request, expires_at=datetime.now(timezone.utc) + timedelta(minutes=1), limits=InputArtifactLimits())
