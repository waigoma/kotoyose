"""管理 UI が route / alias / pool の編集欄を作るための、読み取り専用の事実。

選択肢の源は agent_spec (driver の表) と pool_strategies (戦略 registry) で、UI は写しを持たない。
"""

from __future__ import annotations

from typing import Any

from ..data_policy import connection_data_policy
from ..agent_sanction import route_digest, route_reasons, route_sanction
from ..agent_spec import SURFACE_RANK
from ..registry import registry
from ..pool_strategies import KNOWN_REQUIRES, strategy_names

ALL_SURFACES = tuple(sorted(SURFACE_RANK, key=SURFACE_RANK.__getitem__))


def driver_catalog() -> dict[str, Any]:
    """driver ごとの provider と、place ごとに選べる surface。

    isolated は表の admitted な cell だけ。host は surface を強制しないので全部。
    """
    drivers: dict[str, Any] = {}
    for row in registry.drivers():
        places: dict[str, list[str]] = {}
        if "isolated" in row.places:
            places["isolated"] = [cell.surface for cell in row.surfaces if cell.admitted]
        if "host" in row.places:
            places["host"] = list(ALL_SURFACES)
        drivers[row.driver] = {"provider": row.provider, "places": places}
    return drivers


def pool_catalog() -> dict[str, Any]:
    return {"strategies": strategy_names(), "requires": sorted(KNOWN_REQUIRES)}


def route_sanction_view(cfg, name: str) -> dict[str, Any]:
    """route の公式判定と、承諾の記録 (あれば) と、その承諾が今の姿に合っているか。"""
    route = cfg.routes[name]
    connection = cfg.connections.get(route.connection)
    sanction = route_sanction(cfg, name)
    digest = route_digest(route, connection)
    mine = [ack for ack in cfg.risk_acks if ack.route == name]
    current = next((ack for ack in mine if ack.digest == digest), None)
    shown = current or (mine[-1] if mine else None)
    return {
        "data_policy": connection_data_policy(connection) if connection is not None else "unknown",
        "sanction": sanction.status,
        "sanction_reasons": list(sanction.reasons),
        "digest": digest,
        "current_reasons": route_reasons(route, connection),
        "ack": shown.model_dump(mode="json") if shown is not None else None,
        # 承諾はあるが、route の姿が変わって digest が合わなくなった。
        "ack_stale": shown is not None and current is None,
    }
