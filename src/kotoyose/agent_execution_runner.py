"""Run one authorized Hub activation and settle its durable evidence."""
from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal, Protocol
from uuid import UUID, uuid4

from .agent_deadlines import AgentStartRejected
from .execution_resources import ExecutionError
from .agent_execution_contract import AgentExecutionCreate
from .agent_execution_digest import parse_agent_utc
from .agent_execution_store import _encoded, _json, _row
from .agent_execution_units import ActivationFacts
from .agent_work_admission import internal_agent_request
from .agent_execution_provider import next_probe_at, provider_limit_wire
from .agent_execution_resume import ResumeUnavailable, check_predecessor_resume, resume_snapshot, session_available
from .agent_execution_faults import classify_fault
from .agent_execution_output import check_output


class AgentExecutionAdmission(Protocol):
    execution_id: UUID
    async def check(self, conn) -> None: ...
    async def bind(self, conn, attempt_id: str) -> None: ...


@dataclass(frozen=True)
class ActivationEnd:
    kind: Literal['succeeded', 'lease_interrupted', 'stop_point', 'turns_cap', 'output_cap',
                  'provider_limit', 'approval_required', 'fault', 'deny', 'cancel', 'restart_fault']


def classify_end(attempt_row: dict, facts: ActivationFacts, *, interruption: str | None,
                 stop_point_reached: bool, cancel_requested: bool = False) -> ActivationEnd:
    if cancel_requested:
        return ActivationEnd('cancel')
    if (interruption == 'shutdown' and attempt_row['status'] == 'cancelled') or attempt_row['reason'] in ('system_shutdown', 'cancelled_before_start') or attempt_row['limits_state'] == 'not_started':
        return ActivationEnd('lease_interrupted')
    if attempt_row['reason'] == 'coordinator_restarted':
        return ActivationEnd('restart_fault')
    if interruption == 'deny':
        return ActivationEnd('deny')
    if attempt_row['status'] == 'succeeded' and facts.stop == 'confirmed':
        return ActivationEnd('succeeded')
    if attempt_row['reason'] == 'turn_limit_exceeded':
        return ActivationEnd('turns_cap')
    if attempt_row['reason'] in ('output_limit_exceeded', 'tool_output_limit_exceeded'):
        return ActivationEnd('output_cap')
    if interruption == 'lease_expired':
        return ActivationEnd('lease_interrupted')
    if attempt_row['reason'] == 'provider_usage_limit' and attempt_row['limits_provider'] is not None:
        return ActivationEnd('provider_limit')
    if attempt_row['status'] == 'approval_required':
        return ActivationEnd('approval_required')
    if attempt_row['status'] == 'timeout' and stop_point_reached:
        return ActivationEnd('stop_point')
    return ActivationEnd('fault')


class _Admission:
    def __init__(self, runner, execution_id, nonce):
        self.runner, self.execution_id, self.nonce = runner, UUID(str(execution_id)), nonce

    async def check(self, conn):
        hub = self.runner.service
        row = await hub.store.lock_execution(conn, self.execution_id)
        act = await conn.fetchrow('SELECT * FROM kotoyose.agent_execution_activations WHERE execution_id=$1 AND nonce=$2', self.execution_id, self.nonce)
        auth = await conn.fetchrow('SELECT body FROM kotoyose.agent_execution_authorizations WHERE execution_id=$1 AND revision=$2', self.execution_id, row['authorization_revision'])
        allow = _json(auth['body']) if auth else None
        total = await conn.fetchval('SELECT COALESCE(sum(active_ms),0) FROM kotoyose.agent_execution_activations WHERE execution_id=$1 AND nonce!=$2', self.execution_id, self.nonce)
        now = hub.clock()
        grace = timedelta(milliseconds=row['request']['execution']['time']['grace_ms'])
        ceiling = parse_agent_utc(allow['authority_ceiling_at']) if allow and allow.get('authority_ceiling_at') else None
        if (row['state'] == 'terminal' or row['admission_closed'] or row['current_nonce'] != self.nonce
                or not act or act['state'] != 'awaiting' or not allow or allow['decision'] != 'allow'
                or allow['activation_nonce'] != self.nonce or parse_agent_utc(allow['not_before']) > now
                or parse_agent_utc(allow['expires_at']) - now <= grace
                or (ceiling is not None and now >= ceiling - grace) or allow['allowed_active_ms'] <= total):
            raise AgentStartRejected('grant_expired_before_start', 'Hub authorization cannot start', 409)
        hub.leases.assert_startable((str(self.execution_id), self.nonce))

    async def bind(self, conn, attempt_id):
        await conn.execute("UPDATE kotoyose.agent_execution_activations SET state='starting',attempt_id=$3 WHERE execution_id=$1 AND nonce=$2", self.execution_id, self.nonce, attempt_id)
        await conn.execute("UPDATE kotoyose.agent_executions SET state='running',authority_required=false,sequence=sequence+1,state_revision=state_revision+1,updated_at=$2 WHERE id=$1", self.execution_id, self.runner.service.clock())


