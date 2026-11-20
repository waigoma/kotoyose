"""SSE stream endpoint (`GET /v1/attempts/{attempt_id}/events/stream`) tests."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
import httpx
import pytest

from kotoyose.app import execution_exception_handler
from kotoyose.execution_resources import ExecutionError, NativeExecutionConfig
from kotoyose.routes import executions as executions_routes
from tests.fixtures.loop_limits import isolated_limits

from tests.fixtures.api_keys import sync_fixture_key_store


class InMemoryExecutionStore:
    """In-memory ExecutionStore stub matching ExecutionStore.attempt / events."""

    def __init__(self) -> None:
        self._attempts: dict[str, dict] = {}
        self._events: dict[str, list[dict]] = {}
        self.events_calls: list[tuple[str, str, int, int]] = []

    def create_attempt(
        self,
        attempt_id: str,
        principal: str,
        *,
        status: str = "running",
        finished_at: datetime | None = None,
    ) -> dict:
        attempt = {
            "id": attempt_id,
            "principal": principal,
            "status": status,
            "sequence": 0,
            "created_at": datetime.now(timezone.utc),
            "finished_at": finished_at,
        }
        self._attempts[attempt_id] = attempt
        self._events[attempt_id] = []
        return attempt

    def add_event(
        self,
        attempt_id: str,
        event_type: str | None,
        payload: dict,
        *,
        use_kind_key: bool = False,
    ) -> dict:
        attempt = self._attempts[attempt_id]
        seq = attempt["sequence"] + 1
        attempt["sequence"] = seq
        record: dict = {
            "attempt_id": attempt_id,
            "sequence": seq,
            "payload": payload,
            "created_at": datetime.now(timezone.utc),
        }
        if event_type is not None:
            record["kind" if use_kind_key else "type"] = event_type
        self._events[attempt_id].append(record)
        return record

    def finish_attempt(
        self,
        attempt_id: str,
        status: str = "succeeded",
        payload: dict | None = None,
        *,
        emit_event: bool = True,
    ) -> None:
        attempt = self._attempts[attempt_id]
        attempt["status"] = status
        attempt["finished_at"] = datetime.now(timezone.utc)
        if emit_event:
            self.add_event(attempt_id, status, payload or {"reason": "completed"})

    async def attempt(self, principal: str, attempt_id: str) -> dict:
        row = self._attempts.get(attempt_id)
        if row is None or row["principal"] != principal:
            raise ExecutionError("invalid_request", "Unknown attempt", 404)
        return dict(row)

    async def events(self, principal: str, attempt_id: str, after: int, limit: int) -> list[dict]:
        self.events_calls.append((principal, attempt_id, after, limit))
        await self.attempt(principal, attempt_id)
        matching = [dict(item) for item in self._events[attempt_id] if item["sequence"] > after]
        return matching[:limit]


def _parse_sse(raw: str) -> list[dict]:
    """Parse raw SSE wire text into frames (comments and events)."""
    frames: list[dict] = []
    for block in raw.split("\n\n"):
        if not block.strip():
            continue
        lines = block.split("\n")
        if all(line.startswith(":") for line in lines):
            frames.append({"comment": [line[1:].strip() for line in lines]})
            continue
        frame: dict = {"data_lines": []}
        for line in lines:
            if line.startswith(":"):
                continue
            field, _, value = line.partition(":")
            if value.startswith(" "):
                value = value[1:]
            if field == "id":
                frame["id"] = value
            elif field == "event":
                frame["event"] = value
            elif field == "data":
                frame["data_lines"].append(value)
        frame["raw_data"] = "\n".join(frame.pop("data_lines"))
        frame["data"] = json.loads(frame["raw_data"]) if frame["raw_data"] else None
        frames.append(frame)
    return frames


@pytest.fixture
def sse_app(monkeypatch, tmp_path):
    from kotoyose import config as config_module
    from kotoyose.config import Client, HarnessConfig
    monkeypatch.setenv("TEST_SSE_TOKEN_A", "token-caller-a")
    monkeypatch.setenv("TEST_SSE_TOKEN_B", "token-caller-b")
    monkeypatch.setattr(config_module, "_config", HarnessConfig(clients={
        "caller-a": Client(

            workspace_roots=[tmp_path],
            max_run_seconds=30,
            max_memory_bytes=64 * 1024 * 1024,
            max_processes=16,
            max_cpus=1,
            max_concurrent_runs=2,
        ),
        "caller-b": Client(

            workspace_roots=[tmp_path],
            max_run_seconds=30,
            max_memory_bytes=64 * 1024 * 1024,
            max_processes=16,
            max_cpus=1,
            max_concurrent_runs=2,
        ),
    }))
    config = NativeExecutionConfig.model_validate({
        "schema_version": "1",
        "image_id": "sha256:" + ("a" * 64),
    })
    store = InMemoryExecutionStore()
    app = FastAPI()
    app.state.execution_service = SimpleNamespace(config=config, store=store)
    app.state.api_key_store = sync_fixture_key_store(**{'caller-a': 'token-caller-a', 'caller-b': 'token-caller-b'})
    app.add_exception_handler(ExecutionError, execution_exception_handler)
    app.include_router(executions_routes.router)
    return app, store


@pytest.mark.asyncio
async def test_backlog_then_live_events_and_end_event(sse_app, monkeypatch):
    app, store = sse_app
    monkeypatch.setattr(executions_routes, "SSE_POLL_MIN_SECONDS", 0.01)
    monkeypatch.setattr(executions_routes, "SSE_POLL_MAX_SECONDS", 0.04)

    store.create_attempt("att-1", "caller-a", status="running")
    store.add_event("att-1", "dispatched", {"note": "日本語テスト"})
    store.add_event("att-1", "running", {"step": 1})

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer kys_token-caller-a"},
    ) as client:
        expected_events = (await client.get("/v1/attempts/att-1/events")).json()["events"]

        async def produce_live() -> None:
            await asyncio.sleep(0.03)
            store.add_event("att-1", "progress", {"step": 2, "text": "進行中"})
            await asyncio.sleep(0.03)
            store.finish_attempt("att-1", "succeeded", {"reason": "done"})

        producer = asyncio.create_task(produce_live())
        try:
            async with client.stream("GET", "/v1/attempts/att-1/events/stream") as response:
                assert response.status_code == 200
                assert "text/event-stream" in response.headers["content-type"]
                assert response.headers["cache-control"] == "no-store"
                assert response.headers["x-accel-buffering"] == "no"
                raw = (await response.aread()).decode("utf-8")
        finally:
            await producer

        all_events = (await client.get("/v1/attempts/att-1/events")).json()["events"]

    assert "日本語テスト" in raw
    assert "進行中" in raw
    assert "\\u65e5" not in raw

    frames = _parse_sse(raw)
    event_frames = [f for f in frames if "id" in f]
    end_frames = [f for f in frames if f.get("event") == "end"]

    assert len(event_frames) == 4
    assert [int(f["id"]) for f in event_frames] == [1, 2, 3, 4]
    assert [f["id"] for f in event_frames] == [str(f["data"]["sequence"]) for f in event_frames]
    assert [f["event"] for f in event_frames] == ["dispatched", "running", "progress", "succeeded"]
    assert [f["data"] for f in event_frames[:2]] == expected_events
    assert [f["data"] for f in event_frames] == all_events
    # Compact JSON (no whitespace after separator outside strings).
    assert '":' not in event_frames[1]["raw_data"].replace(" ", "") or ": " not in event_frames[1]["raw_data"]
    assert ", " not in event_frames[1]["raw_data"]

    assert len(end_frames) == 1
    assert end_frames[0]["data"] == {"status": "succeeded", "sequence": 4}
    assert "id" not in end_frames[0]


@pytest.mark.asyncio
async def test_after_and_last_event_id_resume(sse_app):
    app, store = sse_app
    store.create_attempt("att-2", "caller-a", status="running")
    store.add_event("att-2", "dispatched", {})
    store.add_event("att-2", "running", {})
    store.add_event("att-2", "progress", {"n": 3})
    store.finish_attempt("att-2", "succeeded", {"n": 4})

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer kys_token-caller-a"},
    ) as client:
        # Query `after` alone.
        res_after = await client.get("/v1/attempts/att-2/events/stream?after=2")
        assert res_after.status_code == 200
        frames_after = _parse_sse(res_after.text)
        assert [int(f["id"]) for f in frames_after if "id" in f] == [3, 4]
        assert frames_after[-1]["event"] == "end"
        assert frames_after[-1]["data"] == {"status": "succeeded", "sequence": 4}

        # `Last-Event-ID` alone.
        res_header = await client.get("/v1/attempts/att-2/events/stream", headers={"Last-Event-ID": "3"})
        frames_header = _parse_sse(res_header.text)
        assert [int(f["id"]) for f in frames_header if "id" in f] == [4]
        assert frames_header[-1]["data"] == {"status": "succeeded", "sequence": 4}

        # Both present and valid -> Last-Event-ID wins (even when smaller than after, including 0).
        res_both = await client.get("/v1/attempts/att-2/events/stream?after=1", headers={"Last-Event-ID": "3"})
        frames_both = _parse_sse(res_both.text)
        assert [int(f["id"]) for f in frames_both if "id" in f] == [4]

        res_zero = await client.get("/v1/attempts/att-2/events/stream?after=3", headers={"Last-Event-ID": "0"})
        frames_zero = _parse_sse(res_zero.text)
        assert [int(f["id"]) for f in frames_zero if "id" in f] == [1, 2, 3, 4]

        # Invalid Last-Event-ID falls back to `after`.
        for bad in ("not-a-number", "-1", "", "1.5"):
            res_invalid = await client.get(
                "/v1/attempts/att-2/events/stream?after=2",
                headers={"Last-Event-ID": bad},
            )
            frames_invalid = _parse_sse(res_invalid.text)
            assert [int(f["id"]) for f in frames_invalid if "id" in f] == [3, 4]

        # Already caught up -> emits only `end` with last sequence.
        res_caught_up = await client.get("/v1/attempts/att-2/events/stream", headers={"Last-Event-ID": "4"})
        frames_caught_up = _parse_sse(res_caught_up.text)
        assert len(frames_caught_up) == 1
        assert frames_caught_up[0]["event"] == "end"
        assert frames_caught_up[0]["data"] == {"status": "succeeded", "sequence": 4}


@pytest.mark.asyncio
async def test_ownership_404_matches_missing_attempt_and_auth_checks(sse_app):
    app, store = sse_app
    store.create_attempt("att-owned", "caller-a", status="running")

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        missing = await client.get(
            "/v1/attempts/no-such-attempt/events/stream",
            headers={"Authorization": "Bearer kys_token-caller-a"},
        )
        other_owner = await client.get(
            "/v1/attempts/att-owned/events/stream",
            headers={"Authorization": "Bearer kys_token-caller-b"},
        )
        assert missing.status_code == 404
        assert other_owner.status_code == 404
        assert other_owner.json()["error"]["code"] == missing.json()["error"]["code"]
        assert other_owner.json()["error"]["message"] == missing.json()["error"]["message"]

        unauth = await client.get("/v1/attempts/att-owned/events/stream")
        assert unauth.status_code == 401

        app.state.execution_service = None
        unconfigured = await client.get("/v1/attempts/att-owned/events/stream",
                                        headers={"Authorization": "Bearer kys_token-caller-a"})
        assert unconfigured.status_code == 501


@pytest.mark.asyncio
async def test_event_kind_fallback_and_large_backlog_pagination(sse_app, monkeypatch):
    app, store = sse_app
    monkeypatch.setattr(executions_routes, "SSE_BATCH_LIMIT", 2)

    store.create_attempt("att-3", "caller-a", status="running")
    store.add_event("att-3", None, {"msg": "no-kind"})
    store.add_event("att-3", "custom_kind", {"msg": "with-kind"}, use_kind_key=True)
    store.add_event("att-3", "progress", {"msg": "p1"})
    store.add_event("att-3", "progress", {"msg": "p2"})
    store.finish_attempt("att-3", "failed", {"reason": "boom"})

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer kys_token-caller-a"},
    ) as client:
        res = await client.get("/v1/attempts/att-3/events/stream")
        assert res.status_code == 200

    frames = _parse_sse(res.text)
    event_frames = [f for f in frames if "id" in f]
    assert [int(f["id"]) for f in event_frames] == [1, 2, 3, 4, 5]
    assert [f["event"] for f in event_frames] == ["event", "custom_kind", "progress", "progress", "failed"]
    assert frames[-1]["event"] == "end"
    assert frames[-1]["data"] == {"status": "failed", "sequence": 5}


@pytest.mark.asyncio
async def test_heartbeat_and_idle_backoff(sse_app, monkeypatch):
    app, store = sse_app
    monkeypatch.setattr(executions_routes, "SSE_POLL_MIN_SECONDS", 0.02)
    monkeypatch.setattr(executions_routes, "SSE_POLL_MAX_SECONDS", 0.06)
    monkeypatch.setattr(executions_routes, "SSE_HEARTBEAT_SECONDS", 0.03)

    store.create_attempt("att-hb", "caller-a", status="running")
    store.add_event("att-hb", "dispatched", {})

    async def finish_later() -> None:
        await asyncio.sleep(0.16)
        store.finish_attempt("att-hb", "succeeded", {})

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer kys_token-caller-a"},
    ) as client:
        task = asyncio.create_task(finish_later())
        try:
            res = await client.get("/v1/attempts/att-hb/events/stream")
        finally:
            await task

    assert res.status_code == 200
    assert ": keep-alive\n\n" in res.text
    frames = _parse_sse(res.text)
    assert any("comment" in f and f["comment"] == ["keep-alive"] for f in frames)
    event_frames = [f for f in frames if "id" in f]
    assert [int(f["id"]) for f in event_frames] == [1, 2]
    assert frames[-1]["event"] == "end"
    assert frames[-1]["data"] == {"status": "succeeded", "sequence": 2}


@pytest.mark.asyncio
async def test_client_disconnect_stops_generator(monkeypatch):
    monkeypatch.setattr(executions_routes, "SSE_POLL_MIN_SECONDS", 0.01)
    monkeypatch.setattr(executions_routes, "SSE_POLL_MAX_SECONDS", 0.02)
    monkeypatch.setattr(executions_routes, "SSE_HEARTBEAT_SECONDS", 5.0)

    store = InMemoryExecutionStore()
    initial = store.create_attempt("att-disc", "caller-a", status="running")
    store.add_event("att-disc", "dispatched", {})

    calls = 0

    async def is_disconnected() -> bool:
        nonlocal calls
        calls += 1
        return calls >= 3

    fake_request = SimpleNamespace(is_disconnected=is_disconnected)
    chunks: list[str] = []
    async for chunk in executions_routes._attempt_event_stream(
        store, "caller-a", "att-disc", 0, fake_request, initial,
    ):
        chunks.append(chunk)

    frames = _parse_sse("".join(chunks))
    assert [int(f["id"]) for f in frames if "id" in f] == [1]
    assert not any(f.get("event") == "end" for f in frames)
    assert calls >= 3
    polls_at_stop = len(store.events_calls)
    await asyncio.sleep(0.05)
    assert len(store.events_calls) == polls_at_stop


@pytest.mark.asyncio
async def test_max_stream_lifetime_closes_without_end_event(sse_app, monkeypatch):
    app, store = sse_app
    monkeypatch.setattr(executions_routes, "SSE_POLL_MIN_SECONDS", 0.01)
    monkeypatch.setattr(executions_routes, "SSE_POLL_MAX_SECONDS", 0.02)
    monkeypatch.setattr(executions_routes, "SSE_MAX_LIFETIME_SECONDS", 0.05)

    store.create_attempt("att-life", "caller-a", status="running")
    store.add_event("att-life", "dispatched", {})

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer kys_token-caller-a"},
    ) as client:
        res = await client.get("/v1/attempts/att-life/events/stream")

    assert res.status_code == 200
    frames = _parse_sse(res.text)
    assert [int(f["id"]) for f in frames if "id" in f] == [1]
    assert not any(f.get("event") == "end" for f in frames)


@pytest.mark.asyncio
async def test_terminal_transition_race_drains_remaining_events(monkeypatch):
    monkeypatch.setattr(executions_routes, "SSE_POLL_MIN_SECONDS", 0.01)
    store = InMemoryExecutionStore()
    initial = store.create_attempt("att-race", "caller-a", status="running")
    store.add_event("att-race", "dispatched", {})

    original_attempt = store.attempt
    flipped = False

    async def racing_attempt(principal: str, attempt_id: str) -> dict:
        nonlocal flipped
        if not flipped:
            flipped = True
            store.finish_attempt("att-race", "succeeded", {"race": True})
        return await original_attempt(principal, attempt_id)

    store.attempt = racing_attempt  # type: ignore[method-assign]
    fake_request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))

    chunks: list[str] = []
    async for chunk in executions_routes._attempt_event_stream(
        store, "caller-a", "att-race", 0, fake_request, initial,
    ):
        chunks.append(chunk)

    frames = _parse_sse("".join(chunks))
    event_frames = [f for f in frames if "id" in f]
    assert [int(f["id"]) for f in event_frames] == [1, 2]
    assert [f["event"] for f in event_frames] == ["dispatched", "succeeded"]
    assert frames[-1]["event"] == "end"
    assert frames[-1]["data"] == {"status": "succeeded", "sequence": 2}


@pytest.mark.asyncio
async def test_db_backed_attempt_events_stream(tmp_path, monkeypatch):
    """DB-backed integration test using isolated_limits when test DB is configured."""
    url = os.environ.get("KOTOYOSE_EXECUTION_TEST_DATABASE_URL")
    if not url:
        pytest.skip("KOTOYOSE_EXECUTION_TEST_DATABASE_URL is not set")

    monkeypatch.setattr(executions_routes, "SSE_POLL_MIN_SECONDS", 0.01)
    monkeypatch.setattr(executions_routes, "SSE_POLL_MAX_SECONDS", 0.04)

    from tests.test_execution_store import _admit_ready

    async with isolated_limits(url) as (store, _service, app):
        _, _, request_payload, snapshot = await _admit_ready(store, tmp_path)
        attempt, _ = await store.admit("caller-a", request_payload, snapshot, 1)
        attempt_id = attempt["id"]
        assert await store.mark_running(attempt_id)

        async def finish_later() -> None:
            await asyncio.sleep(0.03)
            await store.append_event(attempt_id, "progress", {"step": 1})
            await store.finish(attempt_id, "succeeded", "ok", "confirmed", 0, {})

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer kys_caller-a"},
        ) as client:
            task = asyncio.create_task(finish_later())
            try:
                res = await client.get(f"/v1/attempts/{attempt_id}/events/stream")
            finally:
                await task
            assert res.status_code == 200
            polled = (await client.get(f"/v1/attempts/{attempt_id}/events")).json()["events"]
            denied = await client.get(
                f"/v1/attempts/{attempt_id}/events/stream",
                headers={"Authorization": "Bearer kys_caller-b"},
            )
            assert denied.status_code == 404

        frames = _parse_sse(res.text)
        event_frames = [f for f in frames if "id" in f]
        assert [f["data"] for f in event_frames] == polled
        assert [int(f["id"]) for f in event_frames] == [e["sequence"] for e in polled]
        assert frames[-1]["event"] == "end"
        assert frames[-1]["data"] == {"status": "succeeded", "sequence": polled[-1]["sequence"]}
