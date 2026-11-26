"""child が受け取った turn を、方針で狭めてから既存の runtime builder に渡す。

child は config.yml を持たない。host から来た alias と route と connection で
メモリ上の HarnessConfig を組み、`use_config` の間だけ builder に見せる。
login は `kotoyose-child run` を動かしている OS user の普段のログイン先を使う。
"""

from __future__ import annotations

import asyncio
import ctypes
import hashlib
import importlib
import json
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from weakref import WeakValueDictionary

from pydantic import ValidationError
import structlog

from .agent_spec import driver_manifest
from .alias_mcp import mcp_reference, validate_mcp_references
from .child_mcp import approved_versions, relay_turn_timeout, valid_version, versions as mcp_versions
from .child_mcp_proxy import ChildMcpProxy, pop_relay_env, push_relay_env
from .child_identity import load_host_pin, load_policy, valid_data_mcp_name
from .child_local_llm import LOCAL_LLM_CONNECTION_FIELDS, LocalLLM, completion_url, valid_local_llm_name
from .child_launch import child_builder_options
from .child_output import ChildOutputGuard
from .child_process_tree import TURN_PIDS as _TURN_PIDS
from .child_process_tree import STRICT_WINDOWS_JOBS, install_turn_popen, kill_pids, new_turn_pids, uninstall_turn_popen
from .child_protocol import CHILD_CONNECTION_FIELDS, LOCAL_LLM_PRESET_FIELDS
from .child_projection import bind_login_home, child_provider
from .child_projection import use_local_cli_login as _use_local_cli_login
from .child_sdk_skills import validate_legacy_skill_name, validate_turn_files
from .child_windows import normalize_windows_path, resolve_windows_shim, windows_path_key
from .config import SESSION_TTL_SECONDS, HarnessConfig, use_child_mcp_projection, use_config
from .provider_spec import CLAUDE_PRESET_OVERRIDES, CODEX_PRESET_OVERRIDES, PROMPT_PREFIX_SKILL_FORMAT, SDK_SKILL_FORMATS
from .registry import registry
from .runtime.base import (
    RuntimeChatHistory,
    RuntimeEvent,
    RuntimeImageInput,
    RuntimePrompt,
    RuntimeResult,
    RuntimeTextInput,
    RuntimeTurnError,
)
from .runtime.skill_resources import CliInstructions, validate_native_folders
from .skill_folders import SkillFolder, decode_skill_folders, write_skill_plugin

log = structlog.get_logger()


class _TurnTiming:
    """turn の段ごとの所要時間を、`kotoyose-child run` の窓 (stderr) に 1 行で出す。本文は出さない。"""

    def __init__(self) -> None:
        self.started = time.monotonic()
        self.last = self.started
        self.phases: list[tuple[str, float]] = []
        self.cold = False
        self.outcome = "failed"
        self._first = False

    def mark(self, name: str) -> None:
        now = time.monotonic()
        self.phases.append((name, now - self.last))
        self.last = now

    def first_output(self) -> None:
        if not self._first:
            self._first = True
            self.mark("first output")

    def report(self, stream: int) -> None:
        total = time.monotonic() - self.started
        parts = ", ".join(f"{name} {seconds:.1f}s" for name, seconds in self.phases)
        kind = "cold" if self.cold else "warm"
        print(f"turn {stream} {self.outcome} ({kind}): {parts}{', ' if parts else ''}total {total:.1f}s",
              file=sys.stderr, flush=True)


class ChildTurnError(Exception):
    """方針で turn を始める前に止めた。runtime は作らない。"""


@dataclass
class _Warm:
    key: str
    fingerprint: str
    runtime: Any
    pids: set[int]
    temps: list[tempfile.TemporaryDirectory[str]] = field(default_factory=list)
    touched: float = 0.0
    resume_id: str | None = None


@dataclass
class _LiveTurn:
    stream: int
    task: asyncio.Task
    pids: set[int]
    key: str | None


