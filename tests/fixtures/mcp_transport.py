"""TaskHub → Kotoyose → MCP の実 HTTP と未知効果の照合をつなぐ loopback fixture。"""

import asyncio
from uuid import uuid4

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from kotoyose.mcp_contract import ExternalMcpServerConfig, McpPinRequest
from tests.fixtures.client_tool_transport import app
from tests.test_execution_agent import _resources
from tests.test_mcp_transport import TOOL
from tests.fixtures.clients import clients, set_client


@app.api_route('/mcp-remote', methods=['POST', 'GET', 'DELETE'])
async def mcp_remote(request: Request):
    """外部変更の直後に応答を失う server。実呼び出し回数を別途確認する。"""
    if request.method != 'POST':
        return Response(status_code=405)
    body = await request.json()
    if body['method'].startswith('notifications/'):
        return Response(status_code=202)
    if body['method'] == 'initialize':
        result = {'protocolVersion': body['params']['protocolVersion'], 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'fixture', 'version': '1'}}
    elif body['method'] == 'tools/list':
        result = {'tools': [TOOL]}
    elif body['method'] == 'tools/call':
        app.state.mcp_calls.append(body)
        return Response(status_code=500)
    else:
        return Response(status_code=400)
    return JSONResponse({'jsonrpc': '2.0', 'id': body['id'], 'result': result})


@app.post('/mcp-fixture')
async def mcp_fixture(request: Request):
    """provider は起動せず、実 Broker の unknown 操作を残した終端 Attempt を作る。"""
    service = app.state.execution_service
    server = ExternalMcpServerConfig(endpoint=str(request.base_url) + 'mcp-remote', server_name='fixture', server_version='1',
        trust_profile_ref='operator:fixture', allow_insecure_loopback=True, tools={'append': {'effect': 'write'}})
    service.config.mcp_servers['fixture'] = server
    service.admission.mcp_resolver.servers = service.config.mcp_servers
    policy = clients()['caller-a']
    if 'fixture' not in policy.mcp_servers:
        policy.mcp_servers.append('fixture')
    app.state.mcp_calls = []
    binding = await service.admission.mcp_resolver.pin(policy, McpPinRequest(schema_version='1', server_id='fixture', tools={'append': 'fixture_append'}))
    binding_ref = await service.store.put_resource('caller-a', binding)
    execution, _, _ = await _resources(service, policy, app.state.fixture_root / str(uuid4()), tool_refs=[])
    grant = await service.store.resource('caller-a', execution.execution_grant_ref)
    grant_ref = await service.store.put_resource('caller-a', grant.model_copy(update={'version': 2, 'resource_refs': [*grant.resource_refs, binding_ref]}))
    execution = execution.model_copy(update={'execution_grant_ref': grant_ref, 'mcp_refs': [binding_ref]})
    snapshot = await service.admission.resolve('caller-a', policy, execution)
    attempt, _ = await service.store.admit('caller-a', execution.model_dump(mode='json'), snapshot, 20)
    await service.store.mark_running(attempt['id'])
    await service.agent_executor.mcp_broker.call('caller-a', attempt['id'], binding_ref + '#fixture_append', {'value': 'one'}, asyncio.Event())
    await service.store.finish(attempt['id'], 'failed', 'fixture', 'confirmed', 1, {})
    return {'attemptId': attempt['id'], 'bindingRef': binding_ref}


@app.get('/mcp-observation')
async def mcp_observation():
    """HTTP 再送で外部効果が増えていないことだけを返す。"""
    return {'callCount': len(app.state.mcp_calls)}


@app.post('/mcp-narrowing-fixture')
async def mcp_narrowing_fixture(request: Request):
    """TaskHub が pin request と binding の narrowing wire を実 HTTP で照合できるようにする。"""
    service = app.state.execution_service
    server = ExternalMcpServerConfig(
        endpoint=str(request.base_url) + 'mcp-remote', server_name='fixture', server_version='1',
        trust_profile_ref='operator:fixture', allow_insecure_loopback=True,
        tools={'append': {
            'effect': 'write',
            'argument_constraints': {
                'type': 'object', 'required': ['value'],
                'properties': {'value': {'type': 'string', 'enum': ['one', 'two']}},
            },
        }},
    )
    service.config.mcp_servers['fixture'] = server
    service.admission.mcp_resolver.servers = service.config.mcp_servers
    policy = clients()['caller-a']
    if 'fixture' not in policy.mcp_servers:
        policy.mcp_servers.append('fixture')
    narrowing = {
        'type': 'object', 'required': ['value'],
        'properties': {'value': {'type': 'string', 'const': 'one'}},
        'additionalProperties': False,
    }
    binding = await service.admission.mcp_resolver.pin(
        policy,
        McpPinRequest(
            schema_version='1', server_id='fixture', tools={'append': 'fixture_append'},
            narrowing_constraints={'append': narrowing},
        ),
    )
    binding_ref = await service.store.put_resource('caller-a', binding)
    return {
        'bindingRef': binding_ref,
        'narrowingConstraints': binding.tools[0].narrowing_constraints,
        'narrowingDigest': binding.tools[0].narrowing_digest,
    }
