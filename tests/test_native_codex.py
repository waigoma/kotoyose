"""課金 provider を呼ばず、Codex jail・tool・停止の境界を検証する。"""

import asyncio
from contextlib import asynccontextmanager
import json
import shutil
from pathlib import Path
import os
import select
from unittest.mock import AsyncMock, Mock

import pytest

from kotoyose.config import AgentRoute, AliasAgent, Connection, HarnessConfig, ModelPreset
from kotoyose.execution_jobs import WorkerResult
from kotoyose.execution_resources import NativeRuntimeProfile, NativeExecutionConfig, resolve_agent_model, agent_execution_mode
from kotoyose.native_codex import NativeCodexWorker
from kotoyose.provider_jail import provider_environment
from tests.test_native_claude import _job
from tests.codex_runtime_fixture import codex_rootfs  # noqa: F401

_fixture_rootfs = None


@pytest.fixture
def provider(tmp_path, codex_rootfs):
    global _fixture_rootfs
    _fixture_rootfs = codex_rootfs
    # 真の process / pipe を使い、固定 protocol transcript を provider として再生する。
    launcher = tmp_path / "fake-provider"
    launcher.write_text('''#!/usr/bin/python3
import json,sys,time
import os,socket
def request_response():
 s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
 s.connect(os.environ['KOTOYOSE_NATIVE_CODEX_EGRESS_SOCKET'])
 body=json.dumps({'model':'gpt-6-luna','stream':True,'store':False,
  'tools':([{'type':'function','name':'sandbox_exec','parameters':{'type':'object','additionalProperties':False,'required':['argv','cwd'],'properties':{'argv':{'type':'array','minItems':1,'items':{'type':'string'}},'cwd':{'type':'string'}}}}] if mode=='quota-after-model' else [])}).encode()
 s.sendall(b'POST /v1/responses HTTP/1.1\\r\\nHost: 127.0.0.1:18080\\r\\nAuthorization: Bearer '+os.environ['OPENAI_API_KEY'].encode()+b'\\r\\nContent-Length: '+str(len(body)).encode()+b'\\r\\n\\r\\n'+body)
 while s.recv(4096):pass
 s.close()
def emit(value):
 if globals().get('mode') in ['resume-first','resume-next']:
  params=value.get('params',{})
  if params.get('threadId')=='thread':params['threadId']='00000000-0000-4000-8000-000000000001'
 if value.get('method')=='item/tool/call':value['params'].setdefault('callId',str(value['id']))
 if value.get('method')=='item/tool/call' and mode=='missing-callid':value['params'].pop('callId')
 if value.get('method')=='item/tool/call' and mode=='namespace':value['params']['namespace']='unadmitted'
 if value.get('method')=='item/tool/call' and mode=='unknown-tool':value['params']['tool']='view_image'
 print(json.dumps(value),flush=True)
def notify(method,params):
 emit({'method':method,'params':params})
if '--version' in sys.argv:
 print('codex-cli 0.170.0');sys.exit(0)
for line in sys.stdin:
 m=json.loads(line); method=m.get('method'); p=m.get('params',{})
 if method=='initialize':emit({'id':m['id'],'result':{'userAgent':'fake'}})
 elif method in ['thread/start','thread/resume']:
  mode=p['developerInstructions'].splitlines()[0]
  assert p['sandbox']=='read-only' and p['approvalPolicy']=='never'
  assert p['config']['features.shell_tool'] is False and p['modelProvider']=='kotoyose_native'
  if mode in ['resume-first','resume-next']:
   thread='00000000-0000-4000-8000-000000000001'
   assert (method=='thread/start') == (mode=='resume-first')
   assert (p.get('ephemeral') is False if mode=='resume-first' else p.get('threadId')==thread and p.get('excludeTurns') is True)
   thread_info={'id':thread,'path':'/home/kotoyose/.codex/sessions/'+thread+'.jsonl'}
  else:
   assert p['ephemeral'] and p['environments']==[]
   thread_info={'id':'thread'}
  emit({'id':m['id'],'result':{'thread':thread_info,'model':'other' if mode=='wrong-model' else p['model'],'modelProvider':'other' if mode=='wrong-provider' else 'kotoyose_native','reasoningEffort':p['config'].get('model_reasoning_effort')}})
 elif method=='turn/start':
  emit({'id':m['id'],'result':{'turn':{'id':'turn'}}})
  if mode in ['quota','quota-direct']:
   if mode=='quota-direct':request_response()
   notify('turn/completed',{'threadId':'thread','turn':{'id':'turn','status':'failed','error':{'codexErrorInfo':'usageLimitExceeded'}}})
  elif mode=='quota-after-model':
   request_response()
   notify('item/completed',{'threadId':'thread','turnId':'turn','item':{'type':'agentMessage','id':'a','text':'answer'}})
   emit({'id':'call','method':'item/tool/call','params':{'threadId':'thread','turnId':'turn','tool':'sandbox_exec','arguments':{'argv':['true'],'cwd':'.'}}})
  elif mode in ['resume-first','resume-next']:
   texts=['aaaaa','bbbbbb','cccccc'] if mode=='resume-first' else ['x'*21,'y'*21]
   for n,text in enumerate(texts):
    request_response()
    notify('item/completed',{'threadId':'thread','turnId':'turn','item':{'type':'agentMessage','id':str(n),'text':text}})
   if mode=='resume-first':
    request_response()
    notify('turn/completed',{'threadId':'thread','turn':{'id':'turn','status':'failed','error':{'codexErrorInfo':'usageLimitExceeded'}}})
   else:notify('turn/completed',{'threadId':'thread','turn':{'id':'turn','status':'completed'}})
  elif mode=='multi-complete':
   for item in ['a','b','c']:
    request_response()
    notify('item/completed',{'threadId':'thread','turnId':'turn','item':{'type':'agentMessage','id':item,'text':'answer'}})
    if item=='a':notify('item/completed',{'threadId':'thread','turnId':'turn','item':{'type':'agentMessage','id':item,'text':'answer'}})
   notify('turn/completed',{'threadId':'thread','turn':{'id':'turn','status':'completed'}})
  elif mode=='hang':time.sleep(60)
  elif mode=='missing':sys.exit(0)
  elif mode=='approval':emit({'id':'call','method':'item/commandExecution/requestApproval','params':{'threadId':'thread','turnId':'turn'}})
  elif mode in ['tool-method','tool-mutated']:
   p={'threadId':'thread','turnId':'turn','tool':'sandbox_exec','arguments':{'argv':['true'],'cwd':'.'}}
   emit({'id':'call','method':'item/tool/call','params':p})
   if mode=='tool-mutated':p['arguments']['argv']=['false']
   emit({'id':'call','method':'item/commandExecution/requestApproval' if mode=='tool-method' else 'item/tool/call','params':p})
  elif mode in ['tool-loop','tool-replay','semantic-replay','semantic-rpc-mutated']:
   for n in range(3):
    rpc='call' if mode=='tool-replay' else (str(min(n,1)) if mode=='semantic-rpc-mutated' else str(n))
    p={'threadId':'thread','turnId':'turn','tool':'sandbox_exec','arguments':{'argv':['true'],'cwd':'.'}}
    if mode.startswith('semantic'):p['callId']='same' if n<2 or mode=='semantic-replay' else 'changed'
    emit({'id':rpc,'method':'item/tool/call','params':p})
   notify('item/completed',{'threadId':'thread','turnId':'turn','item':{'type':'agentMessage','id':'a','text':'answer'}})
   notify('turn/completed',{'threadId':'thread','turn':{'id':'turn','status':'completed'}})
  elif mode in ['tool','foreign','bad-cwd','tool-unconfirmed','namespace','unknown-tool','missing-callid']:
   emit({'id':'call','method':'item/tool/call','params':{'threadId':'foreign' if mode=='foreign' else 'thread','turnId':'turn','tool':'sandbox_exec','arguments':{'argv':['sh','-c','test -z "${OPENAI_API_KEY+x}" && test -z "${GH_TOKEN+x}" && test ! -e /srv/sample-user && test ! -e /etc/codex && ! grep -q eth0 /proc/net/dev && printf ok > marker'],'cwd':'../outside' if mode=='bad-cwd' else '.'}}})
  else:
   count=4 if mode=='turns' else 1
   for n in range(count):notify('item/completed',{'threadId':'thread','turnId':'turn','item':{'type':'agentMessage','id':str(n),'phase':'commentary' if mode=='commentary' else 'final_answer','text':'x'*2000 if mode=='overflow' else ('not json' if mode=='invalid-json' else 'answer')}})
   notify('thread/tokenUsage/updated',{'threadId':'thread','turnId':'turn','tokenUsage':{'total':{'inputTokens':7,'outputTokens':5}}})
   notify('turn/completed',{'threadId':'thread','turn':{'id':'turn','status':'failed' if mode=='failed' else 'completed'}})
 elif method is None and m.get('id')=='call' and mode not in ['tool-loop','tool-replay','tool-method','tool-mutated']:
  assert ('error' in m) or m['result']['success'] == (mode in ['tool', 'quota-after-model'])
  if mode=='quota-after-model':
   request_response()
   notify('turn/completed',{'threadId':'thread','turn':{'id':'turn','status':'failed','error':{'codexErrorInfo':'usageLimitExceeded'}}})
  else:
   notify('item/completed',{'threadId':'thread','turnId':'turn','item':{'type':'agentMessage','id':'a','text':'answer'}})
   notify('turn/completed',{'threadId':'thread','turn':{'id':'turn','status':'completed'}})
''')
    launcher.chmod(0o700)
    worker = NativeCodexWorker(launcher)
    worker.fixture_rootfs = codex_rootfs
    return worker