class ChildTurns:
    """1 本の mux の上の turn。リンクが切れたら実行中を止め、温めていた session も捨てる。"""

    def __init__(
        self, home: Path, send, received: list[dict] | None, *, session_ttl: float = SESSION_TTL_SECONDS,
        host_fingerprint: str | None = None,
    ) -> None:
        self._home = Path(home)
        self._send = send
        self._host_fingerprint = host_fingerprint
        self._relays: dict[int, ChildMcpProxy] = {}
        self._received = received
        self._session_ttl = session_ttl
        self._warm: dict[str, _Warm] = {}
        self._turns: dict[int, _LiveTurn] = {}
        self._conversation_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
        self._warm_changed = asyncio.Event()
        self._releases: set[asyncio.Task] = set()
        self._pid_sets: list[set[int]] = []
        self._down = False
        self._started = False
        self._sweeper: asyncio.Task | None = None

    def start(self) -> None:
        """Popen の追跡と、idle TTL の掃除を始める。"""
        if self._started:
            return
        self._started = True
        install_turn_popen()
        self._sweeper = asyncio.create_task(self._sweep())

    async def link_lost(self) -> None:
        relays = tuple(self._relays.items())
        for _, relay in relays:
            relay.disconnect()
        # A delivered HTTP response can still be waiting to be read by the CLI.
        tasks = [self._turns[stream].task for stream, _ in relays if stream in self._turns]
        if tasks:
            await asyncio.wait(tasks, timeout=0.75)

    def _open_relay(self, stream: int, payload: dict) -> ChildMcpProxy | None:
        preset = _mapping(payload.get("preset"))
        raw = preset.get("mcp_refs") or []
        if not isinstance(raw, list) or not all(isinstance(ref, str) for ref in raw):
            raise ChildTurnError("invalid mcp_refs")
        names = {mcp_reference(ref)[1] for ref in raw if ref.startswith("host:")}
        publications = mcp_versions(payload.get("host_mcps"))
        if not names and not publications:
            return None
        grant = payload.get("host_mcp_grant")
        if not sys.platform.startswith("linux") or set(publications) != names or not valid_version(grant):
            raise ChildTurnError("host MCP publication is missing or unavailable on this child")
        try:
            current_pin = load_host_pin(self._home).fingerprint
        except ValueError as exc:
            raise ChildTurnError("host MCP relay requires an enrolled host") from exc
        fingerprint = self._host_fingerprint
        if not fingerprint or current_pin != fingerprint:
            raise ChildTurnError("host MCP relay host fingerprint changed")
        approved = approved_versions(self._home, fingerprint)
        for name, version in publications.items():
            if approved.get(name) != version:
                raise ChildTurnError(f"host MCP '{name}' requires child owner approval for the current published version")
        return ChildMcpProxy(
            self._home, fingerprint, stream, self._send, publications, grant,
            turn_timeout=relay_turn_timeout(preset.get("timeout_seconds")),
        )

    def begin_shutdown(self) -> None:
        """リンク断と close の両方から呼ぶ。実行中のプロセスツリーを殺し、turn を取り消す。"""
        if self._down:
            return
        self._down = True
        for pids in self._pid_sets:
            kill_pids(pids)
        for warm in self._warm.values():
            kill_pids(warm.pids)
        for live in self._turns.values():
            if not live.task.cancelling():
                live.task.cancel()
        if self._sweeper is not None:
            self._sweeper.cancel()

    async def finish(self) -> None:
        """turn の終了を待ち、温めていた runtime と一時ディレクトリを捨てる。"""
        self.begin_shutdown()
        tasks = [live.task for live in self._turns.values()] + list(self._releases)
        if self._sweeper is not None:
            tasks.append(self._sweeper)
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        for key in list(self._warm):
            await self._drop(key)
        if self._started:
            uninstall_turn_popen()
            self._started = False

    def handle(self, frame) -> asyncio.Task | None:
        """turn / cancel / release を待たずに受ける。"""
        if frame.kind == "mcp_response":
            if relay := self._relays.get(frame.stream):
                relay.receive(frame.payload)
            return None
        if frame.kind == "cancel":
            self._cancel(int(frame.stream))
            return None
        if frame.kind == "release":
            self._cancel(int(frame.stream))
            key = frame.payload.get("conversation_key") if isinstance(frame.payload, dict) else None
            if isinstance(key, str):
                task = asyncio.create_task(self._release(key))
                self._releases.add(task)
                task.add_done_callback(self._releases.discard)
                return task
            return None
        if frame.kind != "turn" or self._down:
            return None
        stream = int(frame.stream)
        payload = frame.payload if isinstance(frame.payload, dict) else {}
        key = _conversation_key(payload)
        task = asyncio.create_task(self._run_turn(stream, payload, key))
        # どの集合を止めるかは、runtime を温存するかが分かってから決める。それまでは空。
        live = self._turns[stream] = _LiveTurn(stream, task, set(), key)

        def forget(_task: asyncio.Task) -> None:
            if self._turns.get(stream) is live:
                self._turns.pop(stream)
                self._warm_changed.set()

        task.add_done_callback(forget)
        return task

    def _conversation_lock(self, key: str) -> asyncio.Lock:
        lock = self._conversation_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._conversation_locks[key] = lock
        return lock

    async def _run_turn(self, stream: int, payload: dict, key: str | None) -> None:
        if key is None:
            await self._handle_turn(stream, payload)
        else:
            async with self._conversation_lock(key):
                await self._handle_turn(stream, payload)

    async def _release(self, key: str) -> None:
        async with self._conversation_lock(key):
            await self._drop(key)

    def _bind_pids(self, stream: int, pids: set[int]) -> None:
        live = self._turns.get(stream)
        if live is not None:
            live.pids = pids

    def _open_pids(self, stream: int) -> set[int]:
        """新しい runtime の集合。温存した session の 2 回目以降では作らない。"""
        pids = new_turn_pids()
        self._pid_sets.append(pids)
        self._bind_pids(stream, pids)
        return pids

    def _release_pids(self, pids: set[int]) -> None:
        """集合を止めて表から外す。中身が同じ別の集合は、同一性で残す。"""
        kill_pids(pids)
        self._pid_sets = [item for item in self._pid_sets if item is not pids]

    def _cancel(self, stream: int) -> None:
        live = self._turns.get(stream)
        if live is None or live.task.cancelling():
            return
        kill_pids(live.pids)
        live.task.cancel()

    async def _sweep(self) -> None:
        while not self._down:
            self._warm_changed.clear()
            now = time.monotonic()
            busy = {live.key for live in self._turns.values()}
            deadlines = {key: warm.touched + self._session_ttl for key, warm in self._warm.items() if key not in busy}
            if not deadlines:
                await self._warm_changed.wait()
                continue
            deadline = min(deadlines.values())
            if deadline > now:
                try:
                    await asyncio.wait_for(self._warm_changed.wait(), deadline - now)
                except TimeoutError:
                    pass
                continue
            for key, expires in deadlines.items():
                # Dropping a preceding session can suspend and allow a new turn to arrive.
                if expires <= now and not any(live.key == key for live in self._turns.values()):
                    await self._release(key)

    async def _handle_turn(self, stream: int, payload: dict) -> None:
        from .provider_data_child import use_approved

        key = _conversation_key(payload)
        with use_approved(self._home):
            await self._handle_turn_body(stream, payload, key, _TurnTiming())

    async def _handle_turn_body(self, stream: int, payload: dict, key: str | None, timing) -> None:
        token = strict = relay = env_token = None
        try:
            if self._received is not None:
                self._received.append(json.loads(json.dumps(payload)))
            keep_warm = False
            try:
                local_llm = _mapping(payload.get("route")).get("driver") == "openai.chat"
                if local_llm:
                    prompt = _local_llm_prompt(payload.get("prompt"))
                else:
                    prompt = _prompt_from_payload(payload.get("prompt"))
                    relay = self._open_relay(stream, payload)
                if relay is not None:
                    self._relays[stream] = relay
                    try:
                        endpoints = await relay.start()
                    except OSError as exc:
                        raise ChildTurnError("Could not start the local MCP relay") from exc
                    env_token = push_relay_env(relay.auth_env)
                    prepared = _prepare(self._home, payload, relay_mcps=endpoints)
                else:
                    prepared = _prepare(self._home, payload)
            except ChildTurnError as exc:
                timing.outcome = "refused"
                await self._emit_error(stream, str(exc), RuntimeResult(text=""))
                return
            timing.mark("prepare")
            guard = ChildOutputGuard(
                prepared.get("output_guard_env", prepared["provider"]),
                extra_values=(relay.token.removeprefix("Bearer "),) if relay is not None else (),
            )
            warm = self._warm.get(key) if key else None
            matches = warm is not None and warm.fingerprint == prepared["fingerprint"]
            reused = relay is None and matches and warm.runtime is not None
            if relay is not None and matches:
                prepared["resume_id"] = warm.resume_id
            if reused:
                prepared["cleanup"]()
                assert warm is not None
                warm.touched = time.monotonic()
                pids = warm.pids
                self._bind_pids(stream, pids)
            else:
                if key and warm is not None:
                    try:
                        await self._drop(key)
                    except (asyncio.CancelledError, Exception):
                        prepared["cleanup"]()
                        raise
                pids = self._open_pids(stream)
            token = _TURN_PIDS.set(pids)
            if prepared.get("builder_options", {}).get("child_data_capability") == "unrestricted":
                strict = STRICT_WINDOWS_JOBS.set(True)
            timing.cold = not reused
            if not reused:
                try:
                    runtime = await _build_runtime(prepared)
                    timing.mark("start")
                except asyncio.CancelledError:
                    self._release_pids(pids)
                    prepared["cleanup"]()
                    raise
                except Exception as exc:
                    self._release_pids(pids)
                    prepared["cleanup"]()
                    await self._emit_error(stream, str(exc), RuntimeResult(text=""), guard=guard)
                    return
                warm = _Warm(
                    key=key or "",
                    fingerprint=prepared["fingerprint"],
                    runtime=runtime,
                    pids=pids,
                    temps=prepared["temps"],
                    touched=time.monotonic(),
                )
                if key and relay is None:
                    self._warm[key] = warm
            items = None
            try:
                items = warm.runtime.stream(prompt)
                async for item in items:
                    timing.first_output()
                    if self._down:
                        return
                    if isinstance(item, str):
                        if text := guard.delta(item):
                            await self._send(stream, "delta", {"text": text})
                    elif isinstance(item, RuntimeEvent):
                        await self._send(stream, "event", guard.redact({
                            "kind": item.kind,
                            "tool_name": item.tool_name,
                            "summary": item.summary,
                            "payload": item.payload if isinstance(item.payload, dict) else {},
                        }))
                    elif isinstance(item, RuntimeResult):
                        timing.outcome = "ok"
                        if tail := guard.flush():
                            await self._send(stream, "delta", {"text": tail})
                        await self._send(stream, "result", guard.redact(_result_payload(item)))
                        if key and relay is None:
                            warm.touched = time.monotonic()
                            keep_warm = True
                        elif key and item.runtime_session_id:
                            self._warm[key] = _Warm(
                                key, warm.fingerprint, None, set(),
                                touched=time.monotonic(), resume_id=item.runtime_session_id,
                            )
                        return
            except RuntimeTurnError as exc:
                timing.outcome = "error"
                await self._emit_error(stream, str(exc), exc.partial_result, exc.diagnostics, exc.retryable, guard=guard)
            except asyncio.CancelledError:
                timing.outcome = "cancelled"
                # A cancelled send can leave the runtime suspended at its last yield.
                if (close := getattr(items, "aclose", None)) is not None:
                    await close()
                raise
            finally:
                if not keep_warm:
                    if key and relay is None:
                        await self._drop(key)
                    else:
                        await self._discard(warm)
        finally:
            if env_token is not None:
                pop_relay_env(env_token)
            if relay is not None:
                self._relays.pop(stream, None)
                await relay.close()
            timing.report(stream)
            if strict is not None:
                STRICT_WINDOWS_JOBS.reset(strict)
            if token is not None:
                _TURN_PIDS.reset(token)

    async def _emit_error(
        self,
        stream: int,
        message: str,
        partial: RuntimeResult,
        diagnostics: dict | None = None,
        retryable: bool | None = None,
        *,
        guard: ChildOutputGuard | None = None,
    ) -> None:
        safe: dict[str, str | int | bool] = {}
        for name, value in (diagnostics or {}).items():
            if isinstance(value, (str, int, bool)):
                safe[str(name)] = value
        payload = {
            "message": message,
            "partial_result": _result_payload(partial),
            "diagnostics": safe,
            "retryable": bool(retryable) if retryable is not None else False,
        }
        await self._send(stream, "error", guard.redact(payload) if guard is not None else payload)

    async def _drop(self, key: str) -> None:
        warm = self._warm.pop(key, None)
        if warm is None:
            return
        await self._discard(warm)

    async def _discard(self, warm: _Warm) -> None:
        disconnect = getattr(warm.runtime, "disconnect", None)
        try:
            if disconnect is not None:
                await disconnect()
        except Exception:
            pass
        finally:
            # disconnect のあとで、runtime が残した木を止める。Windows では Job を閉じるのと同じ。
            self._release_pids(warm.pids)
            _cleanup(warm.temps)


