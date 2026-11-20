"""Read-only admin MCP probes over real HTTP/SSE and isolated stdio processes.

The admin app is exercised through ASGI without provider/DB startup. MCP transport
traffic goes to an actual ephemeral loopback server, never a mock SDK or provider.
"""

import asyncio
import json
import os
from pathlib import Path
import signal
import socket
import sys
import time
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
import uvicorn
import yaml
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from kotoyose import mcp_probe
from kotoyose.config import get_config, reload_config
from kotoyose.middleware import RequestIDMiddleware
from kotoyose.routes.admin import router


pytestmark = pytest.mark.skipif(os.name != 'posix', reason='Probe worker owns a POSIX process group')
FIXTURE_SERVER = Path(__file__).parent / 'fixtures' / 'mcp_probe_server.py'
TOOL = {'name': 'inspect_fixture', 'description': 'Metadata only', 'inputSchema': {'type': 'object'}}


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    path = tmp_path / 'config.yml'
    path.write_text('models: {}\nmcps: {}\n')
    monkeypatch.setenv('KOTOYOSE_CONFIG', str(path))
    monkeypatch.delenv('KOTOYOSE_EXECUTION_CONFIG', raising=False)
    monkeypatch.setattr('kotoyose.auth.API_KEY', None)
    reload_config()
    yield path
    reload_config()


@pytest_asyncio.fixture
async def admin_client():
    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://admin.test') as client:
        yield client


@pytest_asyncio.fixture
async def admin_tcp():
    """Use the production BaseHTTPMiddleware stack and a real TCP disconnect."""
    app = FastAPI()
    app.add_middleware(RequestIDMiddleware)
    app.include_router(router)
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    address = listener.getsockname()
    server = uvicorn.Server(uvicorn.Config(app, lifespan='off', log_level='error', access_log=False, ws='none',
                                         timeout_graceful_shutdown=1))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if serving.done():
                    await serving
                await asyncio.sleep(0.01)
        yield address
    finally:
        server.should_exit = True
        await asyncio.wait_for(serving, timeout=5)
        listener.close()


@pytest.fixture
def stdio_server(tmp_path):
    def make(mode='normal', *, secret=None, descendants=False):
        env = {'MCP_PROBE_FIXTURE_MODE': mode, 'MCP_PROBE_FIXTURE_RECORD': str(tmp_path / 'requests.jsonl')}
        if secret:
            env['MCP_PROBE_FIXTURE_SECRET'] = secret
        if descendants:
            env['MCP_PROBE_FIXTURE_PIDS'] = str(tmp_path / 'pids.json')
        return {'type': 'stdio', 'command': sys.executable, 'args': ['-u', str(FIXTURE_SERVER)], 'env': env}
    return make


