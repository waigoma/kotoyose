"""host から child へ 1 turn を送る RuntimeSession。

cwd は文字列のまま渡す。解決も方針も child が行う。secret と MCP の起動定義は送らない。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any, NoReturn
from uuid import uuid4

from .. import delegation
from ..child_local_llm import LOCAL_LLM_CONNECTION_FIELDS
from ..child_mcp import RELAY_WRITE_TIMEOUT, relay_turn_timeout
from ..child_mcp_host import HostMcpTurn, select_publications
from ..child_projection import child_provider
from ..child_protocol import (
    CHILD_CONNECTION_FIELDS, LOCAL_LLM_FEATURE, LOCAL_LLM_PRESET_FIELDS,
    PROMPT_PREFIX_SKILLS_FEATURE, PROVIDER_DATA_MCP_FEATURE, PROVIDER_DATA_UNRESTRICTED_FEATURE,
    MAX_RECORD, MAX_SKILL_TURN_RECORD, REPOSITORY_SKILLS_FEATURE, SDK_SKILLS_FEATURE, SKILL_FOLDERS_FEATURE, encode_frame,
)
from ..child_registry import child_on_name
from ..child_sdk_skills import (
    MAX_SKILLS_BYTES, MAX_TURN_FILE_BYTES, read_skill_file, validate_legacy_skill_name, validate_skill_names,
    validate_turn_files,
)
from ..provider_data import UNRESTRICTED, UNRESTRICTED_PLATFORM, child_capability, require_unrestricted_alias
from ..child_server import active_child_hub
from ..config import HarnessConfig, ModelPreset, get_client, get_config, resolve_prompt_texts, resolve_skill_fallback_texts
from ..delegation import MCP_NAME
from ..provider_spec import CODEX_PRESET_OVERRIDES, PROMPT_PREFIX_SKILL_FORMAT, SDK_SKILL_FORMATS
from ..registry import registry
from ..skill_folders import SkillFolder, encode_skill_folders
from .base import (
    RuntimeChatHistory,
    RuntimeEvent,
    RuntimeImageInput,
    RuntimePrompt,
    RuntimeResult,
    RuntimeTextInput,
    RuntimeTurnError,
    SDKConnectionError,
    prompt_text,
)
from .skill_resources import folder_binding, resolve_cli_resources, resolve_cli_skill_texts, resolve_native_folders
from .translation import resolve_cli_prompt_texts, resolve_plugin_skills

_DROP_KEYS = frozenset({
    "secret", "config_dir", "auth_file", "mcp_servers", "plugins", "cwd", *CODEX_PRESET_OVERRIDES,
    "anthropic_auth_token", "anthropic_auth_token_env", "anthropic_auth_token_secret",
})


def require_child_link(name: str, provider: str):
    """接続中の child を取得し、宣言にある hello の capability を照合する。"""
    hub = active_child_hub()
    live = hub.live_for(name) if hub is not None else None
    if live is None:
        _offline(name)
    if provider == "openai_compat" and LOCAL_LLM_FEATURE not in live.features:
        raise SDKConnectionError(f"child {name} does not advertise Local LLM support ({LOCAL_LLM_FEATURE})")
    spec = registry.provider(provider)
    if spec is not None and (feature := spec.child_turn.hello_feature) is not None and feature not in live.features:
        raise SDKConnectionError(f"child {name} does not advertise {spec.display_name} turn support")
    return hub, live


def _offline(name: str) -> NoReturn:
    from ..session import SDKConnectionError
    raise SDKConnectionError(f"child {name} is offline")


def child_cli_binding(
    config: HarnessConfig | None, preset: ModelPreset, cwd: str | None, client_allows_full: bool,
) -> str | None:
    """Bind cached CLI proxies to skill sources, delivered files and any full-access grant."""
    name = child_on_name(preset.on)
    if name is None:
        return None
    shape = {"mcp_refs": preset.mcp_refs} if any(ref.startswith("host:") for ref in preset.mcp_refs) else {}
    if preset.provider not in ("cli_acp", "print_cli"):
        return hashlib.sha256(json.dumps(shape, sort_keys=True).encode()).hexdigest() if shape else None
    if preset.provider == "cli_acp" and preset.allow is not None and preset.allow.full:
        shape.update(alias=preset.model_dump(mode="json"), cwd=cwd, client_allow_full=client_allows_full)
    if config is not None and preset.skill_refs:
        shape["skills"] = [
            (ref, config.skill_definitions[ref].model_dump(mode="json")) for ref in preset.skill_refs
        ]
        hub = active_child_hub()
        live = hub.live_for(name) if hub is not None else None
        if live is not None and SKILL_FOLDERS_FEATURE in live.features:
            shape["skill_folders"] = folder_binding(resolve_cli_resources(config, preset).folders)
    return hashlib.sha256(json.dumps(shape, sort_keys=True).encode()).hexdigest() if shape else None


class ChildRuntimeSession:
    """`on: child:<name>` の会話を、既存の mux の新しい stream で実行する。"""

    def __init__(
        self,
        *,
        name: str,
        provider: str,
        alias: str,
        config: HarnessConfig | None,
        preset: ModelPreset,
        conversation_key: str | None,
        cwd: str | None,
        client_allows_full: bool = False,
        request_headers: dict[str, str] | None = None,
        delegation_parent: delegation.DelegationParent | None = None,
        delegation_app=None,
    ) -> None:
        self.name = name
        self.provider = provider
        self.alias = alias
        self.config = config
        self.preset = preset
        self.conversation_key = conversation_key
        self.cwd = cwd
        self._client_allows_full = client_allows_full
        self._delegation_app = delegation_app
        self._delegation_parent = delegation_parent
        self._delegation_scope = uuid4().hex if delegation_parent is not None else None
        self._delegation_grants: dict[int, delegation.DelegationGrant] = {}
        declared = {key.lower() for key in preset.required_request_headers}
        self._request_headers = {key: value for key, value in (request_headers or {}).items() if key.lower() in declared}
        self._closed = False

    async def disconnect(self) -> None:
        """この host 側の proxy を閉じる。child 側の温かい session は、次のリンクまで残る。"""
        self._closed = True
        for grant in self._delegation_grants.values():
            delegation.revoke(grant)
        self._delegation_grants.clear()

    def _delegation_targets(self, cfg: HarnessConfig, preset: ModelPreset) -> frozenset[str]:
        parent = self._delegation_parent
        caller = get_client(parent.client_id, cfg) if parent is not None else None
        if caller is None:
            raise ValueError("Kotoyose delegation requires the calling client")
        return frozenset(alias for alias in preset.delegates if "*" in caller.models or alias in caller.models)

    async def ask(self, prompt: RuntimePrompt) -> RuntimeResult:
        """stream の最後の結果を返す。途中の失敗は RuntimeTurnError。"""
        result: RuntimeResult | None = None
        text = []
        async for item in self.stream(prompt):
            if isinstance(item, RuntimeResult):
                result = item
            elif self.provider == "openai_compat" and isinstance(item, str):
                text.append(item)
        if result is None:
            raise RuntimeTurnError("child turn ended without a result", partial_result=RuntimeResult(text="", provider=self.provider))
        return replace(result, text="".join(text)) if text else result

    def stream(self, prompt: RuntimePrompt) -> AsyncIterator[str | RuntimeEvent | RuntimeResult]:
        """text delta、RuntimeEvent、最後の RuntimeResult を child から受け取る。"""
        return self._stream(prompt)

    async def _stream(self, prompt: RuntimePrompt) -> AsyncIterator[str | RuntimeEvent | RuntimeResult]:
        if self._closed:
            _offline(self.name)
        hub, live = require_child_link(self.name, self.provider)
        refs = [ref.removeprefix("host:") for ref in self.preset.mcp_refs if ref.startswith("host:")]
        delegation_targets = None
        try:
            cfg = get_config() if refs else self.config
            publications = select_publications(cfg, refs, hub.identity.secret, live) if refs else {}
            if publications:
                current = cfg.models.get(self.alias)
                if current is None or current.on != self.preset.on or current.mcp_refs != self.preset.mcp_refs:
                    raise ValueError("child turn alias MCP references changed")
                if MCP_NAME in publications:
                    delegation_targets = self._delegation_targets(cfg, current)
            payload = build_turn_payload(
                alias=self.alias, config=self.config, preset=self.preset,
                conversation_key=self.conversation_key, cwd=self.cwd, prompt=prompt, live=live,
                client_allows_full=self._client_allows_full, host_mcps=publications,
            )
        except ValueError as exc:
            raise RuntimeTurnError(
                str(exc), partial_result=RuntimeResult(text="", provider=self.provider), retryable=False,
            ) from exc
        stream = hub.allocate_stream()
        queue = live.mux.queue(stream)
        relay = None
        sent = finished = False
        deadline = None
        if self.preset.timeout_seconds is not None:
            deadline = asyncio.get_running_loop().time() + self.preset.timeout_seconds
        try:
            if publications:
                timeout = relay_turn_timeout(self.preset.timeout_seconds)
                grant = None
                parent = self._delegation_parent
                if delegation_targets is not None and parent is not None:
                    _, grant = delegation.issue(
                        parent.client_id, self.alias, self.conversation_key, delegation_targets,
                        parent_depth=0, ttl=timeout, api_key_name=parent.api_key_name, scope=self._delegation_scope,
                    )
                    self._delegation_grants[stream] = grant
                relay = HostMcpTurn(
                    hub, live, stream, self.alias, publications, self._request_headers, timeout,
                    delegation_grant=grant, delegation_app=self._delegation_app,
                )
                payload["host_mcp_grant"] = relay.grant
                live.mux.mcp_turns[stream] = relay
            sent = await hub.send_frame(self.name, stream, "turn", payload, link=live)
            if not sent:
                _offline(self.name)
            while True:
                try:
                    if deadline is None:
                        frame = await queue.get()
                    else:
                        remaining = deadline - asyncio.get_running_loop().time()
                        if remaining <= 0:
                            raise TimeoutError
                        frame = await asyncio.wait_for(queue.get(), remaining)
                except TimeoutError:
                    dispatched = relay is not None and relay.sent_tool_call
                    raise RuntimeTurnError(
                        "child turn timed out; MCP tool execution result is unknown" if dispatched else "child turn timed out",
                        partial_result=RuntimeResult(text="", provider=self.provider),
                        retryable=False if dispatched else True,
                    )
                if frame is None or frame.kind == "closed":
                    if relay is not None and relay.sent_tool_call:
                        raise RuntimeTurnError(
                            "child link disconnected; MCP tool execution result is unknown",
                            partial_result=RuntimeResult(text="", provider=self.provider), retryable=False,
                        )
                    _offline(self.name)
                if frame.kind == "delta":
                    yield str(frame.payload.get("text") or "")
                elif frame.kind == "event":
                    body = frame.payload
                    extra = body.get("payload")
                    yield RuntimeEvent(
                        kind=str(body.get("kind") or "event"),
                        tool_name=body.get("tool_name"),
                        summary=body.get("summary"),
                        payload=extra if isinstance(extra, dict) else None,
                    )
                elif frame.kind == "result":
                    if relay is not None:
                        relay.cancel()
                    finished = True
                    yield _result_from_payload(frame.payload, self.provider)
                    return
                elif frame.kind == "error":
                    finished = relay is None or not relay.reply_failed
                    # A child-supplied retryable flag must not retry a tools/call that already ran.
                    raise _turn_error(
                        frame.payload, self.provider,
                        tool_dispatched=relay is not None and relay.sent_tool_call,
                    )
        finally:
            if grant := self._delegation_grants.pop(stream, None):
                delegation.revoke(grant)
            if relay is not None:
                live.mux.mcp_turns.pop(stream, None)
                await relay.close()
            live.mux.discard(stream)
            if sent and not finished:
                send = asyncio.create_task(self._cancel_remote_turn(hub, live, stream))
                current = asyncio.current_task()
                if current is None or not current.cancelling():
                    await send

    async def _cancel_remote_turn(self, hub, live, stream: int) -> None:
        try:
            async with asyncio.timeout(RELAY_WRITE_TIMEOUT):
                await hub.send_frame(self.name, stream, "cancel", {}, link=live)
        except (OSError, TimeoutError):
            pass


def build_turn_payload(
    *,
    alias: str,
    config: HarnessConfig | None,
    preset: ModelPreset,
    conversation_key: str | None,
    cwd: str | None,
    prompt: RuntimePrompt,
    live=None,
    client_allows_full: bool = False,
    host_mcps: dict[str, str] | None = None,
) -> dict:
    """child が builder を組むための定義。secret も MCP の起動定義も host の path も入れない。"""
    prompts: list[str] = []
    skills: list[dict[str, str]] = []
    skill_format = None
    skill_folders = None
    route: dict = {}
    connection: dict = {}
    local_llm = False
    if config is not None and preset.route is not None:
        route_model = config.routes[preset.route]
        route = _scrub(route_model.model_dump(mode="json"))
        connection_model = config.connections[route_model.connection]
        local_llm = connection_model.kind == "local_llm"
        fields = LOCAL_LLM_CONNECTION_FIELDS if local_llm else CHILD_CONNECTION_FIELDS
        connection = connection_model.model_dump(mode="json", include=fields)
        if local_llm:
            if live is None:
                raise ValueError("A live child catalog is required for Local LLM turns")
            if LOCAL_LLM_FEATURE not in live.features:
                raise ValueError(f"child {live.name} does not advertise Local LLM support ({LOCAL_LLM_FEATURE})")
            models = live.local_llm.get(connection_model.local_llm)
            if models is None:
                raise ValueError(f"child {live.name} has not advertised Local LLM connection '{connection_model.local_llm}'")
            if route_model.model not in models:
                raise ValueError(f"model '{route_model.model}' is not allowed for Local LLM connection '{connection_model.local_llm}' on child {live.name}")
    preset_body = _scrub(preset.model_dump(mode="json", include=LOCAL_LLM_PRESET_FIELDS if local_llm else None))
    provider = child_provider(route)
    provider_format = provider.turn.skill_format if provider is not None else None
    sdk = provider_format in SDK_SKILL_FORMATS
    prefix_provider = provider_format == PROMPT_PREFIX_SKILL_FORMAT
    features = live.features if live is not None else ()
    if preset.repository_skills is not None and live is not None and REPOSITORY_SKILLS_FEATURE not in features:
        raise ValueError(f"child {live.name} must advertise {REPOSITORY_SKILLS_FEATURE} to use repository_skills")
    if config is not None and preset.route is not None:
        prompt_files = config.resolve_prompt_files(preset)
        folder_feature = SKILL_FOLDERS_FEATURE in features
        try:
            if sdk and SDK_SKILLS_FEATURE in features:
                skill_format = provider_format
                if folder_feature:
                    folders = _sdk_folders(config, preset)
                    if folders:
                        skill_folders = encode_skill_folders(folders)
                else:
                    skills = _read_sdk_skills(config, preset)
                try:
                    prompts = resolve_cli_prompt_texts(config, preset)
                except ValueError:
                    raise ValueError(
                        f"child prompt files are unavailable or invalid (limit {MAX_TURN_FILE_BYTES} bytes)"
                    ) from None
            elif prefix_provider and preset.skill_refs and (folder_feature or PROMPT_PREFIX_SKILLS_FEATURE in features):
                skill_format = PROMPT_PREFIX_SKILL_FORMAT
                if folder_feature:
                    resources = resolve_cli_resources(config, preset)
                    prompts, skills = resources.prompts, resources.skills
                    if resources.folders:
                        skill_folders = encode_skill_folders(resources.folders)
                else:
                    skills = resolve_cli_skill_texts(config, preset)
                    prompts = resolve_cli_prompt_texts(config, preset)
                    validate_turn_files(prompts, skills, native_skills=False)
        except SDKConnectionError as exc:
            raise ValueError(str(exc)) from None
        if skill_format is None:
            prompts = resolve_prompt_texts(prompt_files)
            if sdk:
                validate_skill_names(preset.skill_refs, legacy=True)
                for ref in preset.skill_refs:
                    if definition := config.skill_definitions.get(ref):
                        validate_legacy_skill_name(definition.skill)
            if not prefix_provider:
                skills = [
                    {"name": name, "text": text}
                    for name, text in resolve_skill_fallback_texts(config.skill_definitions, preset.skill_refs)
                ]
        if sdk:
            validate_turn_files(prompts, skills, legacy=skill_format is None)
    if provider is not None:
        provider.turn.project(route, preset_body)
    declaration = None
    profile_id = route.get("profile")
    if isinstance(profile_id, str):
        from ..print_cli_profiles import get_print_cli_profile

        try:
            profile = get_print_cli_profile(profile_id)
        except KeyError:
            profile = None
        if profile is not None:
            declaration = profile.data_declaration
            if live is not None and profile.native_skill_layout is not None:
                if declaration is not None and REPOSITORY_SKILLS_FEATURE not in features:
                    raise ValueError(f"child {live.name} must advertise {REPOSITORY_SKILLS_FEATURE} to use this Grok declaration")
                if preset.repository_skills is not None and getattr(live, "platform", None) != "linux-x64":
                    raise ValueError("Grok repository_skills requires a Linux x64 child")
            if profile.provider == "grok" and live is not None and (
                (getattr(live, "platform", None) or "").startswith("windows-") or PROVIDER_DATA_UNRESTRICTED_FEATURE in live.features
            ):
                raise ValueError("Windows Grok requires an approved unrestricted declaration")
    body = {
        "alias": alias,
        "conversation_key": conversation_key,
        "cwd": cwd,
        "prompt": _local_llm_prompt_payload(prompt) if local_llm else _prompt_payload(prompt),
        "preset": preset_body,
        "route": route,
        "connection": connection,
        "prompts": prompts,
        "skills": skills,
    }
    if host_mcps:
        supports_http = (provider is not None and provider.id in {"claude", "codex"}) or (
            declaration is not None and route.get("driver") == "cli.acp"
        )
        if not supports_http:
            raise ValueError("host MCP references require Claude, Codex, or HTTP-capable data-declared ACP")
        body["host_mcps"] = host_mcps
    if skill_format is not None:
        body["skill_format"] = skill_format
    if skill_folders is not None:
        body["skill_folders"] = skill_folders
    if declaration is not None:
        body["declaration_sha256"] = declaration.sha256
        surface = preset.agent.surface if preset.agent is not None else "read"
        capability = child_capability(surface, has_mcp=bool(preset.mcp_refs))
        if live is not None:
            capability = declared_turn_capability(
                declaration, preset, driver=route.get("driver"), name=live.name, platform=getattr(live, "platform", None),
                features=live.features, providers=getattr(live, "providers", ()),
            )
        if capability == UNRESTRICTED and client_allows_full is not True:
            raise ValueError("unrestricted requires an authenticated client with allow_full")
        body["capability"] = capability
    if skill_format is not None or sdk or host_mcps:
        limit = MAX_SKILL_TURN_RECORD if skill_folders is not None else MAX_RECORD
        wire_body = {**body, "host_mcp_grant": "0" * 64} if host_mcps else body
        if len(encode_frame(1_000_000_000, "turn", wire_body).encode("utf-8")) > limit:
            raise ValueError(f"child turn frame exceeds {limit} bytes")
    return body


def _sdk_folders(config: HarnessConfig, preset: ModelPreset) -> tuple[SkillFolder, ...]:
    plugins, selected = config.resolve_skill_resources(preset)
    if isinstance(selected, list):
        validate_skill_names(selected)
    inline = tuple(resolve_plugin_skills(plugins, selected, keep_source_path=True))
    return resolve_native_folders(config, preset, inline=inline)


def _read_sdk_skills(config: HarnessConfig, preset: ModelPreset) -> list[dict[str, str]]:
    plugins, selected = config.resolve_skill_resources(preset)
    if isinstance(selected, list):
        validate_skill_names(selected)
    sources = resolve_plugin_skills(plugins, selected)
    validate_skill_names([skill.name for skill in sources])
    skills = []
    remaining = MAX_SKILLS_BYTES
    for source in sources:
        text = read_skill_file(source.name, source.path, remaining)
        remaining -= len(text.encode("utf-8"))
        skills.append({"name": source.name, "text": text})
    return skills


def declared_turn_capability(declaration, preset, *, driver, name, platform, features, providers) -> str:
    surface = preset.agent.surface if preset.agent is not None else "read"
    if declaration.supports(UNRESTRICTED):
        if platform == UNRESTRICTED_PLATFORM or (
            platform is None and PROVIDER_DATA_UNRESTRICTED_FEATURE in features
        ):
            if PROVIDER_DATA_UNRESTRICTED_FEATURE not in features:
                raise ValueError(f"Child '{name}' does not advertise {PROVIDER_DATA_UNRESTRICTED_FEATURE}.")
            require_unrestricted_alias(
                preset.on, surface, preset.allow is not None and preset.allow.full, driver, tools=preset.tools, deny=preset.deny,
            )
            if child_on_name(preset.on) != name:
                raise ValueError(f"Unrestricted alias must target child '{name}'.")
            if not any(
                row["sha256"] == declaration.sha256 and row["provider"] == declaration.id
                and row["profile"] == declaration.profile_id and row["driver"] == driver
                and row["contract_version"] == declaration.contract_version and row["capability"] == UNRESTRICTED
                for row in providers
            ):
                raise ValueError(f"Child '{name}' has not advertised unrestricted approval for the current '{declaration.id}' declaration.")
            return UNRESTRICTED
        if platform is None:
            raise ValueError(f"Child '{name}' has not advertised its platform for '{declaration.id}'.")
        if declaration.platform(platform) is None:
            raise ValueError(f"Child '{name}' has no '{declaration.id}' artifact for {platform}.")
    capability = child_capability(surface, has_mcp=bool(preset.mcp_refs))
    if capability == "mcp" and PROVIDER_DATA_MCP_FEATURE not in features:
        raise ValueError(f"Child '{name}' does not advertise data provider MCP turn support ({PROVIDER_DATA_MCP_FEATURE}).")
    return capability


def _prompt_payload(prompt: RuntimePrompt) -> dict:
    if isinstance(prompt, (str, RuntimeChatHistory)):
        return {"text": prompt_text(prompt)}
    parts: list[dict[str, Any]] = []
    for part in prompt:
        if isinstance(part, RuntimeImageInput):
            item: dict[str, Any] = {"type": "image", "url": part.url}
            if part.detail is not None:
                item["detail"] = part.detail
            parts.append(item)
        elif isinstance(part, RuntimeTextInput):
            parts.append({"type": "text", "text": part.text})
    return {"parts": parts}


def _local_llm_prompt_payload(prompt: RuntimePrompt) -> dict:
    if isinstance(prompt, RuntimeChatHistory):
        return {"messages": [list(message) for message in prompt.messages]}
    if isinstance(prompt, str) or all(isinstance(part, RuntimeTextInput) for part in prompt):
        return {"text": prompt_text(prompt)}
    raise ValueError("Local LLM connections support only text chat")


def _scrub(value: Any) -> Any:
    """送ってはいけない鍵を、入れ子も含めて落とす。"""
    if isinstance(value, dict):
        return {str(key): _scrub(item) for key, item in value.items() if str(key) not in _DROP_KEYS}
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    return value


def _result_from_payload(payload: dict, provider: str) -> RuntimeResult:
    usage = payload.get("usage")
    return RuntimeResult(
        text=str(payload.get("text") or ""),
        usage=usage if isinstance(usage, dict) else None,
        runtime_session_id=payload.get("runtime_session_id"),
        provider=str(payload.get("provider") or provider),
        structured_output=payload.get("structured_output"),
    )


def _turn_error(payload: dict, provider: str, *, tool_dispatched: bool = False) -> RuntimeTurnError:
    partial = payload.get("partial_result") if isinstance(payload.get("partial_result"), dict) else {}
    diagnostics: dict[str, str | int | bool] = {}
    raw = payload.get("diagnostics")
    if isinstance(raw, dict):
        for key, value in raw.items():
            if isinstance(value, (str, int, bool)):
                diagnostics[str(key)] = value
    retryable = payload.get("retryable")
    if tool_dispatched:
        retryable = False
    elif not isinstance(retryable, bool):
        retryable = None
    return RuntimeTurnError(
        str(payload.get("message") or "child turn failed"),
        partial_result=_result_from_payload(partial, provider),
        diagnostics=diagnostics,
        retryable=retryable,
    )
