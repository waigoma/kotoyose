"""Reusable public-route scenarios for the Hub execution contract."""
import asyncio
from dataclasses import dataclass
from datetime import timedelta
from typing import TypeAlias
from uuid import uuid4

from kotoyose.execution_limits import ProviderUsageLimit
from kotoyose.agent_execution_digest import agent_utc
from tests.fixtures.agent_execution import FakeClock


@dataclass(frozen=True)
class Create:
    pass


@dataclass(frozen=True)
class Authorize:
    revision: int = 2
    same_charge: bool = False


@dataclass(frozen=True)
class Deny:
    reason: dict
    revision: int = 2


@dataclass(frozen=True)
class Runtime:
    script: str
    reset_seconds: int | None = None


@dataclass(frozen=True)
class Advance:
    seconds: int


@dataclass(frozen=True)
class RunDue:
    pass


@dataclass(frozen=True)
class TryNow:
    pass


@dataclass(frozen=True)
class Cancel:
    reason: str = 'owner_abort'


@dataclass(frozen=True)
class Crash:
    pass


@dataclass(frozen=True)
class Restart:
    pass


@dataclass(frozen=True)
class Expect:
    state: str
    authority_required: bool | None = None
    usage_state: str | None = None


Step: TypeAlias = Create | Authorize | Deny | Runtime | Advance | RunDue | TryNow | Cancel | Crash | Restart | Expect

SCENARIOS: dict[str, list[Step]] = {
    'provider_resume': [Runtime('provider', reset_seconds=20), Create(), Expect('waiting_provider'),
                        Advance(26), RunDue(), Expect('resuming', True), Runtime('success'),
                        Authorize(), Expect('terminal')],
    'fault_retry': [Runtime('fault'), Create(), Expect('retrying', True), Runtime('success'),
                    Authorize(), Expect('terminal')],
    'agent_exec_gap': [Runtime('wait'), Create(), Expect('running', usage_state='running'), Advance(20),
                Authorize(same_charge=True), Expect('running', usage_state='running')],
    'wait_restart': [Runtime('provider'), Create(), Expect('waiting_provider'), Crash(), Restart(),
                     Expect('waiting_provider')],
    'retry_restart': [Runtime('fault'), Create(), Expect('retrying', True), Crash(), Restart(),
                      Expect('retrying', True), Runtime('fault'), Authorize(), Expect('terminal')],
}


async def run_scenario(kotoyose, steps: list[Step]) -> list[dict]:
    """Exercise actions through public routes and return observed snapshot traces."""
    hub = kotoyose
    clock = FakeClock()
    hub.clock = clock
    kotoyose.agent_executions.clock = clock
    kotoyose.agent_executions.leases.clock = clock
    raw = None
    snapshot = None
    trace = []
    for step in steps:
        if isinstance(step, Runtime):
            hub.runtime.script = step.script
            if step.script == 'provider':
                reset = clock() + timedelta(seconds=step.reset_seconds) if step.reset_seconds is not None else None
                hub.runtime.provider_limit = ProviderUsageLimit(provider='claude', limit_window='rate',
                    reset_at=reset, detected_at=clock(), resume='native', unavailable_reason=None)
        elif isinstance(step, Create):
            raw = hub.create_body(await hub.register_work(tool_refs=[]))
            raw['execution']['initial_authorization'].update(issued_at=agent_utc(clock()),
                not_before=agent_utc(clock() - timedelta(seconds=30)),
                expires_at=agent_utc(clock() + timedelta(seconds=30)))
            response = await hub.client.post('/v1/executions', json=raw)
            assert response.status_code == 202, response.text
        elif isinstance(step, Advance):
            clock.advance(seconds=step.seconds)
        elif isinstance(step, RunDue):
            await kotoyose.agent_executions.run_due()
        elif isinstance(step, Crash):
            await hub.crash()
        elif isinstance(step, Restart):
            await hub.restart()
            # public_agent_execution_harness mounts a separate app from isolated_agent_execution_harness's internal router.
            hub.client._transport.app.state.execution_service = hub.service
            kotoyose.agent_executions.clock = clock
            kotoyose.agent_executions.leases.clock = clock
        else:
            assert raw is not None
            key = raw['execution']['idempotency_key']
            path = '/v1/executions/by-key/' + key
            if isinstance(step, Expect):
                async def observe():
                    while True:
                        response = await hub.client.get(path)
                        assert response.status_code == 200, response.text
                        value = response.json()
                        if (
                            value['state'] == step.state
                            and (step.authority_required is None or
                                 value['activation']['authority_required'] == step.authority_required)
                            and (step.usage_state is None or value['usage']['state'] == step.usage_state)
                        ):
                            return value
                        await asyncio.sleep(.02)
                try:
                    snapshot = await asyncio.wait_for(observe(), 5)
                except TimeoutError as exc:
                    current = (await hub.client.get(path)).json()
                    raise AssertionError(f'Expected {step}; observed {current}') from exc
                trace.append({'state': snapshot['state'], 'activation': snapshot['activation'],
                    'usage': snapshot['usage'], 'receipts': snapshot['receipts'],
                    'attempts': len(hub.runtime.jobs), 'snapshot': snapshot, 'time': agent_utc(clock())})
            elif isinstance(step, Authorize):
                assert snapshot is not None
                charge = raw['execution']['initial_authorization']['charge'] if step.same_charge else None
                overrides = {'revision': step.revision, 'activation_nonce': snapshot['activation']['nonce'],
                    'settled_receipt_cursor': snapshot['final_receipt_cursor'], 'issued_at': agent_utc(clock()),
                    'not_before': raw['execution']['initial_authorization']['not_before'],
                    'expires_at': agent_utc(clock() + timedelta(seconds=30))}
                if step.same_charge:
                    overrides['charge'] = charge
                response = await hub.client.put(path + '/authorization', json=hub.allow(snapshot, **overrides))
                assert response.status_code == 200, response.text
            elif isinstance(step, Deny):
                assert snapshot is not None
                response = await hub.client.put(path + '/authorization', json=hub.deny(snapshot, step.reason,
                    revision=step.revision, activation_nonce=snapshot['activation']['nonce']))
                assert response.status_code == 200, response.text
            elif isinstance(step, TryNow):
                assert snapshot is not None
                response = await hub.client.post(path + '/try-now', json={'action_key': str(uuid4()),
                    'expected_revision': snapshot['state_revision']})
                assert response.status_code == 202, response.text
            elif isinstance(step, Cancel):
                response = await hub.client.post(path + '/cancel', json={'action_key': str(uuid4()), 'reason': step.reason})
                assert response.status_code in (200, 202), response.text
    return trace
