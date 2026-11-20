"""Cursor SDK の一時認証失効からの復旧テスト。"""
from types import SimpleNamespace

import pytest

from kotoyose.config import HarnessConfig
from kotoyose.runtime.base import RuntimeResult
from kotoyose.runtime.cursor import CursorRuntimeSession, _is_recoverable_auth_error


_AUTH_ERROR = "Authentication error If you are logged in, try logging out and back in"


class FakeClient:
    def __init__(self):
        self.closed = False

    async def aclose(self):
        self.closed = True


class FakeRun:
    def __init__(self, events, usage=None):
        self._events = events
        self.usage = usage
        self.id = "run-test"
        self.status = "running"
        self.cancel_count = 0

    async def events(self):
        for event in self._events:
            if event.result is not None:
                self.status = event.result.get("status", self.status)
            yield event
        self.status = "finished"

    async def wait(self):
        return SimpleNamespace(id=self.id, status=self.status, result="", usage=self.usage)

    async def cancel(self):
        self.cancel_count += 1
        self.status = "cancelled"


class FakeAgent:
    def __init__(self, agent_id, *, run=None, send_error=None):
        self.agent_id = agent_id
        self.run = run
        self.send_error = send_error
        self.closed = False
        self.prompts = []

    async def send(self, prompt):
        self.prompts.append(prompt)
        if self.send_error is not None:
            raise self.send_error
        return self.run

    async def close(self):
        self.closed = True


def assistant(text):
    return SimpleNamespace(
        sdk_message=SimpleNamespace(
            type="assistant",
            message=SimpleNamespace(content=[SimpleNamespace(text=text)]),
        ),
        result=None,
    )


def error_event(message):
    return SimpleNamespace(
        sdk_message=None,
        result={"status": "error", "message": message},
    )


def test_recoverable_auth_error_is_narrowly_matched():
    assert _is_recoverable_auth_error(_AUTH_ERROR)
    assert _is_recoverable_auth_error("rpc failed: ERROR_NOT_LOGGED_IN")
    assert not _is_recoverable_auth_error("Authentication error: invalid API key")


async def test_pre_send_auth_error_recycles_bridge_and_retries_once(monkeypatch):
    stale = FakeAgent("agent-stale", send_error=RuntimeError(_AUTH_ERROR))
    fresh = FakeAgent("agent-stale", run=FakeRun([assistant("ok")]))
    agents = [stale, fresh]
    clients = []
    calls = {"create": 0, "resume": 0}

    async def launch_bridge(**kwargs):
        client = FakeClient()
        clients.append(client)
        return client

    async def create(options, *, client, api_key=None):
        calls["create"] += 1
        assert api_key == "test-key"
        if calls["create"] == 1:
            agents.pop(0)
            raise RuntimeError(_AUTH_ERROR)
        return agents.pop(0)

    async def resume(agent_id, options, *, client):
        calls["resume"] += 1
        assert agent_id == "agent-stale"
        assert options.api_key == "test-key"
        return agents.pop(0)

    from kotoyose.config import Connection
    monkeypatch.setenv("FIXTURE_CURSOR_KEY", "test-key")
    monkeypatch.setattr(
        "kotoyose.runtime.cursor.get_config",
        lambda: HarnessConfig(connections={"cursor": Connection(provider="cursor", kind="api_key", data_policy="no_training", secret="env:FIXTURE_CURSOR_KEY")}),
    )
    monkeypatch.setattr("kotoyose.runtime.cursor.AsyncClient.launch_bridge", launch_bridge)
    monkeypatch.setattr("kotoyose.runtime.cursor.AsyncAgent.create", create)
    monkeypatch.setattr("kotoyose.runtime.cursor.AsyncAgent.resume", resume)

    session = CursorRuntimeSession(None, "/tmp/work")
    values = [item async for item in session.stream("hello")]

    assert values == ["ok", RuntimeResult("ok", None, "agent-stale", "cursor")]
    assert calls == {"create": 2, "resume": 0}
    assert len(clients) == 2
    assert clients[0].closed is True
    assert stale.closed is False
    assert stale.prompts == []
    assert fresh.prompts == ["hello"]


async def test_auth_error_after_visible_output_is_not_retried(monkeypatch):
    agent = FakeAgent(
        "agent-one",
        run=FakeRun([assistant("partial"), error_event(_AUTH_ERROR)]),
    )
    launches = 0

    async def launch_bridge(**kwargs):
        nonlocal launches
        launches += 1
        return FakeClient()

    async def create(options, *, client, api_key=None):
        return agent

    from kotoyose.config import Connection
    monkeypatch.setenv("FIXTURE_CURSOR_KEY", "test-key")
    monkeypatch.setattr(
        "kotoyose.runtime.cursor.get_config",
        lambda: HarnessConfig(connections={"cursor": Connection(provider="cursor", kind="api_key", data_policy="no_training", secret="env:FIXTURE_CURSOR_KEY")}),
    )
    monkeypatch.setattr("kotoyose.runtime.cursor.AsyncClient.launch_bridge", launch_bridge)
    monkeypatch.setattr("kotoyose.runtime.cursor.AsyncAgent.create", create)

    session = CursorRuntimeSession(None, "/tmp/work")
    seen = []
    with pytest.raises(RuntimeError, match="status error"):
        async for item in session.stream("hello"):
            seen.append(item)

    assert seen[0] == "partial"
    assert launches == 1
    assert agent.closed is True


async def test_auth_error_during_send_is_ambiguous_and_never_retried():
    agent = FakeAgent("agent-stale", send_error=RuntimeError(_AUTH_ERROR))
    session = CursorRuntimeSession(None, "/tmp/work")
    session._agent = agent
    with pytest.raises(RuntimeError, match="Cursor SDK request failed"):
        await session.ask("hello")
    assert agent.prompts == ["hello"]
    assert agent.closed
    assert session.last_outcome.status == "unknown"
    assert not session.last_outcome.stop_confirmed