def _conversation_key(payload: dict) -> str | None:
    key = payload.get("conversation_key")
    if key is None:
        return None
    text = str(key).strip()
    return text or None


def _result_payload(result: RuntimeResult) -> dict:
    structured = result.structured_output
    if not isinstance(structured, (dict, list, str, int, float, bool)) and structured is not None:
        structured = None
    return {
        "text": result.text,
        "usage": result.usage if isinstance(result.usage, dict) else None,
        "runtime_session_id": result.runtime_session_id,
        "provider": result.provider,
        "structured_output": structured,
    }


def _prompt_from_payload(payload: object) -> RuntimePrompt:
    if not isinstance(payload, dict):
        return ""
    if "parts" not in payload:
        return str(payload.get("text") or "")
    parts: list[RuntimeTextInput | RuntimeImageInput] = []
    for part in payload.get("parts") or []:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "image":
            parts.append(RuntimeImageInput(str(part.get("url") or ""), part.get("detail")))
        else:
            parts.append(RuntimeTextInput(str(part.get("text") or "")))
    return parts


def _local_llm_prompt(payload: object) -> RuntimePrompt:
    if isinstance(payload, dict):
        if payload.keys() == {"text"} and isinstance(payload["text"], str):
            return payload["text"]
        if payload.keys() == {"messages"} and isinstance(messages := payload["messages"], list) and messages:
            if all(
                isinstance(item, list) and len(item) == 2
                and isinstance(item[0], str) and item[0] in ("system", "user", "assistant")
                and isinstance(item[1], str)
                for item in messages
            ):
                return RuntimeChatHistory(tuple((role, text) for role, text in messages))
    raise ChildTurnError("Local LLM connections support only text chat")