@pytest_asyncio.fixture
async def remote():
    app = FastAPI()
    state = SimpleNamespace(mode='normal', tools=[dict(TOOL)], methods=[], requests=[], cursors=[],
                            status=200, redirect=None, trap_hits=[], secret='', sse_queue=asyncio.Queue())

    def rpc_response(body):
        method = body['method']
        state.methods.append(method)
        if method.startswith('notifications/'):
            return None
        if method == 'initialize':
            result = {'protocolVersion': body['params']['protocolVersion'],
                      'capabilities': {} if state.mode == 'unsupported' else {'tools': {}},
                      'serverInfo': {'name': 'probe-http-fixture', 'version': '1'},
                      'instructions': state.secret}
        elif method == 'tools/list':
            cursor = body.get('params', {}).get('cursor')
            state.cursors.append(cursor)
            result = {'tools': state.tools}
            if state.mode == 'pagination':
                page = len(state.cursors)
                result = {'tools': [{**TOOL, 'name': f'tool_{page}'}], 'nextCursor': f'page_{page}'}
            elif state.mode == 'two_pages':
                result = {'tools': [{**TOOL, 'name': 'first' if cursor is None else 'second'}]}
                if cursor is None:
                    result['nextCursor'] = 'second-page'
            elif state.mode == 'repeated_cursor':
                result = {'tools': [], 'nextCursor': 'same-cursor'}
        else:
            # Persist evidence even if a future bug sends tools/call, then fail assertions.
            result = {'content': [{'type': 'text', 'text': 'MUST NOT BE CALLED'}]}
        return {'jsonrpc': '2.0', 'id': body['id'], 'result': result}

    @app.api_route('/mcp', methods=['GET', 'POST', 'DELETE'])
    async def mcp(request: Request):
        state.requests.append((request.method, dict(request.headers)))
        if state.redirect:
            return Response(status_code=307, headers={'Location': state.redirect})
        if state.status != 200:
            return Response(f'private upstream error {state.secret}', status_code=state.status)
        if request.method != 'POST':
            return Response(status_code=405)
        body = await request.json()
        if state.mode == 'invalid':
            return Response(f'not json {state.secret}', media_type='application/json')
        reply = rpc_response(body)
        return Response(status_code=202) if reply is None else JSONResponse(reply)

    @app.get('/sse')
    async def sse(request: Request):
        state.requests.append((request.method, dict(request.headers)))
        async def events():
            yield 'event: endpoint\ndata: /sse-message\n\n'
            while True:
                body = await state.sse_queue.get()
                yield f'event: message\ndata: {json.dumps(body)}\n\n'
        return StreamingResponse(events(), media_type='text/event-stream')

    @app.post('/sse-message')
    async def sse_message(request: Request):
        state.requests.append((request.method, dict(request.headers)))
        reply = rpc_response(await request.json())
        if reply is not None:
            await state.sse_queue.put(reply)
        return Response(status_code=202)

    @app.api_route('/trap', methods=['GET', 'POST', 'DELETE'])
    async def trap(request: Request):
        state.trap_hits.append((request.method, dict(request.headers)))
        return Response(status_code=500)

    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    state.base_url = f'http://127.0.0.1:{listener.getsockname()[1]}'
    state.endpoint = state.base_url + '/mcp'
    server = uvicorn.Server(uvicorn.Config(app, log_level='error', access_log=False, ws='none',
                                         timeout_graceful_shutdown=1))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if serving.done():
                    await serving
                await asyncio.sleep(0.01)
        yield state
    finally:
        server.should_exit = True
        await asyncio.wait_for(serving, timeout=5)
        listener.close()


def assert_result(result, status, *, stage=None, code=None, count=None):
    assert result['status'] == status, result
    assert result['stage'] in {'validation', 'connect', 'initialize', 'tools_list', 'cleanup'}
    if stage is not None:
        assert result['stage'] == stage, result
    if code is not None:
        assert result['error_code'] == code, result
    assert isinstance(result['elapsed_ms'], int) and result['elapsed_ms'] >= 0
    assert result['tool_count'] == count
    if status == 'error':
        assert result['tools'] == []


def recorded_methods(tmp_path):
    return [json.loads(line)['method'] for line in (tmp_path / 'requests.jsonl').read_text().splitlines()]


@pytest.mark.parametrize('transport', ['http', 'sse'])
async def test_http_and_sse_list_only_metadata(admin_client, remote, transport):
    url = remote.endpoint if transport == 'http' else remote.base_url + '/sse'
    response = await admin_client.post('/admin/mcp-probe', json={'server': {'type': transport, 'url': url}})
    assert response.status_code == 200
    result = response.json()
    assert_result(result, 'ok', stage='tools_list', count=1)
    assert result['tools'] == [{'name': 'inspect_fixture'}]
    assert remote.methods == ['initialize', 'notifications/initialized', 'tools/list']
    assert 'tools/call' not in remote.methods


@pytest.mark.parametrize(('mode', 'status', 'count', 'stage'), [
    ('normal', 'ok', 1, 'tools_list'),
    ('empty', 'no_tools', 0, 'tools_list'),
    ('unsupported', 'tools_unsupported', None, 'initialize'),
])
async def test_real_stdio_distinguishes_tool_capability(stdio_server, tmp_path, mode, status, count, stage):
    result = await mcp_probe.run_mcp_probe(stdio_server(mode))
    assert_result(result, status, stage=stage, count=count)
    expected = ['initialize', 'notifications/initialized']
    if mode != 'unsupported':
        expected.append('tools/list')
    assert recorded_methods(tmp_path) == expected


@pytest.mark.parametrize(('mode', 'status', 'count'), [('normal', 'no_tools', 0), ('unsupported', 'tools_unsupported', None)])
async def test_http_zero_tools_is_not_unsupported(remote, mode, status, count):
    remote.mode, remote.tools = mode, []
    result = await mcp_probe.run_mcp_probe({'type': 'http', 'url': remote.endpoint})
    assert_result(result, status, count=count)
    assert ('tools/list' in remote.methods) == (mode != 'unsupported')


