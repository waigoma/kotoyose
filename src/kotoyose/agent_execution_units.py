"""Evidence-based closure of an execution charge unit."""
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Sequence

from .agent_execution_digest import agent_utc


@dataclass(frozen=True)
class ActivationFacts:
    nonce: str
    launch: str
    measurement: str
    active_ms: int
    turns: int | None
    output_bytes: int | None
    provider_rejected_only: bool
    stop: str
    effects: str


@dataclass(frozen=True)
class UnitClosure:
    outcome: str
    units: str | None
    active_ms: int
    turns: int
    output_bytes: int
    execution_confirmed: bool


def close_unit_outcome(activations: Sequence[ActivationFacts], *, allocation_units: str | None) -> UnitClosure | Literal['pending', 'unprovable']:
    if not activations or any(a.stop != 'confirmed' or a.effects != 'resolved' for a in activations):
        return 'pending'
    if any(a.launch == 'unknown' for a in activations):
        return 'unprovable'
    started = [a for a in activations if a.launch == 'started']
    if not started:
        outcome = 'not_started_closed'
    elif all(a.provider_rejected_only and a.turns == 0 for a in started):
        outcome = 'provider_zero_turn'
    else:
        outcome = 'executed'
    units = None if allocation_units is None else allocation_units if outcome == 'executed' else '0'
    return UnitClosure(outcome, units, sum(a.active_ms for a in activations),
        sum(a.turns or 0 for a in activations), sum(a.output_bytes or 0 for a in activations), bool(started))


def receipt_body(closure: UnitClosure, *, charge: dict | None, nonces: list[str], finished_at: datetime) -> dict:
    return {'charge_ref': charge['charge_ref'] if charge else None,
        'reservation_ref': charge['reservation_ref'] if charge else None,
        'allocation_units': charge['allocation_units'] if charge else None,
        'activation_nonces': nonces, 'outcome': closure.outcome, 'units': closure.units,
        'active_ms': closure.active_ms, 'turns': closure.turns, 'output_bytes': closure.output_bytes,
        'execution_confirmed': closure.execution_confirmed, 'stop_confirmation': 'confirmed',
        'side_effect_resolution': 'resolved', 'finished_at': agent_utc(finished_at)}