def job(tmp_path, mode="answer", **values):
    if _fixture_rootfs is not None:
        values.setdefault("codex_rootfs", _fixture_rootfs[0])
        values.setdefault("codex_rootfs_digest", _fixture_rootfs[1])
    return _job(tmp_path, **{"model": "gpt-6-luna", "credential_name": "OPENAI_API_KEY", "instructions": mode,
                            "cli_binary": Path("/usr/bin/sh"), **values})


async def run(provider, tmp_path, mode="answer", **values):
    values.setdefault("codex_rootfs", provider.fixture_rootfs[0])
    values.setdefault("codex_rootfs_digest", provider.fixture_rootfs[1])
    callback = AsyncMock(return_value=WorkerResult("succeeded", 0, b"ok", b"", "confirmed", "done"))
    result = await provider.execute(job(tmp_path, mode, **values), asyncio.Event(), callback,
                                    on_process_started=AsyncMock())
    return result, callback


@asynccontextmanager
async def attempt_counter_fixture(provider, tmp_path, monkeypatch):
    """Two fake provider executions on one retained session, with distinct responses."""
    from kotoyose import codex_egress
    from kotoyose.native_sessions import CodexSessionDirectory
    provider.sessions = CodexSessionDirectory(tmp_path / 'retained-sessions')
    scope = 'a' * 64
    thread = '00000000-0000-4000-8000-000000000001'
    path = provider.sessions.directory(scope) / (thread + '.jsonl')
    path.write_text('fixture')
    ref = provider.sessions.reference(thread, '/home/kotoyose/.codex/sessions/' + path.name)
    statuses = [200, 200, 200, 429, 200, 200]
    calls = []
    async def respond(reader, writer):
        headers = await reader.readuntil(b'\r\n\r\n')
        length = next(int(line.split(b':', 1)[1]) for line in headers.split(b'\r\n')
                      if line.lower().startswith(b'content-length:'))
        await reader.readexactly(length)
        status = statuses[len(calls)]
        calls.append(status)
        record = {'type': 'response.completed', 'response': {'id': str(len(calls)), 'status': 'completed'}}
        body = b'data: ' + json.dumps(record).encode() + b'\n\n' if status == 200 else b''
        writer.write(f'HTTP/1.1 {status} Fixture\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n'.encode() + body)
        await writer.drain()
        writer.close()
        await writer.wait_closed()
    server = await asyncio.start_server(respond, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setattr(codex_egress, 'connect_endpoint', lambda: asyncio.open_connection('127.0.0.1', port))
    async def execute(mode, attempt_id):
        options = {'attempt_id': attempt_id, 'session_scope': scope, 'limits_mode': 'agent_work_v1', 'max_turns': 10}
        if mode == 'resume-next':
            options.update(resume_mode='native', provider_session_ref=ref)
        result, _ = await run(provider, tmp_path, mode, **options)
        return result
    try:
        yield execute, ref, calls
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize("instructions", ["", "  Preset instructions.\n\n作業範囲を守る。  \n"])
@pytest.mark.parametrize("granted", [False, True])
async def test_workspace_contract_preserves_instructions_and_provider_policy(tmp_path, instructions, granted):
    rpc = Mock(image_uris=(), turn_id=None)
    rpc.send = AsyncMock()
    rpc.request = AsyncMock(side_effect=[
        {},
        {"thread": {"id": "thread"}, "model": "gpt-6-luna", "modelProvider": "kotoyose_native"},
        {"turn": {"id": "turn"}},
    ])
    rpc.next = AsyncMock(side_effect=[
        {"method": "item/completed", "params": {"threadId": "thread", "turnId": "turn",
            "item": {"type": "agentMessage", "id": "answer", "text": "answer"}}},
        {"method": "turn/completed", "params": {"threadId": "thread", "turn": {"id": "turn", "status": "completed"}}},
    ])
    from kotoyose.native_codex import _CONFIG
    current_job = job(tmp_path, instructions, tool_refs=("builtin:sandbox-exec@1",) if granted else ())
    result = await NativeCodexWorker()._run(rpc, current_job, asyncio.Event(), AsyncMock(), {}, {}, dict(_CONFIG))
    assert result == b"answer"
    params = rpc.request.await_args_list[1].args[1]
    assert params["sandbox"] == "read-only" and params["approvalPolicy"] == "never"
    assert params["environments"] == [] and params["ephemeral"] is True
    assert params["cwd"] == "/tmp" and params["modelProvider"] == "kotoyose_native"
    assert params["config"] == _CONFIG
    for key in (
        "features.shell_tool", "features.code_mode_host", "features.goals", "features.sleep_tool",
        "features.view_image", "features.unified_exec", "agents.enabled", "features.apps",
        "tools.update_plan.enabled", "tools.experimental_request_user_input.enabled",
    ):
        assert params["config"][key] is False
    assert params["config"]["web_search"] == "disabled"
    if not granted:
        assert params["developerInstructions"] == instructions
        assert params["dynamicTools"] == []
        return
    prefix = instructions + "\n\n"
    assert params["developerInstructions"].startswith(prefix)
    contract = params["developerInstructions"][len(prefix):]
    assert contract.startswith("Kotoyose workspace tool contract:\n")
    assert "read-only sandbox is separate from the admitted Kotoyose workspace" in contract
    assert "use sandbox_exec to edit workspace files and run tests" in contract
    assert "user-authorized task" in contract and "actual tool errors" in contract
    assert "Do not conclude that all editing or testing is unavailable from that metadata alone" in contract
    assert "report actual tool failures" in contract
    assert "grants no additional access or permissions" in contract
    assert len(params["dynamicTools"]) == 1
    tool = params["dynamicTools"][0]
    assert tool["type"] == "function" and tool["name"] == "sandbox_exec"
    for text in (contract, tool["description"]):
        assert "workspace-relative" in text and "'.'" in text
        assert str(tmp_path) not in text and "/tmp" not in text
    assert "user-authorized task" in tool["description"]
    assert "admission restrictions and actual tool errors" in tool["description"]
    assert tool["inputSchema"]["required"] == ["argv", "cwd"]


@pytest.mark.parametrize("effort", [None, "low", "medium", "high", "xhigh", "max"])
async def test_transcript_completes_with_reported_tokens_and_observed_stop(provider, tmp_path, effort):
    result, callback = await run(provider, tmp_path, reasoning_effort=effort)
    assert (result.status, result.content, result.stop_confirmation) == ("succeeded", b"answer", "confirmed")
    assert result.usage == {"source": "reported", "cost": None, "input_tokens": 7, "output_tokens": 5}
    callback.assert_not_awaited()


async def test_workspace_tool_uses_the_admitted_callback(provider, tmp_path):
    result, callback = await run(provider, tmp_path, "tool", tool_refs=("builtin:sandbox-exec@1",))
    assert result.status == "succeeded"
    callback.assert_awaited_once_with(("sh", "-c", 'test -z "${OPENAI_API_KEY+x}" && test -z "${GH_TOKEN+x}" && test ! -e /srv/sample-user && test ! -e /etc/codex && ! grep -q eth0 /proc/net/dev && printf ok > marker'), ".")


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="Real provider jail requires bwrap")
async def test_real_codex_launcher_protocol_roundtrip(provider, tmp_path):
    callback = AsyncMock(return_value=WorkerResult("succeeded", 0, b"ok", b"", "confirmed", "done"))
    result = await NativeCodexWorker().execute(
        job(tmp_path, "tool", cli_binary=provider.launcher_path, tool_refs=("builtin:sandbox-exec@1",)),
        asyncio.Event(), callback, on_process_started=AsyncMock(),
    )
    assert (result.status, result.stop_confirmation) == ("succeeded", "confirmed")
    callback.assert_awaited_once()


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="Real provider jail requires bwrap")
@pytest.mark.parametrize("inventory", ["all", "namespace_init_only"])
async def test_real_jail_cancel_removes_provider_descendants(provider, tmp_path, monkeypatch, inventory):
    from kotoyose.process_identity import _pidfd_backend
    open_pidfd, _ = _pidfd_backend()
    assert callable(open_pidfd)
    text = provider.launcher_path.read_text().replace("import json,sys,time", "import json,sys,time,os")
    text = text.replace("if mode=='hang':time.sleep(60)", """if mode=='hang':
   if os.fork()==0:time.sleep(60);sys.exit(0)
   emit({'id':'call','method':'item/tool/call','params':{'threadId':'thread','turnId':'turn','tool':'sandbox_exec','arguments':{'argv':['true'],'cwd':'.'}}})
   time.sleep(60)""")
    provider.launcher_path.write_text(text)
    compile(text, str(provider.launcher_path), "exec")
    cancel = asyncio.Event()
    descriptors = []
    outer = None

    async def started(pid):
        nonlocal outer
        outer = pid

    def descendants(pid):
        descriptors.append(open_pidfd(pid, 0))
        for child in Path(f"/proc/{pid}/task/{pid}/children").read_text().split():
            descendants(int(child))

    async def tool(argv, cwd):
        descendants(outer)
        if inventory == "namespace_init_only":
            from kotoyose import codex_process_stop
            original = codex_process_stop._children

            def init_only(identity):
                # 列挙後 fork/reparent と同じく、未採取の子も private init の退出で止まる。
                children = original(identity)
                init = codex_process_stop._private_init(children)
                assert init is not None
                return [init]

            monkeypatch.setattr(codex_process_stop, "_children", init_only)
        cancel.set()
        return WorkerResult("cancelled", None, b"", b"", "confirmed", "cancelled")

    try:
        result = await NativeCodexWorker().execute(job(tmp_path, "hang", cli_binary=provider.launcher_path,
            tool_refs=("builtin:sandbox-exec@1",)), cancel, tool, on_process_started=started)
        assert (result.status, result.stop_confirmation) == ("cancelled", "confirmed"), result
        assert len(descriptors) >= 4
        assert set(select.select(descriptors, [], [], 1)[0]) == set(descriptors)
    finally:
        for descriptor in descriptors:
            os.close(descriptor)