@pytest.mark.parametrize('body', [b'{', b'null', b'[]', b'{}', b'{"server":{},"save":true}', b'\xff'])
async def test_bad_json_and_envelope_are_safe_errors(admin_client, body):
    response = await admin_client.post('/admin/mcp-probe', content=body, headers={'Content-Type': 'application/json'})
    assert response.status_code == 200
    assert_result(response.json(), 'error', stage='validation', code='invalid_config')


@pytest.mark.parametrize('server', [
    None, [], 'secret-invalid-config', {},
    {'type': 'websocket', 'url': 'https://example.invalid'},
    {'type': 'stdio', 'command': ''},
    {'type': 'stdio', 'command': 'python', 'args': 'not-an-array'},
    {'type': 'stdio', 'command': 'python', 'env': {'KEY': 123}},
    {'type': 'stdio', 'command': 'python', 'env': {'BAD=KEY': 'secret-invalid-config'}},
    {'type': 'http', 'url': 'file:///private/secret-invalid-config'},
    {'type': 'http', 'url': 'https://user:secret-invalid-config@example.invalid'},
    {'type': 'http', 'url': 'https://example.invalid/#secret-invalid-config'},
    {'type': 'http', 'url': 'https://example.invalid:not-a-port'},
    {'type': 'http', 'url': 'https://example.invalid', 'headers': {'X-Token': 'secret-invalid-config\nX-Evil: true'}},
    {'type': 'http', 'url': 'https://example.invalid', 'headers': {'Authorization': '{{header:x-api-token}}'}},
    {'type': 'http', 'url': 'https://example.invalid', 'unexpected': 'secret-invalid-config'},
])
async def test_invalid_config_never_starts_worker(admin_client, monkeypatch, server):
    started = []
    async def forbidden_worker(*args, **kwargs):
        started.append(args)
        raise AssertionError('Invalid config must not start a worker')
    monkeypatch.setattr(mcp_probe.asyncio, 'create_subprocess_exec', forbidden_worker)
    response = await admin_client.post('/admin/mcp-probe', json={'server': server})
    assert_result(response.json(), 'error', stage='validation', code='invalid_config')
    assert started == []
    assert 'secret-invalid-config' not in response.text


async def test_body_size_is_bounded(admin_client):
    response = await admin_client.post('/admin/mcp-probe', content=b' ' * (mcp_probe.MAX_CONFIG_BYTES + 1),
                                       headers={'Content-Type': 'application/json'})
    assert_result(response.json(), 'error', stage='validation', code='invalid_config')


async def test_deeply_nested_json_is_a_safe_validation_error(admin_client):
    body = b'{"server":' + b'[' * 1500 + b'0' + b']' * 1500 + b'}'
    response = await admin_client.post('/admin/mcp-probe', content=body, headers={'Content-Type': 'application/json'})
    assert response.status_code == 200
    assert_result(response.json(), 'error', stage='validation', code='invalid_config')


@pytest.mark.parametrize('endpoint', ['/admin/mcp-probe', '/admin/mcps/unknown/probe'])
async def test_both_endpoints_require_admin_basic_auth(admin_client, monkeypatch, endpoint):
    monkeypatch.setattr('kotoyose.auth.API_KEY', 'fixture-admin-password')
    monkeypatch.setattr('kotoyose.auth.BASIC_AUTH_USER', 'fixture-admin')
    for headers in ({}, {'Authorization': 'Bearer fixture-admin-password'}):
        response = await admin_client.post(endpoint, json={'server': {}}, headers=headers)
        assert response.status_code == 401
        assert response.headers['www-authenticate'].startswith('Basic ')
    response = await admin_client.post(endpoint, json={'server': {}}, auth=('fixture-admin', 'wrong-password'))
    assert response.status_code == 401
    response = await admin_client.post(endpoint, json={'server': {}}, auth=('fixture-admin', 'fixture-admin-password'))
    assert response.status_code == (404 if '/mcps/' in endpoint else 200)


async def test_missing_saved_mcp_is_404_without_spawning(admin_client, monkeypatch):
    async def forbidden_probe(*args, **kwargs):
        raise AssertionError('Unknown saved MCP must not start a probe')
    monkeypatch.setattr(mcp_probe, 'run_mcp_probe', forbidden_probe)
    response = await admin_client.post('/admin/mcps/unknown/probe', json={})
    assert response.status_code == 404
    assert response.json() == {'detail': 'MCP not found'}


