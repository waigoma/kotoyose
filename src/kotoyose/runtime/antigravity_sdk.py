"""Google Antigravity Python SDK runtime for Gemini API key and Vertex authentication."""

from __future__ import annotations

import asyncio
import contextlib
import os
from importlib.metadata import version
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import process_env
from ..activity import emit_runtime_event
from ..logging_setup import log
from .base import RuntimeEvent, RuntimePrompt, RuntimeResult, RuntimeTurnError, prompt_text
from .translation import TranslatedRuntimeConfig, translate_runtime_config


_TOOL_MAP = {
    "Read": "view_file",
    "Glob": "find_file",
    "Grep": "search_directory",
    "Bash": "run_command",
    "Write": "create_file",
    "Edit": "edit_file",
    "WebSearch": "search_web",
    "WebFetch": "read_url_content",
}


# Kotoyose owns these snapshots. Never call SDK read_only/default/all_tools helpers:
# new SDK tools must not silently gain authority or outlive an HTTP turn.
_SDK_VERSION = "0.1.23"
_READ_ONLY_TOOLS = ("view_file", "read_url_content", "finish")
_AUTONOMOUS_TOOLS = (
    "view_file", "create_file", "edit_file", "run_command", "invoke_subagent",
    "generate_image", "search_web", "read_url_content", "finish",
)
_LIFESPAN_TOOLS = ("schedule", "manage_task")
_COMMON_MODEL_ALIASES = frozenset({"haiku", "sonnet", "opus", "flash", "pro"})


