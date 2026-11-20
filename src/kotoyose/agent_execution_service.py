"""Admission and observation of durable Hub logical executions."""
import asyncio
import logging
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from pydantic import TypeAdapter, ValidationError

from .agent_execution_contract import AgentExecutionAck, AgentExecutionAction, AgentExecutionCancel, AgentExecutionCreate, AgentExecutionError, AgentExecutionEvents, Key, validate_create
from .agent_execution_contract import parse_event_cursor
from .agent_execution_digest import agent_digest, agent_request_digest
from .agent_execution_digest import parse_agent_utc
from .agent_execution_store import AgentExecutionStore
from .agent_work_admission import pin_work
from .agent_execution_authority import AgentExecutionLeaseRegistry, apply_authorization
from .agent_execution_runner import AgentExecutionRunner
from .agent_execution_provider import TRY_NOW_DEDUP_MS
from .agent_execution_resume import session_available
from .agent_execution_output import validate_result_schema
from .agent_execution_store import _row
from .agent_execution_client_tools import AgentExecutionClientPolicy, AgentExecutionClientTools
from .agent_execution_recovery import AgentExecutionRecovery


log = logging.getLogger(__name__)

_accepted_decision = ContextVar('agent_execution_accepted_decision', default=None)


class _DecisionLeaseRegistry(AgentExecutionLeaseRegistry):
    def prepare(self, *args, **kwargs):
        marker = _accepted_decision.get()
        if marker is not None:
            marker.append(True)
        return super().prepare(*args, **kwargs)

    def fence(self, *args, **kwargs):
        marker = _accepted_decision.get()
        if marker is not None:
            marker.append(True)
        return super().fence(*args, **kwargs)


