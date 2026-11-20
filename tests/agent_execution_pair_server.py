"""Test-only HTTP launcher for the Hub / Kotoyose pair smoke (boundary rework F1d).

It serves the real public execution router (``kotoyose.routes.executions.router``) over HTTP on 127.0.0.1 with the same service
that ``tests.fixtures.agent_execution.isolated_agent_execution_harness(database_url, tmp_path, monkeypatch, execute=True)`` builds: the same
``_config``, the same callers (``Bearer kys_caller-a`` is the principal) and ``ScriptedRuntime`` instead of a provider.
The Hub's real HTTP clients can therefore be pointed at it. Unlike the fixture, ``service.start()`` is called (as
``AgentExecutionHarness.restart()`` does), so the background probe loop and the recovery of a killed predecessor run for real.

Run it from the Kotoyose worktree with the disposable PostgreSQL named by ``KOTOYOSE_EXECUTION_TEST_DATABASE_URL``::

    .venv/bin/python -m tests.agent_execution_pair_server --database ahs_execution_test_pair_x --root /abs/workspace/root [--port 0]

``--database`` is created when absent (it must start with ``ahs_execution_test_``). ``--reuse`` accepts an existing one, so that
a killed launcher can be started again on the same data. A launcher never drops anything; the caller owns the database.
``READY <port>`` is printed on stdout once requests are accepted. ``--independent-stop-check`` is described at
``install_independent_stop_check``.

Control routes for the smoke, none of them authenticated: ``POST /_pair/script`` (what the next activation does, and a provider
refusal's reset in seconds), ``POST /_pair/release`` (ends a waiting script), ``POST /_pair/work`` (``AgentExecutionHarness.register_work()``
and ``create_body()``: registered resources and the Create body they make), ``POST /_pair/command-work`` (the same for a command), ``POST /_pair/tool-work`` (the same with one client tool pinned and
``client:fixture`` as the client key), ``POST /_pair/call`` (a client call on the running attempt of an idempotency key, the way
``tests.test_agent_execution_client_tools.create_call`` makes one) and ``GET /_pair/state`` (runtime starts, provider process ids, row counts, advertised capabilities).
This module is a test fixture: never deploy it and never point it at a database that holds real data.
"""
from __future__ import annotations

import argparse
import asyncio
import ctypes
import os
import re
import shutil
import signal
import sys
import types
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import asyncpg
import uvicorn
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse

import kotoyose.execution_service as execution_service_module
from kotoyose.execution_jobs import WorkerResult
from kotoyose.execution_limits import ProviderUsageLimit
from kotoyose.execution_resources import (
    ArgumentList, CommandSpec, Environment, ExecutionBudget, ExecutionError, ExecutionGrant, TimeoutPolicy,
    WorkingDirectory, WorkspaceRegistration,
)
from kotoyose.execution_service import NATIVE_CLAUDE_SDK_VERSION, ExecutionService
from kotoyose.execution_store import ExecutionStore
from kotoyose.native_claude import NativeAgentResult
from kotoyose.routes.executions import router as public_router
from tests.fixtures.api_keys import fixture_key_store
from tests.fixtures.agent_execution import AgentExecutionHarness, ScriptedRuntime
from tests.test_execution_agent import _config
from tests.fixtures.clients import clients, set_client

DATABASE_NAME = re.compile(r'^ahs_execution_test_[a-z0-9_]{1,40}$')
LOOPBACK = {'127.0.0.1', 'localhost', '::1'}
SCRIPTS = {'success', 'wait', 'provider', 'fault', 'launch_hang'}
COMMAND_RUN = 'run:pair-smoke-command'
COORDINATOR_HELD = 'Another Kotoyose coordinator owns the LocalWorker'


class PairWorker:
    """Command worker without a process: every command is a confirmed success. Agent work never reaches it."""

    async def probe(self):
        return True

    async def execute(self, job, cancel):
        return WorkerResult('succeeded', 0, b'', b'', 'confirmed', 'completed')

    async def stop(self, attempt_id, generation, grace_seconds):
        return 'confirmed'


