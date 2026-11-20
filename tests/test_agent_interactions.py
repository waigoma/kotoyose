"""Native agent interaction の非同期記録と終端状態を検証する。"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from kotoyose import agent_interactions, db
from kotoyose.runtime.base import RuntimeEvent


def _attempt():
    return {
        "id": "attempt-1", "generation": 3, "principal": "caller",
        "snapshot": {"model": "grok-test", "alias": "grok-coding", "provider": "cursor", "inputs": [
            {"kind": "text", "text": "Implement task"},
            {"kind": "image", "content": "must not persist"},
        ]},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("status,stop", [
    ("cancelled", "confirmed"), ("timeout", "confirmed"),
    ("succeeded", "unconfirmed"), ("failed", "confirmed"),
])
async def test_agent_interaction_terminal_failures_and_bounded_activity(monkeypatch, status, stop):
    received = AsyncMock(return_value=7)
    failed = AsyncMock()
    completed = AsyncMock()
    queue = asyncio.Queue()
    monkeypatch.setattr(agent_interactions, "record_received", received)
    monkeypatch.setattr(agent_interactions, "record_failed", failed)
    monkeypatch.setattr(agent_interactions, "record_completed", completed)
    monkeypatch.setattr(agent_interactions, "start_event_writer", lambda _: queue)

    interaction = agent_interactions.AgentInteraction(_attempt())
    interaction.emit(RuntimeEvent(kind="text", summary="x" * 1_000))
    interaction.finish(status, "reason", stop, b"", {"source": "unknown", "cost": None})
    await asyncio.gather(*list(agent_interactions._tasks))

    received.assert_awaited_once()
    assert received.await_args.kwargs["request_id"] == "native-agent:attempt-1:3"
    assert received.await_args.kwargs["conversation_key"] == "attempt:attempt-1"
    assert received.await_args.kwargs["source"] == "native-agent"
    # Recent activity shows the alias, like chat; the provider model id is kept beside it.
    assert received.await_args.kwargs["model"] == "grok-coding"
    assert received.await_args.kwargs["metadata"]["provider_model"] == "grok-test"
    assert received.await_args.kwargs["idempotent"] is True
    assert received.await_args.kwargs["prompt"] == "[text]\nImplement task"
    event = queue.get_nowait()
    assert event.kind == "text" and len(event.summary) == 500
    assert queue.get_nowait() is None
    completed.assert_not_awaited()
    assert status in failed.await_args.args[1]
    if stop == "unconfirmed":
        assert "stop unconfirmed" in failed.await_args.args[1]


@pytest.mark.asyncio
async def test_agent_interaction_success_records_completion_with_usage(monkeypatch):
    received = AsyncMock(return_value=7)
    failed = AsyncMock()
    completed = AsyncMock()
    monkeypatch.setattr(agent_interactions, "record_received", received)
    monkeypatch.setattr(agent_interactions, "record_failed", failed)
    monkeypatch.setattr(agent_interactions, "record_completed", completed)
    monkeypatch.setattr(agent_interactions, "start_event_writer", lambda _: asyncio.Queue())

    interaction = agent_interactions.AgentInteraction(_attempt())
    usage = {"source": "provider", "cost": 0.25, "input_tokens": 120, "output_tokens": 30}
    interaction.finish("succeeded", "completed", "confirmed", b"done", usage)
    await asyncio.gather(*list(agent_interactions._tasks))

    failed.assert_not_awaited()
    completed.assert_awaited_once()
    recorded = repr(completed.await_args)
    for expected in ("done", "120", "30", "0.25"):
        assert expected in recorded


@pytest.mark.asyncio
async def test_agent_interaction_db_failure_does_not_interrupt_attempt(monkeypatch):
    pool = MagicMock()
    pool.fetchrow = AsyncMock(side_effect=RuntimeError("db down"))
    completed = AsyncMock()
    monkeypatch.setattr(db, "_pool", pool)
    monkeypatch.setattr(agent_interactions, "record_completed", completed)
    interaction = agent_interactions.AgentInteraction(_attempt())
    interaction.finish("succeeded", "completed", "confirmed", b"ok", {})
    await asyncio.gather(*list(agent_interactions._tasks))
    pool.fetchrow.assert_awaited_once()
    completed.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["native", "legacy"])
async def test_resource_backed_inputs_appear_in_recorded_prompt(monkeypatch, backend):
    received = AsyncMock(return_value=None)
    monkeypatch.setattr(agent_interactions, "record_received", received)
    monkeypatch.setattr(db, "_pool", object())
    attempt = _attempt()
    attempt["snapshot"]["backend"] = backend
    attempt["snapshot"]["inputs"] = [
        {"kind": "instructions", "content_ref": "resource:instructions", "text": "Follow the task brief"},
        {"kind": "task_request", "content_ref": "resource:task", "text": "Implement feature"},
        {"kind": "repository_context", "content_ref": "resource:context", "text": "Relevant files"},
    ]

    agent_interactions.AgentInteraction(attempt)
    await asyncio.gather(*list(agent_interactions._tasks))

    assert received.await_args.kwargs["prompt"] == (
        "[instructions]\nFollow the task brief\n\n"
        "[task_request]\nImplement feature\n\n"
        "[repository_context]\nRelevant files"
    )


def test_input_prompt_is_truncated_to_text_limit():
    prompt = agent_interactions._input_prompt({"inputs": [
        {"kind": "instructions", "text": "x" * (agent_interactions._TEXT_LIMIT * 2)},
        {"kind": "task_request", "text": "must not appear"},
    ]})

    assert len(prompt) == agent_interactions._TEXT_LIMIT
    assert prompt == "[instructions]\n" + "x" * (agent_interactions._TEXT_LIMIT - len("[instructions]\n"))


def test_unresolved_input_uses_descriptive_fallback():
    assert agent_interactions._input_prompt({"inputs": [
        {"kind": "repository_context", "content_ref": "resource:missing", "size": 2048},
        {"kind": "image", "artifact_ref": "artifact:image", "size": 64, "text": "not sent to agent"},
    ]}) == "[repository_context: resource:missing (2048 bytes)]\n\n[image: artifact:image (64 bytes)]"
    assert agent_interactions._input_prompt({"inputs": []}) == "[agent attempt]"


@pytest.mark.asyncio
async def test_agent_interaction_without_alias_falls_back_to_provider_model(monkeypatch):
    received = AsyncMock(return_value=None)
    monkeypatch.setattr(agent_interactions, "record_received", received)
    monkeypatch.setattr(agent_interactions.db, "_pool", None)
    attempt = _attempt()
    del attempt["snapshot"]["alias"]
    interaction = agent_interactions.AgentInteraction(attempt)
    interaction.finish("succeeded", "completed", "confirmed", b"", {"source": "unknown", "cost": None})
    await asyncio.gather(*list(agent_interactions._tasks))
    assert received.await_args.kwargs["model"] == "grok-test"