async def test_draft_and_saved_probes_do_not_mutate_config(admin_client, isolated_config, remote):
    saved = {'type': 'http', 'url': remote.endpoint}
    isolated_config.write_text(yaml.safe_dump({'models': {}, 'mcps': {'fixture': {'description': 'Keep me', 'server': saved}}}))
    reload_config()
    before = isolated_config.read_bytes()
    before_mtime = isolated_config.stat().st_mtime_ns
    in_memory = get_config().model_dump()
    for endpoint, body in [('/admin/mcp-probe', {'server': saved}), ('/admin/mcps/fixture/probe', {})]:
        response = await admin_client.post(endpoint, json=body)
        assert_result(response.json(), 'ok', count=1)
        assert isolated_config.read_bytes() == before
        assert isolated_config.stat().st_mtime_ns == before_mtime
        assert get_config().model_dump() == in_memory
        assert not list(isolated_config.parent.glob('config.yml*bak*'))
    assert remote.methods.count('tools/list') == 2


@pytest.mark.parametrize('content_type', [None, 'text/plain', 'application/x-www-form-urlencoded', 'multipart/form-data'])
@pytest.mark.parametrize('endpoint', ['/admin/mcp-probe', '/admin/mcps/fixture/probe'])
async def test_cross_origin_simple_posts_cannot_start_probe(admin_client, isolated_config, monkeypatch, endpoint, content_type):
    isolated_config.write_text(yaml.safe_dump({'models': {}, 'mcps': {'fixture': {'server': {'command': 'must-not-run'}}}}))
    reload_config()
    started = []
    async def forbidden_probe(*args, **kwargs):
        started.append(args)
        raise AssertionError('Simple cross-origin requests cannot execute a configured command')
    monkeypatch.setattr(mcp_probe, 'run_mcp_probe', forbidden_probe)
    headers = {'Origin': 'https://untrusted.example'}
    if content_type is not None:
        headers['Content-Type'] = content_type
    response = await admin_client.post(endpoint, content=b'{"server":{"command":"must-not-run"}}', headers=headers)
    if '/mcps/' in endpoint:
        assert response.status_code == 415
    else:
        assert_result(response.json(), 'error', stage='validation', code='invalid_config')
    assert started == []


@pytest.mark.parametrize('status', [401, 403, 500])
async def test_upstream_http_errors_are_sanitized(remote, caplog, capfd, status):
    remote.status, remote.secret = status, 'http-secret-must-not-escape'
    result = await mcp_probe.run_mcp_probe({'type': 'http', 'url': remote.endpoint,
                                           'headers': {'Authorization': f'Bearer {remote.secret}'}})
    assert_result(result, 'error', stage='initialize', code='authentication_failed' if status in (401, 403) else 'http_error')
    assert remote.secret not in json.dumps(result) + caplog.text + ''.join(capfd.readouterr())


@pytest.mark.parametrize('mode', ['invalid', 'protocol_error'])
async def test_stdio_protocol_errors_do_not_echo_secrets(stdio_server, caplog, capfd, mode):
    secret = 'stdio-protocol-secret-718b'
    result = await mcp_probe.run_mcp_probe(stdio_server(mode, secret=secret))
    assert_result(result, 'error', stage='initialize')
    assert result['error_code'] != 'timeout'
    assert secret not in json.dumps(result) + caplog.text + ''.join(capfd.readouterr())


async def test_invalid_http_protocol_is_an_error(remote):
    remote.mode = 'invalid'
    result = await mcp_probe.run_mcp_probe({'type': 'http', 'url': remote.endpoint})
    assert_result(result, 'error', stage='initialize', code='transport_rejected')
    assert result['elapsed_ms'] < 5000, 'Malformed JSON must fail before the SDK request timeout'


async def test_missing_stdio_command_is_actionable(tmp_path):
    result = await mcp_probe.run_mcp_probe({'type': 'stdio', 'command': str(tmp_path / 'does-not-exist')})
    assert_result(result, 'error', stage='connect', code='command_not_found')
    assert str(tmp_path) not in json.dumps(result)


@pytest.mark.parametrize('transport', ['stdio', 'http', 'sse'])
async def test_reflected_secrets_are_redacted_and_tool_metadata_is_omitted(remote, stdio_server, caplog, capfd, transport):
    secret = 'fixture-sensitive-token-e290'
    if transport == 'stdio':
        server = stdio_server(secret=secret)
    else:
        remote.secret = secret
        remote.tools = [{**TOOL, 'name': f'inspect_{secret}', 'description': secret,
                         'inputSchema': {'type': 'object', 'description': secret}}]
        url = remote.endpoint if transport == 'http' else remote.base_url + '/sse'
        server = {'type': transport, 'url': url, 'headers': {'Authorization': f'Bearer {secret}'}}
    result = await mcp_probe.run_mcp_probe(server)
    assert_result(result, 'ok', count=1)
    assert result['tools'] == [{'name': 'inspect_[redacted]'}]
    assert secret not in json.dumps(result) + caplog.text + ''.join(capfd.readouterr())
    if transport != 'stdio':
        assert remote.requests
        assert all(headers.get('authorization') == f'Bearer {secret}' for _, headers in remote.requests)


