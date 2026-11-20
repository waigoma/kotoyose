"""Authorization decisions and the runtime lease fence."""
from datetime import timedelta
from decimal import Decimal, localcontext

from pydantic import TypeAdapter, ValidationError

from .agent_deadlines import AgentStartRejected
from .execution_resources import ExecutionError
from .agent_execution_contract import AgentExecutionAuthorization, AgentExecutionAllow, AgentExecutionCreate, AgentExecutionError, validate_allow_bounds
from .agent_execution_digest import agent_digest, parse_agent_utc
from .agent_execution_store import _encoded, _json, _row

LeaseKey = tuple[str, str]


class AgentExecutionLeaseRegistry:
    def __init__(self, clock):
        self.clock = clock
        self._leases = {}
        self._revisions = {}
        self._pending = {}

    def update(self, key: LeaseKey, allow: dict, *, base_active_ms: int, grace_ms: int, ceiling_at) -> None:
        revision = allow.get('revision', 0)
        current = self._revisions.get(key, -1)
        if revision < current or (revision == current and self._pending.get(key) != 'allow'):
            return
        self._revisions[key] = revision
        self._pending.pop(key, None)
        self._leases[key] = (dict(allow), base_active_ms, grace_ms, ceiling_at)

    def forget(self, key: LeaseKey) -> None:
        self._leases.pop(key, None)

    def prepare(self, key: LeaseKey, revision: int, *, candidate=None, base_active_ms=0, grace_ms=0, ceiling_at=None) -> None:
        if revision >= self._revisions.get(key, -1):
            self._revisions[key] = revision
            self._pending[key] = 'allow'
            old = self._leases.get(key)
            if old is None or candidate is None:
                self.forget(key)
                return
            constrained = dict(candidate)
            constrained['not_before'] = max(old[0]['not_before'], candidate['not_before'], key=parse_agent_utc)
            constrained['expires_at'] = min(old[0]['expires_at'], candidate['expires_at'], key=parse_agent_utc)
            constrained['allowed_active_ms'] = max(0, min(old[0]['allowed_active_ms'] - old[1],
                                                          candidate['allowed_active_ms'] - base_active_ms))
            ceilings = [value for value in (old[3], ceiling_at) if value is not None]
            self._leases[key] = (constrained, 0, max(old[2], grace_ms), min(ceilings) if ceilings else None)

    def fence(self, key: LeaseKey, revision: int) -> None:
        if revision >= self._revisions.get(key, -1):
            self._revisions[key] = revision
            self._pending[key] = 'deny'
            self.forget(key)

    def remaining_seconds(self, key: LeaseKey, active_ms: int) -> float:
        lease = self._leases.get(key)
        return (lease[0]['allowed_active_ms'] - lease[1] - active_ms) / 1000 if lease else 0.0

    def lease_expires_at(self, key: LeaseKey):
        lease = self._leases.get(key)
        return parse_agent_utc(lease[0]['expires_at']) if lease else None

    def assert_startable(self, key: LeaseKey) -> None:
        lease = self._leases.get(key)
        now = self.clock()
        if (not lease or parse_agent_utc(lease[0]['not_before']) > now
                or parse_agent_utc(lease[0]['expires_at']) - now <= timedelta(milliseconds=lease[2])
                or (lease[3] is not None and now >= lease[3] - timedelta(milliseconds=lease[2]))
                or self.remaining_seconds(key, 0) <= 0):
            raise AgentStartRejected('grant_expired_before_start', 'Hub authorization cannot start', 409)

    def call_deadline(self, key: LeaseKey):
        lease = self._leases.get(key)
        if lease is None:
            raise AgentStartRejected('grant_expired_before_start', 'Hub authorization is missing', 409)
        deadline = self.clock() + timedelta(seconds=3600)
        return min(deadline, lease[3] - timedelta(milliseconds=lease[2])) if lease[3] else deadline


async def agent_execution_lease_guard(conn, attempt_id: str, now) -> None:
    row = await conn.fetchrow('''SELECT e.state,e.admission_closed,e.current_nonce,a.nonce,z.body
        FROM kotoyose.agent_execution_activations a JOIN kotoyose.agent_executions e ON e.id=a.execution_id
        LEFT JOIN kotoyose.agent_execution_authorizations z ON z.execution_id=e.id AND z.revision=e.authorization_revision
        WHERE a.attempt_id=$1 FOR SHARE OF e''', attempt_id)
    allow = _json(row['body']) if row and row['body'] else None
    if (not row or row['state'] == 'terminal' or row['admission_closed'] or row['nonce'] != row['current_nonce']
            or not allow or allow['decision'] != 'allow' or allow['activation_nonce'] != row['nonce']
            or not parse_agent_utc(allow['not_before']) <= now < parse_agent_utc(allow['expires_at'])):
        raise ExecutionError('permission_denied', 'Hub authorization does not admit operations', 403)


