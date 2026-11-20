"""Antigravity CLI provider adapter。

Google AI Pro / Ultra の subscription-backed quota を維持するため、公式 `agy`
CLI の headless protocol を subprocess 境界で利用する。最新版 CLI の
stream-json protocol を使い、MCP / Skills / Agents は workspace plugin overlay
として注入する。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shlex
import shutil
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..activity import emit_runtime_event
from ..logging_setup import log
from ..print_cli_agy import parse_agy_error as _parse_agy_error
from ..print_cli_events import (
    CANONICAL_STATUSES as _CANONICAL_STATUSES,
    TERMINAL_FAILURE_STATUSES as _TERMINAL_FAILURE_STATUSES,
    extract_session_id as _extract_session_id,
    extract_text_delta as _extract_text_delta,
    extract_usage as _extract_usage,
    parse_json_text as _parse_json_text,
    read_stderr_tail as _read_stderr_tail,
    runtime_event_from_step as _runtime_event_from_step,
    structured_error_fields as _structured_error_fields,
)
from .antigravity_probe import (
    MIN_ANTIGRAVITY_CLI_VERSION,
    probe_antigravity_cli,
)
from .antigravity_workspace import AntigravityWorkspaceOverlay
from .base import RuntimeEvent, RuntimePrompt, RuntimeResult, RuntimeTurnError, prompt_text
from .translation import apply_common_timeout, translate_runtime_config

_DEFAULT_TIMEOUT_SECONDS = 600.0
_STDERR_TAIL_LINES = 80
_STREAM_READER_LIMIT_BYTES = 4 * 1024 * 1024
_PROCESS_EXIT_TIMEOUT_SECONDS = 2.0
_MIN_CLI_VERSION = MIN_ANTIGRAVITY_CLI_VERSION


@dataclass(slots=True)
class AntigravityRuntimeSession:
    """`agy` headless session を kotoyose の RuntimeSession として扱う。"""

    model: str | None
    cwd: str
    config: dict[str, Any] = field(default_factory=dict)
    session_id: str | None = None
    provider: str = "antigravity"
    system_preamble: str | None = None
    overlay: AntigravityWorkspaceOverlay | None = None
    # 子の env。Kotoyose の環境 (secret を含む) を引き継がず、allowlist と login home だけ渡す。
    env: dict[str, str] | None = None

    _process: asyncio.subprocess.Process | None = field(default=None, init=False, repr=False)
    _stderr_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _stderr_tail: deque[str] = field(
        default_factory=lambda: deque(maxlen=_STDERR_TAIL_LINES),
        init=False,
        repr=False,
    )
    _turn_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _preamble_sent: bool = field(default=False, init=False, repr=False)
    _last_usage: dict[str, Any] | None = field(default=None, init=False, repr=False)
    invalidated: bool = field(default=False, init=False)

    async def disconnect(self) -> None:
        """永続 `agy` process と一時 workspace customization を解放する。"""
        proc = self._process
        self._process = None
        if proc is not None and proc.returncode is None:
            if proc.stdin is not None:
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    proc.stdin.close()
                    await proc.stdin.wait_closed()
            try:
                async with asyncio.timeout(2.0):
                    await proc.wait()
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()

        if self._stderr_task is not None:
            if not self._stderr_task.done():
                self._stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._stderr_task
            self._stderr_task = None

        if self.overlay is not None:
            self.overlay.cleanup()

    async def ask(self, prompt: RuntimePrompt) -> RuntimeResult:
        """Return the terminal result and preserve all normalized metadata."""
        final_result: RuntimeResult | None = None
        async for item in self.stream(prompt):
            if isinstance(item, RuntimeEvent):
                emit_runtime_event(item)
            elif isinstance(item, RuntimeResult):
                final_result = item
        if final_result is not None:
            return final_result
        raise RuntimeError("agy stream ended without a result")

    async def stream(
        self, prompt: RuntimePrompt
    ) -> AsyncIterator[str | RuntimeEvent | RuntimeResult]:
        """`agy` stream-json を OpenAI SSE / activity event へ正規化する。"""
        text = prompt_text(prompt)
        iterator = (
            self._stream_persistent(text)
            if self.config.get("persistent", True)
            else self._stream_one_shot(text)
        )
        try:
            async for item in iterator:
                yield item
        finally:
            await iterator.aclose()

    async def _stream_persistent(
        self, prompt: str
    ) -> AsyncIterator[str | RuntimeEvent | RuntimeResult]:
        async with self._turn_lock:
            proc = await self._ensure_process()
            completed = False
            try:
                if proc.stdin is None or proc.stdout is None:
                    raise RuntimeError("agy stream pipes are unavailable")

                self._stderr_tail.clear()
                prompt = self._with_preamble(prompt)
                message = {
                    "event": "user",
                    "message": {"content": prompt},
                }
                proc.stdin.write(
                    (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
                )
                await proc.stdin.drain()

                async for item in self._read_turn(proc.stdout, cumulative_usage=True):
                    yield item
                completed = True
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise RuntimeError(
                    self._process_error("agy persistent process closed before prompt")
                ) from exc
            finally:
                if not completed:
                    # 中断した stream の unread result を次 turn が回答として拾わないよう、
                    # process ごと無効化する。kill は await より先に行い、task cancellation
                    # の最中でも stale process を確実に止める。
                    await self._invalidate_process(proc)

    async def _stream_one_shot(
        self, prompt: str
    ) -> AsyncIterator[str | RuntimeEvent | RuntimeResult]:
        prompt = self._with_preamble(prompt)
        cmd = self._build_command(prompt=prompt, persistent=False)
        executable = shutil.which(cmd[0])
        if executable is None:
            raise RuntimeError("agy CLI が見つかりません。PATH に agy を追加してください")
        cmd[0] = executable
        await _verify_cli_version(self._version_command(executable))

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.cwd,
            env=self.env,
            limit=_STREAM_READER_LIMIT_BYTES,
        )
        assert proc.stdout is not None
        assert proc.stderr is not None
        stderr_task = asyncio.create_task(_read_stderr_tail(proc.stderr))
        final_result: RuntimeResult | None = None
        turn_error: RuntimeTurnError | None = None
        try:
            try:
                async for item in self._read_turn(proc.stdout, cumulative_usage=False):
                    # Completion is withheld until a clean process exit.
                    if isinstance(item, RuntimeResult):
                        final_result = item
                    else:
                        yield item
            except RuntimeTurnError as exc:
                if exc.terminal_reason == "idle_timeout":
                    raise
                turn_error = exc
            partial = (turn_error.partial_result if turn_error else final_result) or RuntimeResult(
                text="", provider=self.provider, runtime_session_id=self.session_id,
            )
            try:
                async with asyncio.timeout(_PROCESS_EXIT_TIMEOUT_SECONDS):
                    stderr = await stderr_task
                    code = await proc.wait()
            except TimeoutError:
                self.invalidated = True
                raise _turn_error(
                    partial, reason="process_exit_timeout",
                    message="agy が result 後に終了しませんでした",
                ) from None
            if code != 0 or turn_error:
                self.invalidated = True
                raise _turn_error(
                    partial, reason=str(turn_error.terminal_reason) if turn_error else "process_exit_error",
                    stderr=stderr, exit_code=code,
                    previous=turn_error,
                ) from None
            if final_result is not None:
                yield final_result
        finally:
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
            if not stderr_task.done():
                stderr_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stderr_task
            if proc.returncode is None:
                try:
                    async with asyncio.timeout(_PROCESS_EXIT_TIMEOUT_SECONDS):
                        await asyncio.shield(proc.wait())
                except TimeoutError:
                    log.warning("antigravity.one_shot_process_reap_timeout")
                except Exception:
                    pass

    async def _ensure_process(self) -> asyncio.subprocess.Process:
        if self._process is not None and self._process.returncode is None:
            return self._process

        cmd = self._build_command(prompt=None, persistent=True)
        executable = shutil.which(cmd[0])
        if executable is None:
            raise RuntimeError("agy CLI が見つかりません。PATH に agy を追加してください")
        cmd[0] = executable
        await _verify_cli_version(self._version_command(executable))

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.cwd,
            env=self.env,
            limit=_STREAM_READER_LIMIT_BYTES,
        )
        assert proc.stderr is not None
        self._stderr_tail.clear()
        # stream-json の usage は process 全体の累積値。child を再生成したら
        # counter も 0 から始まるため、前 process の基準値を持ち越さない。
        self._last_usage = None
        self._stderr_task = asyncio.create_task(self._drain_stderr(proc.stderr))
        self._process = proc
        return proc

    async def _drain_stderr(self, reader: asyncio.StreamReader) -> None:
        try:
            async for raw in reader:
                line = raw.decode("utf-8", errors="replace").rstrip()
                if line:
                    self._stderr_tail.append(line)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("antigravity.stderr_drain_failed", error=str(exc))

    async def _invalidate_process(self, proc: asyncio.subprocess.Process) -> None:
        """未完了 turn の process を停止し、session を再利用不可にする。"""
        self.invalidated = True
        if self._process is proc:
            self._process = None
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        if self.overlay is not None:
            self.overlay.cleanup()
        with contextlib.suppress(Exception):
            await proc.wait()
        if self._stderr_task is not None:
            if not self._stderr_task.done():
                self._stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._stderr_task
            self._stderr_task = None

    async def _read_turn(
        self,
        reader: asyncio.StreamReader,
        *,
        cumulative_usage: bool,
    ) -> AsyncIterator[str | RuntimeEvent | RuntimeResult]:
        timeout = _resolve_timeout(self.config)
        chunks: list[str] = []
        final_usage: dict | None = None

        while True:
            try:
                async with asyncio.timeout(timeout):
                    raw = await reader.readline()
            except TimeoutError:
                self.invalidated = True
                raise _turn_error(
                    RuntimeResult(text="".join(chunks), provider=self.provider, runtime_session_id=self.session_id),
                    reason="idle_timeout",
                    message=f"{self.provider} が {timeout:g} 秒間出力を返しませんでした (idle timeout)",
                ) from None

            if not raw:
                self.invalidated = True
                raise _turn_error(
                    RuntimeResult(text="".join(chunks), provider=self.provider, runtime_session_id=self.session_id),
                    reason="missing_terminal_result", stderr="\n".join(self._stderr_tail),
                    message="agy stream closed unexpectedly",
                )

            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            event = _parse_json_text(line)
            if event is None:
                log.debug("antigravity.stream_json_parse_skipped")
                continue

            event_name = str(event.get("event") or "").lower()

            if event_name == "init":
                session_id = _extract_session_id(event)
                if session_id:
                    self.session_id = session_id
                continue

            if event_name == "step_update":
                update = event.get("step_update")
                if not isinstance(update, dict):
                    continue
                session_id = _extract_session_id(update)
                if session_id:
                    self.session_id = session_id

                delta = update.get("text_delta")
                if isinstance(delta, str) and delta:
                    chunks.append(delta)
                    yield delta

                runtime_event = _runtime_event_from_step(update)
                if runtime_event is not None:
                    yield runtime_event
                continue

            if event_name == "result":
                result = event.get("result")
                payload = result if isinstance(result, dict) else event
                session_id = _extract_session_id(payload) or _extract_session_id(event)
                if session_id:
                    self.session_id = session_id

                response = payload.get("response")
                raw_usage = _extract_usage(payload)
                final_usage = self._usage_delta(raw_usage) if cumulative_usage else raw_usage
                status = str(payload.get("status") or "").upper()
                if status != "SUCCESS" or payload.get("error"):
                    self.invalidated = True
                    raise _turn_error(
                        RuntimeResult(
                            text=response if isinstance(response, str) and response else "".join(chunks),
                            usage=final_usage, runtime_session_id=self.session_id, provider=self.provider,
                        ),
                        reason="provider_error" if status in _TERMINAL_FAILURE_STATUSES else "invalid_terminal_result",
                        payload=payload, stderr="\n".join(self._stderr_tail),
                    )
                if isinstance(response, str) and response and not chunks:
                    chunks.append(response)
                    yield response

                yield RuntimeResult(
                    text=response if isinstance(response, str) and response else "".join(chunks),
                    usage=final_usage,
                    runtime_session_id=self.session_id,
                    provider=self.provider,
                )
                return

            # Forward-compatible: future event names are ignored, just like agy stdin
            # protocol ignores unknown message kinds.
            log.debug("antigravity.stream_event_ignored", event=event_name or "unknown")

    def _usage_delta(self, usage: dict | None) -> dict | None:
        if usage is None:
            return None
        previous = self._last_usage
        self._last_usage = dict(usage)
        if previous is None:
            return usage

        delta: dict[str, Any] = {}
        for key, value in usage.items():
            old = previous.get(key)
            if isinstance(value, int) and isinstance(old, int):
                delta[key] = max(0, value - old)
            else:
                delta[key] = value
        return delta

    def _with_preamble(self, prompt: str) -> str:
        if self.system_preamble and not self._preamble_sent and not self.session_id:
            self._preamble_sent = True
            return f"{self.system_preamble}\n\n{prompt}"
        self._preamble_sent = True
        return prompt

    def _build_command(self, prompt: str | None, *, persistent: bool) -> list[str]:
        command = self.config.get("command") or "agy"
        cmd = _command_parts(command)

        for arg in self.config.get("args") or []:
            if isinstance(arg, str):
                cmd.append(arg)

        if self.model:
            cmd.extend(["--model", self.model])

        effort = self.config.get("effort")
        if isinstance(effort, str) and effort in {"low", "medium", "high"}:
            cmd.extend(["--effort", effort])

        timeout = self.config.get("print_timeout") or self.config.get("timeout")
        if isinstance(timeout, str) and timeout:
            cmd.extend(["--print-timeout", timeout])

        project = self.config.get("project")
        if isinstance(project, str) and project:
            cmd.extend(["--project", project])

        agent = self.config.get("agent")
        if isinstance(agent, str) and agent:
            cmd.extend(["--agent", agent])

        conversation = self.session_id or self.config.get("conversation")
        if isinstance(conversation, str) and conversation:
            cmd.extend(["--conversation", conversation])
        elif self.config.get("continue"):
            cmd.append("--continue")

        add_dirs = self.config.get("add_dirs") or self.config.get("add_dir") or []
        if isinstance(add_dirs, str):
            add_dirs = [add_dirs]
        for directory in add_dirs:
            if isinstance(directory, str) and directory:
                cmd.extend(["--add-dir", directory])
        if self.config.get("new_project"):
            cmd.append("--new-project")

        if self.config.get("dangerously_skip_permissions"):
            cmd.append("--dangerously-skip-permissions")
        if self.config.get("sandbox"):
            cmd.append("--sandbox")

        for key, value in (
            (self.config.get("options") or {}).items()
            if isinstance(self.config.get("options"), dict)
            else []
        ):
            flag = f"--{str(key).replace('_', '-')}"
            if isinstance(value, bool):
                if value:
                    cmd.append(flag)
            elif value is not None:
                cmd.extend([flag, str(value)])

        if persistent:
            cmd.extend(["--input-format", "stream-json", "--output-format", "stream-json"])
        else:
            cmd.extend(["--output-format", "stream-json", "--print", prompt or ""])
        return cmd

    def _process_error(self, fallback: str) -> str:
        # Stderr can contain command output, prompts, and authentication data.
        return fallback

    def _version_command(self, executable: str) -> list[str]:
        command = _command_parts(self.config.get("command") or "agy")
        command[0] = executable
        return [*command, "--version"]


def build_antigravity_session(
    preset,
    cwd_override: str | None,
    session_id: str | None,
    request_headers: dict[str, str] | None = None,
):
    """ModelPreset から Antigravity runtime を構築する。

    `backend: cli` (既定) は `agy` の cached Google login を使うため Google AI
    Pro / Ultra subscription quota を維持する。`backend: sdk` は Gemini API key /
    Vertex 用の明示 opt-in で、subscription quota とは別経路。
    """
    backend = str((preset.antigravity or {}).get("backend") or "cli").lower()
    if backend == "sdk":
        from .antigravity_sdk import build_antigravity_sdk_session

        return build_antigravity_sdk_session(
            preset, cwd_override, session_id, request_headers
        )
    if backend != "cli":
        raise ValueError("antigravity.backend は 'cli' または 'sdk' を指定してください")

    from ..config import get_config

    cwd = os.path.expanduser(cwd_override) if cwd_override is not None else preset.cwd
    resolved_cwd = str(Path(cwd).expanduser())
    translated = translate_runtime_config(get_config(), preset, request_headers)

    config = apply_common_timeout(
        dict(preset.antigravity or {}), translated.timeout_seconds
    )
    if translated.effort is not None and "effort" not in config:
        if translated.effort in {"low", "medium", "high"}:
            config["effort"] = translated.effort
        else:
            log.warning(
                "antigravity.cli_effort_unsupported",
                effort=translated.effort,
                supported=["low", "medium", "high"],
            )

    native_plugin_paths = tuple(
        value
        for value in config.get("plugin_paths", [])
        if isinstance(value, str) and value
    )
    overlay = AntigravityWorkspaceOverlay(
        cwd=resolved_cwd,
        translated=translated,
        native_plugin_paths=native_plugin_paths,
    )
    try:
        overlay.prepare()
    except Exception:
        overlay.cleanup()
        raise

    if translated.unsupported_fields:
        log.warning(
            "antigravity.runtime_fields_unsupported",
            fields=list(translated.unsupported_fields),
        )

    return AntigravityRuntimeSession(
        model=preset.model,
        cwd=resolved_cwd,
        config=config,
        session_id=session_id or None,
        system_preamble=translated.instructions,
        overlay=overlay,
        env=_cli_env_for(preset),
    )


def _cli_env_for(preset) -> dict[str, str]:
    """agy に渡す env。route の connection に専用の login home があればそれを HOME に、
    無ければこの機械の HOME を使う。どちらも Kotoyose の環境変数は引き継がない。"""
    from ..config import get_config
    from ..print_cli_profiles import AGY_PROFILE, build_print_cli_env

    login_home: str | None = None
    route_name = getattr(preset, "route", None)
    if route_name:
        cfg = get_config()
        route = cfg.routes.get(route_name)
        conn = cfg.connections.get(route.connection) if route is not None else None
        if conn is not None and conn.config_dir is not None:
            login_home = os.path.expanduser(str(conn.config_dir))
    return build_print_cli_env(AGY_PROFILE, login_home or os.path.expanduser("~"))


async def _verify_cli_version(command: list[str]) -> None:
    """structured stream protocol を持つ `agy` だけを起動する。"""
    diagnostic = await probe_antigravity_cli(command)
    if diagnostic.error_code == "timeout":
        raise RuntimeError("agy --version が 5 秒以内に完了しませんでした") from None
    if not diagnostic.compatible:
        minimum = ".".join(str(part) for part in _MIN_CLI_VERSION)
        detected = (
            ".".join(str(part) for part in diagnostic.detected_version)
            if diagnostic.detected_version
            else "unknown"
        )
        raise RuntimeError(
            f"Antigravity CLI {minimum} 以上が必要です (検出: {detected})。"
            " `agy update` を実行してください"
        )


def _command_parts(command: object) -> list[str]:
    if isinstance(command, list) and all(isinstance(part, str) for part in command):
        return list(command)
    if isinstance(command, str):
        return shlex.split(command)
    return ["agy"]


def _resolve_timeout(config: dict[str, Any]) -> float:
    timeout = config.get("timeout")
    if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and timeout > 0:
        return float(timeout)
    if isinstance(timeout, str) and timeout:
        log.warning(
            "antigravity.timeout_config_is_string",
            value=timeout,
            hint="timeout は数値で指定してください。文字列は --print-timeout フラグとして扱われます",
        )
    return _DEFAULT_TIMEOUT_SECONDS


def _turn_error(
    partial_result: RuntimeResult, *, reason: str,
    payload: dict[str, Any] | None = None, stderr: str = "",
    exit_code: int | None = None, previous: RuntimeTurnError | None = None,
    message: str = "agy did not complete successfully",
) -> RuntimeTurnError:
    diagnostics = dict(previous.diagnostics) if previous else {}
    diagnostics["terminal_reason"] = reason
    if payload:
        diagnostics.update(_structured_error_fields(payload))
        if isinstance(payload.get("error"), dict):
            diagnostics.update(_structured_error_fields(payload["error"]))
    diagnostics.update(_parse_agy_error(stderr))
    if exit_code is not None:
        diagnostics["exit_code"] = exit_code
    retryable = diagnostics.get("retryable")
    return RuntimeTurnError(
        message, partial_result=partial_result, diagnostics=diagnostics,
        retryable=retryable if isinstance(retryable, bool) else None,
    )