def _local_llm_url(payload: dict, connection: dict, route: dict, preset: dict, catalog: dict[str, LocalLLM]) -> str:
    if (
        payload.keys() - {"alias", "conversation_key", "cwd", "prompt", "preset", "route", "connection", "prompts", "skills"}
        or connection.keys() - LOCAL_LLM_CONNECTION_FIELDS
        or route.keys() - {"driver", "model", "connection"}
        or preset.keys() - LOCAL_LLM_PRESET_FIELDS
    ):
        raise ChildTurnError("Local LLM turns refuse host-supplied URLs, credentials, headers, and other extra fields")
    name = connection.get("local_llm")
    if connection.get("kind") != "local_llm" or connection.get("provider") != "openai_compat" or not valid_local_llm_name(name):
        raise ChildTurnError("child openai.chat requires a named Local LLM connection")
    entry = catalog.get(name)
    if entry is None:
        raise ChildTurnError(f"Local LLM connection '{name}' is not allowed by child policy")
    if route.get("model") not in entry.models:
        raise ChildTurnError(f"model is not allowed for Local LLM connection '{name}' by child policy")
    return completion_url(entry.url)


def supports_component_turn(driver: str) -> bool:
    """SDK コンポーネントが子機 turn の契約を持つか返す。"""
    return registry.provider_for_driver(driver) is not None