async def test_failed_output_is_retained_as_evidence(provider, tmp_path):
    result, _ = await run(provider, tmp_path, "failed")
    assert result.status == "failed" and result.content == b"answer"


@pytest.mark.parametrize("mode", ["answer", "failed"])
async def test_provider_secret_is_redacted_from_success_and_failure_evidence(provider, tmp_path, mode):
    text = provider.launcher_path.read_text().replace("import json,sys,time", "import json,sys,time,os")
    text = text.replace("else 'answer'", "else os.environ['OPENAI_API_KEY']")
    text = text.replace("for line in sys.stdin:", "print(os.environ['OPENAI_API_KEY'],file=sys.stderr,flush=True)\nfor line in sys.stdin:")
    provider.launcher_path.write_text(text)
    activity, progress = Mock(), Mock()
    result, _ = await run(provider, tmp_path, mode, activity_event=activity, progress_event=progress)
    assert result.content == b"[redacted]"
    assert "dummy-secret" not in str(result) and "dummy-secret" not in str(activity.mock_calls)
    assert "dummy-secret" not in str(progress.mock_calls)


async def test_commentary_only_is_evidence_and_cannot_succeed(provider, tmp_path):
    result, _ = await run(provider, tmp_path, "commentary")
    assert (result.status, result.reason, result.content) == ("failed", "missing_completion", b"answer")


