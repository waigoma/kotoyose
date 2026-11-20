"""Isolated PostgreSQL and non-executing HTTP app for Hub boundary tests."""
import copy
import asyncio
import json
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4
from unittest.mock import AsyncMock

import asyncpg
import pytest
import httpx
from fastapi import Depends, FastAPI

from kotoyose.execution_http import authorize_execution
from kotoyose.execution_resources import ClientToolSpec
from kotoyose.execution_limits import ProviderUsageLimit
from kotoyose.execution_service import ExecutionService
from kotoyose.execution_store import ExecutionStore
from kotoyose.agent_execution_digest import agent_utc, agent_work_digest
from kotoyose.routes.agent_executions import AgentExecutionRoute, agent_execution_create, router
from kotoyose.native_claude import NativeAgentResult
from kotoyose.runtime.resume import ResumeSupport
from tests.fixtures.api_keys import fixture_key_store
from tests.test_execution_agent import _config, _NoopWorker, _resources
from tests.fixtures.clients import clients


class ScriptedRuntime:
    def __init__(self, script='success'):
        self.script = script
        self.jobs = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.ended_at = None
        self.stop_confirmation = 'confirmed'
        self.retained = True
        self.session_ref = 'session'
        self.provider_limit = None

    def resume_support(self):
        return ResumeSupport(('native',), 'test runtime')

    def resume_session_ref(self, session_id):
        return session_id

    async def can_resume_session(self, scope, ref):
        return bool(ref) and self.retained

    async def execute(self, job, cancel, execute_tool, *, on_process_started):
        self.jobs.append(job)
        if self.script == 'unknown':
            return NativeAgentResult('failed', 'worker_failure', b'', 'confirmed', {},
                                     output_bytes=None, turns=None, launch_outcome='unknown')
        await job.validate_start()
        process = await asyncio.create_subprocess_exec(sys.executable, '-c', 'import time; time.sleep(30)',
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        try:
            await on_process_started(process.pid)
            self.started.set()
            if self.script == 'approval':
                binding = job.approval_bindings[0]
                outcome = await job.client_tool_call('builtin:request-approval@1',
                    {'approval_id': binding['approval_id'], 'subject_digest': binding['subject_digest']})
                self.script = 'success'
                return NativeAgentResult('approval_required', 'approval_required', b'', self.stop_confirmation, {},
                    required_action=outcome.required_action, provider_session_id=self.session_ref,
                    output_bytes=0, turns=1, launch_outcome='started')
            if self.script == 'wait':
                while not cancel.is_set() and not self.release.is_set() and job.work_remaining() > 0:
                    await asyncio.sleep(.02)
                status, reason = (('cancelled', 'execution_cancelled') if cancel.is_set() else
                                  ('succeeded', 'completed') if self.release.is_set() else ('timeout', 'timeout'))
            elif self.script == 'turns':
                status, reason = 'failed', 'turn_limit_exceeded'
            elif self.script == 'output':
                status, reason = 'failed', 'output_limit_exceeded'
            elif self.script == 'provider':
                status, reason = 'failed', 'provider_usage_limit'
            elif self.script == 'fault':
                status, reason = 'failed', 'worker_failure'
            else:
                status, reason = 'succeeded', 'completed'
            limit = self.provider_limit if self.script == 'provider' else None
            if self.script == 'provider' and limit is None:
                limit = ProviderUsageLimit(provider='claude', limit_window='unknown', reset_at=None,
                    detected_at=datetime.now(timezone.utc), resume='native', unavailable_reason=None)
            return NativeAgentResult(status, reason, b'output', self.stop_confirmation, {}, provider_session_id=self.session_ref,
                provider_limit=limit, output_bytes=6, turns=0 if self.script == 'provider' else 1, launch_outcome='started')
        finally:
            if process.returncode is None:
                process.terminate()
            await process.wait()
            self.ended_at = datetime.now(timezone.utc)


class FakeClock:
    def __init__(self, now=None):
        self.now = now or datetime.now(timezone.utc).replace(microsecond=0)

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)


