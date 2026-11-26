"""pool alias の解決。候補の状態を集め、戦略に並べさせ、admission へ 1 つ渡す。

状態の更新は、attempt が終わったときに Kotoyose が既に持つ証拠 (provider の利用枠の観測、成功) だけで行う。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import structlog

from .data_policy import alias_data_policy, configured_capabilities
from .agent_sanction import alias_route_sanction
from .agent_spec import driver_manifest
from .execution_limits import ProviderUsageLimit
from .execution_resources import ExecutionError
from .agent_execution_provider import KNOWN_RESET_PAD_MS, UNKNOWN_BACKOFF_MS
from .pool_strategies import PoolCandidate, PoolContext, get_strategy, supports, usable

log = structlog.get_logger()

# reset の分からない利用枠は、既存の probe backoff の最長に合わせて保守的に見る。
DEFAULT_LIMIT_BACKOFF = timedelta(milliseconds=UNKNOWN_BACKOFF_MS[-1])
# 認証の失効は回復を観測できない。短く保留して、後の受理で自然に再試行させる。
AUTH_RETRY_AFTER = timedelta(minutes=5)
_SANCTION_ORDER = {"official": 0, "acknowledged": 1, "needs_ack": 2}


def pool_name(config, alias: str) -> str | None:
    """alias の route が使う connection ID。route が共有する接続先を pool 状態キーにする。"""
    preset = config.models.get(alias)
    if preset is None or preset.route is None or preset.route not in config.routes:
        return None
    return config.routes[preset.route].connection


def pool_names_in_use(config) -> set[str]:
    return {name for pool in config.pools.values() for alias in pool.candidates
            if (name := pool_name(config, alias)) is not None}


def pool_provider(config, alias: str) -> tuple[str, str]:
    driver = config.routes[config.models[alias].route].driver
    return driver_manifest(driver).provider, driver


def pool_sanction(config, pool_alias: str) -> str:
    """候補のうち最も重い判定。official は 'official'。"""
    worst = "official"
    for alias in config.pools[pool_alias].candidates:
        found = alias_route_sanction(config, alias)
        if found is not None and _SANCTION_ORDER[found[1].status] > _SANCTION_ORDER[worst]:
            worst = found[1].status
    return worst


class ModelPools:
    """ModelPoolStore の上の解決。config は呼び出しごとに渡す (hot reload に追従する)。"""

    def __init__(self, store, *, clock=lambda: datetime.now(timezone.utc)):
        self.store, self.clock = store, clock

    def _candidates(self, config, aliases: list[str], states: dict, last: dict) -> list[PoolCandidate]:
        out = []
        for alias in aliases:
            name = pool_name(config, alias)
            row = states.get(name)
            provider, driver = pool_provider(config, alias)
            out.append(PoolCandidate(
                alias=alias, pool=name, provider=provider, driver=driver,
                state=row["state"] if row else "unknown", until=row["until"] if row else None,
                last_selected_at=last.get(alias)))
        return out

    async def select(self, config, pool_alias: str, *, refuse_needs_ack: bool,
                     training_capabilities: set[str] | None = None,
                     defer_data_policy: bool = False,
                     candidate_aliases: list[str] | None = None) -> list[str]:
        """戦略が並べた候補を返し、先頭を選択済みとして記録する。全て不可なら理由つきで断る。"""
        pool = config.pools[pool_alias]
        context = PoolContext(pool_alias, frozenset(pool.requires), self.clock())
        strategy = get_strategy(pool.policy)
        candidates = pool.candidates if candidate_aliases is None else candidate_aliases
        names = sorted({pool_name(config, alias) for alias in candidates})
        refused = {alias for alias in candidates
                   if refuse_needs_ack and (found := alias_route_sanction(config, alias)) is not None
                   and found[1].status == "needs_ack"}
        training_refused = {}
        if not defer_data_policy:
            required = set(training_capabilities or ())
            if "images" in pool.requires:
                required.add("attachments")
            training_refused = {
                alias: message for alias in candidates
                if (message := alias_data_policy(config, alias).refusal(
                    required | configured_capabilities(config.models[alias])
                ))
            }
        refused.update(training_refused)
        seen: list[PoolCandidate] = []

        def choose(states, last):
            nonlocal seen
            seen = [item for item in self._candidates(config, candidates, states, last) if item.alias not in refused]
            return [item.alias for item in strategy(seen, context)]

        ordered = await self.store.select(pool_alias, names, choose)
        if ordered:
            return ordered
        qualified = [item for item in seen if supports(item, context)]
        if not qualified and training_refused:
            raise ExecutionError("permission_denied", " ".join(training_refused.values()), 403,
                                 reason_code="data_policy_restricted")
        waiting = [item.until for item in qualified if not usable(item, context) and item.until is not None]
        if waiting:
            earliest = min(waiting)
            raise ExecutionError(
                "capacity_exhausted", f"Every candidate of pool '{pool_alias}' is limited; earliest recovery {earliest.isoformat()}",
                429, reason_code="provider_usage_limit", recovery_at=earliest.isoformat())
        raise ExecutionError("unsupported_capability",
                             f"No candidate of pool '{pool_alias}' can be selected for this request", 422,
                             reason_code="pool_no_candidate")

    async def note_selected(self, pool_alias: str, alias: str) -> None:
        await self.store.record_selection(pool_alias, alias)

    async def observe_limit(self, pool: str, limit: ProviderUsageLimit) -> None:
        detected = limit.detected_at
        if limit.reset_at is not None and limit.reset_at > detected:
            until = limit.reset_at + timedelta(milliseconds=KNOWN_RESET_PAD_MS)
        else:
            until = detected + DEFAULT_LIMIT_BACKOFF
        await self.store.observe(pool, "limited", until, detected)

    async def observe_success(self, pool: str) -> None:
        await self.store.observe(pool, "available", None, self.clock())

    async def observe_auth_expired(self, pool: str) -> None:
        now = self.clock()
        await self.store.observe(pool, "auth_expired", now + AUTH_RETRY_AFTER, now)

    async def observe_attempt(self, config, alias: str | None, status: str, limit: ProviderUsageLimit | None) -> None:
        """終わった attempt の証拠を pool へ。pool alias が使う pool だけを書き、無ければ何もしない。"""
        if not getattr(config, "pools", None) or alias is None:
            return
        name = pool_name(config, alias)
        if name is None or name not in pool_names_in_use(config):
            return
        if limit is not None:
            await self.observe_limit(name, limit)
        elif status == "succeeded":
            await self.observe_success(name)

    async def view(self, config, *, candidate_aliases: dict[str, list[str]] | None = None) -> dict:
        """GET /v1/pools と /v1/models の根拠。読み取りだけで、状態は更新しない。"""
        now = self.clock()
        states = await self.store.states(sorted(pool_names_in_use(config)))
        last = await self.store.last_selected(list(config.pools))
        out = {}
        for pool_alias, pool in config.pools.items():
            aliases = pool.candidates if candidate_aliases is None else candidate_aliases.get(pool_alias, pool.candidates)
            candidates = self._candidates(config, aliases, states, {a: last.get((pool_alias, a)) for a in aliases})
            context = PoolContext(pool_alias, frozenset(pool.requires), now)
            out[pool_alias] = {
                "policy": pool.policy, "requires": list(pool.requires),
                "candidates": [{
                    "alias": item.alias, "pool": item.pool,
                    "state": "available" if item.state in {"limited", "auth_expired"} and usable(item, context) else item.state,
                    "until": item.until.isoformat() if item.until is not None and not usable(item, context) else None,
                    "last_selected_at": item.last_selected_at.isoformat() if item.last_selected_at else None,
                } for item in candidates],
                "first_choice": next((item.alias for item in get_strategy(pool.policy)(candidates, context)), None),
            }
        return out


def published_pool(config, pool_alias: str, view: dict | None, *, candidate_aliases: list[str] | None = None) -> dict:
    """/v1/models の pool alias。place と surface は候補が共有する値。

    provider と driver は現在の第一候補のもの。使える候補が無い (または状態を読めない) ときは、
    config で最初の候補のもの。sanction は最も重い候補に従う (official なら key を足さない)。
    """
    pool = config.pools[pool_alias]
    candidates = pool.candidates if candidate_aliases is None else candidate_aliases
    first = (view or {}).get("first_choice") or candidates[0]
    agent_cell = config.models[pool.candidates[0]].agent
    provider, driver = pool_provider(config, first)
    agent = {"place": agent_cell.place, "surface": agent_cell.surface, "provider": provider, "driver": driver,
             "enforced": agent_cell.place == "isolated"}
    worst = pool_sanction(config, pool_alias)
    if worst != "official":
        reasons = sorted({reason for alias in pool.candidates
                          if (found := alias_route_sanction(config, alias)) is not None and found[1].status == worst
                          for reason in found[1].reasons})
        agent.update({"sanction": worst, "sanction_reasons": reasons})
    return {"agent": agent, "candidates": list(candidates), "policy": pool.policy}


def pool_data_policy(config, pool_alias: str) -> dict:
    decisions = [alias_data_policy(config, alias) for alias in config.pools[pool_alias].candidates]
    order = {"no_training": 0, "unknown": 1, "trains": 2}
    return {
        "data_policy": max((item.policy for item in decisions), key=order.__getitem__),
        "training_ack": any(item.restricted for item in decisions)
        and all(item.unlocked for item in decisions if item.restricted),
    }