def _prepare(home: Path, payload: dict, *, relay_mcps: dict[str, dict] | None = None) -> dict:
    """方針に合う cwd と MCP 名だけを残し、builder 用の設定をメモリ上に組む。"""
    try:
        policy = load_policy(home / "policy.json")
    except ValueError as exc:
        raise ChildTurnError(str(exc)) from exc
    preset = _mapping(payload.get("preset"))
    route = _mapping(payload.get("route"))
    connection = _mapping(payload.get("connection"))
    driver = str(route.get("driver") or "")
    builder_options = {}
    if driver == "openai.chat":
        builder_options["loopback_url"] = _local_llm_url(payload, connection, route, preset, policy.local_llm)
        timeout = preset.get("timeout_seconds")
        if timeout is not None:
            if type(timeout) not in (int, float) or timeout != timeout:
                raise ChildTurnError("Local LLM timeout_seconds must be a number")
            preset["timeout_seconds"] = min(3600, max(1, timeout))
    else:
        connection = _strip_connection(connection)
    _reject_launch_definitions(preset)
    raw_mcp_refs = preset.get("mcp_refs") or []
    if not isinstance(raw_mcp_refs, list) or not all(isinstance(ref, str) for ref in raw_mcp_refs):
        raise ChildTurnError("invalid mcp_refs")
    scoped_mcps = [mcp_reference(ref) for ref in raw_mcp_refs]
    mcp_refs = [name for _, name in scoped_mcps]
    declared = None
    capability = None
    approval_generation = None
    profile_id = route.get("profile")
    if isinstance(profile_id, str):
        from .cli_install_spec import platform_key
        from .print_cli_profiles import get_print_cli_profile

        try:
            profile = get_print_cli_profile(profile_id)
            declared = profile.data_declaration
            if profile.provider == "grok" and platform_key().startswith("windows-"):
                raise ChildTurnError("Windows Grok requires an approved unrestricted declaration")
        except KeyError:
            declared = None
    if declared is None and payload.get("declaration_sha256"):
        raise ChildTurnError("unapproved or changed provider declaration")
    if declared is not None:
        from .cli_install_spec import platform_key
        from .provider_data import UNRESTRICTED, child_capability, require_unrestricted_alias
        from .provider_data_child import approved_for

        names = {declared.id, declared.profile_id, *declared.aliases}
        if names.isdisjoint(policy.adapters):
            raise ChildTurnError(f"provider '{declared.id}' is not allowed")
        if payload.get("declaration_sha256") != declared.sha256:
            raise ChildTurnError("unapproved or changed provider declaration")
        surface = _mapping(preset.get("agent")).get("surface", "read")
        if surface not in ("read", "write"):
            raise ChildTurnError("data provider alias requires a read or write surface")
        if surface == "read" and raw_mcp_refs:
            raise ChildTurnError("data provider mcp_refs require a write surface")
        target = declared.platform(platform_key())
        if target is None:
            raise ChildTurnError("no artifact for this platform")
        if target.supports(UNRESTRICTED):
            try:
                require_unrestricted_alias(
                    preset.get("on"), surface, _mapping(preset.get("allow")).get("full"), driver,
                    tools=preset.get("tools"), deny=preset.get("deny"),
                )
            except ValueError as exc:
                raise ChildTurnError(str(exc)) from exc
            capability = UNRESTRICTED
        else:
            capability = child_capability(surface, has_mcp=bool(raw_mcp_refs))
            if surface == "write" and (driver != "cli.acp" or not target.supports("write")):
                raise ChildTurnError("data provider write requires a sandboxed ACP write_argv")
            if not target.supports(capability):
                raise ChildTurnError("data provider mcp_refs require MCP capability")
        if payload.get("capability") != capability:
            raise ChildTurnError("data provider capability must match the alias surface")
        if not all(map(valid_data_mcp_name, mcp_refs)):
            raise ChildTurnError("data provider MCP names in mcp_refs must match ^[A-Za-z0-9][A-Za-z0-9-]*$")
        if surface == "write":
            try:
                approval_generation = approved_for(home, declared, capability).generation
            except (OSError, ValueError) as exc:
                raise ChildTurnError(str(exc)) from exc
            builder_options["child_data_capability"] = capability
    elif driver != "openai.chat" and driver not in policy.adapters:
        raise ChildTurnError(f"adapter '{driver}' is not allowed")
    try:
        validate_mcp_references(raw_mcp_refs)
    except ValueError as exc:
        raise ChildTurnError(str(exc)) from exc
    from .child_components import providers

    component = next((row for row in providers() if row["packages"] and row["driver"] == driver), None)
    if component and not supports_component_turn(driver):
        raise ChildTurnError(f"adapter '{driver}' does not support child turns")
    provider = child_provider(route)
    login_home = None
    if provider is not None:
        spec = provider.turn
        if connection.get("kind") != spec.connection_kind or connection.get("provider") != provider.id:
            raise ChildTurnError(f"child {provider.name} requires a {spec.connection_kind} connection")
        spec.project(route, preset)
        login_home = bind_login_home(connection, spec.login_home)
        try:
            if options := spec.options:
                cap = options.sandbox
                sandbox_max = getattr(policy, cap.policy_key, None) if cap is not None else None
                preset[options.field] = options.local(preset[options.field], sandbox_max=sandbox_max, display_name=provider.name)
            builder_options.update(child_builder_options(home, spec.launch))
        except (ValueError, FileNotFoundError) as exc:
            raise ChildTurnError(str(exc)) from exc
    relay_mcps = relay_mcps or {}
    if relay_mcps and not (
        provider is not None and provider.id in {"claude", "codex"} or declared is not None and driver == "cli.acp"
    ):
        raise ChildTurnError("host MCP references require Claude, Codex, or HTTP-capable data-declared ACP")
    if relay_mcps and provider is not None and provider.id == "claude":
        preset["strict_mcp_config"] = True
    mcps = {}
    for scope, name in scoped_mcps:
        if scope == "host":
            if name not in relay_mcps:
                raise ChildTurnError(f"host MCP '{name}' is unavailable")
            mcps[name] = relay_mcps[name]
        else:
            if name not in policy.mcp:
                raise ChildTurnError(f"unknown MCP '{name}'")
            mcps[name] = _policy_mcp(policy.mcp[name])
    temps: list[tempfile.TemporaryDirectory[str]] = []
    cwd_text = payload.get("cwd")
    if cwd_text is None or cwd_text == "":
        if builder_options.get("child_data_capability"):
            raise ChildTurnError("data provider write requires a cwd inside workspace_roots")
        resolved = None
    else:
        alias_roots = (payload.get("preset") or {}).get("cwd_allowed_roots") or []
        resolved = _resolve_cwd(str(cwd_text), policy, alias_roots=alias_roots)
    if declared is not None and driver == "cli.acp":
        from .provider_data_runtime import DeclarationRejected, checked_write_cwd, require_acp_landlock

        try:
            if capability in ("write", "mcp", UNRESTRICTED):
                assert resolved is not None
                resolved = checked_write_cwd(resolved, home)
            acp_spec = declared.acp
            if capability == UNRESTRICTED:
                acp_spec = declared.acp_unrestricted
            elif capability in ("write", "mcp"):
                acp_spec = declared.acp_write
            require_acp_landlock(acp_spec)
        except DeclarationRejected as exc:
            raise ChildTurnError(str(exc)) from exc
    skill_format = payload.get("skill_format")
    provider_format = provider.turn.skill_format if provider is not None else None
    if "skill_format" in payload:
        if skill_format not in (*SDK_SKILL_FORMATS, PROMPT_PREFIX_SKILL_FORMAT) or skill_format != provider_format:
            raise ChildTurnError("invalid skill format for child provider")
        prompts, skills = payload.get("prompts"), payload.get("skills")
    else:
        prompts = [str(item) for item in payload.get("prompts") or []]
        skills = [] if provider_format in ("none", PROMPT_PREFIX_SKILL_FORMAT) else _skills(payload.get("skills"))
    native_skills = skill_format in SDK_SKILL_FORMATS
    prefix = None
    folders: tuple[SkillFolder, ...] = ()
    try:
        if skill_format == PROMPT_PREFIX_SKILL_FORMAT:
            prefix = CliInstructions.from_payload(prompts, skills, payload.get("skill_folders", []))
            if declared is not None and declared.native_skill_layout is not None and platform_key() == "linux-x64":
                builder_options["instructions"] = prefix
        elif provider_format in SDK_SKILL_FORMATS:
            if "skill_folders" in payload:
                if not native_skills or skills != []:
                    raise ValueError("native skill folders require a supported format and no legacy skills")
                folders = decode_skill_folders(payload["skill_folders"])
                validate_native_folders(folders)
            validate_turn_files(prompts, skills, legacy=not native_skills)
        elif "skill_folders" in payload:
            raise ValueError("skill folders require a supported skill format")
    except ValueError as exc:
        raise ChildTurnError(str(exc)) from exc
    alias = str(payload.get("alias") or "")
    route_name = str(preset.get("route") or "route")
    connection_name = str(route.get("connection") or "connection")
    try:
        if resolved is None:
            temp = tempfile.TemporaryDirectory(prefix="kotoyose-child-chat-")
            temps.append(temp)
            resolved = Path(temp.name)
        files = tempfile.TemporaryDirectory(prefix="kotoyose-child-prompt-")
        temps.append(files)
        root = Path(files.name)
        if prefix is not None and "instructions" not in builder_options:
            prompts_to_write = [prefix.place(root / "skills")]
        else:
            prompts_to_write = prompts
        prompt_files, prompt_refs = _write_prompts(root, prompts_to_write)
        staged_skills = [] if prefix else skills
        if provider_format == "codex_skill_input" and not native_skills:
            # The legacy wire sends the body only. Codex discovery requires a description.
            staged_skills = [
                {
                    "name": skill["name"],
                    "text": (
                        f'---\nname: {json.dumps(skill["name"], ensure_ascii=False)}\n'
                        f'description: Instructions for the Kotoyose alias.\n---\n\n{skill["text"]}'
                    ),
                }
                for skill in skills
            ]
        native_folders = () if prefix else folders or tuple(
            SkillFolder(skill["name"], {"SKILL.md": skill["text"].encode("utf-8")}) for skill in staged_skills
        )
        names = [folder.name for folder in native_folders]
        plugin = write_skill_plugin(root / "plugin", native_folders) if names else None
        skill_refs = [] if native_skills else names
        preset_body = _child_preset(
            preset, cwd=str(resolved), prompt_refs=prompt_refs, skill_refs=skill_refs,
            plugins=[str(plugin)] if plugin is not None else [], mcp_refs=mcp_refs,
        )
        if native_skills and names:
            preset_body["skills"] = names
        config_body = {
            "connections": {connection_name: connection},
            "routes": {route_name: {**route, "connection": connection_name}},
            "models": {alias: preset_body},
            "mcps": mcps,
            "prompt_files": prompt_files,
            "skill_definitions": _skill_definitions(plugin, skill_refs),
        }
        with use_child_mcp_projection():
            config = HarnessConfig.model_validate(config_body)
        config._child_mcp_projection = True
    except Exception as exc:
        _cleanup(temps)
        if isinstance(exc, ValidationError):
            errors = exc.errors(include_input=False, include_context=False, include_url=False)
            error_types = [error["type"] for error in errors]
        else:
            error_types = [type(exc).__name__]
        log.warning("child.turn.invalid_configuration", error_types=error_types)
        raise ChildTurnError("Invalid child turn configuration.") from None
    fingerprint = hashlib.sha256(json.dumps({
        "alias": alias,
        "cwd": str(resolved) if cwd_text else None,
        "preset": {key: value for key, value in preset.items() if key not in _PRESET_DROP},
        "route": route,
        "connection": connection,
        "prompts": prompts,
        "skill_format": skill_format,
        "skills": skills,
        "skill_folders": payload.get("skill_folders"),
        "mcps": {name: definition for name, definition in mcps.items() if name not in relay_mcps},
        "host_mcps": payload.get("host_mcps"),
        "login_home": login_home,
        "builder_options": {key: value for key, value in builder_options.items() if key != "instructions"},
        "declaration_sha256": declared.sha256 if declared is not None else None,
        "capability": capability,
        "approval_generation": approval_generation,
    }, sort_keys=True, default=str).encode("utf-8")).hexdigest()

    def cleanup() -> None:
        _cleanup(temps)

    return {
        "alias": alias,
        "provider": driver_manifest(driver).provider,
        "config": config,
        "cwd": str(resolved),
        "builder_options": builder_options,
        "output_guard_env": provider.turn.output_guard_env if provider is not None else (),
        "fingerprint": fingerprint,
        "temps": temps,
        "cleanup": cleanup,
    }