class PairRuntime(ScriptedRuntime):
    """ScriptedRuntime plus launcher-only knobs: a refusal whose reset is relative to the refusal, a launch that never proves itself, and the provider process ids."""

    def __init__(self):
        super().__init__('success')
        self.reset_in = None
        self.entered = asyncio.Event()
        self.pids: dict[str, int] = {}
        self.launcher_path = Path('/bin/sh')

    def rearm(self, script, reset_in):
        """One execution at a time: a new script gets fresh start / release / entry signals."""
        self.script, self.reset_in, self.provider_limit = script, reset_in, None
        self.started, self.entered, self.release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def execute(self, job, cancel, execute_tool, *, on_process_started):
        if self.script == 'provider' and self.reset_in is not None:
            now = datetime.now(timezone.utc)
            self.provider_limit = ProviderUsageLimit(provider='claude', limit_window='rate', reset_at=now + timedelta(seconds=self.reset_in),
                                                     detected_at=now, resume='native', unavailable_reason=None)
        if self.script == 'launch_hang':
            # Like the AC16 trace in test_agent_execution_conformance: the start is validated, no process identity or start time is ever recorded.
            self.jobs.append(job)
            await job.validate_start()
            self.entered.set()
            await cancel.wait()
            return NativeAgentResult('cancelled', 'execution_cancelled', b'', 'confirmed', {}, launch_outcome='not_started')

        async def record_pid(pid, **kwargs):
            self.pids[job.attempt_id] = pid
            await on_process_started(pid, **kwargs)

        return await super().execute(job, cancel, execute_tool, on_process_started=record_pid)


@dataclass
class Pair:
    runtime: PairRuntime
    kotoyose: AgentExecutionHarness
    pool: asyncpg.Pool


def database_url(base_url: str, name: str) -> str:
    return urlparse(base_url)._replace(path='/' + name).geturl()


async def prepare_database(base_url: str, name: str, *, reuse: bool) -> None:
    """Create the database when absent. An existing one is only accepted with --reuse; nothing is ever dropped here."""
    admin = await asyncpg.connect(base_url)
    try:
        exists = await admin.fetchval('SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname=$1)', name)
        if exists and not reuse:
            raise SystemExit(f'database {name} already exists; pass --reuse to keep it')
        if not exists:
            await admin.execute(f'CREATE DATABASE "{name}"')
    finally:
        await admin.close()


def die_with_parent() -> None:
    """If the smoke is killed without cleaning up, the kernel ends the launcher too (Linux PR_SET_PDEATHSIG, delivered as SIGTERM)."""
    try:
        ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGTERM)
    except (OSError, AttributeError):
        pass


def patch_for_test_service() -> None:
    """The steps isolated_agent_execution_harness / AgentExecutionHarness.restart take with pytest's monkeypatch, done once for this process."""
    os.environ['KOTOYOSE_AGENT_PROVIDER_TOKEN'] = 'test-provider-token'

    execution_service_module.version = lambda _: NATIVE_CLAUDE_SDK_VERSION
    execution_service_module.shutil = types.SimpleNamespace(which=lambda _: '/bin/sh', rmtree=shutil.rmtree)


def install_independent_stop_check() -> None:
    """Stand-in for an operator's independent stop check, as the AC16 conformance trace patches ``_stop_attempt``.

    An agent attempt that never recorded a provider process identity has a pending ``provider_process`` operation that no
    process check can settle; here the fake provably started nothing, so it is confirmed. An attempt with an identity is
    stopped by the real code. Production keeps the first kind unconfirmed, which is why this is behind a flag.
    """
    original = ExecutionService._stop_attempt

    async def stop_attempt(self, attempt):
        if attempt['snapshot']['kind'] == 'agent':
            operations = await self.store.operations(attempt['id'])
            if not any(item['kind'] == 'provider_process' and item['result'] for item in operations):
                return 'confirmed'
        return await original(self, attempt)

    ExecutionService._stop_attempt = stop_attempt


async def start_service(config, store, runtime) -> ExecutionService:
    """Start like the production lifespan; a SIGKILLed predecessor's coordinator lock may need a moment to be released by PostgreSQL."""
    for attempt in range(40):
        service = ExecutionService(config, store, PairWorker())
        service.agent_executor.runtime = runtime
        try:
            await service.start()
            return service
        except RuntimeError as exc:
            if COORDINATOR_HELD not in str(exc) or attempt == 39:
                raise
            await asyncio.sleep(.5)
    raise AssertionError('unreachable')