async def test_turn_budget_stops_repeated_tools_before_new_dispatch(provider, tmp_path):
    result, callback = await run(provider, tmp_path, "tool-loop", max_turns=1, tool_refs=("builtin:sandbox-exec@1",))
    assert (result.status, result.reason) == ("failed", "turn_limit_exceeded")
    assert callback.await_count == 1


async def test_duplicate_tool_rpc_does_not_repeat_an_effect(provider, tmp_path):
    result, callback = await run(provider, tmp_path, "tool-replay", tool_refs=("builtin:sandbox-exec@1",))
    assert result.status == "succeeded"
    assert callback.await_count == 1


async def test_semantic_call_replay_with_another_rpc_id_does_not_repeat_effect(provider, tmp_path):
    result, callback = await run(provider, tmp_path, "semantic-replay", tool_refs=("builtin:sandbox-exec@1",))
    assert result.status == "succeeded"
    assert callback.await_count == 1


async def test_cached_semantic_retry_also_pins_its_rpc_id(provider, tmp_path):
    result, callback = await run(provider, tmp_path, "semantic-rpc-mutated", tool_refs=("builtin:sandbox-exec@1",))
    assert (result.status, result.reason) == ("failed", "invalid_protocol")
    assert callback.await_count == 1


@pytest.mark.parametrize("mode", ["wrong-model", "wrong-provider"])
async def test_resolved_model_and_provider_must_match_before_turn(provider, tmp_path, mode):
    result, callback = await run(provider, tmp_path, mode)
    assert (result.status, result.reason) == ("failed", "provider_error")
    callback.assert_not_awaited()