def _cleanup(temps: list[tempfile.TemporaryDirectory[str]]) -> None:
    for temp in temps:
        temp.cleanup()
    temps.clear()


async def _build_runtime(prepared: dict):
    """最初の turn で既存の runtime builder を遅延登録する。"""
    config: HarnessConfig = prepared["config"]
    alias = prepared["alias"]
    with use_config(config):
        effective = config.preset_for(alias)
        provider = effective.provider
        spec = registry.runtime_providers.get(provider)
        child_builder = registry.child_builder(provider)
        cwd = prepared["cwd"]
        options = prepared.get("builder_options", {})
        resume = prepared.get("resume_id")
        loopback_url = options.get("loopback_url")
        route = config.routes.get(effective.route)
        local_llm_driver = route is not None and route.driver == "openai.chat"
        if local_llm_driver != (provider == "openai_compat") or (not local_llm_driver and "loopback_url" in options):
            raise ChildTurnError("Local LLM driver/provider mismatch")
        if local_llm_driver:
            if not isinstance(loopback_url, str) or not loopback_url:
                raise ChildTurnError("Local LLM runtime requires a policy-approved loopback URL")
            from .runtime.openai_chat import build_loopback_chat_session

            return build_loopback_chat_session(effective, config=config, loopback_url=loopback_url)
        if spec is None and child_builder is None and provider == "cli_acp":
            from .runtime.acp import build_acp_session

            return build_acp_session(effective, cwd, resume, None, **options)
        if spec is None and child_builder is None:
            importlib.import_module("kotoyose.session")
            spec = registry.runtime_providers.get(provider)
        # child 専用の builder を先に使う。host の builder は child の引数 (cli_path など) を知らない。
        if child_builder is not None:
            if resume is not None:
                options = {**options, "resume_id": resume}
            built = child_builder(alias, effective, cwd, **options)
        elif spec is not None:
            built = spec.builder(effective, cwd, resume, None, **options)
        else:
            raise ChildTurnError(f"adapter provider '{provider}' is not available")
        return await built if asyncio.iscoroutine(built) else built