@dataclass(slots=True)
class AntigravitySdkRuntimeSession:
    """`google-antigravity` Agent を Kotoyose RuntimeSession に適合させる。"""

    model: str | None
    cwd: str
    translated: TranslatedRuntimeConfig
    config: dict[str, Any] = field(default_factory=dict)
    provider: str = "antigravity"

    _agent_cm: Any = field(default=None, init=False, repr=False)
    _agent: Any = field(default=None, init=False, repr=False)
    _turn_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    invalidated: bool = field(default=False, init=False)

    async def disconnect(self) -> None:
        if self._agent_cm is not None:
            with contextlib.suppress(Exception):
                await self._agent_cm.__aexit__(None, None, None)
        self._agent = None
        self._agent_cm = None

    async def ask(self, prompt: RuntimePrompt) -> RuntimeResult:
        final_result: RuntimeResult | None = None
        async for item in self.stream(prompt):
            if isinstance(item, RuntimeEvent):
                emit_runtime_event(item)
            elif isinstance(item, RuntimeResult):
                final_result = item
        if final_result is not None:
            return final_result
        raise RuntimeError("antigravity SDK stream ended without a result")

    async def stream(
        self, prompt: RuntimePrompt
    ) -> AsyncIterator[str | RuntimeEvent | RuntimeResult]:
        async with self._turn_lock:
            iterator = self._stream_turn(prompt)
            timeout = _sdk_timeout(self.config)
            remaining = timeout
            terminal_result = False
            loop = asyncio.get_running_loop()
            try:
                while True:
                    started = loop.time()
                    try:
                        # The budget counts provider advancement, excluding time
                        # spent in our caller after yield. Never leave a task-
                        # cancellation timer armed across an outward yield.
                        async with asyncio.timeout(remaining):
                            item = await anext(iterator)
                    except StopAsyncIteration:
                        break
                    finally:
                        if remaining is not None:
                            remaining = max(0.0, remaining - (loop.time() - started))
                    terminal_result = isinstance(item, RuntimeResult)
                    yield item
                    if terminal_result:
                        break
            except TimeoutError:
                self.invalidated = True
                await self.disconnect()
                message = (
                    f"antigravity SDK が {timeout:g} 秒以内に応答を完了しませんでした"
                    if timeout is not None else "antigravity SDK provider timed out"
                )
                raise RuntimeError(message) from None
            except BaseException:
                if not terminal_result:
                    self.invalidated = True
                    await self.disconnect()
                raise
            finally:
                await iterator.aclose()

    async def _stream_turn(
        self, prompt: RuntimePrompt
    ) -> AsyncIterator[str | RuntimeEvent | RuntimeResult]:
        agent = await self._ensure_agent()
        response = await agent.chat(prompt_text(prompt))

        tool_queue: asyncio.Queue[RuntimeEvent | None] = asyncio.Queue()

        async def collect_tools() -> None:
            try:
                async for call in response.tool_calls:
                    name = getattr(call, "name", None)
                    server_name = getattr(call, "server_name", None)
                    payload: dict[str, Any] = {}
                    call_id = getattr(call, "id", None)
                    step_id = getattr(call, "step_id", None)
                    if call_id:
                        payload["call_id"] = str(call_id)
                    if step_id:
                        payload["step_id"] = str(step_id)
                    if server_name:
                        payload["server_name"] = str(server_name)
                    await tool_queue.put(
                        RuntimeEvent(
                            kind="tool",
                            tool_name=str(getattr(name, "value", name)) if name else None,
                            summary="tool call",
                            payload=payload or None,
                        )
                    )
            except Exception as exc:
                log.debug("antigravity.sdk.tool_stream_failed", error_type=type(exc).__name__)
            finally:
                await tool_queue.put(None)

        tool_task = asyncio.create_task(collect_tools())
        chunks: list[str] = []
        try:
            async for token in response:
                while not tool_queue.empty():
                    event = tool_queue.get_nowait()
                    if event is not None:
                        yield event
                if isinstance(token, str) and token:
                    chunks.append(token)
                    yield token

            await tool_task
            while not tool_queue.empty():
                event = tool_queue.get_nowait()
                if event is not None:
                    yield event

            usage = _normalize_usage(getattr(response, "usage_metadata", None))
            result = RuntimeResult(
                text="".join(chunks),
                usage=usage,
                runtime_session_id=None,
                provider=self.provider,
            )
        except BaseException as exc:
            cancel = getattr(response, "cancel", None)
            if callable(cancel):
                with contextlib.suppress(Exception):
                    maybe = cancel()
                    if hasattr(maybe, "__await__"):
                        await maybe
            if isinstance(exc, Exception):
                raise RuntimeTurnError(
                    "antigravity SDK did not complete successfully",
                    partial_result=RuntimeResult(
                        text="".join(chunks), usage=_normalize_usage(getattr(response, "usage_metadata", None)),
                        provider=self.provider,
                    ),
                    diagnostics={"terminal_reason": "provider_error"},
                ) from None
            raise
        finally:
            if not tool_task.done():
                tool_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await tool_task
        # Final output is outside failure/cancellation handling: closing a
        # completed stream must not cancel an already successful response.
        yield result

    async def _ensure_agent(self):
        if self._agent is not None:
            return self._agent

        sdk = _load_sdk()
        kwargs = _build_sdk_config_kwargs(
            sdk=sdk,
            model=self.model,
            cwd=self.cwd,
            translated=self.translated,
            config=self.config,
        )
        local_config = sdk["LocalAgentConfig"](**kwargs)
        cm = sdk["Agent"](local_config)
        agent = await cm.__aenter__()
        self._agent_cm = cm
        self._agent = agent
        return agent


def build_antigravity_sdk_session(
    preset,
    cwd_override: str | None,
    session_id: str | None,
    request_headers: dict[str, str] | None = None,
) -> AntigravitySdkRuntimeSession:
    """Build the Antigravity SDK session for API key or Vertex authentication."""
    from ..config import get_config

    cwd = os.path.expanduser(cwd_override) if cwd_override is not None else preset.cwd
    translated = translate_runtime_config(get_config(), preset, request_headers)
    sdk_config = dict((preset.antigravity or {}).get("sdk") or {})
    if translated.timeout_seconds is not None and "timeout_seconds" not in sdk_config:
        sdk_config["timeout_seconds"] = translated.timeout_seconds

    if session_id:
        log.warning(
            "antigravity.sdk.persisted_resume_ignored",
            hint="SDK backend は Kotoyose DB session id resume をまだ公開しません",
        )
    if translated.unsupported_fields:
        log.warning(
            "antigravity.sdk.runtime_fields_unsupported",
            fields=list(translated.unsupported_fields),
        )
    if translated.effort is not None:
        log.warning(
            "antigravity.sdk.effort_unsupported",
            effort=translated.effort,
            hint="Antigravity SDK は effort を公開していません",
        )

    return AntigravitySdkRuntimeSession(
        model=preset.model,
        cwd=str(Path(cwd).expanduser()),
        translated=translated,
        config=sdk_config,
    )