@pytest.mark.parametrize('cross_origin', [False, True])
async def test_redirects_are_refused_without_sending_credentials_to_target(remote, cross_origin):
    # localhost vs 127.0.0.1 is a different origin, backed by the same trap listener.
    target = remote.base_url.replace('127.0.0.1', 'localhost') if cross_origin else remote.base_url
    remote.redirect = target + '/trap'
    result = await mcp_probe.run_mcp_probe({'type': 'http', 'url': remote.endpoint,
                                           'headers': {'Authorization': 'Bearer redirect-fixture-token'}})
    assert_result(result, 'error', stage='initialize', code='redirect_rejected')
    assert len(remote.requests) == 1
    assert remote.trap_hits == []


async def test_successful_pagination_collects_tools_without_invoking_them(remote):
    remote.mode = 'two_pages'
    result = await mcp_probe.run_mcp_probe({'type': 'http', 'url': remote.endpoint})
    assert_result(result, 'ok', count=2)
    assert result['tools'] == [{'name': 'first'}, {'name': 'second'}]
    assert remote.cursors == [None, 'second-page']
    assert 'tools/call' not in remote.methods


@pytest.mark.parametrize(('mode', 'page_count'), [('pagination', 8), ('repeated_cursor', 2)])
async def test_pagination_is_bounded(remote, mode, page_count):
    remote.mode = mode
    result = await mcp_probe.run_mcp_probe({'type': 'http', 'url': remote.endpoint})
    assert_result(result, 'error', stage='tools_list', code='transport_rejected')
    assert len(remote.cursors) == page_count
    assert 'tools/call' not in remote.methods


async def test_tool_inventory_is_bounded(remote):
    remote.tools = [{**TOOL, 'name': f'tool_{index}'} for index in range(mcp_probe.MAX_TOOLS + 1)]
    result = await mcp_probe.run_mcp_probe({'type': 'http', 'url': remote.endpoint})
    assert_result(result, 'error', stage='tools_list', code='transport_rejected')
    assert len(remote.cursors) == 1


async def test_full_allowed_inventory_exceeds_64k_worker_output_without_truncation(remote):
    remote.tools = [{**TOOL, 'name': f'tool_{index:03}_' + 'x' * 247} for index in range(mcp_probe.MAX_TOOLS)]
    result = await mcp_probe.run_mcp_probe({'type': 'http', 'url': remote.endpoint})
    assert_result(result, 'ok', count=mcp_probe.MAX_TOOLS)
    assert result['tools'] == [{'name': tool['name']} for tool in remote.tools]
    assert len(json.dumps(result)) > 65536


@pytest.mark.parametrize('server', [
    {'env': {'TOKEN': 'secret-token-value'}},
    {'headers': {'Authorization': 'Bearer secret-token-value'}},
    {'args': ['--token=secret-token-value']},
    {'url': 'http://example.invalid/mcp?token=secret-token-value'},
    {'url': 'http://example.invalid/mcp/secret-token-value'},
])
def test_tool_name_normalization_cannot_reconstruct_secrets(server):
    from kotoyose.mcp_probe_worker import safe_name
    name = 'tool_secret-\x00token-\nvalue'
    assert safe_name(name, server) == 'tool_[redacted]'


@pytest.mark.parametrize('nested', [False, True], ids=['direct', 'nested-task-groups'])
@pytest.mark.parametrize(('rpc_code', 'expected'), [(408, 'timeout'), (-32603, 'protocol_error')])
def test_sdk_timeout_is_classified_separately_from_protocol_error(nested, rpc_code, expected):
    from mcp.shared.exceptions import McpError
    from mcp.types import ErrorData
    from kotoyose.mcp_probe_worker import error_code
    error = McpError(ErrorData(code=rpc_code, message='private SDK diagnostic secret-value'))
    if nested:
        error = ExceptionGroup('outer private secret-value', [ExceptionGroup('inner private secret-value', [error])])
    code, message = error_code(error)
    assert code == expected
    assert 'secret-value' not in message