@pytest.mark.parametrize("mode", ["tool-method", "tool-mutated"])
async def test_changed_rpc_method_or_arguments_cannot_reuse_cached_success(provider, tmp_path, mode):
    result, callback = await run(provider, tmp_path, mode, tool_refs=("builtin:sandbox-exec@1",))
    assert (result.status, result.reason) == ("failed", "invalid_protocol")
    assert callback.await_count == 1


@pytest.mark.parametrize("mode", ["foreign", "bad-cwd", "tool", "namespace", "unknown-tool", "missing-callid"])
async def test_unscoped_tool_is_denied_before_any_operation(provider, tmp_path, mode):
    result, callback = await run(provider, tmp_path, mode,
                                 tool_refs=("builtin:sandbox-exec@1",) if mode != "tool" else ())
    assert result.stop_confirmation == "confirmed"
    callback.assert_not_awaited()
    assert result.status == ("succeeded" if mode == "foreign" else "failed")


@pytest.mark.parametrize("mode,reason", [
    ("missing", "missing_completion"), ("failed", "provider_error"), ("overflow", "output_limit_exceeded"),
    ("turns", "turn_limit_exceeded"), ("invalid-json", "invalid_json_output"), ("approval", "tool_denied"),
])
async def test_incomplete_over_budget_and_builtin_requests_cannot_succeed(provider, tmp_path, mode, reason):
    result, callback = await run(provider, tmp_path, mode, output_contract="json" if mode == "invalid-json" else "text")
    assert (result.status, result.reason, result.stop_confirmation) == ("failed", reason, "confirmed")
    callback.assert_not_awaited()


