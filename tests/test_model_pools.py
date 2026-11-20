"""pool alias: 設定の検証、状態の遷移、戦略、受理時の選択と pin、公開。"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from pydantic import ValidationError

from kotoyose import pool_strategies
from kotoyose.agent_sanction import route_digest
from kotoyose.config import AgentRoute, AliasAgent, Connection, HarnessConfig, ModelPreset, get_config, save_config
from kotoyose.execution_admission import ExecutionAdmission
from kotoyose.execution_contract import AgentExecution
from kotoyose.execution_limits import ProviderUsageLimit
from kotoyose.execution_resources import ExecutionError
from kotoyose.model_pools import DEFAULT_LIMIT_BACKOFF, ModelPools, pool_name, published_pool
from kotoyose.pool_strategies import PoolCandidate, PoolContext, balanced, first_available, register_strategy
from tests.test_alias_execution_routing import routing, _install_agent_drivers  # noqa: F401  (fixture)

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)


def _config(**pool) -> HarnessConfig:
    connections = {
        "a": {"provider": "claude", "kind": "api_key", "data_policy": "no_training", "secret": "env:A"},
        "shared": {"provider": "claude", "kind": "api_key", "data_policy": "no_training", "secret": "env:B"},
    }
    routes = {
        "a": {"driver": "claude.agent-sdk", "model": "claude-x", "connection": "a"},
        "b": {"driver": "claude.agent-sdk", "model": "claude-y", "connection": "shared"},
        "c": {"driver": "claude.agent-sdk", "model": "claude-z", "connection": "shared"},
    }
    agent = {"place": "isolated", "surface": "write"}
    models = {
        "m-a": {"route": "a", "agent": agent}, "m-b": {"route": "b", "agent": agent},
        "m-c": {"route": "c", "agent": agent},
        "coding": {"candidates": ["m-a", "m-b"], "policy": "first_available", **pool},
    }
    return HarnessConfig.model_validate({"connections": connections, "routes": routes, "models": models})


def _limit(reset=None, detected=NOW) -> ProviderUsageLimit:
    return ProviderUsageLimit(provider="claude", limit_window="five_hour", reset_at=reset, detected_at=detected,
                              resume="unavailable", unavailable_reason="no_session")


# --- config -----------------------------------------------------------------

def test_pool_alias_loads_from_models_and_round_trips(tmp_path):
    config = _config(requires=["images"])
    assert list(config.models) == ["m-a", "m-b", "m-c"] and list(config.pools) == ["coding"]
    dumped = config.model_dump(mode="json")
    assert dumped["models"]["coding"] == {"candidates": ["m-a", "m-b"], "policy": "first_available",
                                          "requires": ["images"]}
    assert HarnessConfig.model_validate(yaml.safe_load(yaml.safe_dump(dumped))).pools == config.pools


def test_config_without_pools_dumps_exactly_as_before():
    config = HarnessConfig.model_validate({"models": {"x": {}}})
    assert "pools" not in config.model_dump() and set(config.model_dump()["models"]) == {"x"}


def _bad(models_update, match):
    base = _config().model_dump(mode="json")
    base["models"].update(models_update)
    with pytest.raises(ValidationError, match=match):
        HarnessConfig.model_validate(base)


def test_pool_validation_rejects_bad_shapes():
    _bad({"coding": {"candidates": ["m-a"], "policy": "nope"}}, "unknown pool policy")
    _bad({"coding": {"candidates": [], "policy": "balanced"}}, "at least 1")
    _bad({"coding": {"candidates": ["ghost"], "policy": "balanced"}}, "不明な候補")
    _bad({"coding": {"candidates": ["m-a"], "policy": "balanced", "requires": ["telepathy"]}}, "不明な requires")
    _bad({"coding": {"candidates": ["m-a"], "policy": "balanced", "route": "a"}}, "route")
    _bad({"inner": {"candidates": ["m-a"], "policy": "balanced"},
          "outer": {"candidates": ["inner"], "policy": "balanced"}}, "入れ子")
    _bad({"m-b": {"route": "b", "agent": {"place": "isolated", "surface": "read"}},
          "coding": {"candidates": ["m-a", "m-b"], "policy": "balanced"}}, "place と agent.surface")
    _bad({"plain": {}, "coding": {"candidates": ["plain"], "policy": "balanced"}}, "agent がありません")


def test_pool_name_is_route_pool_or_route():
    config = _config()
    assert (pool_name(config, "m-a"), pool_name(config, "m-b"), pool_name(config, "m-c")) == ("a", "shared", "shared")
    assert pool_name(config, "coding") is None


# --- strategies ---------------------------------------------------------------

def _cand(alias, state="available", until=None, last=None, provider="claude"):
    return PoolCandidate(alias, alias, provider, "claude.agent-sdk", state, until, last)


CONTEXT = PoolContext("coding", frozenset(), NOW)


def test_first_available_skips_limited_and_expired_and_honours_until():
    items = [_cand("a", "limited", NOW + timedelta(minutes=5)), _cand("b", "auth_expired", NOW + timedelta(minutes=1)),
             _cand("c", "limited", NOW - timedelta(seconds=1)), _cand("d", "unknown")]
    assert [item.alias for item in first_available(items, CONTEXT)] == ["c", "d"]


def test_first_available_honours_requires():
    items = [_cand("a", provider="cursor"), _cand("b", provider="codex")]
    wanted = PoolContext("coding", frozenset({"images"}), NOW)
    assert [item.alias for item in first_available(items, wanted)] == ["b"]
    assert [item.alias for item in first_available(items, CONTEXT)] == ["a", "b"]


def test_balanced_prefers_never_then_least_recently_selected():
    items = [_cand("a", last=NOW - timedelta(minutes=1)), _cand("b", last=NOW - timedelta(minutes=9)), _cand("c")]
    assert [item.alias for item in balanced(items, CONTEXT)] == ["c", "b", "a"]
    items.append(_cand("d", "limited", NOW + timedelta(hours=1)))
    assert "d" not in [item.alias for item in balanced(items, CONTEXT)]


def test_strategy_registry_extends_without_touching_admission(monkeypatch):
    monkeypatch.setattr(pool_strategies, "_STRATEGIES", dict(pool_strategies._STRATEGIES))

    @register_strategy("last_listed")
    def last_listed(candidates, context):
        return list(reversed(pool_strategies.eligible(candidates, context)))

    assert "last_listed" in pool_strategies.strategy_names()
    with pytest.raises(ValueError, match="already registered"):
        register_strategy("last_listed")(last_listed)
    config = _config()
    base = config.model_dump(mode="json")
    base["models"]["coding"]["policy"] = "last_listed"
    assert HarnessConfig.model_validate(base).pools["coding"].policy == "last_listed"
    assert [item.alias for item in pool_strategies.get_strategy("last_listed")([_cand("a"), _cand("b")], CONTEXT)] == ["b", "a"]


# --- state and selection (database) ------------------------------------------

@pytest.fixture
async def pools_db():
    from tests.fixtures.loop_limits import isolated_limits
    url = os.environ.get("KOTOYOSE_EXECUTION_TEST_DATABASE_URL")
    if not url:
        pytest.skip("isolated test database required")
    async with isolated_limits(url) as (store, _service, _app):
        yield store


def _pools(store, clock=lambda: NOW):
    return ModelPools(store.model_pools.store, clock=clock)


async def _state(store, name):
    return (await store.model_pools.store.states([name])).get(name)


async def test_evidence_updates_pool_state_only_for_pools_in_use(pools_db):
    store, config, pools = pools_db, _config(), _pools(pools_db)
    await pools.observe_attempt(config, "m-b", "failed", _limit(reset=NOW + timedelta(hours=2)))
    row = await _state(store, "shared")
    assert row["state"] == "limited" and row["until"] == NOW + timedelta(hours=2, seconds=5)
    await pools.observe_attempt(config, "m-a", "failed", _limit())  # no reset: conservative backoff
    assert (await _state(store, "a"))["until"] == NOW + DEFAULT_LIMIT_BACKOFF
    await pools.observe_attempt(config, "m-b", "succeeded", None)
    assert (await _state(store, "shared"))["state"] == "available"
    await pools.observe_attempt(config, "m-b", "failed", None)  # failure without limit evidence changes nothing
    assert (await _state(store, "shared"))["state"] == "available"
    # m-c shares the pool, a pool alias lists m-a / m-b only, but the pool name is in use through m-b.
    await pools.observe_attempt(config, "m-c", "succeeded", None)
    await pools.observe_attempt(HarnessConfig(), "x", "succeeded", None)
    assert await _state(store, "m-c") is None


async def test_observation_with_older_timestamp_does_not_overwrite(pools_db):
    store, pools = pools_db, _pools(pools_db)
    await pools.observe_limit("a", _limit(detected=NOW))
    await store.model_pools.store.observe("a", "available", None, NOW - timedelta(minutes=1))
    assert (await _state(store, "a"))["state"] == "limited"


async def test_auth_expired_is_recorded_and_expires(pools_db):
    store, config = pools_db, _config()
    clock = [NOW]
    pools = _pools(store, lambda: clock[0])
    await pools.observe_auth_expired("a")
    assert (await _state(store, "a"))["state"] == "auth_expired"
    assert (await pools.select(config, "coding", refuse_needs_ack=False)) == ["m-b"]
    clock[0] = NOW + timedelta(minutes=6)
    assert (await pools.select(config, "coding", refuse_needs_ack=False))[0] == "m-a"


async def test_limited_pool_past_until_is_available_in_view_and_selection(pools_db):
    store, config = pools_db, _config()
    clock = [NOW]
    pools = _pools(store, lambda: clock[0])
    await pools.observe_limit("a", _limit(reset=NOW + timedelta(minutes=10)))
    assert (await pools.select(config, "coding", refuse_needs_ack=False))[0] == "m-b"
    view = await pools.view(config)
    assert [(c["alias"], c["state"]) for c in view["coding"]["candidates"]] == [("m-a", "limited"), ("m-b", "unknown")]
    clock[0] = NOW + timedelta(minutes=11)
    assert (await pools.select(config, "coding", refuse_needs_ack=False))[0] == "m-a"
    assert (await pools.view(config))["coding"]["candidates"][0]["state"] == "available"


async def test_all_limited_refusal_carries_earliest_recovery(pools_db):
    store, config, pools = pools_db, _config(), _pools(pools_db)
    await pools.observe_limit("a", _limit(reset=NOW + timedelta(hours=3)))
    await pools.observe_limit("shared", _limit(reset=NOW + timedelta(hours=1)))
    with pytest.raises(ExecutionError) as caught:
        await pools.select(config, "coding", refuse_needs_ack=False)
    error = caught.value
    assert (error.code, error.status, error.reason_code) == ("capacity_exhausted", 429, "provider_usage_limit")
    assert error.recovery_at == (NOW + timedelta(hours=1, seconds=5)).isoformat()
    assert error.recovery_at in str(error)


async def test_unsatisfiable_requires_is_not_reported_as_limit(pools_db):
    config = _config(requires=["images"])
    # Force a driver without the capability by narrowing the table for the call.
    pool_strategies.CAPABILITY_PROVIDERS["images"] = frozenset({"cursor"})
    try:
        with pytest.raises(ExecutionError) as caught:
            await _pools(pools_db).select(config, "coding", refuse_needs_ack=False)
    finally:
        pool_strategies.CAPABILITY_PROVIDERS["images"] = frozenset({"claude", "codex"})
    assert caught.value.reason_code == "pool_no_candidate" and caught.value.recovery_at is None


async def test_balanced_spreads_and_is_stable_under_concurrency(pools_db):
    store = pools_db
    base = _config().model_dump(mode="json")
    base["models"]["coding"] = {"candidates": ["m-a", "m-b", "m-c"], "policy": "balanced"}
    config = HarnessConfig.model_validate(base)
    pools = _pools(store)
    first = await asyncio.gather(*(pools.select(config, "coding", refuse_needs_ack=False) for _ in range(30)))
    chosen = [order[0] for order in first]
    assert {alias: chosen.count(alias) for alias in ("m-a", "m-b", "m-c")} == {"m-a": 10, "m-b": 10, "m-c": 10}
    # The history is persisted: a new ModelPools (restart) continues the rotation.
    selections = await store.model_pools.store.last_selected(["coding"])
    oldest = min(selections, key=selections.get)[1]
    again = await _pools(store).select(config, "coding", refuse_needs_ack=False)
    assert again[0] == oldest


def _acked(config: HarnessConfig, route: str) -> None:
    config.connections["a-ep"] = Connection(
        provider="claude", kind="endpoint", data_policy="no_training", endpoint="https://router.example/v1", secret="env:A",
    )
    config.routes[route] = AgentRoute(driver="claude.agent-sdk", model="claude-x", connection="a-ep")


async def test_needs_ack_candidate_is_skipped_only_when_enforcement_would_refuse(pools_db):
    config = _config()
    _acked(config, "a")  # unofficial endpoint, no risk_ack: needs_ack
    pools = _pools(pools_db)
    assert (await pools.select(config, "coding", refuse_needs_ack=False))[0] == "m-a"
    assert (await pools.select(config, "coding", refuse_needs_ack=True)) == ["m-b"]
    config.risk_acks.append(SimpleNamespace(
        route="a", digest=route_digest(config.routes["a"], config.connections[config.routes["a"].connection]),
    ))
    assert (await pools.select(config, "coding", refuse_needs_ack=True))[0] == "m-a"


# --- admission: pin and resume ------------------------------------------------

class FakePools:
    def __init__(self, order):
        self.order, self.selected, self.auth_failed, self.noted = order, 0, [], []

    async def select(self, config, pool_alias, *, refuse_needs_ack):
        self.selected += 1
        return list(self.order)

    async def note_selected(self, pool_alias, alias):
        self.noted.append(alias)

    async def observe_auth_expired(self, name):
        self.auth_failed.append(name)


def _request(model="coding"):
    return AgentExecution.model_construct(model=model, runtime_profile=None, requirements=SimpleNamespace(),
                                          tool_refs=[])


def _admission(monkeypatch, pools, resolver=None):
    config = _config()
    monkeypatch.setattr("kotoyose.config.get_config", lambda: config)
    admission = ExecutionAdmission.__new__(ExecutionAdmission)
    admission.store, admission.settings = SimpleNamespace(model_pools=pools), SimpleNamespace(sanction_enforcement="off")
    admission._model_allowed = lambda model, authority: model in {"coding", "m-a", "m-b"}
    admission._effective_request = lambda request, **kw: (request, "profile")
    seen = []

    async def resolve(principal, policy, request, profile, *, requested_model=None):
        seen.append((request.model, requested_model))
        if resolver is not None:
            resolver(request.model)
        return {"alias": request.model}

    admission._resolve = resolve
    return admission, seen


async def test_admission_resolves_pool_once_and_pins_resolved_model(monkeypatch):
    pools = FakePools(["m-b", "m-a"])
    admission, seen = _admission(monkeypatch, pools)
    snapshot = await admission.resolve("p", object(), _request())
    assert seen == [("m-b", "coding")] and pools.selected == 1
    assert snapshot == {"alias": "m-b", "resolved_model": "m-b", "requested_model": "coding"}


async def test_admission_falls_through_on_authentication_failure_and_notes_the_pool(monkeypatch):
    def resolver(model):
        if model == "m-b":
            raise ExecutionError("authentication_failed", "login required", 401)
    pools = FakePools(["m-b", "m-a"])
    admission, _ = _admission(monkeypatch, pools, resolver)
    snapshot = await admission.resolve("p", object(), _request())
    assert snapshot["resolved_model"] == "m-a" and pools.auth_failed == ["shared"] and pools.noted == ["m-a"]


async def test_pinned_model_never_reselects(monkeypatch):
    pools = FakePools(["m-b"])
    admission, seen = _admission(monkeypatch, pools)
    snapshot = await admission.resolve("p", object(), _request(), pinned_model="m-a")
    assert pools.selected == 0 and snapshot["resolved_model"] == "m-a" and seen == [("m-a", "coding")]
    with pytest.raises(ExecutionError, match="no longer configured"):
        await admission.resolve("p", object(), _request(), pinned_model="m-c")


async def test_plain_alias_is_untouched(monkeypatch):
    pools = FakePools(["m-b"])
    admission, seen = _admission(monkeypatch, pools)
    snapshot = await admission.resolve("p", object(), _request("m-a"))
    assert snapshot == {"alias": "m-a"} and seen == [("m-a", None)] and pools.selected == 0


async def test_resume_uses_pinned_candidate_and_never_selects(monkeypatch):
    pools = FakePools(["m-b"])
    admission, _ = _admission(monkeypatch, pools)
    captured = []

    def effective(request, **kw):
        captured.append(request.model)
        raise ExecutionError("unsupported_capability", "stop here", 422)
    admission._effective_request = effective
    previous = {"snapshot": {"requested_model": "coding", "resolved_model": "m-a"}}
    resume = SimpleNamespace(execution=_request())
    with pytest.raises(ExecutionError):
        await admission.resolve_resume("p", object(), previous, resume, object())
    assert captured == ["m-a"] and pools.selected == 0
    bare = {"snapshot": {}}
    with pytest.raises(ExecutionError) as caught:
        await admission.resolve_resume("p", object(), bare, resume, object())
    assert caught.value.code == "resume_unsafe" and captured == ["m-a"]


# --- publication ----------------------------------------------------------------

class FakeView:
    def __init__(self, config, first="m-b"):
        self.config, self.first = config, first

    async def view(self, config):
        return {"coding": {"policy": "first_available", "requires": [], "first_choice": self.first,
                           "candidates": [{"alias": "m-a", "pool": "a", "state": "limited",
                                           "until": "2026-11-14T13:00:00+00:00", "last_selected_at": None},
                                          {"alias": "m-b", "pool": "shared", "state": "available",
                                           "until": None, "last_selected_at": None}]}}


def _publish_setup(routing, view=True):
    cfg = get_config().model_copy(deep=True)
    base = _config()
    cfg.connections.update(base.connections)
    cfg.routes.update(base.routes)
    cfg.models.update(base.models)
    cfg.pools.update(base.pools)
    cfg.clients["caller-a"] = cfg.clients["caller-a"].model_copy(update={
        "models": ["coding", "m-a"], "places": ["isolated"]})
    save_config(cfg)
    _install_agent_drivers(routing.service.config)
    routing.service.store = SimpleNamespace(pool=object(), model_pools=FakeView(cfg) if view else None)


def test_models_publish_pool_alias(routing):
    _publish_setup(routing)
    assert get_config().pools["coding"].candidates == ["m-a", "m-b"]  # survives save and reload
    items = {item["id"]: item for item in routing.client.get("/v1/models").json()["data"]}
    pool = items["coding"]
    assert pool["candidates"] == ["m-a", "m-b"] and pool["policy"] == "first_available"
    assert pool["agent"] == {"place": "isolated", "surface": "write", "provider": "claude",
                             "driver": "claude.agent-sdk", "enforced": True}
    assert "candidates" not in items["m-a"]
    assert "m-b" not in items  # unlisted candidate alias is filtered by caller scope


def test_models_pool_without_state_uses_first_configured_candidate_and_worst_sanction(routing):
    _publish_setup(routing, view=False)
    cfg = get_config().model_copy(deep=True)
    _acked(cfg, "a")
    save_config(cfg)
    pool = {item["id"]: item for item in routing.client.get("/v1/models").json()["data"]}["coding"]
    assert pool["agent"]["sanction"] == "needs_ack" and pool["agent"]["sanction_reasons"] == ["unofficial_endpoint"]
    assert pool["agent"]["driver"] == "claude.agent-sdk"


def test_pools_endpoint_is_read_only_and_authenticated(routing):
    _publish_setup(routing)
    response = routing.client.get("/v1/pools")
    assert response.status_code == 200
    [item] = response.json()["data"]
    assert item["id"] == "coding" and item["first_choice"] == "m-b"
    assert [c["state"] for c in item["candidates"]] == ["limited", "available"]
    assert routing.client.post("/v1/pools").status_code == 405
    assert routing.client.get("/v1/pools", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_published_pool_without_view_uses_neutral_first_candidate():
    config = _config()
    entry = published_pool(config, "coding", None)
    assert entry["agent"]["driver"] == "claude.agent-sdk" and entry["candidates"] == ["m-a", "m-b"]


# --- chat ------------------------------------------------------------------------

async def test_chat_pool_alias_is_resolved_once_and_reported(monkeypatch):
    from fastapi import Response
    from kotoyose.models import ChatCompletionRequest
    from kotoyose.routes import chat

    config = _config()
    monkeypatch.setattr(chat, "get_config", lambda: config)
    seen = []

    async def inner(req, request, response, key):
        seen.append(req.model)
        return Response(content="ok")

    monkeypatch.setattr(chat, "_chat_completions", inner)
    pools = FakePools(["m-b"])
    service = SimpleNamespace(store=SimpleNamespace(pool=object(), model_pools=pools),
                              config=SimpleNamespace(sanction_enforcement="off"))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(execution_service=service)))
    req = ChatCompletionRequest(model="coding", messages=[{"role": "user", "content": "hi"}])
    result = await chat.chat_completions(req, request, Response(), None)
    assert seen == ["m-b"] and result.headers["x-resolved-model"] == "m-b" and pools.selected == 1
    plain = ChatCompletionRequest(model="m-a", messages=[{"role": "user", "content": "hi"}])
    result = await chat.chat_completions(plain, request, Response(), None)
    assert seen == ["m-b", "m-a"] and "x-resolved-model" not in result.headers and pools.selected == 1