def _inside_any(resolved: Path, roots) -> bool:
    for root in roots:
        try:
            root_resolved = Path(root).resolve(strict=True)
        except (OSError, TypeError):
            continue
        if resolved.is_relative_to(root_resolved):
            return True
    return False


def _long_path(path: str) -> str:
    """8.3 短縮名を GetLongPathNameW で伸ばす。Windows 以外のテストはこれを差し替える。"""
    if sys.platform != "win32":
        return path
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetLongPathNameW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
    kernel.GetLongPathNameW.restype = ctypes.c_uint32
    size = 32768
    buffer = ctypes.create_unicode_buffer(size)
    written = kernel.GetLongPathNameW(str(path), buffer, size)
    if written == 0:
        raise OSError(ctypes.get_last_error(), "GetLongPathNameW failed")
    if written > size:
        buffer = ctypes.create_unicode_buffer(written)
        written = kernel.GetLongPathNameW(str(path), buffer, written)
        if written == 0:
            raise OSError(ctypes.get_last_error(), "GetLongPathNameW failed")
    return buffer.value


def _realpath(path: str) -> str:
    """junction を含む実体の path。存在しなければ OSError。"""
    resolved = Path(path).resolve(strict=True)
    if not resolved.is_dir():
        raise NotADirectoryError(path)
    return str(resolved)