async def test_cancel_and_deadline_stop_the_provider(provider, tmp_path):
    cancel = asyncio.Event()
    started = asyncio.Event()

    async def observe(pid):
        started.set()

    stopped = []
    task = asyncio.create_task(provider.execute(job(tmp_path, "hang", on_runtime_stopped=lambda: stopped.append(True)), cancel, AsyncMock(), on_process_started=observe))
    await started.wait()
    cancel.set()
    result = await task
    assert (result.status, result.stop_confirmation) == ("cancelled", "confirmed")
    assert stopped == [True]
    result, _ = await run(provider, tmp_path, "hang", timeout_seconds=0.05)
    assert (result.status, result.stop_confirmation) == ("timeout", "confirmed")


async def test_unconfirmed_workspace_stop_never_becomes_success(provider, tmp_path):
    callback = AsyncMock(return_value=WorkerResult("succeeded", 0, b"", b"", "unconfirmed", "unknown"))
    cancel = asyncio.Event()
    result = await provider.execute(job(tmp_path, "tool-unconfirmed", tool_refs=("builtin:sandbox-exec@1",)),
                                    cancel, callback, on_process_started=AsyncMock())
    assert result.status != "succeeded" and cancel.is_set()


@pytest.mark.parametrize("values", [{"resume_mode": "native"}, {"provider_session_ref": "old"},
                                    {"base_url": "https://host.invalid"}, {"tool_refs": ("external",)}])
async def test_unsupported_job_stops_before_process_start(provider, tmp_path, values):
    started = AsyncMock()
    result = await provider.execute(job(tmp_path, **values), asyncio.Event(), AsyncMock(), on_process_started=started)
    assert (result.status, result.reason) == ("failed", "invalid_job")
    started.assert_not_awaited()


def test_no_native_session_claim_for_disposable_codex_state(provider):
    assert provider.resume_support().modes == ("durable_state",)
    assert provider.resume_session_ref("observed-id") is None


def test_codex_environment_is_exact_and_cannot_inherit_host_state():
    environment = provider_environment({"OPENAI_API_KEY": "fake", "HOME": "/host", "CODEX_HOME": "/host-config",
                                        "OPENAI_BASE_URL": "https://host.invalid", "LD_PRELOAD": "/host.so",
                                        "CLAUDE_CODE_OAUTH_TOKEN": "other-provider", "GH_TOKEN": "host"}, provider="codex")
    assert set(environment) == {"OPENAI_API_KEY", "PATH", "HOME", "LANG", "CODEX_HOME", "OPENAI_BASE_URL"}
    assert environment["HOME"] == "/home/kotoyose" and environment["CODEX_HOME"] == "/home/kotoyose/.codex"


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="Real provider jail requires bwrap")
async def test_real_provider_jail_hides_host_config_and_workspace(provider, tmp_path):
    # sentinel を含む host filesystem を追加 bind せず、artifact と tmpfs だけを使う。
    text = provider.launcher_path.read_text().replace("import json,sys,time", """import json,sys,time,os,socket
assert not os.path.exists('/srv/sample-user') and not os.path.exists('/etc/ssl/private')
assert not os.path.exists('/usr/local') and not os.path.exists('/workspace')
assert not os.environ.get('GH_TOKEN') and not os.environ.get('ANTHROPIC_API_KEY')
assert os.path.isdir('/home/kotoyose/.codex')
s=socket.socket();s.settimeout(.2)
try:s.connect(('169.254.169.254',80));raise AssertionError('network escaped')
except OSError:pass
finally:s.close()
""")
    provider.launcher_path.write_text(text)
    result = await NativeCodexWorker().execute(job(tmp_path, cli_binary=provider.launcher_path),
        asyncio.Event(), AsyncMock(), on_process_started=AsyncMock())
    assert (result.status, result.stop_confirmation) == ("succeeded", "confirmed")


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="Real provider jail requires bwrap")
async def test_real_network_namespace_has_no_host_loopback_lan_metadata_or_public_route(provider, tmp_path):
    seen = []

    async def host_fixture(reader, writer):
        seen.append(True)
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(host_fixture, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    checks = f"""import json,sys,time,socket
assert not any(line.split()[0] != 'Iface' for line in open('/proc/net/route'))
for destination in [('127.0.0.1',{port}),('10.0.0.1',{port}),('192.168.0.1',{port}),('169.254.169.254',80),('8.8.8.8',443)]:
 s=socket.socket();s.settimeout(.2)
 try:s.connect(destination);raise AssertionError('network escaped')
 except OSError:pass
 finally:s.close()
"""
    provider.launcher_path.write_text(provider.launcher_path.read_text().replace("import json,sys,time", checks))
    try:
        result = await NativeCodexWorker().execute(job(tmp_path, cli_binary=provider.launcher_path),
            asyncio.Event(), AsyncMock(), on_process_started=AsyncMock())
    finally:
        server.close()
        await server.wait_closed()
    assert (result.status, result.stop_confirmation) == ("succeeded", "confirmed")
    assert seen == []


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="Real provider jail requires bwrap")
async def test_changed_cli_version_is_rejected_before_protocol_or_model(provider, tmp_path):
    provider.launcher_path.write_text(provider.launcher_path.read_text().replace("codex-cli 0.170.0", "codex-cli 0.160.0"))
    callback = AsyncMock()
    result = await NativeCodexWorker().execute(job(tmp_path, cli_binary=provider.launcher_path),
        asyncio.Event(), callback, on_process_started=AsyncMock())
    assert result.status == "failed"
    # CLI が先に終了すると namespace init を観測できず、停止は安全側で未確認になる。
    assert result.stop_confirmation in {"confirmed", "unconfirmed"}
    callback.assert_not_awaited()