async def test_concurrency_cap_rejects_without_starting_another_worker(monkeypatch):
    monkeypatch.setattr(mcp_probe, '_active_probes', 4)
    result = await mcp_probe.run_mcp_probe({'command': 'must-not-run'})
    assert_result(result, 'error', stage='validation', code='busy')
    assert mcp_probe._active_probes == 4


def process_running(pid):
    """An orphan zombie is already terminated; containers may defer its reaping."""
    try:
        stat = Path(f'/proc/{pid}/stat').read_text()
    except (FileNotFoundError, ProcessLookupError):
        return False
    return stat.rsplit(')', 1)[1].split()[0] != 'Z'


@pytest.mark.skipif(not Path('/proc/self/stat').exists(), reason='Process cleanup assertions use Linux /proc')
@pytest.mark.parametrize('ending', ['success', 'timeout', 'disconnect', 'cancel'])
async def test_process_group_and_descendants_are_cleaned_up(stdio_server, tmp_path, ending):
    pid_path = tmp_path / 'pids.json'
    server = stdio_server('normal' if ending == 'success' else 'stall', descendants=True)
    async def disconnected():
        return pid_path.exists()
    kwargs = {'timeout_seconds': 3} if ending == 'timeout' else {}
    if ending == 'disconnect':
        kwargs['disconnected'] = disconnected
    task = asyncio.create_task(mcp_probe.run_mcp_probe(server, **kwargs))
    pids = None
    started = time.monotonic()
    try:
        async with asyncio.timeout(10):
            while not pid_path.exists():
                if task.done():
                    pytest.fail(f'Probe stopped before fixture started: {await task}')
                await asyncio.sleep(0.02)
        pids = json.loads(pid_path.read_text())
        assert pids['group'] != os.getpgrp(), 'Worker must own a separate process group'
        if ending == 'cancel':
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            result = await task
            if ending == 'success':
                assert_result(result, 'ok', count=1)
            else:
                assert_result(result, 'error', code='timeout' if ending == 'timeout' else 'cancelled')
        async with asyncio.timeout(3):
            while any(process_running(pids[key]) for key in ('group', 'server', 'child')):
                await asyncio.sleep(0.02)
        assert time.monotonic() - started < 10
        assert mcp_probe._active_probes == 0
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if pids:
            try:
                os.killpg(pids['group'], signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.skipif(not Path('/proc/self/stat').exists(), reason='Process cleanup assertions use Linux /proc')
@pytest.mark.parametrize('saved', [False, True], ids=['draft', 'saved'])
async def test_real_http_disconnect_through_middleware_promptly_kills_descendants(
    admin_tcp, isolated_config, stdio_server, tmp_path, saved,
):
    """An abort must beat SDK/deadline timeouts, including behind BaseHTTPMiddleware.

    Polling Request.is_disconnected() can miss the middleware receive checkpoint.
    Closing a live TCP connection catches that regression; an injected callback cannot.
    """
    config = stdio_server('stall', descendants=True)
    if saved:
        isolated_config.write_text(yaml.safe_dump({'models': {}, 'mcps': {'fixture': {'server': config}}}))
        reload_config()
    endpoint = '/admin/mcps/fixture/probe' if saved else '/admin/mcp-probe'
    body = json.dumps({} if saved else {'server': config}).encode()
    host, port = admin_tcp
    _, connection = await asyncio.open_connection(host, port)
    pids = None
    try:
        headers = (f'POST {endpoint} HTTP/1.1\r\nHost: {host}:{port}\r\n'
                   f'Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n').encode()
        connection.write(headers + body)
        await connection.drain()
        async with asyncio.timeout(5):
            while pids is None:
                try:
                    pids = json.loads((tmp_path / 'pids.json').read_text())
                except (FileNotFoundError, json.JSONDecodeError):
                    await asyncio.sleep(0.01)
        assert pids['group'] != os.getpgrp()
        assert all(process_running(pids[key]) for key in ('group', 'server', 'child'))
        disconnected_at = time.monotonic()
        connection.close()
        await connection.wait_closed()
        async with asyncio.timeout(2):
            while mcp_probe._active_probes or any(process_running(pids[key]) for key in ('group', 'server', 'child')):
                await asyncio.sleep(0.01)
        assert time.monotonic() - disconnected_at < 2
    finally:
        connection.close()
        await connection.wait_closed()
        if pids:
            try:
                os.killpg(pids['group'], signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_default_hard_deadline_is_fifteen_seconds():
    assert mcp_probe.PROBE_TIMEOUT_SECONDS == 15
