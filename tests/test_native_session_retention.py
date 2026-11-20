"""公式session機構への接続を、非公開fixtureだけで検証する。"""
import asyncio
import json
import sys
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from kotoyose.native_sessions import CodexSessionDirectory, ScopedSessionStore, session_scope
from kotoyose.native_codex import NativeCodexWorker, _CONFIG
from kotoyose.native_claude import NativeClaudeWorker
from tests.test_native_claude import _job, _Client, _successful_tool
from tests.test_native_codex_protocol import run_rpc
from tests.test_execution_store import store
from kotoyose.session_store import PostgresSessionStore


class MemoryStore:
    def __init__(self):
        self.entries = {}

    async def append(self, key, entries):
        self.entries.setdefault((key['project_key'], key['session_id']), []).extend(entries)

    async def load(self, key):
        return self.entries.get((key['project_key'], key['session_id']))


async def test_official_store_entries_stay_opaque_and_scoped_to_owner_workspace():
    store = MemoryStore()
    first = ScopedSessionStore(store, session_scope('owner-a', 'workspace-1', 'claude'))
    other = ScopedSessionStore(store, session_scope('owner-b', 'workspace-1', 'claude'))
    key = {'project_key': 'SDK-project-key', 'session_id': str(uuid4())}
    opaque = [{'type': 'user', 'uuid': str(uuid4()), 'message': {'role': 'user', 'content': 'fixture'}}]
    await first.append(key, opaque)
    assert await first.load(key) == opaque
    assert await other.load(key) is None
    assert all(project.startswith('native-') for project, _ in store.entries)


async def test_official_claude_materialization_reads_only_new_empty_config_not_host_credentials(tmp_path, monkeypatch):
    from claude_agent_sdk import ClaudeAgentOptions
    from claude_agent_sdk._internal import session_resume
    empty = tmp_path/'empty-controller-config'
    empty.mkdir(mode=0o700)
    store = ScopedSessionStore(MemoryStore(), session_scope('owner', 'workspace', 'claude'))
    session_id = str(uuid4())
    entry = {'type': 'user', 'uuid': str(uuid4()), 'sessionId': session_id,
             'message': {'role': 'user', 'content': 'offline SDK fixture'}}
    await store.append({'session_id': session_id}, [entry])
    original = session_resume._read_if_present
    def only_new_config(path):
        assert path.is_relative_to(empty), 'must never inspect a host credential directory'
        return original(path)
    monkeypatch.setattr(session_resume, '_read_if_present', only_new_config)
    materialized = await session_resume.materialize_resume_session(ClaudeAgentOptions(
        cwd='/tmp', resume=session_id, session_store=store, env={'CLAUDE_CONFIG_DIR': str(empty)}))
    assert materialized is not None
    try:
        transcript = materialized.config_dir/'projects'/'-tmp'/f'{session_id}.jsonl'
        assert json.loads(transcript.read_text().strip()) == entry
    finally:
        await materialized.cleanup()


def test_claude_session_mount_is_exact_and_after_tmpfs(tmp_path):
    from kotoyose.provider_jail import build_jail_command
    config = tmp_path/'claude-resume-fixture'
    config.mkdir(mode=0o700)
    command = build_jail_command(Path(sys.executable).resolve(), [], session_directory=config)
    bind = command.index(str(config))
    temporary_mount = next(i for i in range(len(command)-1) if command[i:i+2] == ['--tmpfs', '/tmp'])
    assert temporary_mount < bind < command.index('--')
    assert command[bind-1:bind+2] == ['--bind', str(config), str(config)]


