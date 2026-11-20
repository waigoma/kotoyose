import copy
import json
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from kotoyose.agent_execution_contract import (
    AgentExecutionAck, AgentExecutionAction, AgentExecutionAllow, AgentExecutionCancel,
    AgentExecutionClosed, AgentExecutionCreate, AgentExecutionDeny, AgentExecutionError,
    AgentExecutionReceipt, AgentExecutionSnapshot, AgentExecutionErrorEnvelope, AgentExecutionTerminal, validate_create,
)
from kotoyose.agent_execution_digest import agent_canonical_json, agent_digest, agent_request_digest, agent_work_digest, read_agent_json

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/agent_execution_v1.json').read_text())
MODELS = {'acks': AgentExecutionAck, 'snapshots': AgentExecutionSnapshot, 'allows': AgentExecutionAllow,
          'denies': AgentExecutionDeny, 'receipts': AgentExecutionReceipt, 'actions': AgentExecutionAction,
          'cancels': AgentExecutionCancel, 'closed': AgentExecutionClosed, 'errors': AgentExecutionErrorEnvelope}


def test_fixture_vectors_parse_strictly():
    for section, model in MODELS.items():
        for value in FIXTURE[section]:
            assert model.model_validate(value).model_dump(mode='json') == value
    for vector in FIXTURE['creates']:
        value = vector['value']
        assert AgentExecutionCreate.model_validate(value).model_dump(mode='json') == value
        for mutate in (
            lambda v: v.update(unknown=1),
            lambda v: v['execution']['work']['requirements']['worker_requirements'].update(unknown=1),
            lambda v: v['execution']['work'].pop('role'),
            lambda v: v['execution'].update(schema_version='unknown'),
            lambda v: v['execution']['work'].update(work_kind='unknown'),
            lambda v: v['execution']['time'].update(run_hard_ms=2**53),
            lambda v: v['execution']['recovery'].update(fault_retries_used=True),
            lambda v: v['execution']['money'].update(allocation_units='1.1234567890123'),
            lambda v: v['execution']['initial_authorization'].update(issued_at='2026-01-01T00:00:00+01:00'),
        ):
            bad = copy.deepcopy(value)
            mutate(bad)
            with pytest.raises(ValidationError):
                AgentExecutionCreate.model_validate(bad)
    with pytest.raises(AgentExecutionError):
        read_agent_json(b'{"x":"\\ud800"}')


def test_digests_match_fixture():
    for vector in FIXTURE['creates']:
        assert agent_work_digest(vector['value']) == vector['work_digest']
        assert agent_request_digest(vector['value']) == vector['request_digest']
    raw = copy.deepcopy(FIXTURE['creates'][0]['value'])
    original = (agent_work_digest(raw), agent_request_digest(raw))
    raw['execution']['initial_authorization']['charge']['reservation_ref'] = 'changed'
    assert agent_work_digest(raw) == original[0]
    assert agent_request_digest(raw) != original[1]
    raw['execution']['predecessor'] = {'idempotency_key': 'previous', 'execution_id': 'previous', 'relation': 'rerun'}
    assert agent_work_digest(raw) != original[0]


def test_canonical_vectors_match():
    for vector in FIXTURE['canonical']:
        assert agent_canonical_json(vector['value']) == vector['canonical']
        assert agent_digest(vector['value']) == vector['digest']
    assert agent_canonical_json({'10': 0, '2': 0, '01': 0, '0': 0, '4294967294': 0, '4294967295': 0, '00': 0}) == (
        '{"0":0,"2":0,"10":0,"4294967294":0,"00":0,"01":0,"4294967295":0}'
    )


def test_additional_contract_rules():
    raw = copy.deepcopy(FIXTURE['creates'][0]['value'])
    for model, profile in [(None, None), ('model', 'profile')]:
        body = copy.deepcopy(raw)
        body['execution']['work'].update(model=model, runtime_profile=profile)
        body['execution']['work_digest'] = agent_work_digest(body)
        body['execution']['initial_authorization']['work_digest'] = body['execution']['work_digest']
        with pytest.raises(AgentExecutionError):
            validate_create(AgentExecutionCreate.model_validate(body), body)
    body = copy.deepcopy(raw)
    body['execution']['idempotency_key'] = 'a/b'
    with pytest.raises(ValidationError):
        AgentExecutionCreate.model_validate(body)
    terminal = {'kind': 'resume_unavailable', 'cause': 'predecessor_mismatch', 'handoff_refs': [],
                'workspace_ref': 'workspace:1', 'work_digest': '0' * 64}
    assert TypeAdapter(AgentExecutionTerminal).validate_python(terminal).cause == 'predecessor_mismatch'