class AgentExecutionService:
    def __init__(self, execution, *, clock=None):
        self.execution = execution
        self.store = AgentExecutionStore(getattr(execution.store, 'pool', None))
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.leases = _DecisionLeaseRegistry(self.clock)
        self.runner = AgentExecutionRunner(self)
        self.recovery = AgentExecutionRecovery(self)
        self._background = None
        self._client_tools = None
        execution.agent_executor.agent_execution_leases = self.leases

    @property
    def client_tools(self):
        if self._client_tools is None:
            self.execution.store.client_tools.agent_execution_policy = AgentExecutionClientPolicy(self)
            self._client_tools = AgentExecutionClientTools(self)
        return self._client_tools

    async def _kick(self, execution_id):
        # Install the bridge policy before an agent can issue a client call.
        self.client_tools
        await self.runner.kick(execution_id)

    async def recover(self) -> None:
        await self.recovery.recover()

    def start_background(self) -> None:
        if self._background is None:
            self._background = asyncio.create_task(self._background_loop())

    async def stop_background(self) -> None:
        if self._background is not None:
            self._background.cancel()
            await asyncio.gather(self._background, return_exceptions=True)
            self._background = None

    async def _background_loop(self) -> None:
        while True:
            try:
                await self.run_due()
                await self.recovery.reconcile_pass(self.clock())
                rows = await self.store.pool.fetch("""SELECT e.id FROM kotoyose.agent_executions e
                    JOIN kotoyose.agent_execution_activations a ON a.execution_id=e.id AND a.nonce=e.current_nonce
                    WHERE e.state!='terminal' AND NOT e.admission_closed AND a.state='awaiting'""")
                for row in rows:
                    await self._kick(row['id'])
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception('hub execution background pass failed')
            await asyncio.sleep(1)

    async def cancel(self, principal, key, raw) -> tuple[int, dict]:
        try:
            TypeAdapter(Key).validate_python(key)
            action = AgentExecutionCancel.model_validate(raw)
            digest = agent_digest(raw)
        except (ValidationError, ValueError, TypeError) as exc:
            raise AgentExecutionError('invalid_request', 'Invalid cancel', 422) from exc
        now = self.clock()
        attempt_id = None
        error = None
        async with self.store.pool.acquire() as conn, conn.transaction():
            await conn.execute('SELECT pg_advisory_xact_lock(hashtextextended($1,0))', 'kotoyose-caller:' + principal)
            raw_row = await conn.fetchrow('SELECT * FROM kotoyose.agent_executions WHERE principal=$1 AND idempotency_key=$2 FOR UPDATE', principal, key)
            if raw_row is None:
                closed = await self.store.close_key(principal, key, raw, now, conn=conn)
                return 200, closed
            row = _row(raw_row)
            old = await conn.fetchrow('SELECT kind,body_digest FROM kotoyose.agent_execution_actions WHERE execution_id=$1 AND action_key=$2', row['id'], action.action_key)
            if old and (old['kind'] != 'cancel' or old['body_digest'] != digest):
                error = ('request_conflict', 'Action key has different body')
            elif not old:
                await conn.execute("INSERT INTO kotoyose.agent_execution_actions(execution_id,action_key,kind,body_digest,created_at) VALUES($1,$2,'cancel',$3,$4)", row['id'], action.action_key, digest, now)
                if row['state'] != 'terminal':
                    await conn.execute("UPDATE kotoyose.agent_executions SET cancel_requested=true,admission_closed=true,authority_required=false,sequence=sequence+1,state_revision=state_revision+1,updated_at=$2 WHERE id=$1", row['id'], now)
                    await conn.execute("UPDATE kotoyose.agent_execution_activations SET state='closed' WHERE execution_id=$1 AND state='awaiting'", row['id'])
                    attempt_id = await conn.fetchval("SELECT attempt_id FROM kotoyose.agent_execution_activations WHERE execution_id=$1 AND state IN ('starting','running') ORDER BY ordinal DESC LIMIT 1", row['id'])
                    await self.store.close_open_units(conn, row['id'], now)
                    pending = await conn.fetchval("""SELECT EXISTS(SELECT 1 FROM kotoyose.agent_execution_activations a
                        LEFT JOIN kotoyose.execution_attempts t ON t.id=a.attempt_id WHERE a.execution_id=$1 AND
                        (a.state IN ('starting','running') OR (a.attempt_id IS NOT NULL AND
                        (t.id IS NULL OR t.stop_confirmation!='confirmed' OR t.side_effect_resolution!='resolved'))))""", row['id'])
                    if not pending:
                        await self.store.finish_terminal(conn, row['id'], {'kind': 'cancelled'}, now)
            if old and error is None and row['state'] != 'terminal':
                attempt_id = await conn.fetchval("SELECT attempt_id FROM kotoyose.agent_execution_activations WHERE execution_id=$1 AND state IN ('starting','running') ORDER BY ordinal DESC LIMIT 1", row['id'])
        if attempt_id:
            await self.execution.store.cancel(principal, attempt_id, 'agent_execution_cancelled')
            self.runner.interrupt(str(row['id']), 'cancel')
        snapshot = await self.store.snapshot(principal, key, 0, now)
        if error:
            raise AgentExecutionError(error[0], error[1], 409, snapshot)
        return 202, snapshot

    async def _replay(self, principal, key, digest):
        existing = await self.store.find(principal, key)
        if existing is None:
            return None
        if existing.get('closed'):
            raise AgentExecutionError('request_conflict', 'Key was closed', 409)
        if existing['request_digest'] != digest:
            current = await self.store.snapshot(principal, key, 0, self.clock())
            raise AgentExecutionError('request_conflict', 'Key has a different request', 409, current)
        return AgentExecutionAck.model_validate({'schema_version': 'agent_execution_v1', 'execution_id': str(existing['id']),
            'idempotency_key': key, 'request_digest': digest}).model_dump(mode='json')

    async def create(self, principal, policy, raw, api_key_name: str | None = None) -> dict:
        try:
            create = AgentExecutionCreate.model_validate(raw)
            validate_create(create, raw)
            digest = agent_request_digest(raw)
        except (ValidationError, ValueError, KeyError, TypeError) as exc:
            raise AgentExecutionError('invalid_request', 'Invalid execution request', 422) from exc
        schema = create.execution.work.result_contract.json_schema
        if schema is not None:
            validate_result_schema(schema)
        key = create.execution.idempotency_key
        replay = await self._replay(principal, key, digest)
        if replay is not None:
            return replay
        self.execution._require_accepting()
        try:
            plan = await pin_work(self.execution, principal, policy, create, api_key_name)
            self.execution._require_accepting()
        except Exception:
            replay = await self._replay(principal, key, digest)
            if replay is not None:
                return replay
            raise
        row, created = await self.store.create(principal, raw, request_digest=digest,
            work_digest=create.execution.work_digest, pinned_plan=plan, now=self.clock())
        if not created:
            return await self._replay(principal, key, digest)
        allow = raw['execution']['initial_authorization']
        ceiling = parse_agent_utc(allow['authority_ceiling_at']) if allow['authority_ceiling_at'] else None
        self.leases.update((str(row['id']), allow['activation_nonce']), allow, base_active_ms=0,
            grace_ms=create.execution.time.grace_ms, ceiling_at=ceiling)
        await self._kick(str(row['id']))
        return AgentExecutionAck.model_validate({'schema_version': 'agent_execution_v1', 'execution_id': str(row['id']),
            'idempotency_key': key, 'request_digest': digest}).model_dump(mode='json')

    async def read(self, principal, key, after_receipt) -> dict:
        await self.runner.observe(principal, key)
        return await self.store.snapshot(principal, key, after_receipt, self.clock())

    async def events(self, principal, key, after: str, limit: int) -> dict:
        cursor = parse_event_cursor(after)
        page = await self.store.events(principal, key, cursor, limit)
        return AgentExecutionEvents.model_validate(page).model_dump(mode='json')

    async def authorize(self, principal, key, raw) -> dict:
        accepted = []
        token = _accepted_decision.set(accepted)
        try:
            snapshot = await apply_authorization(self, principal, key, raw, self.clock())
        finally:
            _accepted_decision.reset(token)
        if accepted:
            if raw['decision'] == 'deny':
                self.runner.interrupt(snapshot['execution_id'], 'deny')
            else:
                await self._kick(snapshot['execution_id'])
        return snapshot

    async def _probe(self, raw, now):
        row = _row(raw)
        attempt_id = await self.store.pool.fetchval('''SELECT attempt_id FROM kotoyose.agent_execution_activations
            WHERE execution_id=$1 AND attempt_id IS NOT NULL ORDER BY ordinal DESC LIMIT 1''', row['id'])
        previous = await self.execution.store.attempt(row['principal'], attempt_id) if attempt_id else None
        cause = await session_available(self.execution, previous) if previous else 'no_session'
        limit = row['provider_limit']
        if cause != 'adapter_unsupported' and limit and limit['resume'] == 'unavailable':
            cause = limit['unavailable_reason'] or 'no_session'
        async with self.store.pool.acquire() as conn, conn.transaction():
            current = await self.store.lock_execution(conn, row['id'])
            if (current['state'] != 'waiting_provider' or current['admission_closed'] or current['next_probe_at'] is None
                    or current['next_probe_at'] > now or current['next_probe_at'] != row['next_probe_at']):
                return False
            if cause:
                refs = [item['ref'] for item in previous['artifacts']] if previous else []
                terminal = {'kind': 'resume_unavailable', 'cause': cause, 'handoff_refs': refs,
                    'workspace_ref': current['workspace_ref'], 'work_digest': current['work_digest']}
                await self.store.finish_terminal(conn, row['id'], terminal, now)
                await conn.execute('UPDATE kotoyose.agent_executions SET next_probe_at=NULL WHERE id=$1', row['id'])
            else:
                act = await conn.fetchrow('SELECT * FROM kotoyose.agent_execution_activations WHERE execution_id=$1 AND nonce=$2', row['id'], current['current_nonce'])
                ordinal = act['ordinal'] + 1
                unit = await conn.fetchval('SELECT COALESCE(MAX(unit_ordinal),0)+1 FROM kotoyose.agent_execution_charge_units WHERE execution_id=$1', row['id'])
                nonce = 'act:' + uuid4().hex
                await conn.execute("INSERT INTO kotoyose.agent_execution_charge_units(execution_id,unit_ordinal,state) VALUES($1,$2,'open')", row['id'], unit)
                await conn.execute("INSERT INTO kotoyose.agent_execution_activations(execution_id,nonce,ordinal,reason,unit_ordinal,state,launch,measurement) VALUES($1,$2,$3,'provider_resume',$4,'awaiting','not_started','none')", row['id'], nonce, ordinal, unit)
                await conn.execute("UPDATE kotoyose.agent_executions SET state='resuming',current_nonce=$2,authority_required=true,next_probe_at=NULL,sequence=sequence+1,state_revision=state_revision+1,updated_at=$3 WHERE id=$1", row['id'], nonce, now)
            return True

    async def run_due(self, now: datetime | None = None) -> int:
        now = now or self.clock()
        rows = await self.store.pool.fetch("SELECT * FROM kotoyose.agent_executions WHERE state='waiting_provider' AND next_probe_at <= $1", now)
        count = 0
        for row in rows:
            count += int(await self._probe(row, now))
        return count

    async def try_now(self, principal, key, raw) -> dict:
        try:
            action = AgentExecutionAction.model_validate(raw)
            digest = agent_digest(raw)
        except (ValidationError, ValueError, TypeError) as exc:
            raise AgentExecutionError('invalid_request', 'Invalid action', 422) from exc
        now = self.clock()
        error = None
        probe = False
        async with self.store.pool.acquire() as conn, conn.transaction():
            raw_row = await conn.fetchrow('SELECT * FROM kotoyose.agent_executions WHERE principal=$1 AND idempotency_key=$2 FOR UPDATE', principal, key)
            if not raw_row:
                raise AgentExecutionError('not_found', 'Execution not found', 404)
            row = _row(raw_row)
            old = await conn.fetchrow('SELECT kind,body_digest FROM kotoyose.agent_execution_actions WHERE execution_id=$1 AND action_key=$2', row['id'], action.action_key)
            if row['admission_closed']:
                error = ('request_conflict', 'Execution is closed')
            elif old:
                if old['kind'] != 'try_now' or old['body_digest'] != digest:
                    error = ('request_conflict', 'Action key has different body')
            elif action.expected_revision != row['state_revision']:
                error = ('stale_revision', 'State revision is stale')
            elif row['state'] != 'waiting_provider':
                error = ('request_conflict', 'Execution is not waiting for provider')
            else:
                await conn.execute("INSERT INTO kotoyose.agent_execution_actions(execution_id,action_key,kind,body_digest,created_at) VALUES($1,$2,'try_now',$3,$4)", row['id'], action.action_key, digest, now)
                if row['last_try_now_at'] is None or now - row['last_try_now_at'] >= timedelta(milliseconds=TRY_NOW_DEDUP_MS):
                    await conn.execute('UPDATE kotoyose.agent_executions SET next_probe_at=$2,last_try_now_at=$2,sequence=sequence+1,state_revision=state_revision+1,updated_at=$2 WHERE id=$1', row['id'], now)
                    probe = True
        if probe:
            candidate = await self.store.pool.fetchrow('SELECT * FROM kotoyose.agent_executions WHERE id=$1', row['id'])
            if candidate:
                await self._probe(candidate, now)
        snapshot = await self.store.snapshot(principal, key, 0, now)
        if error:
            raise AgentExecutionError(error[0], error[1], 409, snapshot)
        return snapshot