async def test_postgres_mirror_overflow_disables_resume_without_stopping_coding_or_loading_prefix(store):
    raw = PostgresSessionStore(store.pool, max_session_bytes=1024)
    await raw.create_schema()
    view = ScopedSessionStore(raw, session_scope('caller', 'workspace', 'claude'))
    key = {'session_id': str(uuid4())}
    entries = [{'type': 'user', 'message': {'role': 'user', 'content': 'public fixture'}}]
    await view.append(key, entries)
    assert await view.load(key) == entries
    await view.append(key, [{'type': 'assistant', 'message': {'content': 'x'*2048}}])
    assert await view.load(key) is None
    await view.append(key, [{'type': 'user', 'message': {'content': 'later'}}])
    assert await view.load(key) is None
    # 共有の試験 DB には他の実行の行が残るので、この試験の session だけを数える。
    stored = await store.pool.fetchval(
        'SELECT coalesce(sum(octet_length(entry::text)), 0) FROM kotoyose.claude_session_store WHERE session_id = $1',
        key['session_id'],
    )
    rows = await store.pool.fetchval(
        'SELECT count(*) FROM kotoyose.claude_session_store WHERE session_id = $1', key['session_id']
    )
    assert rows > 0 and stored < 1024


def test_codex_locator_does_not_search_or_cross_owner_directories(tmp_path):
    sessions = CodexSessionDirectory(tmp_path/'private-state')
    scope = session_scope('caller-a', 'workspace', 'codex')
    thread = str(uuid4())
    relative = f'2026/10/19/rollout-{thread}.jsonl'
    ref = sessions.reference(thread, '/home/kotoyose/.codex/sessions/'+relative)
    assert not sessions.retained(scope, ref)
    file = sessions.directory(scope)/relative
    file.parent.mkdir(parents=True)
    file.write_text('opaque official rollout fixture\n')
    assert sessions.retained(scope, ref)
    other_scope = session_scope('caller-b', 'workspace', 'codex')
    assert not sessions.retained(other_scope, ref)
    assert not (sessions.root/other_scope).exists()
    with pytest.raises(ValueError):
        sessions.reference(thread, '/home/kotoyose/.codex/sessions/../../'+thread)


def test_missing_or_replaced_session_root_is_not_recreated_by_availability_check(tmp_path):
    sessions = CodexSessionDirectory(tmp_path/'private-root')
    scope, thread = session_scope('caller', 'workspace', 'codex'), str(uuid4())
    ref = sessions.reference(thread, '/home/kotoyose/.codex/sessions/'+thread+'.jsonl')
    sessions.root.rename(tmp_path/'preserved-old-root')
    assert not sessions.retained(scope, ref)
    assert not sessions.root.exists()


async def test_codex_uses_official_resume_without_replaying_tools_or_starting_new_thread(tmp_path):
    worker = NativeCodexWorker(session_root=tmp_path/'state')
    scope = session_scope('caller', 'workspace', 'codex')
    thread = str(uuid4())
    relative = f'2026/10/19/rollout-{thread}.jsonl'
    path = worker.sessions.directory(scope)/relative
    path.parent.mkdir(parents=True)
    path.write_text('official opaque fixture')
    ref = worker.sessions.reference(thread, '/home/kotoyose/.codex/sessions/'+relative)
    event = lambda method, **params: {'method': method, 'params': {'threadId': thread, 'turnId': 'turn', **params}}
    rpc = run_rpc([event('item/completed', item={'type': 'agentMessage', 'id': 'answer', 'phase': 'final_answer', 'text': 'continued'}),
                   event('turn/completed', turn={'id': 'turn', 'status': 'completed'})])
    rpc.request.side_effect = [{}, {'thread': {'id': thread}, 'model': 'gpt-6-luna', 'modelProvider': 'kotoyose_native'},
                               {'turn': {'id': 'turn'}}]
    job = replace(_job(tmp_path), model='gpt-6-luna', session_scope=scope,
                  resume_mode='native', provider_session_ref=ref)
    assert await worker._run(rpc, job, asyncio.Event(), AsyncMock(), {}, {}, _CONFIG) == b'continued'
    method, params = rpc.request.await_args_list[1].args
    assert method == 'thread/resume' and params['threadId'] == thread
    assert params['excludeTurns'] is True
    assert 'ephemeral' not in params and 'dynamicTools' not in params and 'environments' not in params
    assert rpc.session_ref == ref