async def apply_authorization(hub, principal, key, decision, now) -> dict:
    try:
        parsed = TypeAdapter(AgentExecutionAuthorization).validate_python(decision)
        digest = agent_digest(decision)
    except (ValidationError, ValueError, TypeError) as exc:
        raise AgentExecutionError('invalid_request', 'Invalid authorization', 422) from exc
    registry_update = None
    registry_forget = None
    error = None
    async with hub.store.pool.acquire() as conn, conn.transaction():
        raw = await conn.fetchrow('SELECT * FROM kotoyose.agent_executions WHERE principal=$1 AND idempotency_key=$2 FOR UPDATE', principal, key)
        if not raw:
            raise AgentExecutionError('not_found', 'Execution not found', 404)
        row = _row(raw)
        existing = await conn.fetchrow('SELECT digest FROM kotoyose.agent_execution_authorizations WHERE execution_id=$1 AND revision=$2', row['id'], parsed.revision)
        # Cancellation fences even a replay; otherwise an exact replay returns the current snapshot.
        if row['cancel_requested'] and (not existing or parsed.decision != 'deny'):
            error = ('request_conflict', 'Execution is cancelled')
        elif existing:
            if existing['digest'] != digest:
                error = ('request_conflict', 'Revision has different authorization')
        elif parsed.revision <= row['authorization_revision']:
            error = ('stale_revision', 'Authorization revision is stale')
        elif parsed.work_digest != row['work_digest'] or parsed.caller_run_ref != row['caller_run_ref']:
            error = ('request_conflict', 'Authorization identity mismatch')
        elif row['state'] == 'terminal' or row['admission_closed']:
            error = ('request_conflict', 'Execution is closed')
        else:
            act = await conn.fetchrow('SELECT * FROM kotoyose.agent_execution_activations WHERE execution_id=$1 AND nonce=$2', row['id'], row['current_nonce'])
            if parsed.activation_nonce != row['current_nonce'] or (isinstance(parsed, AgentExecutionAllow) and act['state'] not in ('awaiting', 'starting', 'running')):
                error = ('stale_activation', 'Activation is stale')
            elif isinstance(parsed, AgentExecutionAllow):
                create = AgentExecutionCreate.model_validate(row['request'])
                validate_allow_bounds(create, parsed)
                approval = decision['approval']
                previous_approval = _json(act['approval']) if act['approval'] else None
                pause = row['pause']
                if act['reason'] == 'approval_continue' and previous_approval is None:
                    if (not approval or not pause or approval['call_id'] != pause['call_id']
                            or approval['subject_digest'] != pause['subject_digest']):
                        error = ('request_conflict', 'Approval does not match the pending call')
                elif approval is not None and (act['reason'] != 'approval_continue' or approval != previous_approval):
                    error = ('request_conflict', 'Approval does not match the activation')
                if error is None:
                    unit = await conn.fetchrow('SELECT * FROM kotoyose.agent_execution_charge_units WHERE execution_id=$1 AND unit_ordinal=$2', row['id'], act['unit_ordinal'])
                    charge = decision['charge']
                    existing_charge = ({k: unit[k] for k in ('charge_ref', 'reservation_ref', 'allocation_units')}
                                       if unit['charge_ref'] is not None else None)
                    if existing_charge is not None and existing_charge != charge:
                        error = ('request_conflict', 'Charge cannot change')
                    elif existing_charge is None and charge is not None:
                        used = await conn.fetchval('''SELECT EXISTS(SELECT 1 FROM kotoyose.agent_execution_charge_units
                            WHERE execution_id=$1 AND unit_ordinal!=$2 AND (charge_ref=$3 OR reservation_ref=$4))''',
                            row['id'], act['unit_ordinal'], charge['charge_ref'], charge['reservation_ref'])
                        amounts = await conn.fetch('''SELECT allocation_units FROM kotoyose.agent_execution_charge_units
                            WHERE execution_id=$1 AND state='open' AND unit_ordinal!=$2 AND allocation_units IS NOT NULL
                            UNION ALL SELECT body->>'units' FROM kotoyose.agent_execution_receipts
                            WHERE execution_id=$1 AND body->>'outcome'='executed' AND body->>'units' IS NOT NULL''', row['id'], act['unit_ordinal'])
                        values = [Decimal(x[0]) for x in amounts] + [Decimal(charge['allocation_units'])]
                        with localcontext() as context:
                            context.prec = max(len(x.as_tuple().digits) + abs(x.as_tuple().exponent) for x in values) + len(str(len(values))) + 1
                            total = sum(values, Decimal(0))
                        if used or total > Decimal(row['request']['execution']['money']['run_units_ceiling']):
                            error = ('request_conflict', 'Charge exceeds ceiling or is already used')
                        else:
                            await conn.execute('UPDATE kotoyose.agent_execution_charge_units SET charge_ref=$3,reservation_ref=$4,allocation_units=$5 WHERE execution_id=$1 AND unit_ordinal=$2', row['id'], act['unit_ordinal'], charge['charge_ref'], charge['reservation_ref'], charge['allocation_units'])
                if error is None:
                    if act['reason'] == 'approval_continue' and previous_approval is None:
                        await conn.execute('UPDATE kotoyose.agent_execution_activations SET approval=$3::jsonb WHERE execution_id=$1 AND nonce=$2', row['id'], act['nonce'], _encoded(approval))
                        await conn.execute('UPDATE kotoyose.agent_executions SET pause=NULL WHERE id=$1', row['id'])
                    # A committed decision must never leave an older, looser cached lease usable.
                    base = int(await conn.fetchval('SELECT COALESCE(SUM(active_ms),0) FROM kotoyose.agent_execution_activations WHERE execution_id=$1 AND nonce!=$2', row['id'], act['nonce']))
                    ceiling = parse_agent_utc(parsed.authority_ceiling_at) if parsed.authority_ceiling_at else None
                    hub.leases.prepare((str(row['id']), act['nonce']), parsed.revision, candidate=decision,
                        base_active_ms=base, grace_ms=create.execution.time.grace_ms, ceiling_at=ceiling)
                    required = not (parse_agent_utc(parsed.not_before) <= now < parse_agent_utc(parsed.expires_at))
                    await conn.execute('''UPDATE kotoyose.agent_executions SET authorization_revision=$2,settled_receipt_cursor=$3,
                        authority_required=$4,sequence=sequence+1,state_revision=state_revision+CASE WHEN authority_required!=$4 THEN 1 ELSE 0 END,updated_at=$5 WHERE id=$1''', row['id'], parsed.revision, parsed.settled_receipt_cursor, required, now)
                    registry_update = ((str(row['id']), act['nonce']), decision, base, create.execution.time.grace_ms, ceiling)
            else:
                reason = decision['reason']
                hub.leases.fence((str(row['id']), act['nonce']), parsed.revision)
                pending = await conn.fetchval('''SELECT EXISTS(SELECT 1 FROM kotoyose.agent_execution_activations a
                    LEFT JOIN kotoyose.execution_attempts t ON t.id=a.attempt_id WHERE a.execution_id=$1
                    AND (a.state IN ('starting','running') OR (a.attempt_id IS NOT NULL AND
                    (t.id IS NULL OR t.stop_confirmation!='confirmed' OR t.side_effect_resolution!='resolved'))))''', row['id'])
                await conn.execute('''UPDATE kotoyose.agent_executions SET authorization_revision=$2,settled_receipt_cursor=$3,
                    admission_closed=true,cancel_requested=$4,sequence=sequence+1,state_revision=state_revision+1,updated_at=$5 WHERE id=$1''',
                    row['id'], parsed.revision, parsed.settled_receipt_cursor, reason['kind'] == 'cancelled', now)
                if pending:
                    await conn.execute('UPDATE kotoyose.agent_execution_activations SET interruption=$3::jsonb WHERE execution_id=$1 AND nonce=$2', row['id'], act['nonce'], _encoded({'kind': 'deny', 'reason': reason}))
                else:
                    await conn.execute("UPDATE kotoyose.agent_execution_activations SET state='closed' WHERE execution_id=$1 AND state IN ('awaiting','starting')", row['id'])
                    await hub.store.close_open_units(conn, row['id'], now)
                    await hub.store.finish_terminal(conn, row['id'], reason, now)
                registry_forget = (str(row['id']), act['nonce'])
            if error is None:
                await conn.execute('INSERT INTO kotoyose.agent_execution_authorizations(execution_id,revision,nonce,decision,body,digest) VALUES($1,$2,$3,$4,$5::jsonb,$6)', row['id'], parsed.revision, parsed.activation_nonce, parsed.decision, _encoded(decision), digest)
    if registry_forget:
        hub.leases.fence(registry_forget, parsed.revision)
    if registry_update:
        lease_key, allow, base, grace, ceiling = registry_update
        hub.leases.update(lease_key, allow, base_active_ms=base, grace_ms=grace, ceiling_at=ceiling)
    snapshot = await hub.store.snapshot(principal, key, 0, now)
    if error:
        raise AgentExecutionError(error[0], error[1], 409, snapshot)
    return snapshot