def _load_sdk() -> dict[str, Any]:
    try:
        from google.antigravity import Agent, CapabilitiesConfig, LocalAgentConfig, types
        from google.antigravity.hooks import policy
    except ImportError as exc:
        raise RuntimeError(
            f"Antigravity SDK backend には google-antigravity=={_SDK_VERSION} が必要です。"
            " `pip install 'kotoyose[antigravity]'` を実行してください"
        ) from exc
    installed_version = version("google-antigravity")
    if installed_version != _SDK_VERSION:
        raise RuntimeError(f"Antigravity SDK backend requires google-antigravity=={_SDK_VERSION}")
    log.info("antigravity.sdk.loaded", sdk_version=installed_version)
    return {
        "Agent": Agent,
        "CapabilitiesConfig": CapabilitiesConfig,
        "LocalAgentConfig": LocalAgentConfig,
        "types": types,
        "policy": policy,
    }


def _build_sdk_config_kwargs(
    *,
    sdk: dict[str, Any],
    model: str | None,
    cwd: str,
    translated: TranslatedRuntimeConfig,
    config: dict[str, Any],
) -> dict[str, Any]:
    types = sdk["types"]
    kwargs: dict[str, Any] = {
        "workspaces": [cwd],
        "mcp_servers": _build_sdk_mcp_servers(types, translated.mcp_servers),
        "skills_paths": [str(Path(skill.path).parent) for skill in translated.skills],
        "subagents": _build_sdk_subagents(types, translated.agents, config.get("subagent_models")),
    }
    if translated.instructions:
        kwargs["system_instructions"] = translated.instructions
    if model:
        kwargs["model"] = model

    autonomous = bool(config.get("autonomous_tools") or config.get("allow_all"))
    kwargs["capabilities"] = sdk["CapabilitiesConfig"](
        enabled_tools=[types.BuiltinTools(name) for name in (_AUTONOMOUS_TOOLS if autonomous else _READ_ONLY_TOOLS)],
        run_command_config=types.RunCommandConfig(enable_daemons=False),
    )
    policy = sdk["policy"]
    # Specific denies outrank the wildcard allow, including in allow_all mode.
    policies = [policy.deny(name, reason="Kotoyose does not support tasks beyond the request lifespan") for name in _LIFESPAN_TOOLS]
    policies.append(policy.deny("run_command", when=_requests_daemon, reason="Kotoyose does not permit daemon commands"))
    if config.get("allow_all"):
        policies.append(policy.allow_all())
    else:
        # Preserve LocalAgentConfig's command-confirmation default explicitly.
        policies.extend(policy.confirm_run_command())
    kwargs["policies"] = policies

    vertex = bool(config.get("vertex"))
    if vertex:
        kwargs["vertex"] = True
        for key in ("project", "location"):
            value = config.get(key)
            if isinstance(value, str) and value:
                kwargs[key] = value
    api_key_env = config.get("api_key_env")
    if isinstance(api_key_env, str) and api_key_env:
        api_key = process_env.get(api_key_env)
        if not api_key:
            raise RuntimeError(f"Antigravity SDK api_key_env '{api_key_env}' が設定されていません")
        kwargs["api_key"] = api_key

    extra = config.get("options")
    if isinstance(extra, dict):
        # Explicit escape hatch for new SDK fields. Authentication and resource
        # fields above win so a generic options dict cannot silently replace them.
        for key, value in extra.items():
            kwargs.setdefault(str(key), value)
    return kwargs


def _build_sdk_mcp_servers(types: Any, servers: dict[str, dict]) -> list[Any]:
    result: list[Any] = []
    for raw_name, raw in servers.items():
        name = _sdk_safe_name(raw_name)
        server = dict(raw)
        stype = server.get("type")
        common: dict[str, Any] = {"name": name}
        for key in ("timeout_seconds", "enabled_tools", "disabled_tools"):
            value = server.get(key)
            if value is not None:
                common[key] = value

        if stype == "sse":
            raise ValueError(
                f"Antigravity SDK MCP '{raw_name}' は legacy SSE transport をサポートしません"
            )
        if stype == "http" or server.get("url") or server.get("serverUrl"):
            url = server.get("serverUrl") or server.get("url") or server.get("httpUrl")
            kwargs = {**common, "url": url}
            headers = server.get("headers")
            if isinstance(headers, dict):
                kwargs["headers"] = {str(k): str(v) for k, v in headers.items()}
            result.append(types.McpStreamableHttpServer(**kwargs))
            continue

        kwargs = {**common, "command": str(server["command"])}
        args = server.get("args")
        if isinstance(args, list):
            kwargs["args"] = [str(value) for value in args]
        env = server.get("env")
        if isinstance(env, dict):
            kwargs["env"] = {str(k): str(v) for k, v in env.items()}
        result.append(types.McpStdioServer(**kwargs))
    return result