async def test_missing_codex_state_does_not_silently_run_fresh(tmp_path):
    worker = NativeCodexWorker(session_root=tmp_path/'state')
    scope = session_scope('caller', 'workspace', 'codex')
    thread = str(uuid4())
    ref = worker.sessions.reference(thread, '/home/kotoyose/.codex/sessions/'+thread+'.jsonl')
    job = replace(_job(tmp_path), model='gpt-6-luna', session_scope=scope, resume_mode='native', provider_session_ref=ref)
    rpc = run_rpc([])
    with pytest.raises(ValueError, match='provider_session_unavailable'):
        await worker._run(rpc, job, asyncio.Event(), AsyncMock(), {}, {}, _CONFIG)
    assert rpc.request.await_count == 1  # initialize only, no provider turn


async def test_claude_connects_existing_sdk_resume_and_scoped_store(tmp_path, monkeypatch):
    from claude_agent_sdk import ResultMessage
    session_id = str(uuid4())
    scope = session_scope('owner', 'workspace', 'claude')
    store = MemoryStore()
    await ScopedSessionStore(store, scope).append({'session_id': session_id}, [{'type': 'user'}])
    _Client.instances = []
    _Client.messages = [ResultMessage(subtype='success', duration_ms=1, duration_api_ms=1, is_error=False,
        num_turns=1, session_id=session_id, result='continued')]
    monkeypatch.setattr('kotoyose.native_claude.ClaudeSDKClient', _Client)
    worker = NativeClaudeWorker(session_store=store)
    result = await worker.execute(_job(tmp_path, session_scope=scope, resume_mode='native', provider_session_ref=session_id),
                                  asyncio.Event(), _successful_tool, on_process_started=AsyncMock())
    options = _Client.instances[-1].options
    assert options.resume == session_id and isinstance(options.session_store, ScopedSessionStore)
    assert options.env['KOTOYOSE_NATIVE_CLAUDE_SESSION_ENABLED'] == '1'
    assert result.provider_session_id == session_id
    _Client.messages = [ResultMessage(subtype='success', duration_ms=1, duration_api_ms=1, is_error=True,
        num_turns=0, session_id=session_id, api_error_status=429, terminal_reason='api_error')]
    limited = await worker.execute(_job(tmp_path, session_scope=scope, limits_mode='agent_work_v1'),
                                   asyncio.Event(), _successful_tool, on_process_started=AsyncMock())
    assert limited.provider_limit.resume == 'native' and limited.provider_session_id == session_id


async def test_claude_mirror_failure_does_not_publish_a_prefix_as_resumable(tmp_path, monkeypatch):
    from claude_agent_sdk import ResultMessage
    session_id, scope = str(uuid4()), session_scope('owner', 'workspace', 'claude')
    class FailedMirror(MemoryStore):
        async def append(self, key, entries):
            raise RuntimeError('fixture store failure')
    raw = FailedMirror()
    raw.entries[('native-'+scope, session_id)] = [{'type': 'user'}]
    class Client(_Client):
        async def query(self, prompt):
            try:
                await self.options.session_store.append({'session_id': session_id}, [{'type': 'user'}])
            except RuntimeError:
                pass
    Client.messages = [ResultMessage(subtype='success', duration_ms=1, duration_api_ms=1, is_error=False,
        num_turns=1, session_id=session_id, result='continued')]
    monkeypatch.setattr('kotoyose.native_claude.ClaudeSDKClient', Client)
    worker = NativeClaudeWorker(session_store=raw)
    result = await worker.execute(_job(tmp_path, session_scope=scope), asyncio.Event(), _successful_tool,
                                  on_process_started=AsyncMock())
    assert result.provider_session_id is None
    assert result.status == 'succeeded'
    Client.messages = [ResultMessage(subtype='success', duration_ms=1, duration_api_ms=1, is_error=True,
        num_turns=0, session_id=session_id, api_error_status=429, terminal_reason='api_error')]
    limited = await worker.execute(_job(tmp_path, session_scope=scope, limits_mode='agent_work_v1'),
                                   asyncio.Event(), _successful_tool, on_process_started=AsyncMock())
    assert limited.provider_limit.resume == 'unavailable'
    assert limited.provider_limit.unavailable_reason == 'not_retained' and limited.provider_session_id is None
