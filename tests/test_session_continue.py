"""引き継ぎ context、data policy、認可、route の優先順位の契約。"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from kotoyose.api_key_store import ApiKeyStore
from kotoyose.app import app
from kotoyose.config import Client, HarnessConfig
from kotoyose.models import ChatCompletionRequest, ChatCompletionResponse
from kotoyose.routes import chat
from kotoyose.routes import session_continue as routes
from kotoyose.session_continue import (
    DEFAULT_MAX_CONTEXT_BYTES, DEFAULT_MAX_TURNS, build_fork_metadata, build_handoff_context,
    check_handoff_data_policy, compose_fork_prompt, derive_origin_hint, new_fork_conversation_key,
    parse_turn_metadata,
)

NOW = datetime(2026, 11, 27, tzinfo=timezone.utc)
ADMIN = ("admin", "admin-password")
SECRET = "ghp_" + "a1B2c3D4e5" * 4


def turn(number=1, **changes):
    return {
        "id": number, "conversation_key": "import:source", "provider": "codex", "runtime_session_id": "thread",
        "claude_session_id": None, "prompt": f"question-{number}", "response": f"answer-{number}",
        "received_at": NOW, "deleted_at": None, "metadata": None, **changes,
    }


def build_config() -> HarnessConfig:
    connections, aliases, models = {}, {"safe": "no_training", "trains": "trains", "unknown": "unknown"}, {}
    for name, policy in aliases.items():
        connections[name] = {
            "provider": "claude", "kind": "endpoint", "data_policy": policy,
            "endpoint": f"https://{name}.invalid", "secret": "env:DEMO_GATEWAY_KEY",
        }
        models[name] = {"route": name, "agent": {"place": "isolated", "surface": "read"}}
    models["direct"] = {}
    models["header-auth"] = {"required_request_headers": ["Authorization"]}
    models["safe-pool"] = {"candidates": ["safe"], "policy": "first_available"}
    models["mixed-pool"] = {"candidates": ["safe", "trains"], "policy": "first_available"}
    return HarnessConfig.model_validate({
        "connections": connections,
        "routes": {name: {"driver": "claude.agent-sdk", "model": "demo", "connection": name} for name in aliases},
        "models": models,
        "clients": {
            "runner": Client(models=["*"]),
            "continuer": Client(models=["*"], scopes=["sessions:continue"]),
            "exporter": Client(models=["*"], scopes=["sessions:export"]),
            "narrow": Client(models=["safe"], scopes=["sessions:continue"]),
        },
    })


def completion(model="safe", content="continued") -> ChatCompletionResponse:
    return ChatCompletionResponse(
        id="chatcmpl-test", created=1, model=model,
        choices=[{"message": {"role": "assistant", "content": content}}],
        usage={"prompt_tokens": 1, "completion_tokens": 1},
    )


@pytest.fixture
def cfg(monkeypatch):
    config = build_config()
    monkeypatch.setattr("kotoyose.config._config", config)
    monkeypatch.setattr("kotoyose.auth.API_KEY", "admin-password")
    monkeypatch.setattr("kotoyose.auth.BASIC_AUTH_USER", "admin")
    monkeypatch.setattr(app.state, "api_key_store", ApiKeyStore())
    monkeypatch.setattr(app.state, "execution_service", None, raising=False)
    return config


@pytest.fixture
def client(cfg):
    return TestClient(app)


@pytest.fixture
def history(monkeypatch):
    """ソースの会話キーだけに turn を返し、fork の記録は空にする。"""
    rows = [turn(1), turn(2)]

    async def fetch(conversation_key, *args, **kwargs):
        return [] if conversation_key.startswith("fork:") else list(rows)

    unscoped = AsyncMock(side_effect=fetch)
    scoped = AsyncMock(side_effect=fetch)
    monkeypatch.setattr(routes, "get_session_turns", unscoped)
    monkeypatch.setattr(routes, "get_scoped_session_turns", scoped)
    return SimpleNamespace(rows=rows, unscoped=unscoped, scoped=scoped)


@pytest.fixture
def execute(monkeypatch):
    mock = AsyncMock(return_value=completion())
    monkeypatch.setattr(routes, "execute_continued_turn", mock)
    return mock


async def bearer(caller):
    _, token = await app.state.api_key_store.create(f"key-{caller}", caller)
    return {"Authorization": f"Bearer {token}"}


def body(**changes):
    return {"source_conversation_key": "import:source", "model": "safe", "message": "next step", **changes}


# --- handoff context ------------------------------------------------------------

def test_handoff_orders_turns_chronologically_with_frame():
    text, included, truncated = build_handoff_context([turn(1), turn(2)], parent_conversation_key="parent")
    assert (included, truncated) == (2, False)
    assert text == (
        "[Conversation Handoff Context]\n"
        "Prior conversation history from session 'parent' (included 2 of 2 turns). "
        "Continue seamlessly from where the conversation left off.\n\n"
        "--- Turn 1 (user) ---\nquestion-1\n--- Turn 1 (assistant) ---\nanswer-1\n"
        "--- Turn 2 (user) ---\nquestion-2\n--- Turn 2 (assistant) ---\nanswer-2\n"
        "[End of Handoff Context]"
    )


def test_handoff_keeps_most_recent_max_turns():
    turns = [turn(n) for n in range(1, DEFAULT_MAX_TURNS + 4)]
    text, included, truncated = build_handoff_context(turns, parent_conversation_key="p")
    assert (included, truncated) == (DEFAULT_MAX_TURNS, True)
    assert "question-3\n" not in text and "question-4\n" in text
    text, included, truncated = build_handoff_context(turns, parent_conversation_key="p", max_turns=1)
    assert (included, truncated) == (1, True)
    assert f"included 1 of {len(turns)} turns" in text and f"question-{len(turns)}\n" in text


def test_handoff_drops_oldest_turns_for_byte_budget():
    turns = [turn(n, response=str(n) * 700) for n in range(1, 6)]
    text, included, truncated = build_handoff_context(turns, parent_conversation_key="p", max_bytes=2000)
    assert (included, truncated) == (2, True)
    assert len(text.encode()) <= 2000
    assert "question-3" not in text
    assert text.index("question-4") < text.index("question-5")


@pytest.mark.parametrize("max_turns", [1, 5, 100])
def test_handoff_byte_budget_is_exact(max_turns):
    turns = [turn(n, response="界" * n) for n in range(1, 13)]
    full, count, _ = build_handoff_context(turns, parent_conversation_key="p", max_turns=max_turns, max_bytes=10**6)
    size = len(full.encode())
    assert build_handoff_context(turns, parent_conversation_key="p", max_turns=max_turns, max_bytes=size)[:2] == (
        full, count,
    )
    smaller, fewer, truncated = build_handoff_context(
        turns, parent_conversation_key="p", max_turns=max_turns, max_bytes=size - 1,
    )
    assert truncated and len(smaller.encode()) <= size - 1
    assert fewer == max(count - 1, 1)


@pytest.mark.parametrize("prompt,response", [
    ("界" * 4000, "short answer"), ("short question", "界" * 4000), ("問" * 4000, "答" * 4000),
])
def test_handoff_truncates_single_oversized_turn(prompt, response):
    text, included, truncated = build_handoff_context(
        [turn(1), turn(2, prompt=prompt, response=response)], parent_conversation_key="p", max_bytes=1024,
    )
    assert (included, truncated) == (1, True)
    assert len(text.encode()) <= 1024
    assert text.endswith("[End of Handoff Context]") and "...[truncated]" in text
    # Both sides of the turn survive.
    assert prompt[:5] in text and response[:5] in text


@pytest.mark.parametrize("max_bytes", [0, 10, 100, 1024])
def test_handoff_budget_holds_for_long_keys_and_tiny_budgets(max_bytes):
    text, _, truncated = build_handoff_context(
        [turn(1, prompt="x" * 5000)], parent_conversation_key="鍵" * 512, max_bytes=max_bytes,
    )
    assert truncated and len(text.encode()) <= max_bytes


def test_handoff_redacts_credentials_before_truncation():
    turns = [turn(1, prompt=f"token {SECRET}", response=f"Authorization: Bearer {SECRET}")]
    text, _, truncated = build_handoff_context(turns, parent_conversation_key="p")
    assert SECRET not in text and text.count("[REDACTED]") == 2 and not truncated
    turns = [turn(1, prompt="x" * 400 + SECRET + "y" * 5000)]
    for budget in range(560, 1100, 13):
        text, _, _ = build_handoff_context(turns, parent_conversation_key="p", max_bytes=budget)
        assert "ghp_" not in text


def test_handoff_skips_deleted_and_empty_turns():
    turns = [
        turn(1, deleted_at=NOW), turn(2, prompt="", response=None), turn(3, response=None),
    ]
    text, included, truncated = build_handoff_context(turns, parent_conversation_key="p")
    assert (included, truncated) == (1, False)
    assert "question-1" not in text and "question-3" in text and "included 1 of 1 turns" in text


def test_default_budget_and_fork_helpers():
    text, _, truncated = build_handoff_context([turn(1, response="x" * 60_000)], parent_conversation_key="p")
    assert truncated and len(text.encode()) <= DEFAULT_MAX_CONTEXT_BYTES == 32 * 1024
    assert compose_fork_prompt("context", "  go on \n") == "context\n\ngo on"
    keys = {new_fork_conversation_key() for _ in range(20)}
    assert len(keys) == 20 and all(key.startswith("fork:") and len(key) == 21 for key in keys)


def test_fork_metadata():
    values = dict(
        parent_conversation_key="p", parent_provider="codex", parent_runtime_session_id="thread",
        included_turns=2, total_turns=3, truncated=True,
    )
    expected = {
        "forked_from": "p", "parent_conversation_key": "p", "parent_provider": "codex",
        "parent_runtime_session_id": "thread", "handoff_turns_included": 2, "handoff_turns_total": 3,
        "handoff_truncated": True,
    }
    assert build_fork_metadata(**values, origin_hint=None) == expected
    assert build_fork_metadata(**values, origin_hint="work") == {**expected, "origin_hint": "work"}


# --- origin hint and data policy ------------------------------------------------

@pytest.mark.parametrize("metadata,expected", [
    ('{"a": 1}', {"a": 1}), ({"a": 1}, {"a": 1}), ("not json", {}), ("[1]", {}), (None, {}), (5, {}),
])
def test_parse_turn_metadata(metadata, expected):
    assert parse_turn_metadata({"metadata": metadata}) == expected


@pytest.mark.parametrize("hints,expected", [
    (["personal", "work"], "work"), (["work", "personal"], "work"), ([None, "personal"], "personal"),
    ([None, "other"], None), ([], None),
])
def test_origin_hint_fails_closed(hints, expected):
    turns = [turn(n, metadata={"origin_hint": hint} if hint else None) for n, hint in enumerate(hints)]
    assert derive_origin_hint(turns) == expected
    encoded = [turn(n, metadata=f'{{"origin_hint": "{hint}"}}') for n, hint in enumerate(hints)]
    assert derive_origin_hint(encoded) == expected


@pytest.mark.parametrize("alias,blocked", [
    ("safe", False), ("trains", True), ("unknown", True), ("direct", False),
    ("safe-pool", False), ("mixed-pool", True),
])
def test_work_history_requires_no_training(alias, blocked):
    config = build_config()
    reason = check_handoff_data_policy(config, alias, origin_hint="work")
    assert (reason is not None) is blocked
    if blocked:
        assert "work-origin" in reason and "data_policy=no_training" in reason and alias in reason
    assert check_handoff_data_policy(config, alias, origin_hint="personal") is None
    assert check_handoff_data_policy(config, alias, origin_hint=None) is None


# --- admin API ------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/admin/sessions/continue", "/admin/sessions/handoff-preview"])
@pytest.mark.parametrize("auth", [None, ("admin", "wrong")])
def test_admin_requires_auth(client, history, execute, path, auth):
    assert client.post(path, json=body(), auth=auth).status_code == 401
    history.unscoped.assert_not_awaited()
    execute.assert_not_awaited()


def preview_body(**changes):
    value = body(**changes)
    del value["message"]
    return value


def test_preview(client, history, execute):
    history.rows[0]["metadata"] = {"origin_hint": "work"}
    result = client.post("/admin/sessions/handoff-preview", auth=ADMIN, json=preview_body(
        model="trains", max_turns=1, source_provider="codex", source_runtime_session_id="thread",
    ))
    assert result.status_code == 200
    data = result.json()
    expected, _, _ = build_handoff_context(history.rows, parent_conversation_key="import:source", max_turns=1)
    assert data.pop("policy_blocked_reason").startswith("Cannot hand off work-origin")
    assert data == {
        "source_conversation_key": "import:source", "model": "trains", "handoff_turns_included": 1,
        "handoff_turns_total": 2, "handoff_truncated": True, "preview_bytes": len(expected.encode()),
        "origin_hint": "work",
    }
    history.unscoped.assert_any_await("import:source", "thread", "codex")
    history.unscoped.assert_any_await("import:source")
    allowed = client.post("/admin/sessions/handoff-preview", auth=ADMIN, json=preview_body()).json()
    assert allowed["policy_blocked_reason"] is None
    execute.assert_not_awaited()


@pytest.mark.parametrize("path", ["/admin/sessions/continue", "/admin/sessions/handoff-preview"])
@pytest.mark.parametrize("rows", [[], [turn(1, deleted_at=NOW)]])
def test_admin_missing_session(client, history, execute, path, rows):
    history.rows[:] = rows
    payload = body() if path.endswith("continue") else preview_body()
    result = client.post(path, auth=ADMIN, json=payload)
    assert result.status_code == 404
    assert result.json()["error"]["message"] == "Source session not found or has no turns"
    execute.assert_not_awaited()


def test_admin_continue(client, history, execute):
    history.rows[0]["metadata"] = '{"origin_hint": "work"}'
    result = client.post("/admin/sessions/continue", auth=ADMIN, json=body(reasoning_effort="high"))
    assert result.status_code == 200
    data = result.json()
    key = data.pop("conversation_key")
    assert key.startswith("fork:")
    assert data == {
        "parent_conversation_key": "import:source", "model": "safe", "provider": "claude",
        "handoff_turns_included": 2, "handoff_turns_total": 2, "handoff_truncated": False,
        "origin_hint": "work", "response": "continued",
    }
    chat_request, _, conversation_key = execute.await_args.args
    assert conversation_key == key
    assert (chat_request.model, chat_request.stream, chat_request.reasoning_effort) == ("safe", False, "high")
    [message] = chat_request.messages
    assert message.content.startswith("[Conversation Handoff Context]")
    assert message.content.endswith("[End of Handoff Context]\n\nnext step")
    assert execute.await_args.kwargs == {"source_override": "continue", "extra_metadata": {
        "forked_from": "import:source", "parent_conversation_key": "import:source", "parent_provider": "codex",
        "parent_runtime_session_id": "thread", "handoff_turns_included": 2, "handoff_turns_total": 2,
        "handoff_truncated": False, "origin_hint": "work",
    }}


def test_admin_continue_prefers_recorded_provider(client, history, execute, monkeypatch):
    async def fetch(conversation_key, *args):
        return [turn(9, provider="recorded")] if conversation_key.startswith("fork:") else [turn(1)]

    monkeypatch.setattr(routes, "get_session_turns", fetch)
    assert client.post("/admin/sessions/continue", auth=ADMIN, json=body()).json()["provider"] == "recorded"


def test_provider_lookup_failure_keeps_reply(client, history, execute, monkeypatch):
    async def fetch(conversation_key, *args):
        if conversation_key.startswith("fork:"):
            raise OSError("database unavailable")
        return [turn(1)]

    monkeypatch.setattr(routes, "get_session_turns", fetch)
    result = client.post("/admin/sessions/continue", auth=ADMIN, json=body())
    assert result.status_code == 200
    assert (result.json()["provider"], result.json()["response"]) == ("claude", "continued")


@pytest.mark.parametrize("model", ["trains", "unknown", "mixed-pool"])
def test_admin_continue_blocks_work_history(client, history, execute, model):
    history.rows[0]["metadata"] = {"origin_hint": "personal"}
    history.rows[1]["metadata"] = {"origin_hint": "work"}
    # max_turns で work の turn が引き継ぎ対象の外でも拒否する。
    history.rows.append(turn(3))
    result = client.post("/admin/sessions/continue", auth=ADMIN, json=body(model=model, max_turns=1))
    assert result.status_code == 403
    assert "data_policy=no_training" in result.json()["error"]["message"]
    execute.assert_not_awaited()


def test_admin_continue_allows_personal_history(client, history, execute):
    history.rows[0]["metadata"] = {"origin_hint": "personal"}
    assert client.post("/admin/sessions/continue", auth=ADMIN, json=body(model="trains")).status_code == 200


def test_admin_continue_unknown_model(client, history, execute):
    assert client.post("/admin/sessions/continue", auth=ADMIN, json=body(model="missing")).status_code == 404
    execute.assert_not_awaited()


def test_admin_continue_preserves_upstream_error(client, history, execute):
    execute.return_value = JSONResponse(status_code=503, content={"error": {"message": "unavailable"}})
    result = client.post("/admin/sessions/continue", auth=ADMIN, json=body())
    assert result.status_code == 503
    assert result.json() == {"error": {"message": "unavailable"}}


@pytest.mark.parametrize("changes", [
    {"message": "   \n"}, {"message": ""}, {"extra": 1}, {"max_turns": 0}, {"max_turns": 101},
    {"max_context_bytes": 1023}, {"max_context_bytes": 131073}, {"source_conversation_key": ""},
])
def test_request_validation(client, history, execute, changes):
    assert client.post("/admin/sessions/continue", auth=ADMIN, json=body(**changes)).status_code == 422
    execute.assert_not_awaited()


# --- client API -----------------------------------------------------------------

def test_client_requires_api_key(client, history, execute):
    assert client.post("/v1/sessions/continue", json=body()).status_code == 401
    assert client.post("/v1/sessions/continue", json=body(), auth=ADMIN).status_code == 401
    history.unscoped.assert_not_awaited()


@pytest.mark.asyncio
async def test_client_model_scope(client, history, execute):
    result = client.post("/v1/sessions/continue", headers=await bearer("narrow"), json=body(model="trains"))
    assert result.status_code == 403
    assert "outside this API key's client scope" in result.json()["error"]["message"]
    history.unscoped.assert_not_awaited()
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_client_import_requires_scope(client, history, execute):
    result = client.post("/v1/sessions/continue", headers=await bearer("runner"), json=body())
    assert result.status_code == 403
    assert "sessions:continue" in result.json()["error"]["message"]
    history.unscoped.assert_not_awaited()
    history.scoped.assert_not_awaited()
    execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", ["continuer", "exporter"])
async def test_client_import_with_scope(client, history, execute, caller):
    result = client.post("/v1/sessions/continue", headers=await bearer(caller), json=body())
    assert result.status_code == 200
    assert result.json()["conversation_key"].startswith("fork:")
    history.scoped.assert_not_awaited()
    history.unscoped.assert_any_await("import:source", None, None)


@pytest.mark.asyncio
async def test_client_continues_own_session_without_import_scope(client, history, execute):
    result = client.post("/v1/sessions/continue", headers=await bearer("runner"), json=body(
        source_conversation_key="own", source_provider="codex", source_runtime_session_id="thread",
    ))
    assert result.status_code == 200
    history.scoped.assert_any_await("own", "key-runner", "thread", "codex")
    history.scoped.assert_any_await("own", "key-runner")
    # 引き継ぎ元は key で絞った読み取りだけを使う。
    assert all(call.args[0].startswith("fork:") for call in history.unscoped.await_args_list)


@pytest.mark.asyncio
async def test_client_cannot_continue_another_keys_session(client, history, execute):
    history.rows[:] = []
    result = client.post("/v1/sessions/continue", headers=await bearer("runner"), json=body(
        source_conversation_key="someone-else",
    ))
    assert result.status_code == 404
    execute.assert_not_awaited()


# --- real chat path -------------------------------------------------------------

@pytest.fixture
def runtime(monkeypatch):
    ports = SimpleNamespace(
        session=AsyncMock(return_value=(AsyncMock(), False)),
        ask=AsyncMock(return_value=("real answer", None, None)),
        received=AsyncMock(return_value=None),
    )
    monkeypatch.setattr(chat, "get_or_resume_client", ports.session)
    monkeypatch.setattr(chat, "ask_claude", ports.ask)
    monkeypatch.setattr(chat, "record_received", ports.received)
    return ports


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["admin", "client"])
async def test_continue_runs_through_chat_and_records_fork(client, history, runtime, surface):
    history.rows[0]["metadata"] = {"origin_hint": "personal"}
    options = {"auth": ADMIN} if surface == "admin" else {"headers": await bearer("continuer")}
    path = "/admin/sessions/continue" if surface == "admin" else "/v1/sessions/continue"
    options.setdefault("headers", {})["x-memory-source"] = "spoofed"
    result = client.post(path, json=body(model="direct"), **options)
    assert result.status_code == 200, result.text
    assert result.json()["response"] == "real answer"
    recorded = runtime.received.await_args.kwargs
    assert recorded["conversation_key"] == result.json()["conversation_key"]
    assert recorded["source"] == "continue"
    assert recorded["api_key_name"] == (None if surface == "admin" else "key-continuer")
    assert recorded["metadata"]["parent_conversation_key"] == "import:source"
    assert recorded["metadata"]["origin_hint"] == "personal"
    assert "is_subagent" not in recorded["metadata"]
    assert "question-2" in recorded["prompt"] and recorded["prompt"].endswith("next step")


def test_admin_continue_does_not_forward_admin_credentials(client, history, runtime):
    result = client.post("/admin/sessions/continue", auth=ADMIN, json=body(model="header-auth"))
    assert result.status_code == 501
    runtime.session.assert_not_awaited()


def test_continue_refuses_native_execution(client, cfg, history, runtime, monkeypatch):
    monkeypatch.setattr(app.state, "execution_service", object())
    monkeypatch.setattr(type(cfg.compatibility_config), "has_model", lambda self, model: True)
    result = client.post("/admin/sessions/continue", auth=ADMIN, json=body(model="direct"))
    assert result.status_code == 501
    assert result.json()["error"]["code"] == "unsupported_capability"
    runtime.received.assert_not_awaited()


@pytest.mark.asyncio
async def test_continue_resolves_pool_candidate(cfg, monkeypatch):
    inner = AsyncMock(return_value=completion("safe"))
    resolve = AsyncMock(side_effect=lambda request, req: (req.model_copy(update={"model": "safe"}), "safe"))
    monkeypatch.setattr(chat, "_chat_completions", inner)
    monkeypatch.setattr(chat, "_resolve_pool_model", resolve)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()), state=SimpleNamespace(), headers={})
    req = ChatCompletionRequest(model="safe-pool", messages=[{"role": "user", "content": "hi"}])
    result = await chat.execute_continued_turn(req, request, "fork:abc", extra_metadata={"a": 1})
    assert result.resolved_model == "safe"
    assert inner.await_args.args[0].model == "safe"
    assert inner.await_args.kwargs == {"extra_metadata": {"a": 1}, "source_override": "continue"}


@pytest.mark.parametrize("delegated", [False, True])
@pytest.mark.parametrize("extra", [None, {"origin_hint": "work", "is_subagent": False}])
def test_chat_metadata_and_source_merge(client, runtime, delegated, extra):
    request = SimpleNamespace(
        app=app, headers={"x-memory-source": "discord"},
        state=SimpleNamespace(execution_principal=None),
    )
    delegation_metadata = {"parent": "p"} if delegated else None

    async def run():
        return await chat._chat_completions(
            ChatCompletionRequest(model="direct", messages=[{"role": "user", "content": "hi"}]),
            request, JSONResponse({}), "key",
            delegation_metadata=delegation_metadata, extra_metadata=extra,
        )

    import asyncio

    asyncio.run(run())
    recorded = runtime.received.await_args.kwargs
    assert recorded["source"] == "discord"
    if delegated:
        assert recorded["metadata"] == {
            **(extra or {}), "parent": "p", "is_subagent": True, "agent_origin": "delegation",
        }
    else:
        assert recorded.get("metadata") == extra


# --- route precedence -----------------------------------------------------------

@pytest.mark.parametrize("path,endpoint", [
    ("/admin/sessions/continue", routes.admin_continue_session),
    ("/admin/sessions/handoff-preview", routes.preview_handoff),
    ("/v1/sessions/continue", routes.continue_session),
])
def test_routes_are_not_shadowed(path, endpoint):
    from starlette.routing import Match

    scope = {"type": "http", "method": "POST", "path": path, "root_path": "", "headers": []}
    matched = next(route for route in app.router.routes if route.matches(scope)[0] is Match.FULL)
    assert matched.endpoint is endpoint
    # GET の {conversation_key:path} より前に登録されている。
    get_scope = {**scope, "method": "GET"}
    first = next(route for route in app.router.routes if route.matches(get_scope)[0] is not Match.NONE)
    assert first.endpoint is endpoint