def test_authorization_bounds_and_shape():
    from datetime import timedelta
    from kotoyose.agent_execution_contract import validate_allow_bounds
    from kotoyose.agent_execution_digest import agent_utc, parse_agent_utc

    raw = copy.deepcopy(FIXTURE['creates'][0]['value'])
    create = AgentExecutionCreate.model_validate(raw)
    allow = create.execution.initial_authorization
    validate_allow_bounds(create, allow)
    issued = parse_agent_utc(allow.issued_at)
    for change in (
        {'expires_at': agent_utc(issued + timedelta(milliseconds=3 * 120_000 + 1000 + 1))},
        {'not_before': allow.expires_at},
        {'allowed_active_ms': create.execution.time.run_hard_ms + 1},
        {'authority_ceiling_at': agent_utc(issued + timedelta(seconds=60))},
        {'charge': None},
    ):
        with pytest.raises(AgentExecutionError):
            validate_allow_bounds(create, allow.model_copy(update=change))
    body = copy.deepcopy(raw)
    body['execution']['authority_ceilings'] = [{'kind': 'approval', 'evidence_ref': 'evidence',
                                                'expires_at': agent_utc(issued + timedelta(seconds=65))}]
    body['execution']['initial_authorization']['authority_ceiling_at'] = agent_utc(issued + timedelta(seconds=65))
    body['execution']['initial_authorization']['expires_at'] = agent_utc(issued + timedelta(seconds=60))
    updated = AgentExecutionCreate.model_validate(body)
    validate_allow_bounds(updated, updated.execution.initial_authorization)
    for ceiling in [None, agent_utc(issued + timedelta(seconds=66)), agent_utc(issued + timedelta(seconds=60))]:
        with pytest.raises(AgentExecutionError):
            validate_allow_bounds(updated, updated.execution.initial_authorization.model_copy(update={'authority_ceiling_at': ceiling}))
    for invalid in [b'{"n":1.0}', b'{"n":NaN}', b'[]', b'{"n":9007199254740992}']:
        with pytest.raises(AgentExecutionError):
            read_agent_json(invalid)


# 固定 vector (scratchpad の stage4-decisions.md の表と 1 文字も違えない)。Hub の H4 も同じ表を使う。
LEASE_ISSUED = '2026-11-12T00:00:00.000Z'
LEASE_VECTORS = [
    # (run_hard_ms, grace_ms, authority_ceiling_at | None, expires_at)
    (3_600_000, 10_000, None, '2026-11-12T03:00:10.000Z'),
    (60_000, 10_000, None, '2026-11-12T00:03:10.000Z'),
    (3_600_000, 10_000, '2026-11-12T01:23:20.000Z', '2026-11-12T01:23:10.000Z'),
    (3_600_000, 10_000, '2026-11-12T05:33:20.000Z', '2026-11-12T03:00:10.000Z'),
    (1_000, 1, None, '2026-11-12T00:00:03.001Z'),
    (600_000, 30_000, '2026-11-12T00:30:30.000Z', '2026-11-12T00:30:00.000Z'),
]


def _lease_expiry(run_hard_ms, grace_ms, ceiling):
    """表の式そのもの: min(issued + 3 * run_hard + grace, ceiling - grace)。"""
    from datetime import timedelta
    from kotoyose.agent_execution_digest import parse_agent_utc
    issued = parse_agent_utc(LEASE_ISSUED)
    cap = issued + timedelta(milliseconds=3 * run_hard_ms + grace_ms)
    if ceiling is None:
        return cap
    return min(cap, parse_agent_utc(ceiling) - timedelta(milliseconds=grace_ms))


@pytest.mark.parametrize(('run_hard_ms', 'grace_ms', 'ceiling', 'expires'), LEASE_VECTORS)
def test_lease_expiry_fixed_vectors(run_hard_ms, grace_ms, ceiling, expires):
    from kotoyose.agent_execution_digest import agent_utc, parse_agent_utc
    assert agent_utc(_lease_expiry(run_hard_ms, grace_ms, ceiling)) == expires
    assert parse_agent_utc(expires) == _lease_expiry(run_hard_ms, grace_ms, ceiling)