def _requests_daemon(arguments: dict[str, Any]) -> bool:
    # IsDaemon is the SDK's documented flag. Also reject common alternate forms
    # rather than letting a renamed field silently relax the lifespan boundary.
    return any(arguments.get(key) not in (None, False, 0, "", "false", "False") for key in (
        "IsDaemon", "is_daemon", "enable_daemons", "background", "run_in_background",
    ))


def _build_sdk_subagents(types: Any, agents: dict[str, Any], model_overrides: Any = None) -> list[Any]:
    if model_overrides is None:
        model_overrides = {}
    if not isinstance(model_overrides, dict) or set(model_overrides) - set(agents):
        raise ValueError("antigravity.sdk.subagent_models must map configured agent names to SDK model IDs")
    result: list[Any] = []
    for raw_name, agent in agents.items():
        kwargs: dict[str, Any] = {
            "name": _sdk_safe_name(raw_name),
            "description": agent.description,
            "system_instructions": agent.prompt,
        }
        model = model_overrides.get(raw_name, getattr(agent, "model", "inherit"))
        if raw_name in model_overrides and (not isinstance(model, str) or not model.strip() or model == "inherit"):
            raise ValueError("antigravity.sdk.subagent_models values must be explicit SDK model IDs")
        if model in _COMMON_MODEL_ALIASES:
            raise ValueError(
                f"Antigravity SDK subagent '{raw_name}' requires an explicit model ID in antigravity.sdk.subagent_models"
            )
        if model not in (None, "inherit"):
            if not isinstance(model, str) or not model.strip():
                raise ValueError("Antigravity SDK subagent model must be a nonempty SDK model ID")
            kwargs["model"] = model
        enabled = [types.BuiltinTools(name) for name in _READ_ONLY_TOOLS]
        if agent.tools is not None:
            enabled = []
            for raw_tool in agent.tools:
                mapped = _TOOL_MAP.get(raw_tool)
                if mapped is None:
                    raise ValueError(f"Antigravity SDK subagent '{raw_name}' has an unsupported tool: {raw_tool}")
                enabled.append(types.BuiltinTools(mapped))
        kwargs["capabilities"] = types.SubagentCapabilities(
            enabled_tools=enabled,
            run_command_config=types.RunCommandConfig(enable_daemons=False),
        )
        result.append(types.SubagentConfig(**kwargs))
    return result


def _normalize_usage(usage: Any) -> dict | None:
    if usage is None:
        return None
    if hasattr(usage, "model_dump"):
        values = usage.model_dump()
    elif isinstance(usage, dict):
        values = usage
    else:
        values = {
            key: getattr(usage, key, None)
            for key in (
                "prompt_token_count",
                "cached_content_token_count",
                "candidates_token_count",
                "thoughts_token_count",
                "total_token_count",
            )
        }
    mapped = {
        "input_tokens": values.get("prompt_token_count"),
        "output_tokens": values.get("candidates_token_count"),
        "thinking_tokens": values.get("thoughts_token_count"),
        "cache_read_tokens": values.get("cached_content_token_count"),
        "total_tokens": values.get("total_token_count"),
    }
    return {key: value for key, value in mapped.items() if isinstance(value, int) and not isinstance(value, bool) and value >= 0} or None


def _sdk_safe_name(value: str) -> str:
    # SDK MCP names permit alphanumeric, '-' and '_'. Subagent names accept the same
    # practical subset, so keep one normalization rule for translated resources.
    normalized = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in value)
    return normalized.strip("-_") or "resource"


def _sdk_timeout(config: dict[str, Any]) -> float | None:
    value = config.get("timeout_seconds")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return float(value)
    return None