async def test_wrong_rootfs_digest_is_rejected_before_launch(provider, tmp_path):
    callback = AsyncMock()
    started = AsyncMock()
    result = await provider.execute(job(tmp_path, codex_rootfs_digest="0" * 64),
        asyncio.Event(), callback, on_process_started=started)
    assert (result.status, result.stop_confirmation) == ("failed", "confirmed")
    callback.assert_not_awaited()
    started.assert_not_awaited()


def test_codex_launcher_without_bwrap_refuses_host_fallback(monkeypatch):
    from kotoyose import codex_provider_jail
    monkeypatch.setenv("KOTOYOSE_NATIVE_CODEX_BINARY", "/usr/bin/sh")
    monkeypatch.setenv("OPENAI_API_KEY", "fake")
    monkeypatch.setattr(codex_provider_jail.shutil, "which", lambda name: None)
    execve = Mock(side_effect=RuntimeError("host_exec_called"))
    monkeypatch.setattr(codex_provider_jail.os, "execve", execve)
    with pytest.raises(RuntimeError, match="bubblewrap"):
        codex_provider_jail.main()
    execve.assert_not_called()


def test_agent_binding_selects_sandbox_and_chat_alias_does_not(tmp_path):
    from kotoyose.config import Client
    config = HarnessConfig(
        connections={"reviewed-conn": Connection(provider="codex", kind="api_key", data_policy="no_training", secret="env:FIXTURE_CODEX_TOKEN")},
        routes={"reviewed": AgentRoute(
            driver="codex.app-server", model="gpt-6-luna", connection="reviewed-conn",
        )},
        models={
            "normal": ModelPreset(provider="codex", model="gpt-6-luna"),
            "automatic": ModelPreset(
                route="reviewed",
                agent=AliasAgent(place="isolated", surface="exec"), codex={"sandbox": "read-only"},
            ),
        },
        clients={"fixture": Client(
            workspace_roots=[tmp_path],
            models=["automatic"], allowed_env_vars=[], allowed_provenance=["project"],
            max_run_seconds=10, max_memory_bytes=64*1024*1024, max_processes=10,
            max_cpus=1, max_concurrent_runs=1,
        )},
    )
    settings = NativeExecutionConfig(schema_version="1", image_id="sha256:" + "a" * 64,
        drivers={"codex.app-server": {"cli_binary": "/usr/bin/sh"}})
    resolved = resolve_agent_model("automatic", settings, config=config)
    assert resolved.provider == "codex" and resolved.model == "gpt-6-luna"
    assert resolved.codex_sandbox is None
    assert resolved.codex_preferences.requested_effort is None
    assert resolved.codex_preferences.effort == "low"
    assert agent_execution_mode("automatic", settings, config=config) == "sandbox"
    assert agent_execution_mode("normal", settings, config=config) is None


@pytest.mark.parametrize("values", [{"credential_mode": "runtime_managed", "credential_env": None},
                                    {"credential_name": "ANTHROPIC_API_KEY"}, {"base_url": "https://host.invalid"}])
def test_codex_profile_rejects_implicit_auth_and_endpoint(values):
    with pytest.raises(ValueError):
        NativeRuntimeProfile(**{"provider": "codex", "model": "gpt-6-luna", "cli_binary": "/usr/bin/sh",
                               "credential_mode": "environment", "credential_env": "FIXTURE_CODEX_TOKEN",
                               "credential_name": "OPENAI_API_KEY", **values})