async def register_command_work(pair: Pair) -> dict:
    """Resources for one command Run, the way a Hub verification command registers them (camelCase, ready for ExecutionRequest)."""
    service = pair.kotoyose.service
    policy = clients()['caller-a']
    workspace = pair.kotoyose.root / str(uuid4())
    workspace.mkdir(parents=True)
    workspace_ref = await service.register_workspace('caller-a', policy, WorkspaceRegistration(schema_version='1', path=str(workspace), caller_workspace_ref=str(uuid4())))
    now = datetime.now(timezone.utc)
    put = service.store.put_resource
    timeout = await put('caller-a', TimeoutPolicy(schema_version='1', kind='timeout_policy', soft_seconds=1, hard_seconds=10, grace_seconds=1))
    budget = await put('caller-a', ExecutionBudget(schema_version='1', kind='budget', reservation_ref='reservation-command', mode='observed', deadline=now + timedelta(minutes=2),
                                                   memory_bytes=64 * 1024 * 1024, pids_limit=16, cpus=1, output_limit_bytes=8192))
    command = await service.register_resource('caller-a', policy, CommandSpec(schema_version='1', kind='command_spec', executable='/bin/true'))
    argv = await put('caller-a', ArgumentList(schema_version='1', kind='argv', values=[]))
    cwd = await put('caller-a', WorkingDirectory(schema_version='1', kind='cwd', path='.'))
    env = await put('caller-a', Environment(schema_version='1', kind='env', values={}))
    grant = await put('caller-a', ExecutionGrant(schema_version='1', kind='grant', grant_id=str(uuid4()), version=1, caller_run_ref=COMMAND_RUN, workspace_ref=workspace_ref,
                                                 isolation='sandbox', network='none', command_spec_refs=[command], runtime_profiles=[], resource_refs=[argv, cwd, env, budget, timeout],
                                                 policy_ref='policy-command', not_before=now - timedelta(seconds=1), expires_at=now + timedelta(minutes=2), max_seconds=20, delegation='none'))
    return {'callerRunRef': COMMAND_RUN, 'workspaceRef': workspace_ref, 'executionGrantRef': grant, 'budgetsRef': budget, 'commandSpecRef': command, 'argvRef': argv,
            'cwdRef': cwd, 'envRefs': [env], 'fixtureRefs': [], 'timeoutPolicyRef': timeout}


control = APIRouter(prefix='/_pair')


@control.post('/script')
async def set_script(request: Request):
    """Choose what the next activation does: success, wait (until /release or cancel), provider (refusal), fault, launch_hang (start never proven)."""
    body = await request.json()
    if body.get('script') not in SCRIPTS:
        return JSONResponse(status_code=422, content={'error': f'script must be one of {sorted(SCRIPTS)}'})
    reset_in = body.get('reset_in_seconds')
    request.app.state.pair.runtime.rearm(body['script'], None if reset_in is None else float(reset_in))
    return {'ok': True}


@control.post('/release')
async def release(request: Request):
    request.app.state.pair.runtime.release.set()
    return {'ok': True}


@control.post('/work')
async def register_work(request: Request):
    """AgentExecutionHarness.register_work() and create_body(): registered Kotoyose resources and the complete Create body they make (no client tools, no approval)."""
    pair = request.app.state.pair
    return pair.kotoyose.create_body(await pair.kotoyose.register_work(tool_refs=[]))


@control.post('/tool-work')
async def register_tool_work(request: Request):
    """Like /work, with one client tool pinned (AgentExecutionHarness.register_work(client_tool=True)); also answers the tool ref and the client key."""
    pair = request.app.state.pair
    refs = await pair.kotoyose.register_work(client_tool=True)
    return {'create': pair.kotoyose.create_body(refs), 'tool_ref': refs['tool'], 'client_key': 'client:fixture'}


@control.post('/call')
async def create_call(request: Request):
    """A client call on the latest started attempt of ``key``; the connection for ``client_key`` / ``generation`` must already be active."""
    body = await request.json()
    pair = request.app.state.pair
    attempt = await pair.pool.fetchval(
        'SELECT a.attempt_id FROM kotoyose.agent_execution_activations a JOIN kotoyose.agent_executions e ON e.id=a.execution_id '
        'WHERE e.idempotency_key=$1 AND a.attempt_id IS NOT NULL ORDER BY a.ordinal DESC LIMIT 1', body['key'])
    if attempt is None:
        return JSONResponse(status_code=409, content={'error': 'no attempt has started for this key'})
    call = await pair.kotoyose.store.client_tools.create_call(
        'caller-a', attempt, body.get('client_key', 'client:fixture'), int(body.get('generation', 0)), body['tool_ref'],
        body.get('arguments', {'value': 'hello'}), datetime.now(timezone.utc) + timedelta(seconds=30))
    return {'call_id': str(call['id']), 'operation_id': call['operation_id'], 'arguments_digest': call['arguments_digest']}


@control.post('/command-work')
async def register_command(request: Request):
    return await register_command_work(request.app.state.pair)


