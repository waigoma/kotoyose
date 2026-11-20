"""client call の配送・取消・再照合を実 Postgres と HTTP で確認する。"""

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio

from kotoyose.app import app
from kotoyose.execution_resources import ClientToolSpec, ExecutionError
from tests.test_execution_agent import DATABASE_URL, IMAGE_ID, _resources, _service_for_admission, store
from tests.fixtures.clients import clients, set_client

pytestmark = pytest.mark.skipif(not DATABASE_URL or not IMAGE_ID, reason="Dedicated Agent test PG required")


@pytest_asyncio.fixture
async def client_attempt(store, tmp_path, monkeypatch):
    service, _ = await _service_for_admission(store, tmp_path)
    monkeypatch.setenv("KOTOYOSE_AGENT_CALLER_TOKEN", "client-test-caller")
    policy = clients()["caller-a"]
    tool = await service.register_resource("caller-a", policy, ClientToolSpec(
        schema_version="1", kind="client_tool", name="expression_set", description="Set expression.",
        input_schema={"type": "object", "additionalProperties": False,
                      "properties": {"value": {"type": "string"}}, "required": ["value"]},
        effect="set", timeout_seconds=2,
    ))
    request, workspace, _ = await _resources(service, policy, tmp_path / "workspace", tool_refs=[tool, "builtin:request-approval@1"])
    approval_id = str(uuid4())
    request = type(request).model_validate({**request.model_dump(mode="json"), "client_key": "client:fixture", "approval_bindings": [
        {"approval_id": approval_id, "subject_digest": "a" * 64, "wait_seconds": 1},
    ]})
    snapshot = await service.admission.resolve("caller-a", policy, request)
    attempt, _ = await store.admit("caller-a", request.model_dump(mode="json"), snapshot, 2)
    await store.mark_running(attempt["id"])
    previous = getattr(app.state, "execution_service", None)
    app.state.execution_service = service
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture",
                                     headers={"Authorization": "Bearer client-test-caller"}) as client:
            yield SimpleNamespace(service=service, store=store, tools=store.client_tools, id=attempt["id"],
                                  tool=tool, workspace=workspace, approval_id=approval_id, client=client)
    finally:
        app.state.execution_service = previous


async def connect(h, generation=0):
    response = await h.client.post(f"/v1/attempts/{h.id}/client-connections", json={
        "schema_version": "1", "client_key": "client:fixture", "generation": generation,
    })
    assert response.status_code == 201, response.text
    return response.json()


async def create(h, *, approval=False):
    return await h.tools.create_call("caller-a", h.id, "client:fixture", 0,
        "builtin:request-approval@1" if approval else h.tool,
        {"approval_id": h.approval_id, "subject_digest": "a" * 64} if approval else {"value": "happy"},
        datetime.now(timezone.utc) + timedelta(seconds=10))


async def delivery(h, generation=0):
    return await h.client.get(f"/v1/attempts/{h.id}/client-calls", params={"client_key": "client:fixture", "generation": generation})


async def result(h, call, *, value=None, generation=0, **changes):
    return await h.client.post(f"/v1/attempts/{h.id}/client-calls/{call['id']}/results", json={
        "schema_version": "1", "client_key": "client:fixture", "generation": generation,
        "tool_ref": call["tool_ref"], "arguments_digest": call["arguments_digest"],
        "disposition": "applied", "result": value or {"expression": "happy"}, **changes,
    })


@pytest.mark.asyncio
async def test_delivery_replay_result_identity_and_no_duplicate_operation(client_attempt):
    h = client_attempt
    await connect(h)
    call = await create(h)
    first = await delivery(h)
    assert first.status_code == 200
    assert first.json()["calls"][0]["id"] == call["id"]
    assert (await delivery(h)).json() == first.json()
    assert (await result(h, call, arguments_digest="b" * 64)).status_code == 409
    response = await result(h, call)
    assert response.status_code == 200, response.text
    assert (await result(h, call)).json() == response.json()
    assert (await result(h, call, value={"expression": "sad"})).status_code == 409
    assert await h.store.pool.fetchval("SELECT count(*) FROM kotoyose.execution_operations WHERE kind='client_tool'") == 1


