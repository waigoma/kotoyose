"""公式の口から外れる route の理由。認めるかどうかは決めない。"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

import structlog

from .agent_spec import driver_manifest

log = structlog.get_logger()

_OFFICIAL_CLAUDE_HOSTS = frozenset({"api.anthropic.com"})


def model_family(model: str) -> str | None:
    """model id から家族を読む。分からない id は None で、codex の新しい名前を誤って外さない。"""
    text = model.lower()
    if "claude" in text:
        return "claude"
    if text.startswith("gpt-") or text.startswith(("o1", "o3", "o4")) or "codex" in text:
        return "codex"
    if "gemini" in text:
        return "antigravity"
    if "grok" in text:
        return "grok"
    return None


def sanction_reasons(driver: str, model: str, endpoint: str | None,
                     model_provider: Any = None) -> list[str]:
    """後で承諾が要る理由。空なら公式の組み合わせ。"""
    reasons = []
    if driver == "claude.agent-sdk" and endpoint:
        host = (urlsplit(endpoint).hostname or "").lower()
        if host not in _OFFICIAL_CLAUDE_HOSTS:
            reasons.append("unofficial_endpoint")
    provider = driver_manifest(driver).provider
    family = model_family(model)
    if provider == "claude" and family != "claude":
        reasons.append("cross_provider_model")
    elif provider == "codex" and family not in {None, "codex"}:
        reasons.append("cross_provider_model")
    if model_provider is not None and provider == "codex":
        reasons.append("custom_model_provider")
    return reasons


SanctionStatus = Literal["official", "acknowledged", "needs_ack"]


@dataclass(frozen=True)
class RouteSanction:
    """route の判定。status が needs_ack でも、段階 1 では何も止めない。"""

    status: SanctionStatus
    reasons: tuple[str, ...]

    def as_snapshot(self) -> dict[str, Any]:
        """attempt snapshot の `sanction` key。参考情報で、resume の同一性には入れない。"""
        return {"status": self.status, "reasons": list(self.reasons)}


def route_digest(route, connection=None) -> str:
    """承諾が指す route の姿。これらのどれが変わっても承諾は外れる。"""
    canonical = json.dumps({
        "driver": route.driver,
        "endpoint": connection.endpoint if connection is not None else None,
        "model": route.model,
        "connection_kind": connection.kind if connection is not None else None,
        "model_provider": route.model_provider,
    }, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def route_reasons(route, connection=None) -> list[str]:
    endpoint = connection.endpoint if connection is not None else None
    return sanction_reasons(route.driver, route.model, endpoint, route.model_provider)


def route_sanction(config, route_name: str) -> RouteSanction:
    """official / acknowledged / needs_ack。ack は route 名と digest が両方合うものだけ。"""
    route = config.routes[route_name]
    connection = (getattr(config, "connections", None) or {}).get(route.connection)
    reasons = route_reasons(route, connection)
    if not reasons:
        return RouteSanction("official", ())
    digest = route_digest(route, connection)
    if any(ack.route == route_name and ack.digest == digest for ack in config.risk_acks):
        return RouteSanction("acknowledged", tuple(reasons))
    return RouteSanction("needs_ack", tuple(reasons))


def alias_route_sanction(config, alias: str) -> tuple[str, RouteSanction] | None:
    """alias が route を持つときだけ (route 名, 判定)。route の無い alias は対象外。"""
    preset = (getattr(config, "models", None) or {}).get(alias)
    if preset is None or preset.route is None or preset.route not in config.routes:
        return None
    return preset.route, route_sanction(config, preset.route)


_warned: set[str] = set()
_warned_lock = threading.Lock()


def warn_needs_ack_once(route_name: str, sanction: RouteSanction, *, via: str) -> None:
    """needs_ack の route を使った最初の 1 回だけ、プロセスごとに警告する。"""
    if sanction.status != "needs_ack":
        return
    with _warned_lock:
        if route_name in _warned:
            return
        _warned.add(route_name)
    log.warning("route.needs_acknowledgement", route=route_name,
                sanction_reasons=list(sanction.reasons), via=via)


def reset_warned_routes() -> None:
    with _warned_lock:
        _warned.clear()


def acknowledgement_error_message(route_name: str, sanction: RouteSanction) -> str:
    return (f"route '{route_name}' needs a risk acknowledgement "
            f"({', '.join(sanction.reasons)}); add it under risk_acks")


def with_sanction(agent: dict | None, config, alias: str) -> dict | None:
    """公開する agent object に判定を足す。official は key を足さない (無い = 公式)。

    既存 client と、agent object の完全一致を見る既存の契約を動かさないため。
    """
    found = alias_route_sanction(config, alias) if agent is not None else None
    if found is None or found[1].status == "official":
        return agent
    return {**agent, "sanction": found[1].status, "sanction_reasons": list(found[1].reasons)}