@pytest.mark.parametrize(('run_hard_ms', 'grace_ms', 'ceiling', 'expires'), LEASE_VECTORS)
def test_lease_expiry_vectors_through_validator(run_hard_ms, grace_ms, ceiling, expires):
    from datetime import timedelta
    from kotoyose.agent_execution_digest import agent_utc, parse_agent_utc
    from kotoyose.agent_execution_contract import validate_allow_bounds

    raw = copy.deepcopy(FIXTURE['creates'][0]['value'])
    raw['execution']['time'] = {'run_hard_ms': run_hard_ms, 'grace_ms': grace_ms}
    allow_raw = raw['execution']['initial_authorization']
    allow_raw.update({'issued_at': LEASE_ISSUED, 'not_before': LEASE_ISSUED, 'expires_at': expires,
                      'allowed_active_ms': min(allow_raw['allowed_active_ms'], run_hard_ms)})
    if ceiling is not None:
        raw['execution']['authority_ceilings'] = [{'kind': 'approval', 'evidence_ref': 'evidence',
                                                   'expires_at': agent_utc(parse_agent_utc(ceiling) + timedelta(hours=1))}]
        allow_raw['authority_ceiling_at'] = ceiling
    create = AgentExecutionCreate.model_validate(raw)
    validate_allow_bounds(create, create.execution.initial_authorization)
    later = create.execution.initial_authorization.model_copy(update={'expires_at': agent_utc(parse_agent_utc(expires) + timedelta(milliseconds=1))})
    with pytest.raises(AgentExecutionError):
        validate_allow_bounds(create, later)


def test_g17_values_are_rejected():
    raw = copy.deepcopy(FIXTURE['creates'][0]['value'])
    body = copy.deepcopy(raw)
    body['execution']['work']['approval_bindings'] = [{'approval_id': 'approval:1', 'subject_digest': 'a' * 64}]
    assert AgentExecutionCreate.model_validate(body).model_dump(mode='json') == body
    for extra in ({'wait_seconds': 30}, {'mode': 'blocking'}, {'mode': 'asynchronous'}):
        bad = copy.deepcopy(body)
        bad['execution']['work']['approval_bindings'][0].update(extra)
        with pytest.raises(ValidationError):
            AgentExecutionCreate.model_validate(bad)
    for relation in ('condition_change', 'cap_raise', 'resume_unavailable', 'rerun', 'owner_answer'):
        other = copy.deepcopy(raw)
        other['execution']['predecessor'] = {'idempotency_key': 'previous', 'execution_id': 'previous', 'relation': relation}
        if relation == 'owner_answer':
            with pytest.raises(ValidationError):
                AgentExecutionCreate.model_validate(other)
        else:
            assert AgentExecutionCreate.model_validate(other).execution.predecessor.relation == relation


def test_event_cursor_is_fixed_width_and_monotonic():
    from kotoyose.agent_execution_contract import EVENT_CURSOR_START, AgentExecutionEvents, event_cursor, parse_event_cursor

    assert EVENT_CURSOR_START == event_cursor(0, 0) == '000000:0000000000'
    pairs = [(1, 1), (1, 2), (1, 10), (2, 1), (2, 9), (10, 0), (10, 1)]
    cursors = [event_cursor(*pair) for pair in pairs]
    assert cursors == sorted(cursors) and len(set(cursors)) == len(cursors)
    assert [parse_event_cursor(cursor) for cursor in cursors] == pairs
    for invalid in ['1:1', '000001:1', '000001:0000000001 ', 'a' * 17, '000001-0000000001']:
        with pytest.raises(AgentExecutionError):
            parse_event_cursor(invalid)


def test_event_wire_model_is_strict():
    from kotoyose.agent_execution_contract import AgentExecutionEvents

    event = {'cursor': '000001:0000000001', 'activation_ordinal': 1, 'attempt_id': 'attempt-1', 'sequence': 1,
             'type': 'provider_progress', 'payload': {'kind': 'text'}, 'created_at': '2026-11-12T00:00:00.000Z'}
    page = {'schema_version': 'agent_execution_v1', 'idempotency_key': 'key-1', 'events': [event],
            'next_cursor': '000001:0000000001', 'complete': False}
    assert AgentExecutionEvents.model_validate(page).model_dump(mode='json') == page
    for change in ({'next_cursor': '1:1'}, {'complete': 'no'}, {'extra': 1}):
        with pytest.raises(ValidationError):
            AgentExecutionEvents.model_validate({**page, **change})
    with pytest.raises(ValidationError):
        AgentExecutionEvents.model_validate({**page, 'events': [{**event, 'activation_ordinal': 0}]})
