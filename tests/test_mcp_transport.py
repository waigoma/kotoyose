"""実 HTTP の MCP pin、credential 境界、metadata 改変と POST 再送の拒否を検証する。"""

import asyncio
import socket
from types import SimpleNamespace

import pytest
import pytest_asyncio
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError

from kotoyose.execution_resources import CallerPolicy, ExecutionError
from kotoyose.mcp_contract import ExternalMcpServerConfig, McpPinRequest, validate_mcp_schema
from kotoyose.mcp_resources import McpResourceResolver, server_for_pin, verify_mcp_surface
from kotoyose.mcp_transport import StreamableMcpTransport


TOOL = {'name': 'append', 'description': 'Append one fixture value.',
        'inputSchema': {'type': 'object', 'additionalProperties': False, 'required': ['value'], 'properties': {'value': {'type': 'string'}}},
        'annotations': {'readOnlyHint': True}}


@pytest_asyncio.fixture
async def remote(monkeypatch, tmp_path):
    app = FastAPI()
    state = SimpleNamespace(tool=dict(TOOL), calls=[], headers=[], mode='normal', before_call=None)
    monkeypatch.setenv('KOTOYOSE_MCP_FIXTURE_TOKEN', 'mcp-fixture-only')
    @app.api_route('/mcp', methods=['GET', 'POST', 'DELETE'])
    async def mcp(request: Request):
        state.headers.append(dict(request.headers))
        if request.headers.get('authorization') != 'Bearer mcp-fixture-only':
            return Response(status_code=401)
        if request.method != 'POST':
            return Response(status_code=405)
        body = await request.json()
        method = body['method']
        if method.startswith('notifications/'):
            return Response(status_code=202)
        if method == 'initialize':
            result = {'protocolVersion': body['params']['protocolVersion'], 'capabilities': {'tools': {}},
                      'serverInfo': {'name': 'fixture', 'version': '1'}}
        elif method == 'tools/list':
            result = {'tools': [state.tool]}
        elif method == 'tools/call':
            if state.before_call:
                await state.before_call(body)
            state.calls.append(body)
            if state.mode == 'lost':
                return Response(status_code=500)
            if state.mode == 'redirect':
                return Response(status_code=307, headers={'Location': '/never-authorized'})
            result = {'content': [{'type': 'text', 'text': 'x' * 4096 if state.mode == 'large' else 'done'}], 'isError': False}
        else:
            return JSONResponse({'jsonrpc': '2.0', 'id': body['id'], 'error': {'code': -32601, 'message': 'unsupported'}})
        return JSONResponse({'jsonrpc': '2.0', 'id': body['id'], 'result': result})
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    endpoint = f'http://127.0.0.1:{listener.getsockname()[1]}/mcp'
    server = uvicorn.Server(uvicorn.Config(app, log_level='error', access_log=False, ws='none'))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if serving.done():
                    await serving
                await asyncio.sleep(0.01)
        config = ExternalMcpServerConfig(endpoint=endpoint, server_name='fixture', server_version='1',
            trust_profile_ref='operator:fixture', credential_env='KOTOYOSE_MCP_FIXTURE_TOKEN', allow_insecure_loopback=True,
            max_metadata_bytes=1024, tools={'append': {'effect': 'write', 'max_result_bytes': 1024}})
        policy = CallerPolicy(workspace_roots=[tmp_path], mcp_servers=['fixture'],
            max_seconds=60, max_memory_bytes=67108864, max_pids=8, max_cpus=1, max_concurrent=1)
        yield config, policy, state
    finally:
        server.should_exit = True
        await asyncio.wait_for(serving, timeout=5)
        listener.close()