@pytest.mark.asyncio
async def test_generation_fences_old_results_and_moves_only_unsent_calls(client_attempt):
    h = client_attempt
    await connect(h)
    sent = await create(h)
    await delivery(h)
    pending = await create(h)
    await connect(h, 1)
    assert (await result(h, sent)).status_code in {401, 409}
    calls = (await delivery(h, 1)).json()["calls"]
    assert [call["id"] for call in calls] == [pending["id"]]
    assert calls[0]["generation"] == 1
    assert await h.store.pool.fetchval("SELECT state FROM kotoyose.execution_operations WHERE id=$1", sent["operation_id"]) == "unknown"


@pytest.mark.asyncio
async def test_unknown_effect_keeps_writer_until_late_authenticated_result(client_attempt):
    h = client_attempt
    await connect(h)
    call = await create(h)
    await delivery(h)
    await h.tools.cancel("caller-a", h.id, call["id"])
    await h.store.finish(h.id, "cancelled", "cancelled", "confirmed", None, {})
    description = await h.service.describe("caller-a", h.id)
    assert description["stop_confirmation"] == "confirmed"
    assert description["side_effect_resolution"] == "unresolved"
    assert description["workspace_ref"] == h.workspace
    await h.store.confirm_stop(h.id)
    assert (await h.service.describe("caller-a", h.id))["side_effect_resolution"] == "unresolved"
    assert (await result(h, call)).status_code == 200
    settled = await h.service.reconcile("caller-a", h.id)
    assert settled["status"] == "cancelled"
    assert settled["side_effect_resolution"] == "resolved"
    assert settled["workspace_ref"] != h.workspace
    assert (await h.service.reconcile("caller-a", h.id))["workspace_ref"] == settled["workspace_ref"]


@pytest.mark.asyncio
async def test_pending_cancel_and_sent_cancel_have_distinct_effect_certainty(client_attempt):
    h = client_attempt
    await connect(h)
    sent = await create(h)
    await delivery(h)
    unsent = await create(h)
    await h.tools.cancel_attempt("caller-a", h.id)
    assert await h.store.pool.fetchval("SELECT state FROM kotoyose.execution_operations WHERE id=$1", sent["operation_id"]) == "unknown"
    assert await h.store.pool.fetchval("SELECT state FROM kotoyose.execution_operations WHERE id=$1", unsent["operation_id"]) == "confirmed"


@pytest.mark.asyncio
async def test_tool_schema_and_grant_are_rechecked_before_delivery(client_attempt):
    h = client_attempt
    await connect(h)
    with pytest.raises(ExecutionError):
        await h.tools.create_call("caller-a", h.id, "client:fixture", 0, h.tool, {"value": 123}, datetime.now(timezone.utc) + timedelta(seconds=10))
    with pytest.raises(ExecutionError):
        await h.tools.create_call("caller-a", h.id, "client:fixture", 0, "tool:unregistered", {}, datetime.now(timezone.utc) + timedelta(seconds=10))
    call = await create(h)
    attempt = await h.store.attempt("caller-a", h.id)
    await h.store.revoke_grant("caller-a", attempt["request"]["execution_grant_ref"])
    assert (await delivery(h)).status_code == 403
    assert await h.store.pool.fetchval("SELECT state FROM kotoyose.execution_client_calls WHERE id=$1", call["id"]) == "pending"


@pytest.mark.asyncio
async def test_call_normalizes_integral_float_and_rejects_out_of_range_integer(store, tmp_path):
    service, _ = await _service_for_admission(store, tmp_path)
    policy = clients()["caller-a"]
    tool = await service.register_resource("caller-a", policy, ClientToolSpec(
        schema_version="1", kind="client_tool", name="number_set", description="Set number.",
        input_schema={"type": "object", "additionalProperties": False,
                      "properties": {"value": {"type": "integer"}}, "required": ["value"]},
        effect="set", timeout_seconds=2,
    ))
    request, _, _ = await _resources(service, policy, tmp_path / "workspace", tool_refs=[tool])
    request = type(request).model_validate({**request.model_dump(mode="json"), "client_key": "client:fixture"})
    snapshot = await service.admission.resolve("caller-a", policy, request)
    attempt, _ = await store.admit("caller-a", request.model_dump(mode="json"), snapshot, 2)
    await store.mark_running(attempt["id"])
    await store.client_tools.register_connection("caller-a", attempt["id"], "client:fixture", 0)
    deadline = datetime.now(timezone.utc) + timedelta(seconds=10)

    call = await store.client_tools.create_call(
        "caller-a", attempt["id"], "client:fixture", 0, tool, {"value": 1.0}, deadline,
    )
    assert call["arguments_digest"] == hashlib.sha256(b'{"value":1}').hexdigest()
    assert call["arguments"] == {"value": 1}
    assert type(call["arguments"]["value"]) is int
    with pytest.raises(ExecutionError) as exc:
        await store.client_tools.create_call(
            "caller-a", attempt["id"], "client:fixture", 0, tool, {"value": 9007199254740993}, deadline,
        )
    assert exc.value.code == "invalid_request"
    assert exc.value.status == 400