def _normalize_windows_cwd(path: str) -> str:
    return normalize_windows_path(path, long_path=_long_path, realpath=_realpath)


def _inside_windows(key: str, roots) -> bool:
    for root in roots:
        try:
            root_key = windows_path_key(_normalize_windows_cwd(str(root)))
        except OSError:
            continue
        if key == root_key or key.startswith(root_key + "\\"):
            return True
    return False


def _resolve_cwd_windows(requested: str, policy, *, alias_roots=()) -> Path:
    """Windows の cwd。8.3、junction、`\\\\?\\`、UNC を解決してから方針の root と比べる。"""
    try:
        resolved = _normalize_windows_cwd(requested)
    except OSError as exc:
        raise ChildTurnError(f"cwd does not exist: {requested}") from exc
    key = windows_path_key(resolved)
    if not _inside_windows(key, policy.workspace_roots):
        raise ChildTurnError(f"cwd is outside workspace_roots: {requested}")
    if not _inside_windows(key, alias_roots):
        raise ChildTurnError(f"cwd is outside the alias cwd_allowed_roots: {requested}")
    return Path(resolved)


def _resolve_cwd(requested: str, policy, *, alias_roots=()) -> Path:
    """cwd を child の filesystem で解決し、方針ファイルの root と alias の許可 root の両方の中に限る。"""
    if sys.platform == "win32":
        return _resolve_cwd_windows(requested, policy, alias_roots=alias_roots)
    path = Path(requested)
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ChildTurnError(f"cwd does not exist: {requested}") from exc
    if not resolved.is_dir():
        raise ChildTurnError(f"cwd is not a directory: {requested}")
    if not _inside_any(resolved, policy.workspace_roots):
        raise ChildTurnError(f"cwd is outside workspace_roots: {requested}")
    if not _inside_any(resolved, alias_roots):
        raise ChildTurnError(f"cwd is outside the alias cwd_allowed_roots: {requested}")
    return resolved


def _mapping(value: object) -> dict:
    if isinstance(value, dict):
        return dict(value)
    return {}


def _strip_connection(connection: dict) -> dict:
    """host が紛れ込ませた secret と login の場所は、child の設定に入れない。"""
    return {key: value for key, value in connection.items() if key in CHILD_CONNECTION_FIELDS}


def _reject_launch_definitions(preset: dict) -> None:
    servers = preset.get("mcp_servers") or {}
    if isinstance(servers, dict) and servers:
        raise ChildTurnError("child aliases cannot set mcp_servers")
    preset.pop("mcp_servers", None)
    preset.pop("plugins", None)


def _policy_mcp(spec: dict) -> dict:
    try:
        command, args = resolve_windows_shim(str(spec["command"]), [str(item) for item in spec.get("args") or []])
    except ValueError as exc:
        raise ChildTurnError(str(exc)) from exc
    server: dict[str, object] = {"command": command, "args": args}
    env = spec.get("env")
    if isinstance(env, dict) and env:
        server["env"] = dict(env)
    return {"description": spec.get("description") or "", "server": server}


def _skills(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    skills: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        try:
            validate_legacy_skill_name(name)
        except ValueError as exc:
            raise ChildTurnError(str(exc)) from exc
        skills.append({"name": name, "text": str(item.get("text") or "")})
    return skills


def _write_prompts(root: Path, prompts: list[str]) -> tuple[dict, list[str]]:
    files: dict[str, dict[str, str]] = {}
    refs: list[str] = []
    for index, text in enumerate(prompts):
        name = f"prompt-{index}"
        path = root / f"{name}.txt"
        path.write_bytes(text.encode("utf-8"))
        files[name] = {"path": str(path)}
        refs.append(name)
    return files, refs


def _skill_definitions(plugin: Path | None, names: list[str]) -> dict:
    if plugin is None:
        return {}
    return {name: {"plugin": str(plugin), "skill": name} for name in names}


_PRESET_DROP = {
    "cwd", "plugins", "mcp_servers", "prompt_file_refs", "skill_refs", "delegates",
    "provider", "model", "subscription_only", *CLAUDE_PRESET_OVERRIDES, *CODEX_PRESET_OVERRIDES,
}


def _child_preset(preset: dict, *, cwd: str, prompt_refs: list[str], skill_refs: list[str], plugins: list[str], mcp_refs: list[str]) -> dict:
    body = {key: value for key, value in preset.items() if key not in _PRESET_DROP}
    body["cwd"] = cwd
    body["allow_cwd_override"] = True
    body["cwd_allowed_roots"] = [cwd]
    body["mcp_refs"] = mcp_refs
    body["mcp_servers"] = {}
    body["plugins"] = plugins
    body["skill_refs"] = skill_refs
    body["prompt_file_refs"] = prompt_refs
    return body