def test_mcp_contract_rejects_ambient_endpoint_and_schema_resolution():
    base = {'endpoint': 'https://mcp.example.test/mcp', 'server_name': 'fixture', 'server_version': '1',
            'trust_profile_ref': 'operator:fixture', 'tools': {'append': {'effect': 'write'}}}
    for endpoint in ['http://mcp.example.test/mcp', 'https://user:password@mcp.example.test/mcp', 'https://mcp.example.test/mcp?token=x', 'file:///tmp/mcp', 'http://localhost/mcp']:
        with pytest.raises(ValidationError):
            ExternalMcpServerConfig.model_validate({**base, 'endpoint': endpoint, 'allow_insecure_loopback': True})
    for schema in [{'type': 'object', '$ref': 'https://other/schema'}, {'type': 'object', '$id': 'https://other/schema'}, {'type': 'array'},
                   {'type': 'object', '$ref': '#/$defs/recursive', '$defs': {'recursive': {'$ref': '#/$defs/recursive'}}},
                   {'type': 'object', 'properties': {'value': {'type': 'string', 'pattern': '(a+)+$'}}}]:
        with pytest.raises(ValueError):
            validate_mcp_schema(schema)
    validate_mcp_schema({'type': 'object', '$defs': {'value': {'type': 'string'}}, 'properties': {'value': {'$ref': '#/$defs/value'}}})
    validate_mcp_schema({'type': 'object', 'properties': {'pattern': {'type': 'string'}, '$id': {'type': 'string'}}})


@pytest.mark.asyncio
async def test_reflected_credential_cannot_become_pinned_tool_instructions(remote):
    config, policy, state = remote
    state.tool = {**state.tool, 'description': 'mcp-fixture-only'}
    with pytest.raises(Exception):
        await pin(config, policy)
    assert state.calls == []


async def pin(config, policy):
    return await McpResourceResolver({'fixture': config}).pin(policy, McpPinRequest(schema_version='1', server_id='fixture', tools={'append': 'fixture_append'}))


@pytest.mark.asyncio
async def test_real_http_pin_uses_admin_effect_and_separates_credentials_from_resource(remote):
    config, policy, state = remote
    binding = await pin(config, policy)
    assert binding.tools[0].ceiling.effect == 'write'
    assert binding.tools[0].alias == 'fixture_append'
    assert binding.provenance == 'external_untrusted'
    assert 'mcp-fixture-only' not in binding.model_dump_json()
    assert config.endpoint not in binding.model_dump_json()
    assert all(headers.get('authorization') == 'Bearer mcp-fixture-only' for headers in state.headers)
    async with StreamableMcpTransport().connect(config) as session:
        await verify_mcp_surface(session, binding)
        result = await session.call_tool('append', {'value': 'one'}, 'operation:fixed', 2)
    assert result['content'][0]['text'] == 'done'
    assert len(state.calls) == 1
    assert state.calls[0]['params']['_meta']['kotoyose_operation_id'] == 'operation:fixed'


@pytest.mark.asyncio
async def test_tool_definition_changes_are_detected_before_invocation(remote):
    config, policy, state = remote
    binding = await pin(config, policy)
    state.tool = {**state.tool, 'description': 'Changed server instructions.'}
    with pytest.raises(ExecutionError) as failure:
        await McpResourceResolver({'fixture': config}).validate(policy, binding)
    assert failure.value.code == 'request_conflict'
    assert state.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['lost', 'redirect', 'large'])
async def test_failed_call_is_sent_once_without_following_redirect_or_repeating_effect(remote, mode):
    config, _, state = remote
    state.mode = mode
    with pytest.raises(Exception):
        async with asyncio.timeout(3):
            async with StreamableMcpTransport().connect(config) as session:
                await session.call_tool('append', {'value': 'only-once'}, 'operation:single', 1)
    assert len(state.calls) == 1


@pytest.mark.asyncio
async def test_caller_scope_and_changed_configuration_are_denied_before_network(remote):
    config, policy, state = remote
    binding = await pin(config, policy)
    count = len(state.headers)
    with pytest.raises(ExecutionError) as denied:
        server_for_pin({'fixture': config}, policy.model_copy(update={'mcp_servers': []}), binding)
    assert denied.value.status == 403
    with pytest.raises(ExecutionError) as changed:
        server_for_pin({'fixture': config.model_copy(update={'trust_profile_ref': 'operator:changed'})}, policy, binding)
    assert changed.value.status == 409
    assert len(state.headers) == count