@pytest.mark.asyncio
async def test_approval_control_result_binds_existing_id_and_call(client_attempt):
    h = client_attempt
    await connect(h)
    call = await create(h, approval=True)
    await delivery(h)
    value = {"outcome": "approval_required", "approval_id": h.approval_id,
             "subject_digest": "a" * 64, "reason": "blocking_wait_elapsed"}
    assert (await result(h, call, value={**value, "approval_id": str(uuid4())})).status_code == 409
    assert (await result(h, call, value=value)).status_code == 200
    outcome = await h.tools.wait("caller-a", h.id, call["id"])
    assert outcome.required_action == {"kind": "approval", "approval_id": h.approval_id,
                                       "subject_digest": "a" * 64, "call_id": call["id"]}


@pytest.mark.asyncio
async def test_call_is_persisted_even_before_client_connects(client_attempt):
    h = client_attempt
    operation = asyncio.create_task(h.tools.call("caller-a", h.id, h.tool, {"value": "happy"}, datetime.now(timezone.utc) + timedelta(seconds=2)))
    try:
        for _ in range(100):
            if await h.store.pool.fetchval("SELECT count(*) FROM kotoyose.execution_client_calls"):
                break
            await asyncio.sleep(0.01)
        await connect(h)
        call = (await delivery(h)).json()["calls"][0]
        assert (await result(h, call)).status_code == 200
        assert (await operation).state == "succeeded"
    finally:
        if not operation.done():
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)


@pytest.mark.asyncio
async def test_new_generation_can_reconcile_unknown_without_reexecuting_tool(client_attempt):
    h = client_attempt
    await connect(h)
    call = await create(h)
    await delivery(h)
    await h.store.finish(h.id, "cancelled", "caller_cancelled", "confirmed", None, {})
    await connect(h, 1)
    payload = {"schema_version": "1", "client_key": "client:fixture", "generation": 1,
        "tool_ref": call["tool_ref"], "arguments_digest": call["arguments_digest"],
        "disposition": "applied", "evidence_ref": "client-readback:expression-version-1", "result": {"expression": "happy"}}
    url = f"/v1/attempts/{h.id}/client-calls/{call['id']}/reconciliation"
    response = await h.client.post(url, json=payload)
    assert response.status_code == 200, response.text
    assert (await h.client.post(url, json=payload)).json() == response.json()
    assert (await h.client.post(url, json={**payload, "evidence_ref": "different-proof"})).status_code == 409
    assert (await result(h, call)).status_code == 401
    assert (await h.service.reconcile("caller-a", h.id))["side_effect_resolution"] == "resolved"
    assert await h.store.pool.fetchval("SELECT count(*) FROM kotoyose.execution_operations WHERE kind='client_tool'") == 1


@pytest.mark.asyncio
async def test_approval_wait_expires_mechanically_when_caller_is_disconnected(client_attempt):
    h = client_attempt
    await connect(h)
    call = await create(h, approval=True)
    outcome = await asyncio.wait_for(h.tools.wait("caller-a", h.id, call["id"]), timeout=3)
    assert outcome.required_action["approval_id"] == h.approval_id
    assert outcome.result["reason"] == "blocking_wait_elapsed"
    assert await h.store.pool.fetchval("SELECT state FROM kotoyose.execution_operations WHERE id=$1", call["operation_id"]) == "confirmed"
    events = await h.store.events("caller-a", h.id, 0, 100)
    control = next(event for event in events if event["type"] == "client_tool_result")["payload"]["control"]
    assert control["outcome"] == "approval_required"
    assert control["approval_id"] == h.approval_id
    assert (await result(h, call, value={**control, "outcome": "approved", "reason": "approved_within_grant"})).status_code == 409