class AgentExecutionRunner:
    def __init__(self, hub):
        self.service = hub
        self._running = {}
        self._interruptions = {}

    async def kick(self, execution_id) -> None:
        await self.start(execution_id)

    async def observe(self, principal, key):
        row = await self.service.store.pool.fetchrow('''SELECT e.id,a.nonce,a.attempt_id FROM kotoyose.agent_executions e
            JOIN kotoyose.agent_execution_activations a ON a.execution_id=e.id AND a.nonce=e.current_nonce
            WHERE e.principal=$1 AND e.idempotency_key=$2 AND a.state IN ('starting','running')''', principal, key)
        if not row or not row['attempt_id']:
            return
        tracker = self.service.execution.agent_executor.work_clocks.get(row['attempt_id'])
        if tracker is not None:
            await tracker.checkpoint()
        async with self.service.store.pool.acquire() as conn, conn.transaction():
            execution = await self.service.store.lock_execution(conn, row['id'])
            if execution['current_nonce'] != row['nonce'] or execution['state'] != 'running':
                return
            changed = await conn.fetchrow('''UPDATE kotoyose.agent_execution_activations a SET
                state=CASE WHEN t.limits_started_at IS NOT NULL THEN 'running' ELSE a.state END,
                launch=CASE WHEN t.limits_started_at IS NOT NULL THEN 'started' ELSE a.launch END,
                active_ms=COALESCE(t.limits_active_ms,0),turns=t.limits_turns,output_bytes=t.limits_output_bytes,
                started_at=t.limits_started_at FROM kotoyose.execution_attempts t
                WHERE a.execution_id=$1 AND a.nonce=$2 AND a.attempt_id=t.id AND a.state IN ('starting','running')
                AND (a.active_ms,a.turns,a.output_bytes,a.started_at,a.launch) IS DISTINCT FROM
                    (COALESCE(t.limits_active_ms,0),t.limits_turns,t.limits_output_bytes,t.limits_started_at,
                     CASE WHEN t.limits_started_at IS NOT NULL THEN 'started' ELSE a.launch END)
                RETURNING a.nonce''', row['id'], row['nonce'])
            if changed:
                await conn.execute('UPDATE kotoyose.agent_executions SET sequence=sequence+1,updated_at=$2 WHERE id=$1', row['id'], self.service.clock())

    async def start(self, execution_id) -> bool:
        eid = UUID(str(execution_id))
        raw = await self.service.store.pool.fetchrow('SELECT * FROM kotoyose.agent_executions WHERE id=$1', eid)
        if not raw:
            return False
        row = _row(raw)
        act = await self.service.store.pool.fetchrow('SELECT * FROM kotoyose.agent_execution_activations WHERE execution_id=$1 AND nonce=$2', eid, row['current_nonce'])
        if (not act or act['state'] != 'awaiting' or act['reason'] not in ('initial', 'provider_resume', 'lease_resume', 'fault_retry', 'approval_continue')):
            return False
        key = (str(eid), act['nonce'])
        try:
            self.service.leases.assert_startable(key)
        except AgentStartRejected:
            return False
        auth = await self.service.store.pool.fetchrow('SELECT body FROM kotoyose.agent_execution_authorizations WHERE execution_id=$1 AND revision=$2', eid, row['authorization_revision'])
        allow = _json(auth['body'])
        totals = await self.service.store.pool.fetchrow('SELECT COALESCE(sum(turns),0) AS turns,COALESCE(sum(output_bytes),0) AS output_bytes FROM kotoyose.agent_execution_activations WHERE execution_id=$1', eid)
        body = row['request']['execution']
        turns = body['resources']['max_turns'] - int(totals['turns'])
        output = body['resources']['max_output_bytes'] - int(totals['output_bytes'])
        if turns < 1 or output < 1:
            async with self.service.store.pool.acquire() as conn, conn.transaction():
                await _Admission(self, eid, act['nonce']).check(conn)
                await conn.execute("UPDATE kotoyose.agent_execution_activations SET state='closed' WHERE execution_id=$1 AND nonce=$2", eid, act['nonce'])
                await self.service.store.close_open_units(conn, eid, self.service.clock())
                await self.service.store.finish_terminal(conn, eid, {'kind': 'cost_limit', 'limit': 'turns' if turns < 1 else 'output_bytes'}, self.service.clock())
            return False
        workspace_ref = f"workspace:{row['workspace_id']}:{row['workspace_generation']}"
        request = internal_agent_request(AgentExecutionCreate.model_validate(row['request']),
            idempotency_key=f"hub:{eid}:{act['nonce']}", workspace_ref=workspace_ref, budgets_ref=row['pinned_plan']['budgets_ref'])
        snapshot = copy.deepcopy(row['pinned_plan'])
        snapshot.update(workspace_ref=workspace_ref, generation=row['workspace_generation'], deadline=allow['expires_at'],
            agent_activation={'execution_id': str(eid), 'nonce': act['nonce'], 'ordinal': act['ordinal'], 'reason': act['reason'],
                            'approval': _json(act['approval']) if act['approval'] else None})
        snapshot['timeout']['hard_seconds'] = body['time']['run_hard_ms'] / 1000
        snapshot['timeout']['grace_seconds'] = body['time']['grace_ms'] / 1000
        snapshot['definition']['max_turns'] = turns
        snapshot['budget']['output_limit_bytes'] = min(snapshot['budget']['output_limit_bytes'], output)
        try:
            if act['reason'] == 'initial' and body['work']['continuation_mode'] == 'safe_resume':
                previous = await check_predecessor_resume(self.service.execution, row['principal'], row)
                snapshot = await resume_snapshot(self.service.execution, row['principal'], request, snapshot, previous, run_bound=True)
            elif act['reason'] in ('provider_resume', 'lease_resume', 'approval_continue'):
                previous_id = await self.service.store.pool.fetchval('''SELECT attempt_id FROM kotoyose.agent_execution_activations
                    WHERE execution_id=$1 AND ordinal<$2 AND attempt_id IS NOT NULL ORDER BY ordinal DESC LIMIT 1''', eid, act['ordinal'])
                if previous_id is None:
                    raise ResumeUnavailable('no_session')
                previous = await self.service.execution.store.attempt(row['principal'], previous_id)
                snapshot = await resume_snapshot(self.service.execution, row['principal'], request, snapshot, previous)
            if snapshot.get('backend') == 'print_cli':
                from .print_cli_agent import has_print_cli_home_capacity
                if not has_print_cli_home_capacity(snapshot['print_cli_profile'],
                                                       snapshot['login_home'] or snapshot['connection_id']):
                    raise AgentStartRejected('capacity_exhausted', 'Print CLI login home concurrency limit reached', 409)
            from .config import get_clients

            policy = get_clients()[row['principal']]
            attempt, created = await self.service.execution.store.admit(row['principal'], request.model_dump(mode='json'), snapshot,
                policy.max_concurrent, agent_execution=_Admission(self, eid, act['nonce']))
        except ResumeUnavailable as exc:
            await self._resume_unavailable(eid, act, exc.cause)
            return False
        except (AgentStartRejected, ExecutionError) as exc:
            if isinstance(exc, ExecutionError) and exc.code == 'resume_unsafe':
                cause = ('adapter_unsupported' if exc.reason_code == 'adapter_unsupported' else 'predecessor_mismatch') if act['reason'] == 'initial' and body['work']['continuation_mode'] == 'safe_resume' else ('adapter_unsupported' if exc.reason_code in ('adapter_unsupported', 'provider_mismatch', 'runtime_mismatch') else 'not_retained')
                await self._resume_unavailable(eid, act, cause)
                return False
            # 食い違い (409) と満杯 (capacity_exhausted: caller の同時数・コンテナ数・disk、
            # および containers_unknown / disk_unknown) は、activation を awaiting のまま残して
            # 後の見回りで取り直す。満杯は失敗ではない。
            if isinstance(exc, AgentStartRejected) or exc.status == 409 or exc.code == 'capacity_exhausted':
                return False
            raise
        if not created:
            return False
        cancel = self.service.execution._schedule(attempt, after=self.settle)
        self._running[str(eid)] = (key, cancel)
        asyncio.create_task(self._watch(eid, key, cancel))
        return True

    async def _resume_unavailable(self, eid, act, cause):
        now = self.service.clock()
        async with self.service.store.pool.acquire() as conn, conn.transaction():
            row = await self.service.store.lock_execution(conn, eid)
            if row['current_nonce'] != act['nonce'] or row['state'] == 'terminal' or row['admission_closed']:
                return
            closed = await conn.fetchval("UPDATE kotoyose.agent_execution_activations SET state='closed' WHERE execution_id=$1 AND nonce=$2 AND state='awaiting' RETURNING nonce", eid, act['nonce'])
            if closed is None:
                return
            refs = [r['ref'] for r in await conn.fetch('''SELECT ref FROM kotoyose.execution_artifacts WHERE attempt_id=(
                SELECT attempt_id FROM kotoyose.agent_execution_activations WHERE execution_id=$1 AND attempt_id IS NOT NULL ORDER BY ordinal DESC LIMIT 1) ORDER BY name''', eid)]
            await self.service.store.close_open_units(conn, eid, now)
            await self.service.store.finish_terminal(conn, eid, {'kind': 'resume_unavailable', 'cause': cause,
                'handoff_refs': refs, 'workspace_ref': row['workspace_ref'] or row['request']['execution']['work']['workspace_ref'], 'work_digest': row['work_digest']}, now)

    async def _watch(self, eid, key, cancel):
        last_expiry = self.service.leases.lease_expires_at(key)
        while self._running.get(str(eid), (None,))[0] == key:
            expiry = self.service.leases.lease_expires_at(key) or last_expiry
            if expiry is None:
                await asyncio.sleep(.05)
                continue
            last_expiry = expiry
            delay = (expiry - self.service.clock()).total_seconds()
            if delay <= 0:
                self.interrupt(str(eid), 'lease_expired')
                return
            await asyncio.sleep(min(delay, .05))

    def mark_shutdown(self) -> None:
        for execution_id in self._running:
            self._interruptions.setdefault(execution_id, ('shutdown', self.service.clock()))

    def interrupt(self, execution_id, reason: Literal['lease_expired', 'deny', 'cancel']) -> None:
        entry = self._running.get(str(execution_id))
        if entry:
            if reason in ('deny', 'cancel') or str(execution_id) not in self._interruptions:
                self._interruptions[str(execution_id)] = (reason, self.service.clock())
            entry[1].set()

    async def settle(self, attempt: dict) -> None:
        eid = UUID(attempt['snapshot']['agent_activation']['execution_id'])
        nonce = attempt['snapshot']['agent_activation']['nonce']
        current = await self.service.execution.store.attempt(attempt['principal'], attempt['id'])
        if self._running.get(str(eid), (None,))[0] == (str(eid), nonce):
            self._running.pop(str(eid), None)
        launch = ('started' if current['limits_started_at'] else 'not_started' if current['limits_state'] in ('ended', 'not_started') else 'unknown')
        facts = ActivationFacts(nonce, launch, 'complete' if current['limits_state'] == 'ended' else 'unknown',
            current['limits_active_ms'] or 0, current['limits_turns'], current['limits_output_bytes'],
            current['reason'] == 'provider_usage_limit' and current['limits_turns'] == 0,
            current['stop_confirmation'], current['side_effect_resolution'])
        if current['status'] == 'succeeded' and current['snapshot'].get('agent_result_contract', {}).get('format') == 'json':
            output = next((item for item in current['artifacts'] if item['name'] == 'output'), None)
            content = (await self.service.execution.store.artifact(current['principal'], output['ref'], attempt_id=current['id']))['content'] if output else b''
            invalid = check_output(current['snapshot']['agent_result_contract'], content)
            if invalid:
                current = {**current, 'status': 'failed', 'reason': invalid}
        resume_cause = None
        if current['reason'] == 'resume_unsafe' and launch == 'not_started':
            previous_id = await self.service.store.pool.fetchval('''SELECT attempt_id FROM kotoyose.agent_execution_activations
                WHERE execution_id=$1 AND ordinal<(SELECT ordinal FROM kotoyose.agent_execution_activations
                WHERE execution_id=$1 AND nonce=$2) AND attempt_id IS NOT NULL ORDER BY ordinal DESC LIMIT 1''', eid, nonce)
            previous = await self.service.execution.store.attempt(current['principal'], previous_id) if previous_id else None
            resume_cause = (await session_available(self.service.execution, previous) if previous else 'no_session') or 'not_retained'
        async with self.service.store.pool.acquire() as conn, conn.transaction():
            row = await self.service.store.lock_execution(conn, eid)
            act = await conn.fetchrow('SELECT * FROM kotoyose.agent_execution_activations WHERE execution_id=$1 AND nonce=$2', eid, nonce)
            if row['state'] == 'terminal' or row['current_nonce'] != nonce:
                return
            # この attempt の settle は済んでいる (停止の確認の後に世代を上げた)。二度目は何もしない。
            if act['state'] == 'ended' and row['workspace_generation'] > current['generation']:
                return
            interruption = _json(act['interruption']) if act['interruption'] else None
            recorded = self._interruptions.get(str(eid))
            reason = interruption['kind'] if interruption else recorded[0] if recorded else None
            now = self.service.clock()
            await conn.execute('''UPDATE kotoyose.agent_execution_activations SET state='ended',launch=$3,measurement=$4,
                active_ms=$5,turns=$6,output_bytes=$7,provider_rejected_only=$8,started_at=$9,finished_at=$10
                WHERE execution_id=$1 AND nonce=$2''', eid, nonce, facts.launch, facts.measurement, max(facts.active_ms, act['active_ms']),
                facts.turns, facts.output_bytes, facts.provider_rejected_only, current['limits_started_at'], current['limits_finished_at'])
            await conn.execute('UPDATE kotoyose.agent_executions SET sequence=sequence+1,updated_at=$2 WHERE id=$1', eid, now)
            await conn.execute('''INSERT INTO kotoyose.agent_execution_client_replays(execution_id,call_id,tool_ref,arguments_digest)
                SELECT $1,id,tool_ref,arguments_digest FROM kotoyose.execution_client_calls
                WHERE attempt_id=$2 AND state='unknown' ON CONFLICT DO NOTHING''', eid, current['id'])
            if facts.turns is not None and facts.turns > 0:
                await conn.execute('UPDATE kotoyose.agent_executions SET probe_step=0 WHERE id=$1', eid)
            if facts.stop != 'confirmed' or facts.effects != 'resolved':
                return
            generation = current['generation'] + 1
            await conn.execute('UPDATE kotoyose.agent_executions SET workspace_generation=$2,workspace_ref=$3 WHERE id=$1', eid, generation, f"workspace:{row['workspace_id']}:{generation}")
            auth = await conn.fetchrow('SELECT body FROM kotoyose.agent_execution_authorizations WHERE execution_id=$1 AND revision=$2', eid, row['authorization_revision'])
            allow = _json(auth['body'])
            if allow['decision'] != 'allow':
                allow = _json(await conn.fetchval("SELECT body FROM kotoyose.agent_execution_authorizations WHERE execution_id=$1 AND nonce=$2 AND decision='allow' ORDER BY revision DESC LIMIT 1", eid, nonce))
            if (reason == 'lease_expired' and recorded and current['finished_at'] is not None
                    and current['finished_at'] < recorded[1]):
                reason = None
            if (reason is None and allow and current['finished_at'] is not None
                    and current['finished_at'] >= parse_agent_utc(allow['expires_at'])):
                reason = 'lease_expired'
            total = await conn.fetchval('SELECT COALESCE(sum(active_ms),0) FROM kotoyose.agent_execution_activations WHERE execution_id=$1', eid)
            end = classify_end(current, facts, interruption=reason, stop_point_reached=bool(allow and total >= allow['allowed_active_ms']), cancel_requested=row['cancel_requested'])
            if end.kind == 'cancel':
                await self.service.store.close_open_units(conn, eid, now)
                await self.service.store.finish_terminal(conn, eid, {'kind': 'cancelled'}, now)
            elif end.kind == 'restart_fault':
                await self.service.store.close_open_units(conn, eid, now)
                await self.service.store.finish_terminal(conn, eid, {'kind': 'execution_fault', 'cause': 'worker_lost', 'code': 'coordinator_restarted'}, now)
            elif resume_cause:
                refs = [item['ref'] for item in previous['artifacts']] if previous else []
                await self.service.store.close_open_units(conn, eid, now)
                await self.service.store.finish_terminal(conn, eid, {'kind': 'resume_unavailable', 'cause': resume_cause,
                    'handoff_refs': refs, 'workspace_ref': f"workspace:{row['workspace_id']}:{generation}",
                    'work_digest': row['work_digest']}, now)
            elif end.kind == 'lease_interrupted':
                if ((reason == 'shutdown' or current['reason'] in ('system_shutdown', 'cancelled_before_start'))
                        and facts.measurement != 'complete' and current['limits_state'] != 'not_started'):
                    return
                new_nonce = 'act:' + uuid4().hex
                await conn.execute("INSERT INTO kotoyose.agent_execution_activations(execution_id,nonce,ordinal,reason,unit_ordinal,state,launch,measurement) VALUES($1,$2,$3,'lease_resume',$4,'awaiting','not_started','none')", eid, new_nonce, act['ordinal'] + 1, act['unit_ordinal'])
                await conn.execute("UPDATE kotoyose.agent_executions SET state='resuming',current_nonce=$2,authority_required=true,state_revision=state_revision+1 WHERE id=$1", eid, new_nonce)
            elif end.kind == 'provider_limit':
                await self.service.store.close_open_units(conn, eid, now)
                limit = provider_limit_wire(current)
                step = 0 if facts.turns and facts.turns > 0 else row['probe_step']
                probe = next_probe_at(limit, step, now)
                known = bool(limit['reset_at'] and parse_agent_utc(limit['reset_at']) > parse_agent_utc(limit['detected_at']))
                await conn.execute('''UPDATE kotoyose.agent_executions SET state='waiting_provider',authority_required=false,
                    provider_limit=$2::jsonb,next_probe_at=$3,probe_step=$4,sequence=sequence+1,
                    state_revision=state_revision+1,updated_at=$5 WHERE id=$1''', eid, _encoded(limit), probe,
                    step if known else step + 1, now)
            elif end.kind == 'approval_required':
                action = _json(current['required_action'])
                if not action or not action.get('call_id') or not action.get('subject_digest'):
                    await self.service.store.close_open_units(conn, eid, now)
                    await self.service.store.finish_terminal(conn, eid, {'kind': 'execution_fault', 'cause': 'agent_failure', 'code': 'invalid_approval_action'}, now)
                else:
                    await self.service.store.close_open_units(conn, eid, now)
                    unit = await conn.fetchval('SELECT COALESCE(MAX(unit_ordinal),0)+1 FROM kotoyose.agent_execution_charge_units WHERE execution_id=$1', eid)
                    nonce = 'act:' + uuid4().hex
                    await conn.execute("INSERT INTO kotoyose.agent_execution_charge_units(execution_id,unit_ordinal,state) VALUES($1,$2,'open')", eid, unit)
                    await conn.execute("INSERT INTO kotoyose.agent_execution_activations(execution_id,nonce,ordinal,reason,unit_ordinal,state,launch,measurement) VALUES($1,$2,$3,'approval_continue',$4,'awaiting','not_started','none')", eid, nonce, act['ordinal'] + 1, unit)
                    pause = {'kind': 'approval_required', 'call_id': action['call_id'], 'subject_digest': action['subject_digest']}
                    await conn.execute("UPDATE kotoyose.agent_executions SET state='queued',current_nonce=$2,authority_required=true,pause=$3::jsonb,sequence=sequence+1,state_revision=state_revision+1,updated_at=$4 WHERE id=$1", eid, nonce, _encoded(pause), now)
            else:
                if end.kind == 'deny':
                    terminal = interruption['reason']
                elif end.kind == 'succeeded':
                    terminal = {'kind': 'succeeded'}
                elif end.kind in ('turns_cap', 'output_cap'):
                    terminal = {'kind': 'cost_limit', 'limit': 'turns' if end.kind == 'turns_cap' else 'output_bytes'}
                elif end.kind == 'stop_point':
                    terminal = {'kind': 'time_limit', 'limit': allow['stop_limit']}
                else:
                    decision = classify_fault(current, facts)
                    terminal = {'kind': 'execution_fault', 'cause': decision.cause, 'code': decision.code}
                    all_complete = not await conn.fetchval("""SELECT EXISTS(SELECT 1 FROM kotoyose.agent_execution_activations
                        WHERE execution_id=$1 AND launch!='not_started' AND
                        (launch!='started' OR measurement!='complete' OR turns IS NULL OR output_bytes IS NULL))""", eid)
                    if decision.retryable and all_complete and not row['cancel_requested'] and not row['admission_closed']:
                        await self.service.store.close_open_units(conn, eid, now)
                        if await self.service.store.claim_fault_retry(conn, eid, now):
                            terminal = None
                if terminal is not None:
                    await self.service.store.close_open_units(conn, eid, now)
                    await self.service.store.finish_terminal(conn, eid, terminal, now)
        self._interruptions.pop(str(eid), None)
        self.service.leases.forget((str(eid), nonce))