@pytest.mark.asyncio
async def test_codex_quota_error_is_typed_and_turns_are_attempt_local(provider, tmp_path, monkeypatch):
    result, _ = await run(provider, tmp_path, 'quota', limits_mode='agent_work_v1')
    assert result.reason == 'provider_usage_limit' and result.turns is None and result.output_bytes == 0
    assert result.provider_limit.limit_window == 'unknown'
    legacy, _ = await run(provider, tmp_path, 'quota')
    assert legacy.reason == 'provider_error' and legacy.provider_limit is None
    success, _ = await run(provider, tmp_path, limits_mode='agent_work_v1')
    assert success.turns is None and success.output_bytes == len(b'answer')
    rpc = Mock(image_uris=(), turn_id=None, session_ref=None)
    rpc.send = AsyncMock()
    rpc.request = AsyncMock(side_effect=[{}, {'thread': {'id': 'thread'}, 'model': 'gpt-6-luna', 'modelProvider': 'kotoyose_native'}, {'turn': {'id': 'turn'}}])
    rpc.next = AsyncMock(side_effect=[
        {'method': 'thread/tokenUsage/updated', 'params': {'threadId': 'thread', 'turnId': 'turn', 'tokenUsage': {'total': {'inputTokens': 10000}}}},
        {'method': 'item/completed', 'params': {'threadId': 'thread', 'turnId': 'turn', 'item': {'type': 'agentMessage', 'id': 'new', 'text': 'ok'}}},
        {'method': 'turn/completed', 'params': {'threadId': 'thread', 'turn': {'id': 'turn', 'status': 'completed'}}},
    ])
    worker = NativeCodexWorker()
    worker.can_resume_session = AsyncMock(return_value=True)
    await worker._run(rpc, job(tmp_path, limits_mode='agent_work_v1', resume_mode='native',
        provider_session_ref='scope:thread:rollout', session_scope='scope'), asyncio.Event(), AsyncMock(), {}, {}, {})
    assert rpc.output_bytes == 2
    async with attempt_counter_fixture(provider, tmp_path, monkeypatch) as (execute, ref, calls):
        first = await execute('resume-first', 'first-attempt')
        assert (first.reason, first.output_bytes, first.turns, first.provider_session_id) == ('provider_usage_limit', 17, 3, ref)
        second = await execute('resume-next', 'second-attempt')
        assert (second.status, second.output_bytes, second.turns, second.provider_session_id) == ('succeeded', 42, 2, ref)
        assert calls == [200, 200, 200, 429, 200, 200]
    from kotoyose import codex_egress
    from contextlib import asynccontextmanager
    @asynccontextmanager
    async def upstream(statuses):
        calls = []
        async def respond(reader, writer):
            headers = await reader.readuntil(b'\r\n\r\n')
            length = next(int(line.split(b':', 1)[1]) for line in headers.split(b'\r\n') if line.lower().startswith(b'content-length:'))
            await reader.readexactly(length)
            status = statuses[len(calls)]
            calls.append(status)
            record = {'type': 'response.completed', 'response': {'id': str(len(calls)), 'status': 'completed'}}
            body = (b'data: ' + json.dumps(record).encode() + b'\n\n') * 2 if status == 200 else b''
            writer.write(f'HTTP/1.1 {status} Fixture\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n'.encode() + body)
            await writer.drain()
            writer.close()
            await writer.wait_closed()
        server = await asyncio.start_server(respond, '127.0.0.1', 0)
        port = server.sockets[0].getsockname()[1]
        monkeypatch.setattr(codex_egress, 'connect_endpoint', lambda: asyncio.open_connection('127.0.0.1', port))
        try:
            yield calls
        finally:
            server.close()
            await server.wait_closed()
    async with upstream([200, 429]) as calls:
        after_tool, callback = await run(provider, tmp_path, 'quota-after-model', limits_mode='agent_work_v1',
                                         tool_refs=('builtin:sandbox-exec@1',), max_turns=10)
        assert after_tool.reason == 'provider_usage_limit' and after_tool.turns == 1
        assert callback.await_count == 1 and calls == [200, 429]
    async with upstream([429]) as calls:
        zero, _ = await run(provider, tmp_path, 'quota-direct', limits_mode='agent_work_v1')
        assert zero.reason == 'provider_usage_limit' and zero.turns == 0 and calls == [429]
    async with upstream([200, 200, 200]) as calls:
        multi, _ = await run(provider, tmp_path, 'multi-complete', limits_mode='agent_work_v1', max_turns=10)
        assert multi.status == 'succeeded' and multi.turns == 3 and calls == [200, 200, 200]
    from kotoyose.codex_response_counter import ResponseCounter
    incomplete = ResponseCounter()
    pending = incomplete.begin()
    pending.feed_http(b'HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n')
    pending.finish()
    assert incomplete.value() is None
    server_error = ResponseCounter()
    rejected = server_error.begin()
    rejected.feed_http(b'HTTP/1.1 500 Internal Server Error\r\nContent-Length: 0\r\n\r\n')
    rejected.finish()
    assert server_error.value() is None
