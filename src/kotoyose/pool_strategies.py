"""pool alias が候補を並べる戦略の registry。admission のコードは名前でしか戦略を知らない。

戦略は `(candidates, context) -> 並べた候補` の純関数。候補の状態と選択履歴は呼び出し側が
詰めて渡す。足すときはこのファイルか、import 時に `register_strategy` を呼ぶ module を足すだけでよい。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from .registry import registry

PoolStateName = Literal["available", "limited", "auth_expired", "unknown"]
# driver の provider ごとに、`requires` の能力を満たせるか。manifest に能力欄が無いのでここで宣言する。
CAPABILITY_PROVIDERS: dict[str, frozenset[str]] = {"images": frozenset({"claude", "codex"})}
KNOWN_REQUIRES = frozenset(CAPABILITY_PROVIDERS)


@dataclass(frozen=True, slots=True)
class PoolCandidate:
    """戦略に渡す 1 候補。alias の並びは config の candidates の順。"""

    alias: str
    pool: str
    provider: str
    driver: str
    state: PoolStateName
    until: datetime | None
    last_selected_at: datetime | None


@dataclass(frozen=True, slots=True)
class PoolContext:
    """選択時の文脈。now を渡すので、戦略は時計を読まない。"""

    pool_alias: str
    requires: frozenset[str]
    now: datetime


Strategy = Callable[[Sequence[PoolCandidate], PoolContext], list[PoolCandidate]]
# 組み込みの戦略はこの module で登録する。
_STRATEGIES: dict[str, Strategy] = registry.routers


def register_strategy(name: str) -> Callable[[Strategy], Strategy]:
    """戦略を名前で登録する。同名の二重登録は設定の意味が変わるので拒む。"""
    def decorate(function: Strategy) -> Strategy:
        if name in _STRATEGIES:
            raise ValueError(f"pool strategy {name!r} is already registered")
        _STRATEGIES[name] = function
        return function
    return decorate


def strategy_names() -> list[str]:
    return sorted(_STRATEGIES)


def get_strategy(name: str) -> Strategy:
    try:
        return _STRATEGIES[name]
    except KeyError:
        raise ValueError(f"unknown pool policy {name!r} (known: {', '.join(strategy_names())})") from None


def usable(candidate: PoolCandidate, context: PoolContext) -> bool:
    """limited / auth_expired は until を過ぎたら使える。until の無い印は使えないままにしない。"""
    if candidate.state in {"limited", "auth_expired"}:
        if candidate.until is not None and candidate.until > context.now:
            return False
    return True


def supports(candidate: PoolCandidate, context: PoolContext) -> bool:
    return all(
        candidate.provider in CAPABILITY_PROVIDERS.get(item, frozenset())
        for item in context.requires
    )


def eligible(candidates: Sequence[PoolCandidate], context: PoolContext) -> list[PoolCandidate]:
    return [item for item in candidates if usable(item, context) and supports(item, context)]


@register_strategy("first_available")
def first_available(candidates: Sequence[PoolCandidate], context: PoolContext) -> list[PoolCandidate]:
    """config の並びで、使える最初の候補を先頭にする。"""
    return eligible(candidates, context)


@register_strategy("balanced")
def balanced(candidates: Sequence[PoolCandidate], context: PoolContext) -> list[PoolCandidate]:
    """最後に選ばれた時刻が古い順 (一度も選ばれていないものが先)。同順位は config の並び。"""
    order = {item.alias: index for index, item in enumerate(candidates)}
    return sorted(
        eligible(candidates, context),
        key=lambda item: (item.last_selected_at is not None, item.last_selected_at or context.now, order[item.alias]),
    )
