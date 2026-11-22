"""Claude coding (host_native_coding_v1) を per-Run container の中で実行する。

Claude Code CLI は container の中で動き、built-in tool (Read / Edit / Write / Bash など) で
/workspace を直接扱う。SDK は launcher (agent_container_launcher) を host で起動し、launcher が
docker exec で container 内の CLI へ接続する。CLI の HTTP は container 内の relay を通って
broker の UNIX socket へ届き、実 credential は host の broker だけが持つ。

停止の証明は次の二つだけに置く。
- container が存在しないこと (AgentContainer.remove が confirmed を返すこと)
- egress broker が閉じたこと
さらに SDK の launcher process が終了していること。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import secrets
import shlex
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import structlog
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    __version__ as _SDK_VERSION,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from .agent_spec import admitted_execution_cell
from .execution_delegation import TOOL_SPECS, only_delegation_tools
from .agent_container import container_name
from .agent_deadlines import AgentStartRejected
from .codex_coding_policy import validate_mode, validate_workspace
from .jail_place import (
    CONTAINER_HOME,
    CONTAINER_RELAY,
    CONTAINER_SOCKET,
    CONTAINER_WORKSPACE,
    PROXY_URL,
    RELAY_PATH,
    WALL_MARGIN_SECONDS,
    JailPlace,
    build_connect_proxy_env,
    container_identity,
    container_job_id_for,
    create_egress_socket_dir,
    remove_container_confirmed,
)
from .runtime.claude_options import claude_disallowed_tools
from .native_claude import (
    _CREDENTIAL_KEYS,
    _SDK_PROTOCOL_OVERHEAD,
    NativeAgentJob,
    NativeAgentResult,
    _Invocation,
    _OutputLimit,
    _ProcessIdentityError,
    _capture_process,
    _emit_progress,
    _emit_sdk_tool_result,
    _expired,
    _extract_json_object,
    _extract_usage,
    _query_input,
    _redact_text,
    _result,
    _tool_use_detail,
    _unknown_usage,
)
from .native_compression import NativeCompressionObserver
from .print_cli_egress import PrintCliEgress
from .provider_egress import RELAY_HOST, EgressRoute, EgressSpec, EgressUpstream, ProviderEgress
from .provider_usage_limits import normalize_provider_usage_limit
from .runtime.base import RuntimeEvent
from .runtime.claude import _summarize, _truncate
from .runtime.claude_observations import OBSERVATION_MESSAGES, claude_observation

log = structlog.get_logger()

_CONTAINER_HOME = CONTAINER_HOME
_CONTAINER_CONFIG_DIR = "/home/agent/.claude"
_CONTAINER_WORKSPACE = CONTAINER_WORKSPACE
_CONTAINER_CLI = "/opt/agent/bin/claude"
_CONTAINER_RELAY = CONTAINER_RELAY
_CONTAINER_SOCKET = CONTAINER_SOCKET
_RELAY_PATH = RELAY_PATH
_LAUNCHER_PATH = Path(__file__).with_name("agent_container_launcher.py")
_API_HOST = "api.anthropic.com"
_PROXY_URL = PROXY_URL
_CLAUDE_CONNECT_HOSTS: tuple[str, ...] = (
    "api.anthropic.com",
    "claude.ai",
    "console.anthropic.com",
    "platform.claude.com",
)
_CLAUDE_CONNECT_PORTS: tuple[int, ...] = (443,)
_ROUTES = (EgressRoute("POST", "/v1/messages"), EgressRoute("POST", "/v1/messages/count_tokens"))
# CLI が送る header のうち provider の要件に必要なものだけを通す。framing と認証は broker が決める。
_PASS_HEADERS = frozenset({"accept", "anthropic-beta", "anthropic-version", "content-type", "user-agent"})
_MAX_REQUEST_BYTES = 32 * 1024 * 1024
_MAX_RESPONSE_BYTES = 64 * 1024 * 1024
_IDLE_TIMEOUT_SECONDS = 300.0
# container 作成から終了までの余白。CLI の停止と container の回収に使う。
_WALL_MARGIN_SECONDS = WALL_MARGIN_SECONDS
# built-in の Web 系 tool は coding では使わない。それ以外の built-in は CLI の既定どおり有効にする。
_DISALLOWED_TOOLS = tuple(claude_disallowed_tools(("WebFetch", "WebSearch")))
# container の CLI が見る環境変数。HOME は AgentContainer が固定し、launcher が同じ値を渡す。
_BASE_CONTAINER_ENV = {
    "CLAUDE_CONFIG_DIR": _CONTAINER_CONFIG_DIR,
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "DISABLE_AUTOUPDATER": "1",
    "DISABLE_TELEMETRY": "1",
}
_CONTAINER_ENV = {
    **_BASE_CONTAINER_ENV,
    "ANTHROPIC_BASE_URL": f"http://{RELAY_HOST}",
}
_socket_dir = create_egress_socket_dir
_remove_container = remove_container_confirmed


def _container_job_id(job: NativeAgentJob) -> str:
    return container_job_id_for(job.attempt_id, job.generation)


def _uses_connect_egress(job: NativeAgentJob) -> bool:
    """Return True when a subscription Claude job uses direct CONNECT pass-through."""
    return job.base_url is None and job.credential_name == "CLAUDE_CODE_OAUTH_TOKEN"


def _container_env(*, connect_egress: bool = False) -> dict[str, str]:
    """Return non-secret container env vars for either CONNECT proxy or HTTP-terminating relay."""
    if connect_egress:
        return build_connect_proxy_env(_BASE_CONTAINER_ENV)
    return dict(_CONTAINER_ENV)


def _upstream(base_url: str | None) -> EgressUpstream:
    """base_url があれば固定 gateway へ、無ければ Claude API の公開 endpoint (api.anthropic.com:443) へ向ける。"""
    if base_url is None:
        return EgressUpstream(host=_API_HOST, port=443, tls=True)
    parts = urlsplit(base_url)
    host = parts.hostname
    if (parts.scheme not in {"http", "https"} or not host or ":" in host or parts.username or parts.password
            or parts.query or parts.fragment or any(c.isspace() or c == "\x00" for c in base_url)):
        raise ValueError("invalid_job")
    tls = parts.scheme == "https"
    try:
        port = parts.port or (443 if tls else 80)
    except ValueError as exc:
        raise ValueError("invalid_job") from exc
    # base_url は管理者が設定した審査済みの gateway (9router や自前 LLM の入口)。名前が LAN の private
    # address に解決されるのが普通なので許す。名前は 1 度だけ引いて固定される (provider_egress)。
    # 既定の api.anthropic.com は公開 address だけ。
    return EgressUpstream(host=host, port=port, tls=tls, base_path=parts.path.rstrip("/"), allow_private=True)


# 実 credential の種類ごとに、CLI が自分の認証として扱う環境変数と、CLI が capability を載せる header を決める。
# OAuth (subscription) の token を持つ CLI は OAuth client として動き、authorization: Bearer と OAuth 用の
# anthropic-beta を自分で送る。API key の CLI は x-api-key を送る。broker は同じ header で capability を検証し、
# 上流へは実 credential を同じ形式で注入する。
_AUTH_KINDS = {
    "CLAUDE_CODE_OAUTH_TOKEN": ("CLAUDE_CODE_OAUTH_TOKEN", "authorization"),
    "ANTHROPIC_AUTH_TOKEN": ("ANTHROPIC_AUTH_TOKEN", "authorization"),
    "ANTHROPIC_API_KEY": ("ANTHROPIC_API_KEY", "x-api-key"),
}


def _cli_credential(credential_name: str) -> tuple[str, str]:
    """(CLI が読む環境変数名, capability を載せる header 名)。"""
    return _AUTH_KINDS[credential_name]


def _injected_headers(credential_name: str, credential_value: str) -> dict[str, str]:
    """実 credential を上流の認証 header へ変換する。CLI の認証方式と同じ対応を取る。"""
    if _cli_credential(credential_name)[1] == "x-api-key":
        return {"x-api-key": credential_value}
    return {"authorization": f"Bearer {credential_value}"}


def _validate_job(job: NativeAgentJob, settings) -> tuple[EgressUpstream, Path]:
    """launch 前に入力を検証し、上流の指定と CLI の実体を返す。副作用は持たない。"""
    if settings is None or validate_mode(job.codex_execution_mode) is not True:
        raise ValueError("invalid_job")
    cell = admitted_execution_cell("claude", job.codex_execution_mode)
    if (cell is None or set(job.tool_refs) - TOOL_SPECS.keys() != set(cell.tool_refs)
            or not only_delegation_tools(job.client_tool_specs, job.tool_refs, job.client_tool_call)
            or job.approval_bindings):
        raise ValueError("invalid_job")
    if job.workspace is None:
        raise ValueError("invalid_job")
    validate_workspace(job.workspace, device=job.workspace_device, inode=job.workspace_inode)
    # 再開は host の一時 config dir へ materialize される。container からは見えないため fail closed する。
    if job.resume_mode == "native":
        raise ValueError("invalid_job")
    if job.credential_name not in _CREDENTIAL_KEYS or not job.credential_value or "\x00" in job.credential_value:
        raise ValueError("invalid_job")
    if not job.model or "\x00" in job.model or "\x00" in job.instructions or "\x00" in job.prompt:
        raise ValueError("invalid_job")
    if not isinstance(job.cli_binary, Path) or not job.cli_binary.is_absolute():
        raise ValueError("invalid_job")
    cli = job.cli_binary.resolve(strict=True)
    if job.output_contract not in {"text", "json"}:
        raise ValueError("invalid_job")
    if not isinstance(job.max_turns, int) or isinstance(job.max_turns, bool) or job.max_turns < 1:
        raise ValueError("invalid_job")
    if not isinstance(job.output_limit_bytes, int) or isinstance(job.output_limit_bytes, bool) or job.output_limit_bytes < 1:
        raise ValueError("invalid_job")
    for value in (job.timeout_seconds, job.grace_seconds):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError("invalid_job")
    if job.timeout_seconds <= 0:
        raise ValueError("invalid_job")
    return _upstream(job.base_url), cli


def _egress_spec(job: NativeAgentJob, upstream: EgressUpstream, capability: str) -> EgressSpec:
    return EgressSpec(
        upstream=upstream,
        routes=_ROUTES,
        inject_headers=_injected_headers(job.credential_name, job.credential_value),
        capability=capability,
        capability_header=_cli_credential(job.credential_name)[1],
        pass_headers=_PASS_HEADERS,
        max_request_bytes=_MAX_REQUEST_BYTES,
        max_response_bytes=_MAX_RESPONSE_BYTES,
        idle_timeout_seconds=_IDLE_TIMEOUT_SECONDS,
        request_deadline_seconds=max(1.0, float(job.timeout_seconds)),
    )


def _write_launcher(directory: Path) -> Path:
    """SDK の cli_path は単一の実行ファイルを要求するため、launcher を呼ぶ小さな sh を置く。"""
    wrapper = directory / "claude-launch"
    wrapper.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(_LAUNCHER_PATH))} \"$@\"\n")
    wrapper.chmod(0o700)
    return wrapper


def _launcher_env(
    job_id: str,
    secret_value: str,
    docker_binary: str,
    credential_name: str,
    *,
    connect_egress: bool = False,
) -> dict[str, str]:
    """SDK が launcher へ渡す環境。secret_value は launcher の環境と docker client の -e にだけ現れる。"""
    # SDK は通常これらを CLI の process 環境へ入れる。launcher は名前で指定された変数しか container へ渡さないため、
    # 同じ値を明示する。欠けると CLI が session 状態の frame を出さず、SDK が idle を待つことがある。
    plain = {
        "HOME": _CONTAINER_HOME,
        "CLAUDE_CODE_ENTRYPOINT": "sdk-py",
        "CLAUDE_AGENT_SDK_VERSION": _SDK_VERSION,
        "CLAUDE_CODE_SDK_READS_SESSION_STATE": "1",
        **_container_env(connect_egress=connect_egress),
    }
    secret_name = _cli_credential(credential_name)[0]
    return {
        # SDK は host の環境も継ぐため、認証用の三つの変数を空にしてから CLI が読む一つだけに値を入れる。
        **{name: "" for name in _AUTH_KINDS},
        secret_name: secret_value,
        "KOTOYOSE_AGENT_CONTAINER": container_name(job_id),
        "KOTOYOSE_AGENT_BINARY": _CONTAINER_CLI,
        "KOTOYOSE_AGENT_SECRET_ENV": secret_name,
        "KOTOYOSE_AGENT_ENV": json.dumps(plain, sort_keys=True),
        "KOTOYOSE_AGENT_WORKDIR": _CONTAINER_WORKSPACE,
        "KOTOYOSE_AGENT_RELAY_SOCKET": _CONTAINER_SOCKET,
        "KOTOYOSE_DOCKER_BINARY": docker_binary,
        "CLAUDE_CONFIG_DIR": _CONTAINER_CONFIG_DIR,
    }


def _options(job: NativeAgentJob, wrapper: Path, host_cwd: Path, env: dict[str, str], *,
             servers: dict | None = None, readonly: bool = False) -> ClaudeAgentOptions:
    """built-in tool を有効にした CLI の設定。host の cwd は SDK が存在を要求するため空の dir を使う。"""
    return ClaudeAgentOptions(
        tools=["Read", "Grep", "Glob"] if readonly else None,
        allowed_tools=["mcp__client__" + spec["name"] for spec in job.client_tool_specs],
        disallowed_tools=list(_DISALLOWED_TOOLS),
        system_prompt=job.instructions,
        mcp_servers=servers or {},
        strict_mcp_config=True,
        permission_mode="bypassPermissions",
        max_turns=job.max_turns,
        model=job.model,
        fallback_model=None,
        cwd=host_cwd,
        cli_path=wrapper,
        settings=None,
        env=env,
        extra_args={},
        hooks=None,
        agents=None,
        setting_sources=[],
        skills=[],
        plugins=[],
        max_buffer_size=job.output_limit_bytes + _SDK_PROTOCOL_OVERHEAD,
    )


def _interrupt_if_due(invocation: _Invocation) -> None:
    """cancel と deadline を owner task の中断へ変換する。"""
    if invocation.cancel.is_set():
        invocation.interruption = "cancelled"
        raise asyncio.CancelledError
    if _expired(invocation):
        invocation.interruption = "timeout"
        raise asyncio.CancelledError


async def execute_container_claude(
    worker,
    job: NativeAgentJob,
    cancel,
    *,
    on_process_started,
    container,
    settings,
    image_id: str | None,
) -> NativeAgentResult:
    """coding job を container で実行し、停止を確認できた結果だけを confirmed にする。

    container / settings / image_id のどれかが無いときは何も作らず invalid_job で返す。
    """
    if container is None or settings is None or image_id is None:
        return _result("failed", "invalid_job", b"", "confirmed")
    try:
        upstream, cli = _validate_job(job, settings)
    except (TypeError, ValueError, OSError):
        return _result("failed", "invalid_job", b"", "confirmed")

    loop = asyncio.get_running_loop()
    job_id = _container_job_id(job)
    place = JailPlace(container, settings, image_id)
    invocation = _Invocation(
        deadline=loop.time() + job.timeout_seconds,
        cancel=cancel,
        output_limit=job.output_limit_bytes,
        lock=asyncio.Lock(),
        idle=asyncio.Event(),
        work_remaining=job.work_remaining,
    )
    if job.deadline_monotonic is not None:
        invocation.deadline = min(invocation.deadline, job.deadline_monotonic)
    invocation.idle.set()
    invocation.owner = asyncio.current_task()
    # cancel と期限は watchdog が owner task の中断へ変換する。broker 起動中の cancel もこれで拾う。
    watchdog = asyncio.create_task(worker._watchdog(invocation, invocation.owner))

    usage = _unknown_usage()
    content = bytearray()
    output_bytes = 0
    turn = 1
    session_id = None
    observation_session_id = None
    provider_limit = None
    tool_names: dict[str, str] = {}
    compression = NativeCompressionObserver(job.compression_event)
    status, reason = "failed", "sdk_error"
    client = None
    process = None
    egress = None
    socket_dir = None
    home = None
    launch_dir = None
    remove_task = None
    launch_attempted = False
    process_exited = True
    container_confirmation = "confirmed"
    egress_confirmation = "confirmed"
    egress_closed = False
    confirmation = "unconfirmed"
    try:
        connect_egress = _uses_connect_egress(job)
        socket_dir = _socket_dir(settings.egress_socket_root)
        if connect_egress:
            egress = PrintCliEgress(
                socket_dir / "egress.sock",
                allowed_hosts=_CLAUDE_CONNECT_HOSTS,
                allowed_ports=_CLAUDE_CONNECT_PORTS,
                run_id=job.attempt_id,
                idle_timeout_seconds=_IDLE_TIMEOUT_SECONDS,
            )
            secret_value = job.credential_value
        else:
            capability = secrets.token_urlsafe(32)
            egress = ProviderEgress(_egress_spec(job, upstream, capability), socket_dir / "egress.sock")
            secret_value = capability
        await egress.start()

        home = Path(tempfile.mkdtemp(prefix="kotoyose-agent-home-", dir=settings.home_root))
        (home / ".claude").mkdir()
        launch_dir = Path(tempfile.mkdtemp(prefix="kotoyose-native-claude-"))
        wrapper = _write_launcher(launch_dir)

        cell = admitted_execution_cell("claude", job.codex_execution_mode)
        readonly = cell.workspace_readonly
        validate_workspace(job.workspace, device=job.workspace_device, inode=job.workspace_inode)
        spec = place.build_spec(
            job_id=job_id,
            workspace=job.workspace,
            home=home,
            egress_dir=socket_dir,
            timeout_seconds=job.timeout_seconds,
            grace_seconds=job.grace_seconds,
            env=_container_env(connect_egress=connect_egress),
            ro_files={_CONTAINER_CLI: cli, _CONTAINER_RELAY: _RELAY_PATH},
            workspace_readonly=readonly,
            memory_bytes=job.memory_bytes or 0, cpus=job.cpus or 0, pids_limit=job.pids_limit or 0,
        )
        if job.validate_start is not None:
            await job.validate_start()
        _interrupt_if_due(invocation)

        env = _launcher_env(
            job_id,
            secret_value,
            getattr(container, "docker_binary", "docker"),
            job.credential_name,
            connect_egress=connect_egress,
        )
        servers = worker._build_server(job, invocation, None) if job.client_tool_specs else {}
        options = _options(job, wrapper, launch_dir, env, servers=servers, readonly=readonly)
        # session store は接続しない。SDK の resume は host の一時 config dir へ transcript を展開するが、
        # container の CLI はそれを見られない。保持しない session は ref にならず、Hub は再開を
        # durable_state (新しい Run) へ倒す。
        launch_attempted = True
        container_id = await place.start_container(spec)
        client = ClaudeSDKClient(options=options)
        await client.connect()
        process = _capture_process(client)
        pid = getattr(process, "pid", None)
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            invocation.terminal = True
            status, reason = "failed", "worker_lost"
            raise _ProcessIdentityError
        _interrupt_if_due(invocation)
        try:
            await on_process_started(pid, container_identity=container_identity(container, job_id, container_id))
        except AgentStartRejected:
            raise
        except Exception:
            invocation.terminal = True
            status, reason = "failed", "sdk_error"
            raise _ProcessIdentityError
        _interrupt_if_due(invocation)

        await client.query(_query_input(job) if job.image_inputs else job.prompt)
        async for message in client.receive_response():
            if isinstance(message, SystemMessage) and message.subtype == "init":
                initialized_id = message.data.get("session_id")
                if isinstance(initialized_id, str) and initialized_id:
                    observation_session_id = initialized_id
            if isinstance(message, OBSERVATION_MESSAGES):
                observation = claude_observation(message, session_id=observation_session_id)
                if observation is not None:
                    if observation.kind == "session_store_warning":
                        log.warning("claude.session_store_mirror_error")
                    _emit_progress(job, observation)
                    if job.limits_mode and observation.kind == "rate_limit":
                        limit = normalize_provider_usage_limit("claude", observation.payload, datetime.now(timezone.utc))
                        if limit is not None:
                            provider_limit = limit
                            status, reason = "failed", "provider_usage_limit"
                    if job.activity_event is not None:
                        try:
                            job.activity_event(observation)
                        except Exception as exc:
                            log.warning("native.activity_callback_failed", error_type=type(exc).__name__)
                    if provider_limit is not None:
                        break
                continue
            if isinstance(message, SystemMessage) and message.subtype == "compact_boundary":
                await compression.boundary(message.data)
            elif isinstance(message, AssistantMessage):
                if job.limits_mode and getattr(message, "error", None) == "rate_limit":
                    provider_limit = normalize_provider_usage_limit(
                        "claude", {"subtype": "error_rate_limit"}, datetime.now(timezone.utc))
                    status, reason = "failed", "provider_usage_limit"
                    continue
                if getattr(message, "error", None) == "authentication_failed":
                    _emit_progress(job, RuntimeEvent(
                        kind="provider_error",
                        summary="Claude assistant reported an authentication error",
                        payload={"assistant_error": "authentication_failed"},
                    ))
                if message.session_id:
                    observation_session_id = message.session_id
                _emit_progress(job, RuntimeEvent(kind="turn", summary=f"Assistant turn {turn}", payload={"turn": turn}))
                if not getattr(message, "error", None):
                    turn += 1
                for block in message.content:
                    if isinstance(block, TextBlock):
                        redacted = _redact_text(block.text, job)
                        _emit_progress(job, RuntimeEvent(kind="text", summary=_summarize(redacted),
                                                         payload={"text": _truncate(redacted)}))
                        if job.activity_event is not None:
                            job.activity_event(RuntimeEvent(kind="text", summary=_truncate(redacted)))
                        encoded = redacted.encode("utf-8")
                        output_bytes += len(encoded)
                        content.extend(encoded)
                        if len(content) > job.output_limit_bytes:
                            content = content[: job.output_limit_bytes]
                            status, reason = "failed", "output_limit_exceeded"
                            raise _OutputLimit
                    elif isinstance(block, ThinkingBlock):
                        redacted = _redact_text(block.thinking, job)
                        _emit_progress(job, RuntimeEvent(kind="thinking", summary=_summarize(redacted),
                                                         payload={"text": _truncate(redacted)}))
                    elif isinstance(block, ToolUseBlock):
                        tool_names[block.id] = block.name
                        redacted = _redact_text(_tool_use_detail(block), job)
                        _emit_progress(job, RuntimeEvent(
                            kind="tool_use", tool_name=block.name, summary=_summarize(redacted),
                            payload={"tool_use_id": _truncate(block.id), "input": _truncate(redacted)},
                        ))
                        if job.activity_event is not None:
                            job.activity_event(RuntimeEvent(kind="tool_use", tool_name=block.name, summary=block.name))
                    elif isinstance(block, ToolResultBlock):
                        _emit_sdk_tool_result(job, block, tool_names)
            elif isinstance(message, UserMessage) and isinstance(message.content, list):
                for block in message.content:
                    if isinstance(block, ToolResultBlock):
                        _emit_sdk_tool_result(job, block, tool_names)
            elif isinstance(message, ResultMessage):
                observed_id = getattr(message, "session_id", None)
                if isinstance(observed_id, str) and observed_id:
                    session_id = observed_id
                usage = _extract_usage(message)
                if provider_limit is not None:
                    reason, status = "provider_usage_limit", "failed"
                elif message.is_error:
                    subtype = getattr(message, "subtype", None)
                    provider_limit = (normalize_provider_usage_limit(
                        "claude", {"subtype": subtype, "http_status": getattr(message, "api_error_status", None)},
                        datetime.now(timezone.utc)) if job.limits_mode else None)
                    reason = ("provider_usage_limit" if provider_limit else
                              "turn_limit_exceeded" if job.limits_mode and subtype == "error_max_turns" else "provider_error")
                    status = "failed"
                    log.warning("native.provider_error", subtype=subtype, num_turns=getattr(message, "num_turns", None))
                else:
                    reason, status = "completed", "succeeded"
        compression_deadline = (loop.time() + invocation.work_remaining()
                                if invocation.work_remaining is not None else invocation.deadline)
        await compression.sample(client, compression_deadline)
        if status == "succeeded" and job.output_contract == "json":
            extracted = _extract_json_object(bytes(content))
            if extracted is None:
                status, reason = "failed", "invalid_json_output"
            else:
                content = bytearray(extracted)
    except AgentStartRejected:
        if not launch_attempted:
            raise
        # 型付き拒否は未起動の証拠にならない。起動後の拒否は sdk_error として扱う。
        invocation.terminal = True
        status, reason = "failed", "sdk_error"
    except _OutputLimit:
        invocation.terminal = True
    except _ProcessIdentityError:
        invocation.terminal = True
    except asyncio.CancelledError:
        # 中断は watchdog か caller が要求する。停止確認は container と broker の状態で行う。
        invocation.terminal = True
        status = invocation.interruption or "cancelled"
        reason = invocation.interruption_reason or status
    except Exception:
        # provider や docker の例外本文には秘密が含まれ得るため、型も本文も返さない。
        invocation.terminal = True
        status, reason = "failed", "sdk_error"
    finally:
        invocation.terminal = True
        watchdog.cancel()
        with contextlib.suppress(BaseException):
            await watchdog
        # container の remove は協調的な後始末を待たずに始める。provider の効果は container 内にある。
        if launch_attempted:
            remove_task = asyncio.create_task(place.remove_container(job_id, 0))
        if client is not None:
            process_exited = await worker._shutdown(client, process, invocation, job.grace_seconds)
        if egress is not None:
            try:
                await egress.close()
                egress_confirmation = "confirmed"
            except Exception:
                egress_confirmation = "unconfirmed"
            egress_closed = True
        if remove_task is not None:
            container_confirmation = await remove_task
        confirmation = "confirmed" if (
            container_confirmation == "confirmed" and egress_confirmation == "confirmed" and process_exited
        ) else "unconfirmed"
        if confirmation == "confirmed" and job.on_runtime_stopped is not None:
            with contextlib.suppress(Exception):
                job.on_runtime_stopped()
        # 停止を確認できなかった container は書き込みを続けうるため、home も残して回収に任せる。
        JailPlace.cleanup_run_dirs(
            container_confirmation=container_confirmation,
            egress_closed=egress_closed,
            temp_home=home,
            socket_dir=socket_dir,
            extra_dirs=([launch_dir] if launch_dir is not None else ()),
        )

    if cancel.is_set() and status == "succeeded":
        status, reason = "cancelled", "cancelled"
    if status == "succeeded" and confirmation != "confirmed":
        status, reason = "worker_lost", "worker_lost"
    # container coding の session は host の Store へ保持しないため、native resume は常に利用不可。
    session_id = None
    if provider_limit is not None:
        provider_limit = provider_limit.model_copy(update={
            "resume": "unavailable", "unavailable_reason": "adapter_unsupported"})
    return NativeAgentResult(
        status,
        reason,
        bytes(content),
        confirmation,
        usage,
        provider_session_id=session_id,
        provider_limit=provider_limit,
        output_bytes=output_bytes + invocation.tool_output_bytes,
        turns=turn - 1,
        launch_outcome=("started" if process is not None else "unknown" if launch_attempted else "not_started"),
    )