class AgentExecutionHarness:
    def __init__(self, store, service, app, client, client_b, root, monkeypatch):
        self.store, self.service, self.agent_executions, self.app = store, service, service.agent_executions, app
        self.client, self.client_b, self.root = client, client_b, root
        self.monkeypatch = monkeypatch
        self.crashed = False
        self._guard_settlement()

    def _guard_settlement(self):
        schedule = self.service._schedule
        def guarded(attempt, *, after=None):
            async def settle(value):
                if not self.crashed and after is not None:
                    await after(value)
            return schedule(attempt, after=settle if after else None)
        self.service._schedule = guarded

    async def crash(self):
        self.crashed = True
        self.service._accepting = False
        await self.agent_executions.stop_background()
        tasks = list(self.service._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.service._coordinator is not None:
            await self.service._coordinator.stop_watchdog()
            await self.service._coordinator.release()

    async def restart(self):
        runtime = self.service.agent_executor.runtime
        service = ExecutionService(self.service.config, self.store, _NoopWorker())
        service.agent_executor.runtime = runtime
        runtime.launcher_path = Path('/bin/sh')
        from kotoyose.execution_service import NATIVE_CLAUDE_SDK_VERSION
        self.monkeypatch.setattr('kotoyose.execution_service.version', lambda _: NATIVE_CLAUDE_SDK_VERSION)
        self.monkeypatch.setattr('kotoyose.execution_service.shutil.which', lambda _: '/bin/sh')
        self.service, self.agent_executions = service, service.agent_executions
        self.app.state.execution_service = service
        self.crashed = False
        self._guard_settlement()
        await service.start()
        return self

    async def register_work(self, *, client_tool=False, approval=False, **overrides):
        tool = None
        if client_tool:
            tool = await self.service.register_resource('caller-a', clients()['caller-a'], ClientToolSpec(
                schema_version='1', kind='client_tool', name='set_value', description='Set a value',
                input_schema={'type': 'object', 'additionalProperties': False,
                    'properties': {'value': {'type': 'string'}}, 'required': ['value']}, effect='set', timeout_seconds=10))
        if client_tool or approval:
            overrides['tool_refs'] = ([tool] if tool else []) + (['builtin:request-approval@1'] if approval else [])
        request, workspace, refs = await _resources(self.service, clients()['caller-a'],
            self.root / str(uuid4()), **overrides)
        if client_tool or approval:
            request = request.model_copy(update={'client_key': 'client:fixture', 'approval_bindings': []})
            if approval:
                request = type(request).model_validate({**request.model_dump(mode='json'), 'approval_bindings': [
                    {'approval_id': str(uuid4()), 'subject_digest': 'a' * 64}]})
        return {'request': request, 'workspace': workspace, 'tool': tool, **refs}

    def create_body(self, refs, **overrides):
        fixture = json.loads((Path(__file__).parent / 'agent_execution_v1.json').read_text())
        raw = copy.deepcopy(fixture['creates'][0]['value'])
        body, request = raw['execution'], refs['request']
        body.update(idempotency_key=str(uuid4()), caller_run_ref=request.caller_run_ref)
        body['work'].update(workspace_ref=refs['workspace'], agent_definition_ref=refs['definition'],
            policy_ref='policy-agent', scope_grant_ref=refs['grant'], resource_refs=[refs['budget']],
            model=request.model, runtime_profile=request.runtime_profile, required_authentication_mode=None,
            expected_authentication_binding_digest=None, required_provider=None, required_billing_mode=None,
            inputs=[{'kind': x.kind, 'provenance': x.provenance, 'content_ref': x.content_ref,
                     'expected_revocation_epoch': x.expected_revocation_epoch} for x in request.inputs],
            tool_refs=request.tool_refs, requirements=request.requirements.model_dump(mode='json'),
            client_key=request.client_key, approval_bindings=[{'approval_id': item.approval_id, 'subject_digest': item.subject_digest}
                                                        for item in request.approval_bindings])
        body['time'] = {'run_hard_ms': 10000, 'grace_ms': 1000}
        body.update(overrides)
        body['work_digest'] = agent_work_digest(raw)
        body['initial_authorization'] = self.allow(raw)
        return raw

    def allow(self, create_or_snapshot, **overrides):
        body = create_or_snapshot.get('execution', create_or_snapshot)
        now = datetime.now(timezone.utc)
        # 期限の上限は 3 * run_hard_ms + grace_ms (LEASE_WALL_FACTOR)。既定の 50 秒は短い run に対して上限を超える。
        time = body.get('time') or {'run_hard_ms': 10000, 'grace_ms': 1000}
        lifetime_ms = min(50_000, 3 * time['run_hard_ms'] + time['grace_ms'] - 1000)
        result = {'decision': 'allow', 'revision': 1, 'activation_nonce': str(uuid4()),
            'work_digest': body['work_digest'], 'caller_run_ref': body['caller_run_ref'],
            'issued_at': agent_utc(now), 'not_before': agent_utc(now - timedelta(seconds=1)),
            'expires_at': agent_utc(now + timedelta(milliseconds=lifetime_ms)), 'allowed_active_ms': 10000,
            'stop_limit': 'run_seconds', 'charge': {'charge_ref': 'charge:' + str(uuid4()),
            'reservation_ref': 'reservation:' + str(uuid4()), 'allocation_units': '1.250000000001'},
            'authority_ceiling_at': None, 'approval': None, 'settled_receipt_cursor': 0}
        result.update(overrides)
        return result

    def deny(self, create_or_snapshot, reason, **overrides):
        body = create_or_snapshot.get('execution', create_or_snapshot)
        result = {'decision': 'deny', 'revision': 2, 'activation_nonce': body.get('activation', {}).get('nonce', 'nonce'),
                  'work_digest': body['work_digest'], 'caller_run_ref': body['caller_run_ref'], 'reason': reason,
                  'settled_receipt_cursor': 0}
        result.update(overrides)
        return result

    async def bind_internal_attempt(self, execution_id):
        from tests.test_execution_store import _admit_ready
        root = self.root / str(uuid4())
        root.mkdir()
        _, refs, request, snapshot = await _admit_ready(self.store, root, key=str(uuid4()))
        attempt, _ = await self.store.admit('caller-a', request, snapshot, 1)
        await self.store.pool.execute("UPDATE kotoyose.execution_attempts SET api_operation='agent_activation',status='running' WHERE id=$1", attempt['id'])
        await self.store.pool.execute("UPDATE kotoyose.agent_execution_activations SET attempt_id=$2,state='running',launch='started' WHERE execution_id=$1", execution_id, attempt['id'])
        return attempt, refs


@asynccontextmanager
async def isolated_agent_execution_harness(database_url, tmp_path, monkeypatch, *, execute=False):
    if not database_url:
        pytest.skip("isolated PostgreSQL test URL is not configured")
    admin = await asyncpg.connect(database_url)
    name = 'ahs_execution_test_agent_exec_' + uuid4().hex
    pool = None
    created = False
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        created = True
        pool = await asyncpg.create_pool(urlparse(database_url)._replace(path='/' + name).geturl(), min_size=1, max_size=8)
        await pool.execute('CREATE SCHEMA kotoyose')
        store = ExecutionStore(pool)
        await store.initialize()
        config = _config(tmp_path)
        from kotoyose import config as config_module
        previous_config = config_module.get_config()
        monkeypatch.setattr(config_module, '_config', previous_config.model_copy(update={
            'clients': {**previous_config.clients, 'caller-b': clients()['caller-a'].model_copy(deep=True)},
        }))
        monkeypatch.setenv('KOTOYOSE_AGENT_PROVIDER_TOKEN', 'test-provider-token')
        service = ExecutionService(config, store, _NoopWorker())
        if not execute:
            service.agent_executions.runner.kick = AsyncMock()
        service._accepting = True
        app = FastAPI()
        app.state.execution_service = service
        app.state.api_key_store = await fixture_key_store(**{"caller-a": "caller-a", "caller-b": "caller-b"})
        app.include_router(router)
        app.router.add_api_route('/v1/executions', agent_execution_create, methods=['POST'], dependencies=[Depends(authorize_execution)],
                                 route_class_override=AgentExecutionRoute)
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=True)
        async with httpx.AsyncClient(transport=transport, base_url='http://test', headers={'Authorization': 'Bearer kys_caller-a'}) as client:
            async with httpx.AsyncClient(transport=transport, base_url='http://test', headers={'Authorization': 'Bearer kys_caller-b'}) as client_b:
                kotoyose = AgentExecutionHarness(store, service, app, client, client_b, tmp_path, monkeypatch)
                try:
                    yield kotoyose
                finally:
                    if kotoyose.service._coordinator is not None:
                        await kotoyose.service.close()
    finally:
        if pool is not None:
            await pool.close()
        if created:
            await admin.execute(f'DROP DATABASE "{name}"')
        await admin.close()
