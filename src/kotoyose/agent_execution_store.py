"""Durable logical execution, idempotency, and receipt paging."""
from __future__ import annotations

import json
from decimal import Decimal, localcontext
from uuid import UUID, uuid4

import asyncpg

from .agent_execution_contract import AGENT_EXECUTION_SCHEMA_VERSION, RECEIPT_PAGE_LIMIT, AgentExecutionError, AgentExecutionReceipt, AgentExecutionSnapshot, event_cursor
from .agent_execution_digest import agent_digest, agent_utc, parse_agent_utc
from .agent_execution_units import ActivationFacts, close_unit_outcome, receipt_body


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def _row(row):
    result = dict(row)
    for key in ('request', 'pinned_plan', 'pause', 'provider_limit', 'terminal', 'body', 'artifact_manifest'):
        if key in result:
            result[key] = _json(result[key])
    return result


class AgentExecutionStore:
    def __init__(self, pool):
        self.pool = pool
    async def find(self, principal, key) -> dict | None:
        row = await self.pool.fetchrow('SELECT * FROM kotoyose.agent_executions WHERE principal=$1 AND idempotency_key=$2', principal, key)
        if row:
            return _row(row)
        tombstone = await self.pool.fetchrow('SELECT * FROM kotoyose.agent_execution_cancellations WHERE principal=$1 AND idempotency_key=$2', principal, key)
        return {'closed': True} if tombstone else None

    async def create(self, principal, raw, *, request_digest, work_digest, pinned_plan, now) -> tuple[dict, bool]:
        body = raw['execution']
        key, allow = body['idempotency_key'], body['initial_authorization']
        pred = body['predecessor']
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute('SELECT pg_advisory_xact_lock(hashtextextended($1,0))', 'kotoyose-caller:' + principal)
            existing = await conn.fetchrow('SELECT * FROM kotoyose.agent_executions WHERE principal=$1 AND idempotency_key=$2', principal, key)
            if existing:
                return _row(existing), False
            closed = await conn.fetchval('SELECT EXISTS(SELECT 1 FROM kotoyose.agent_execution_cancellations WHERE principal=$1 AND idempotency_key=$2)', principal, key)
            if closed:
                return {'closed': True}, False
            if pred and await conn.fetchval('SELECT EXISTS(SELECT 1 FROM kotoyose.agent_executions WHERE principal=$1 AND predecessor_execution_id=$2)', principal, pred['execution_id']):
                raise AgentExecutionError('request_conflict', 'Predecessor already has a successor', 409)
            execution_id = uuid4()
            authority_required = not (parse_agent_utc(allow['not_before']) <= now < parse_agent_utc(allow['expires_at']))
            row = await conn.fetchrow('''INSERT INTO kotoyose.agent_executions
                (id,principal,idempotency_key,request_digest,work_digest,caller_run_ref,request,pinned_plan,
                predecessor_execution_id,predecessor_key,predecessor_relation,state,state_revision,sequence,
                current_nonce,authorization_revision,settled_receipt_cursor,fault_retries_used,cancel_requested,
                admission_closed,authority_required,workspace_id,workspace_generation,workspace_ref,created_at,updated_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8::jsonb,$9,$10,$11,'queued',1,1,$12,$13,$14,$15,false,false,$16,$17,$18,NULL,$19,$19) RETURNING *''',
                execution_id, principal, key, request_digest, work_digest, body['caller_run_ref'], _encoded(raw), _encoded(pinned_plan),
                pred['execution_id'] if pred else None, pred['idempotency_key'] if pred else None, pred['relation'] if pred else None,
                allow['activation_nonce'], allow['revision'], allow['settled_receipt_cursor'], body['recovery']['fault_retries_used'],
                authority_required, pinned_plan['workspace_id'], pinned_plan['generation'], now)
            await conn.execute('''INSERT INTO kotoyose.agent_execution_activations
                (execution_id,nonce,ordinal,reason,unit_ordinal,state,launch,measurement)
                VALUES($1,$2,1,'initial',1,'awaiting','not_started','none')''', execution_id, allow['activation_nonce'])
            charge = allow['charge']
            await conn.execute('''INSERT INTO kotoyose.agent_execution_charge_units
                (execution_id,unit_ordinal,charge_ref,reservation_ref,allocation_units,state)
                VALUES($1,1,$2,$3,$4,'open')''', execution_id, charge['charge_ref'] if charge else None,
                charge['reservation_ref'] if charge else None, charge['allocation_units'] if charge else None)
            await conn.execute('''INSERT INTO kotoyose.agent_execution_authorizations
                (execution_id,revision,nonce,decision,body,digest) VALUES($1,$2,$3,'allow',$4::jsonb,$5)''',
                execution_id, allow['revision'], allow['activation_nonce'], _encoded(allow), agent_digest(allow))
            refs = {item['content_ref'] for item in body['work']['inputs'] if item['content_ref'].startswith('artifact:input:')}
            for ref in sorted(refs):
                artifact = await conn.fetchrow('SELECT state,expires_at FROM kotoyose.execution_input_artifacts WHERE principal=$1 AND ref=$2 FOR UPDATE', principal, ref)
                if not artifact or artifact['state'] != 'active' or artifact['expires_at'] <= now:
                    raise AgentExecutionError('permission_denied', 'Input artifact cannot be pinned', 403)
                await conn.execute('INSERT INTO kotoyose.agent_execution_input_pins(execution_id,artifact_ref,principal) VALUES($1,$2,$3)', execution_id, ref, principal)
                await conn.execute('UPDATE kotoyose.execution_input_artifacts SET pin_count=pin_count+1 WHERE ref=$1', ref)
            return _row(row), True

    async def claim_fault_retry(self, conn, execution_id, now) -> str | None:
        """呼び出し側の transaction 内で retry を取得し、新 nonce または None を返す。"""
        row = await conn.fetchrow("""UPDATE kotoyose.agent_executions SET fault_retries_used=1
            WHERE id=$1 AND fault_retries_used=0 AND NOT cancel_requested AND NOT admission_closed
            AND state!='terminal' RETURNING current_nonce""", execution_id)
        if row is None:
            return None
        act = await conn.fetchrow('SELECT ordinal FROM kotoyose.agent_execution_activations WHERE execution_id=$1 AND nonce=$2', execution_id, row['current_nonce'])
        unit = await conn.fetchval('SELECT COALESCE(MAX(unit_ordinal),0)+1 FROM kotoyose.agent_execution_charge_units WHERE execution_id=$1', execution_id)
        nonce = 'act:' + uuid4().hex
        await conn.execute("INSERT INTO kotoyose.agent_execution_charge_units(execution_id,unit_ordinal,state) VALUES($1,$2,'open')", execution_id, unit)
        await conn.execute("""INSERT INTO kotoyose.agent_execution_activations(execution_id,nonce,ordinal,reason,unit_ordinal,state,launch,measurement)
            VALUES($1,$2,$3,'fault_retry',$4,'awaiting','not_started','none')""", execution_id, nonce, act['ordinal'] + 1, unit)
        await conn.execute("""UPDATE kotoyose.agent_executions SET state='retrying',current_nonce=$2,authority_required=true,
            sequence=sequence+1,state_revision=state_revision+1,updated_at=$3 WHERE id=$1""", execution_id, nonce, now)
        return nonce

    async def close_key(self, principal, key, cancel: dict, now, *, conn=None) -> dict | None:
        if conn is None:
            async with self.pool.acquire() as connection, connection.transaction():
                await connection.execute('SELECT pg_advisory_xact_lock(hashtextextended($1,0))', 'kotoyose-caller:' + principal)
                return await self.close_key(principal, key, cancel, now, conn=connection)
        if await conn.fetchval('SELECT EXISTS(SELECT 1 FROM kotoyose.agent_executions WHERE principal=$1 AND idempotency_key=$2)', principal, key):
            return None
        row = await conn.fetchrow('''INSERT INTO kotoyose.agent_execution_cancellations(principal,idempotency_key,action_key,reason,proof_ref,created_at)
            VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT(principal,idempotency_key) DO NOTHING RETURNING *''',
            principal, key, cancel['action_key'], cancel['reason'], 'agent-closed:' + str(uuid4()), now)
        if not row:
            row = await conn.fetchrow('SELECT * FROM kotoyose.agent_execution_cancellations WHERE principal=$1 AND idempotency_key=$2', principal, key)
        return {'schema_version': AGENT_EXECUTION_SCHEMA_VERSION, 'idempotency_key': key, 'outcome': 'not_admitted',
                'admission_closed': True, 'execution_id': None, 'proof_ref': row['proof_ref']}

    async def lock_execution(self, conn, execution_id) -> dict:
        row = await conn.fetchrow('SELECT * FROM kotoyose.agent_executions WHERE id=$1 FOR UPDATE', UUID(str(execution_id)))
        if not row:
            raise AgentExecutionError('not_found', 'Execution not found', 404)
        return _row(row)

    async def finish_terminal(self, conn, execution_id, terminal: dict, now) -> None:
        execution_id = UUID(str(execution_id))
        pins = await conn.fetch('''UPDATE kotoyose.agent_execution_input_pins SET released_at=$2
            WHERE execution_id=$1 AND released_at IS NULL RETURNING artifact_ref''', execution_id, now)
        for pin in pins:
            await conn.execute('UPDATE kotoyose.execution_input_artifacts SET pin_count=pin_count-1 WHERE ref=$1', pin['artifact_ref'])
        await conn.execute('''UPDATE kotoyose.agent_executions SET state='terminal',terminal=$2::jsonb,
            admission_closed=true,authority_required=false,sequence=sequence+1,state_revision=state_revision+1,
            updated_at=$3 WHERE id=$1''', execution_id, _encoded(terminal), now)

    async def append_receipt(self, conn, execution_id, unit_ordinal, body: dict) -> dict:
        execution_id = UUID(str(execution_id))
        await self.lock_execution(conn, execution_id)
        cursor = await conn.fetchval('SELECT COALESCE(MAX(cursor),0)+1 FROM kotoyose.agent_execution_receipts WHERE execution_id=$1', execution_id)
        receipt = {**body, 'cursor': cursor, 'receipt_id': f'agent-receipt:{execution_id}:{unit_ordinal}'}
        receipt.pop('digest', None)
        receipt['digest'] = agent_digest(receipt)
        receipt = AgentExecutionReceipt.model_validate(receipt).model_dump(mode='json')
        await conn.execute('INSERT INTO kotoyose.agent_execution_receipts(execution_id,cursor,receipt_id,unit_ordinal,body,digest) VALUES($1,$2,$3,$4,$5::jsonb,$6)',
                           execution_id, cursor, receipt['receipt_id'], unit_ordinal, _encoded(receipt), receipt['digest'])
        await conn.execute('UPDATE kotoyose.agent_executions SET sequence=sequence+1,updated_at=now() WHERE id=$1', execution_id)
        return receipt

    async def close_open_units(self, conn, execution_id, now) -> list[dict]:
        units = await conn.fetch("SELECT * FROM kotoyose.agent_execution_charge_units WHERE execution_id=$1 AND state='open' ORDER BY unit_ordinal", execution_id)
        receipts = []
        for unit in units:
            acts = await conn.fetch('''SELECT a.*,t.stop_confirmation,t.side_effect_resolution
                FROM kotoyose.agent_execution_activations a LEFT JOIN kotoyose.execution_attempts t ON t.id=a.attempt_id
                WHERE a.execution_id=$1 AND a.unit_ordinal=$2 ORDER BY a.ordinal''', execution_id, unit['unit_ordinal'])
            if any(a['state'] in ('starting', 'running') for a in acts):
                continue
            facts = [ActivationFacts(a['nonce'], a['launch'], a['measurement'], a['active_ms'], a['turns'],
                a['output_bytes'], a['provider_rejected_only'],
                a['stop_confirmation'] if a['attempt_id'] else 'confirmed',
                a['side_effect_resolution'] if a['attempt_id'] else 'resolved') for a in acts]
            closure = close_unit_outcome(facts, allocation_units=unit['allocation_units'])
            if closure == 'pending':
                continue
            if closure == 'unprovable':
                await conn.execute("UPDATE kotoyose.agent_execution_charge_units SET state='unprovable' WHERE execution_id=$1 AND unit_ordinal=$2", execution_id, unit['unit_ordinal'])
                continue
            charge = dict(unit) if unit['charge_ref'] is not None else None
            body = receipt_body(closure, charge=charge, nonces=[a['nonce'] for a in acts], finished_at=now)
            receipt = await self.append_receipt(conn, execution_id, unit['unit_ordinal'], body)
            await conn.execute("UPDATE kotoyose.agent_execution_charge_units SET state='closed',receipt_cursor=$3 WHERE execution_id=$1 AND unit_ordinal=$2", execution_id, unit['unit_ordinal'], receipt['cursor'])
            receipts.append(receipt)
        return receipts

    async def events(self, principal, key, after: tuple[int, int], limit: int) -> dict:
        """所有の確認を取った execution の activation 順の event を、cursor より後から返す。"""
        async with self.pool.acquire() as conn:
            raw = await conn.fetchrow('SELECT id,state FROM kotoyose.agent_executions WHERE principal=$1 AND idempotency_key=$2', principal, key)
            if raw is None:
                raise AgentExecutionError('not_found', 'Execution not found', 404)
            rows = await conn.fetch('''SELECT a.ordinal,a.attempt_id,e.sequence,e.type,e.payload,e.created_at
                FROM kotoyose.agent_execution_activations a JOIN kotoyose.execution_events e ON e.attempt_id=a.attempt_id
                WHERE a.execution_id=$1 AND (a.ordinal>$2 OR (a.ordinal=$2 AND e.sequence>$3))
                ORDER BY a.ordinal,e.sequence LIMIT $4''', raw['id'], after[0], after[1], limit + 1)
        page, more = rows[:limit], len(rows) > limit
        events = [{'cursor': event_cursor(item['ordinal'], item['sequence']), 'activation_ordinal': item['ordinal'],
                   'attempt_id': item['attempt_id'], 'sequence': item['sequence'], 'type': item['type'],
                   'payload': _json(item['payload']), 'created_at': agent_utc(item['created_at'])} for item in page]
        next_cursor = events[-1]['cursor'] if events else event_cursor(*after)
        return {'schema_version': AGENT_EXECUTION_SCHEMA_VERSION, 'idempotency_key': key, 'events': events,
                'next_cursor': next_cursor, 'complete': raw['state'] == 'terminal' and not more}

    async def snapshot(self, principal, key, after_receipt, now) -> dict:
        if not isinstance(after_receipt, int) or isinstance(after_receipt, bool) or not 0 <= after_receipt <= 2**53 - 1:
            raise AgentExecutionError('invalid_request', 'Invalid receipt cursor', 422)
        async with self.pool.acquire() as conn, conn.transaction():
            raw = await conn.fetchrow('SELECT * FROM kotoyose.agent_executions WHERE principal=$1 AND idempotency_key=$2 FOR UPDATE', principal, key)
            if not raw:
                raise AgentExecutionError('not_found', 'Execution not found', 404)
            row = _row(raw)
            acts = await conn.fetch('SELECT * FROM kotoyose.agent_execution_activations WHERE execution_id=$1 ORDER BY ordinal', row['id'])
            current = next(a for a in acts if a['nonce'] == row['current_nonce'])
            authorization = await conn.fetchrow('SELECT body FROM kotoyose.agent_execution_authorizations WHERE execution_id=$1 AND revision=$2', row['id'], row['authorization_revision'])
            allow = _json(authorization['body']) if authorization else None
            required = current['launch'] == 'not_started' and current['state'] in ('awaiting', 'starting') and not (allow and allow['decision'] == 'allow' and allow['activation_nonce'] == current['nonce'] and parse_agent_utc(allow['not_before']) <= now < parse_agent_utc(allow['expires_at']))
            if required != row['authority_required']:
                row = _row(await conn.fetchrow('UPDATE kotoyose.agent_executions SET authority_required=$2,sequence=sequence+1,state_revision=state_revision+1,updated_at=$3 WHERE id=$1 RETURNING *', row['id'], required, now))
            unit = await conn.fetchrow('SELECT * FROM kotoyose.agent_execution_charge_units WHERE execution_id=$1 AND unit_ordinal=$2', row['id'], current['unit_ordinal'])
            receipts = [_json(item['body']) for item in await conn.fetch('SELECT body FROM kotoyose.agent_execution_receipts WHERE execution_id=$1 AND cursor>$2 ORDER BY cursor LIMIT $3', row['id'], after_receipt, RECEIPT_PAGE_LIMIT)]
            all_receipts = [_json(item['body']) for item in await conn.fetch('SELECT body FROM kotoyose.agent_execution_receipts WHERE execution_id=$1 ORDER BY cursor', row['id'])]
            final = all_receipts[-1]['cursor'] if all_receipts else 0
            launched = [a for a in acts if a['launch'] != 'not_started']
            state = ('running' if any(a['state'] == 'running' for a in acts) else
                     'not_started' if not launched else 'unknown' if any(a['measurement'] != 'complete' for a in launched) else 'ended')
            known = all(a['turns'] is not None and a['output_bytes'] is not None and a['measurement'] == 'complete' for a in launched)
            money = row['request']['execution']['money']
            totals = [Decimal(r['units']) for r in all_receipts if r['units'] is not None]
            with localcontext() as context:
                context.prec = max((len(value.as_tuple().digits) + abs(value.as_tuple().exponent) for value in totals), default=1) + len(str(len(totals))) + 1
                units = None if money['mode'] == 'unbudgeted' else format(sum(totals, Decimal(0)), 'f')
            attempt_id = next((a['attempt_id'] for a in reversed(acts) if a['attempt_id']), None)
            attempt = await conn.fetchrow('SELECT stop_confirmation,side_effect_resolution,artifact_manifest FROM kotoyose.execution_attempts WHERE id=$1', attempt_id) if attempt_id else None
            pending_units = await conn.fetchval("SELECT EXISTS(SELECT 1 FROM kotoyose.agent_execution_charge_units WHERE execution_id=$1 AND state!='unprovable' AND receipt_cursor IS NULL)", row['id'])
            finished_id = next((a['attempt_id'] for a in reversed(acts) if a['attempt_id'] and a['state'] == 'ended'), None)
            artifacts = [dict(item) for item in await conn.fetch(
                'SELECT ref,name,digest,COALESCE(byte_size,octet_length(content)) AS size,media_type '
                'FROM kotoyose.execution_artifacts WHERE attempt_id=$1 ORDER BY name', finished_id)] if finished_id else []
            snapshot = {
                'schema_version': AGENT_EXECUTION_SCHEMA_VERSION, 'execution_id': str(row['id']), 'idempotency_key': key,
                'caller_run_ref': row['caller_run_ref'], 'work_digest': row['work_digest'], 'request_digest': row['request_digest'],
                'sequence': row['sequence'], 'state': row['state'], 'state_revision': row['state_revision'],
                'activation': {'nonce': current['nonce'], 'ordinal': current['ordinal'], 'reason': current['reason'],
                    'charge_ref': unit['charge_ref'] if unit else None, 'authority_required': required,
                    'authorization_revision': row['authorization_revision']},
                'pause': row['pause'], 'provider_limit': row['provider_limit'], 'next_probe_at': agent_utc(row['next_probe_at']) if row['next_probe_at'] else None,
                'fault_retries_used': row['fault_retries_used'], 'cancel_requested': row['cancel_requested'], 'admission_closed': row['admission_closed'],
                'usage': {'state': state, 'active_ms': sum(a['active_ms'] for a in acts),
                    'turns': sum(a['turns'] for a in launched) if known else None,
                    'output_bytes': sum(a['output_bytes'] for a in launched) if known else None,
                    'units': units, 'complete': row['terminal'] is not None and state in ('not_started', 'ended') and known,
                    'observed_at': agent_utc(row['updated_at']),
                    'started_at': agent_utc(min(a['started_at'] for a in acts if a['started_at'])) if any(a['started_at'] for a in acts) else None,
                    'finished_at': agent_utc(max(a['finished_at'] for a in acts if a['finished_at'])) if any(a['finished_at'] for a in acts) else None},
                'stop_confirmation': 'unconfirmed' if state == 'running' else attempt['stop_confirmation'] if attempt else 'confirmed',
                'side_effect_resolution': 'unresolved' if state == 'running' else attempt['side_effect_resolution'] if attempt else 'resolved',
                'workspace_ref': row['workspace_ref'], 'terminal': row['terminal'], 'receipts': receipts,
                'next_receipt_cursor': receipts[-1]['cursor'] if receipts else after_receipt, 'final_receipt_cursor': final,
                'receipts_complete': row['terminal'] is not None and not pending_units,
                'artifacts': artifacts,
            }
            snapshot = AgentExecutionSnapshot.model_validate(snapshot).model_dump(mode='json')
            def visible_digest(value):
                return agent_digest({k: v for k, v in value.items() if k not in ('sequence', 'receipts', 'next_receipt_cursor')})
            digest = visible_digest(snapshot)
            prior = await conn.fetchrow('SELECT digest,sequence FROM kotoyose.agent_execution_snapshot_digests WHERE execution_id=$1', row['id'])
            if prior and prior['digest'] != digest and prior['sequence'] == row['sequence']:
                row = _row(await conn.fetchrow('UPDATE kotoyose.agent_executions SET sequence=sequence+1,updated_at=$2 WHERE id=$1 RETURNING *', row['id'], now))
                snapshot['sequence'] = row['sequence']
                snapshot['usage']['observed_at'] = agent_utc(row['updated_at'])
                digest = visible_digest(snapshot)
            if prior is None or prior['digest'] != digest or prior['sequence'] != snapshot['sequence']:
                await conn.execute('''INSERT INTO kotoyose.agent_execution_snapshot_digests(execution_id,digest,sequence)
                    VALUES($1,$2,$3) ON CONFLICT(execution_id) DO UPDATE SET digest=$2,sequence=$3''',
                    row['id'], digest, snapshot['sequence'])
            return snapshot
