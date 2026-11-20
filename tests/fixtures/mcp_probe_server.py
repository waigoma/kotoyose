"""Credential-free, stdlib-only MCP subprocess used by admin probe integration tests."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


mode = os.environ.get('MCP_PROBE_FIXTURE_MODE', 'normal')
record_path = os.environ.get('MCP_PROBE_FIXTURE_RECORD')
pid_path = os.environ.get('MCP_PROBE_FIXTURE_PIDS')
secret = os.environ.get('MCP_PROBE_FIXTURE_SECRET', '')

if pid_path:
    # Both processes ignore TERM. Only whole-group KILL can stop this descendant.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    child = subprocess.Popen([
        sys.executable, '-c',
        'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(300)',
    ])
    Path(pid_path).write_text(json.dumps({'server': os.getpid(), 'child': child.pid, 'group': os.getpgrp()}))

if mode == 'stall':
    time.sleep(300)

for line in sys.stdin:
    body = json.loads(line)
    method = body.get('method')
    if record_path:
        with open(record_path, 'a') as records:
            records.write(json.dumps(body) + '\n')
    if method.startswith('notifications/'):
        continue
    if mode == 'invalid':
        print(f'invalid protocol {secret}', flush=True)
        continue
    if mode == 'protocol_error':
        print(json.dumps({'jsonrpc': '2.0', 'id': body['id'],
                          'error': {'code': -32603, 'message': f'private failure: {secret}'}}), flush=True)
        continue
    if method == 'initialize':
        result = {'protocolVersion': body['params']['protocolVersion'],
                  'capabilities': {} if mode == 'unsupported' else {'tools': {}},
                  'serverInfo': {'name': 'probe-fixture', 'version': '1'},
                  'instructions': secret}
    elif method == 'tools/list':
        name = f'inspect_{secret}' if secret else 'inspect_fixture'
        result = {'tools': [] if mode == 'empty' else [
            {'name': name, 'description': f'private description {secret}',
             'inputSchema': {'type': 'object', 'description': secret}},
        ]}
        # stderr must never become parent logs, including on successful probes.
        print(f'private server stderr {secret}', file=sys.stderr, flush=True)
    else:
        raise AssertionError(f'Probe must never invoke a tool: {method}')
    print(json.dumps({'jsonrpc': '2.0', 'id': body['id'], 'result': result}), flush=True)

if pid_path:
    # Closing stdin is not enough: exercise the parent's unconditional group cleanup.
    time.sleep(300)