@control.get('/state')
async def state(request: Request, key: str | None = None):
    """What the HTTP surface cannot show: runtime starts, provider process ids, row counts and the advertised capabilities; scoped to one idempotency key when given."""
    pair = request.app.state.pair
    scope = ('', []) if key is None else (' WHERE e.idempotency_key=$1', [key])
    counts = {}
    for name, table, join in (('executions', 'agent_executions e', ''), ('activations', 'agent_execution_activations a', ' JOIN kotoyose.agent_executions e ON e.id=a.execution_id'),
                              ('charge_units', 'agent_execution_charge_units a', ' JOIN kotoyose.agent_executions e ON e.id=a.execution_id')):
        counts[name] = await pair.pool.fetchval(f'SELECT count(*) FROM kotoyose.{table}{join}{scope[0]}', *scope[1])
    attempts = None
    if key is not None:
        attempts = {row['attempt_id'] for row in await pair.pool.fetch(
            'SELECT a.attempt_id FROM kotoyose.agent_execution_activations a JOIN kotoyose.agent_executions e ON e.id=a.execution_id '
            'WHERE e.idempotency_key=$1 AND a.attempt_id IS NOT NULL', key)}
    runtime = pair.runtime
    return {'script': runtime.script, 'started': runtime.started.is_set(), 'entered': runtime.entered.is_set(), 'session_ref': runtime.session_ref,
            'jobs': [{'provider_session_ref': job.provider_session_ref} for job in runtime.jobs if attempts is None or job.attempt_id in attempts],
            'provider_pids': [pid for attempt_id, pid in runtime.pids.items() if attempts is None or attempt_id in attempts], 'counts': counts,
            'capabilities': {name: item.status for name, item in pair.kotoyose.service.manifest.capabilities.items()}}


async def execution_error_handler(request: Request, exc: ExecutionError):
    """The production app's envelope for ExecutionError (kotoyose.app), so that the Attempt client sees what it sees in production."""
    return JSONResponse(status_code=exc.status, content={'error': {
        'type': exc.code, 'code': exc.code, 'message': str(exc), 'request_id': 'pair', 'retryable': exc.status >= 500 and exc.status != 501,
        'safe_to_retry': exc.safe_to_retry, **({'reason_code': exc.reason_code} if exc.reason_code is not None else {})}})


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog='python -m tests.agent_execution_pair_server', description='Serve the Kotoyose execution API over HTTP with a scripted runtime (test only).')
    parser.add_argument('--database', required=True, help='database name, ahs_execution_test_*; created when absent')
    parser.add_argument('--root', required=True, help='existing directory that becomes the caller workspace root')
    parser.add_argument('--port', type=int, default=0, help='127.0.0.1 port; 0 lets the OS choose (READY prints the real one)')
    parser.add_argument('--reuse', action='store_true', help='accept an already existing database')
    parser.add_argument('--independent-stop-check', action='store_true', help='confirm the stop of agent attempts that never recorded a process (see install_independent_stop_check)')
    return parser.parse_args(argv)


async def serve(args: argparse.Namespace) -> None:
    base_url = os.environ.get('KOTOYOSE_EXECUTION_TEST_DATABASE_URL')
    if not base_url or urlparse(base_url).hostname not in LOOPBACK:
        raise SystemExit('KOTOYOSE_EXECUTION_TEST_DATABASE_URL must name a disposable PostgreSQL on loopback')
    if not DATABASE_NAME.fullmatch(args.database):
        raise SystemExit('--database must match ahs_execution_test_[a-z0-9_]+')
    root = Path(args.root).resolve(strict=True)
    die_with_parent()
    patch_for_test_service()
    if args.independent_stop_check:
        install_independent_stop_check()
    await prepare_database(base_url, args.database, reuse=args.reuse)
    holder: dict = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        pool = await asyncpg.create_pool(database_url(base_url, args.database), min_size=1, max_size=8)
        service = None
        try:
            await pool.execute('CREATE SCHEMA IF NOT EXISTS kotoyose')
            store = ExecutionStore(pool)
            await store.initialize()
            config = _config(root)
            set_client('caller-b', clients()['caller-a'].model_copy(deep=True))
            runtime = PairRuntime()
            service = await start_service(config, store, runtime)
            app.state.execution_service = service
            app.state.api_key_store = await fixture_key_store(**{"caller-a": "caller-a", "caller-b": "caller-b"})
            app.state.pair = Pair(runtime, AgentExecutionHarness(store, service, app, None, None, root, None), pool)
            announce = asyncio.create_task(announce_ready(holder['server']))
            try:
                yield
            finally:
                announce.cancel()
        finally:
            try:
                if service is not None:
                    await service.close()
            finally:
                await pool.close()

    app = FastAPI(lifespan=lifespan)
    app.include_router(public_router)
    app.include_router(control)
    app.add_exception_handler(ExecutionError, execution_error_handler)
    config = uvicorn.Config(app, host='127.0.0.1', port=args.port, log_level='warning', access_log=False, lifespan='on', timeout_keep_alive=60)
    holder['server'] = server = uvicorn.Server(config)
    await server.serve()


async def announce_ready(server: uvicorn.Server) -> None:
    while not server.started:
        await asyncio.sleep(.02)
    print(f'READY {server.servers[0].sockets[0].getsockname()[1]}', flush=True)


if __name__ == '__main__':
    asyncio.run(serve(parse_args(sys.argv[1:])))
