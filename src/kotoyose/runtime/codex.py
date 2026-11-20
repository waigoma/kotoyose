"""Codex provider adapter。

公式 Python SDK `openai-codex` (app-server JSON-RPC) をクライアントとして使う。
`codex exec --json` の subprocess spawn + JSONL 手動パースだった旧実装と異なり、
型付き notification (pydantic model) を扱う。未対応 notification は本文を
記録せず診断し、SDK / runtime の契約差異は回帰テストで検証する。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tempfile
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openai_codex import (
    ApprovalMode,
    AsyncCodex,
    CodexConfig,
    ImageInput,
    Sandbox,
    SkillInput,
    TextInput,
)
from openai_codex.generated.v2_all import (
    AgentMessageThreadItem,
    CodexErrorInfoValue,
    CollabAgentToolCallThreadItem,
    CommandExecutionThreadItem,
    ContextCompactionThreadItem,
    FileChangeThreadItem,
    MessagePhase,
    DynamicToolCallThreadItem,
    McpToolCallThreadItem,
    TurnStatus,
    WebSearchThreadItem,
)
from openai_codex.models import (
    AgentMessageDeltaNotification,
    ErrorNotification,
    ItemCompletedNotification,
    ItemStartedNotification,
    ThreadTokenUsageUpdatedNotification,
    TurnCompletedNotification,
    TurnStartedNotification,
    UnknownNotification,
)

from ..activity import emit_runtime_event
from ..logging_setup import log
from ..output_guard import guard_enabled, redact_event
from .base import RuntimeEvent, RuntimePrompt, RuntimeResult, RuntimeTextInput, RuntimeTurnError
from .translation import RuntimeSkill, apply_common_timeout, translate_runtime_config

_DEFAULT_TIMEOUT_SECONDS = 600.0  # timeout 未指定時のデフォルト (turn がハングした場合の上限)

# codex CLI 時代のフラグで、app-server (SDK) には相当する概念が無いキー。
# silent ignore はせず debug ログだけ残す (cursor.py の _IGNORED_CLI_KEYS と同型)。
_IGNORED_CLI_KEYS = {"skip_git_repo_check", "ignore_user_config", "ignore_rules"}


def _codex_turn_input(prompt: RuntimePrompt, skills: tuple[RuntimeSkill, ...] = ()):
    """runtime 共通入力を Codex SDK の turn input へ変換する。"""
    if isinstance(prompt, str) and not skills:
        return prompt
    prompt_items = [TextInput(prompt)] if isinstance(prompt, str) else [
        TextInput(part.text)
        if isinstance(part, RuntimeTextInput)
        else ImageInput(part.url)
        for part in prompt
    ]
    return [
        *(SkillInput(name=skill.name, path=skill.path) for skill in skills),
        *prompt_items,
    ]


@dataclass(slots=True)
class _AssistantResponse:
    """Track message identity; mirror the SDK's last-final/last-unphased selection."""

    deltas: dict[str, list[str]] = field(default_factory=dict)
    phases: dict[str, Any] = field(default_factory=dict)
    messages: dict[str, AgentMessageThreadItem] = field(default_factory=dict)

    def add_delta(self, item_id: str, delta: str) -> None:
        self.deltas.setdefault(item_id, []).append(delta)

    def add_item(self, item: Any, *, completed: bool) -> None:
        if not isinstance(item, AgentMessageThreadItem):
            return
        self.phases[item.id] = item.phase
        if completed:
            self.messages.pop(item.id, None)
            self.messages[item.id] = item

    def final_response(self) -> tuple[str, list[str]]:
        fallback: AgentMessageThreadItem | None = None
        for item in reversed(list(self.messages.values())):
            if item.phase == MessagePhase.final_answer:
                return self._message_chunks(item)
            if item.phase is None and fallback is None:
                fallback = item
        if fallback is not None:
            return self._message_chunks(fallback)
        if self.messages:
            return "", []  # A commentary-only turn has no final assistant response.
        # Compatibility with older delta-only streams. Select one message rather
        # than concatenating unrelated items, and never promote known commentary.
        for item_id in reversed(self.deltas):
            if self.phases.get(item_id) != MessagePhase.commentary:
                chunks = self.deltas[item_id]
                return "".join(chunks), chunks
        return "", []

    def _message_chunks(self, item: AgentMessageThreadItem) -> tuple[str, list[str]]:
        chunks = self.deltas.get(item.id, [])
        return item.text, chunks if "".join(chunks) == item.text else [item.text]


def _matches_turn(payload: Any, thread_id: str, turn_id: str) -> bool:
    """Check both identities for every supported notification, including errors."""
    if getattr(payload, "thread_id", thread_id) != thread_id:
        return False
    if isinstance(payload, (TurnStartedNotification, TurnCompletedNotification)):
        return payload.turn.id == turn_id
    return getattr(payload, "turn_id", turn_id) == turn_id


def _validate_output_schema(schema: dict[str, Any] | None) -> None:
    if schema is not None:
        from ..structured_output import validate_output_schema

        validate_output_schema(schema)


def _validate_output(schema: dict[str, Any] | None, text: str) -> RuntimeResult | None:
    if schema is None:
        return None
    from ..structured_output import validate_structured_result

    return validate_structured_result(RuntimeResult(text=text, provider="codex"), schema)


_RETRYABLE_CODEX_ERRORS = frozenset({
    "rateLimitExceeded", "flexUnavailable", "serverOverloaded", "internalServerError",
    "httpConnectionFailed", "responseStreamConnectionFailed", "responseStreamDisconnected",
})


_CODEX_ERROR_CODES = frozenset(item.value for item in CodexErrorInfoValue) | frozenset({
    "httpConnectionFailed", "responseStreamConnectionFailed", "responseStreamDisconnected",
    "responseTooManyFailedAttempts", "activeTurnNotSteerable",
})
_CODEX_ERROR_STATUSES = frozenset({"completed", "failed", "interrupted", "missing_completion", "cleanup_failed"})


class CodexTurnError(RuntimeTurnError):
    """Safe structured terminal error; retryability is advisory, never a replay."""

    def __init__(
        self, *, code: str | None = None, status: str | None = None,
        http_status_code: int | None = None, will_retry: bool | None = None,
    ) -> None:
        # Provider messages can repeat arbitrary prompts, URLs and credentials.
        # Regex redaction is not sufficient: public errors contain only typed,
        # allowlisted classification, never message/additionalDetails contents.
        code = code if isinstance(code, str) and code in _CODEX_ERROR_CODES else None
        status = status if isinstance(status, str) and status in _CODEX_ERROR_STATUSES else None
        http_status_code = http_status_code if type(http_status_code) is int and 100 <= http_status_code <= 599 else None
        will_retry = will_retry if type(will_retry) is bool else None
        message = "codex turn が完了通知なしに終了しました" if status == "missing_completion" else "codex provider turn failed"
        if status is not None and status != "missing_completion":
            message += f" ({status})"
        if code is not None:
            message += f": {code}"
        super().__init__(
            message,
            partial_result=RuntimeResult(text="", provider="codex"),
            diagnostics={"terminal_reason": "provider_error", **({"provider_code": code} if code else {}), **({"status": status} if status else {})},
        )
        self.code = code
        self.status = status
        self.http_status_code = http_status_code
        self.will_retry = will_retry
        self.retryable = code in _RETRYABLE_CODEX_ERRORS
        if http_status_code is not None and 400 <= http_status_code < 500 and http_status_code not in {408, 429}:
            self.retryable = False
        self.diagnostics["retryable"] = self.retryable
        if code is not None:
            self.diagnostics["subtype"] = code
            self.subtype = code
        if http_status_code is not None:
            self.diagnostics["api_error_status"] = http_status_code
            self.api_error_status = http_status_code
        if will_retry is not None:
            self.diagnostics["will_retry"] = will_retry

    @classmethod
    def from_turn_error(cls, error: Any, *, status: str | None = None, will_retry: bool | None = None) -> CodexTurnError:
        """SDK model または native JSON の既知分類だけを読み、本文を保持しない。"""
        if isinstance(error, dict):
            info = error.get("codexErrorInfo")
        else:
            info = getattr(error, "codex_error_info", None)
            info = info.model_dump(mode="json", by_alias=True) if info is not None else None
        code: str | None = None
        http_status_code: int | None = None
        if isinstance(info, str):
            code = info
        elif isinstance(info, dict) and len(info) == 1:
            code, details = next(iter(info.items()))
            if isinstance(details, dict) and type(details.get("httpStatusCode")) is int:
                http_status_code = details["httpStatusCode"]
        return cls(
            code=code, status=status, http_status_code=http_status_code, will_retry=will_retry,
        )

    def as_event(self, *, kind: str = "error") -> RuntimeEvent:
        payload: dict[str, Any] = {"error": _truncate(str(self)), "retryable": self.retryable}
        for key, value in (
            ("code", self.code), ("status", self.status),
            ("http_status_code", self.http_status_code), ("will_retry", self.will_retry),
        ):
            if value is not None:
                payload[key] = value
        return RuntimeEvent(kind=kind, summary=_summarize(str(self)), payload=payload)


@dataclass(slots=True)
class CodexRuntimeSession:
    """codex app-server の 1 thread を kotoyose の RuntimeSession として扱う。"""

    model: str | None
    cwd: str
    config: dict[str, Any] = field(default_factory=dict)
    session_id: str | None = None
    provider: str = "codex"
    # provider 非依存の prompt_file_refs から解決した本文。thread の
    # developer_instructions として注入する (Claude の system prompt append に相当)。
    instructions: str | None = None
    # provider 非依存の mcp_refs / mcp_servers から解決した kotoyose 形式の設定。
    # _build_config_overrides() で mcp_servers.<name>.* の dotted key へ変換する。
    mcp_servers: dict[str, dict] = field(default_factory=dict)
    skills: tuple[RuntimeSkill, ...] = ()
    effort: str | None = None
    output_schema: dict[str, Any] | None = None
    agents: dict[str, Any] = field(default_factory=dict)
    agent_models: dict[str, str] = field(default_factory=dict)
    # preset.codex_home を展開したもの。指定時は CODEX_HOME としてのみ渡す。
    codex_home: str | None = None
    _agent_role_dir: str | None = field(default=None, init=False, repr=False)
    _codex: Any = field(default=None, init=False, repr=False)  # AsyncCodex
    _thread: Any = field(default=None, init=False, repr=False)  # AsyncThread

    async def disconnect(self) -> None:
        """app-server subprocess を閉じ、参照を破棄する (孤児化防止)。"""
        try:
            if self._codex is not None:
                await self._codex.close()
        except Exception:
            raise CodexTurnError(status="cleanup_failed") from None
        finally:
            self._codex = None
            self._thread = None
            if self._agent_role_dir is not None:
                shutil.rmtree(self._agent_role_dir, ignore_errors=True)
                self._agent_role_dir = None

    async def ask(self, prompt: RuntimePrompt) -> RuntimeResult:
        """stream() の最終結果を非ストリーミング呼び出しへ集約する (cursor.py の ask と同型)。"""
        result: RuntimeResult | None = None
        async for item in self.stream(prompt):
            if isinstance(item, RuntimeEvent):
                emit_runtime_event(item)
            elif isinstance(item, RuntimeResult):
                result = item
        assert result is not None
        return result

    async def stream(
        self, prompt: RuntimePrompt
    ) -> AsyncIterator[str | RuntimeEvent | RuntimeResult]:
        """Progress events are live; final body is released only after phase selection.

        Deltas carry item IDs but no phase. Even a phase-less completed message can
        be superseded by a later final_answer. Buffering avoids leaking commentary
        or invalid structured output into the OpenAI-compatible response body.
        """
        timeout = _resolve_timeout(self.config)
        response = _AssistantResponse()
        first_usage: Any = None
        latest_usage: Any = None
        turn_completed = False
        last_error: CodexTurnError | None = None
        runtime_error_emitted = False
        completed = False
        final_text = ""
        structured_output: object | None = None
        _validate_output_schema(self.output_schema)
        try:
            async with asyncio.timeout(timeout) as timeout_cm:
                thread = await self._ensure_thread()
                turn_options: dict[str, Any] = {"effort": self.effort}
                if self.output_schema is not None:
                    turn_options["output_schema"] = self.output_schema
                handle = await thread.turn(
                    _codex_turn_input(prompt, self.skills), **turn_options,
                )
                async for notification in handle.stream():
                    payload = notification.payload
                    # Ignore foreign traffic before refreshing the idle deadline.
                    if not _matches_turn(payload, thread.id, handle.id):
                        continue
                    if isinstance(payload, UnknownNotification):
                        # Unknown payloads may contain credentials or tool arguments;
                        # they do not prove this turn is making progress.
                        log.debug("codex.notification.unknown", method=_truncate(notification.method))
                        continue
                    timeout_cm.reschedule(asyncio.get_running_loop().time() + timeout)
                    if isinstance(payload, TurnStartedNotification):
                        yield RuntimeEvent(kind="turn", summary="Assistant turn 1", payload={"turn": 1})
                    elif isinstance(payload, AgentMessageDeltaNotification):
                        response.add_delta(payload.item_id, payload.delta)
                    elif isinstance(payload, (ItemStartedNotification, ItemCompletedNotification)):
                        response.add_item(payload.item.root, completed=isinstance(payload, ItemCompletedNotification))
                        event = _runtime_event_from_item_notification(payload)
                        if event is not None:
                            yield event
                    elif isinstance(payload, ThreadTokenUsageUpdatedNotification):
                        if first_usage is None:
                            first_usage = payload.token_usage
                        latest_usage = payload.token_usage
                    elif isinstance(payload, ErrorNotification):
                        error = CodexTurnError.from_turn_error(payload.error, will_retry=payload.will_retry)
                        if payload.will_retry:
                            # The SDK owns retries. Never initiate/replay a turn here.
                            log.warning("codex.turn.retry", code=error.code)
                            yield error.as_event(kind="retry")
                            continue
                        last_error = error
                        runtime_error_emitted = True
                        yield error.as_event()
                    elif isinstance(payload, TurnCompletedNotification):
                        turn = payload.turn
                        if turn.status != TurnStatus.completed:
                            error = CodexTurnError.from_turn_error(turn.error, status=turn.status.value)
                            if last_error is not None and error.code is None:
                                error = CodexTurnError(
                                    code=last_error.code, status=turn.status.value,
                                    http_status_code=last_error.http_status_code, will_retry=False,
                                )
                            if not runtime_error_emitted:
                                runtime_error_emitted = True
                                yield error.as_event()
                            raise error
                        if last_error is not None:
                            # A terminal error cannot be laundered by a later success.
                            raise last_error
                        turn_completed = True
                        break
                if not turn_completed:
                    raise last_error or CodexTurnError(status="missing_completion")
                final_text, final_chunks = response.final_response()
                validated = _validate_output(self.output_schema, final_text)
                if validated is not None:
                    final_text = validated.text
                    final_chunks = [final_text]
                    structured_output = validated.structured_output
            # Final body cannot include commentary, superseded text, or failed output.
            for chunk in final_chunks:
                yield chunk
            completed = True
        except TimeoutError:
            detail = f"{self.provider} が {timeout:g} 秒間出力を返しませんでした (idle timeout)"
            if not runtime_error_emitted:
                yield _error_event(detail)
            raise RuntimeTurnError(
                detail,
                partial_result=RuntimeResult(response.final_response()[0], _per_turn_usage(first_usage, latest_usage), self.session_id, self.provider),
                diagnostics={"terminal_reason": "idle_timeout"}, retryable=False,
            ) from None
        except Exception as exc:
            error = exc if isinstance(exc, RuntimeTurnError) else CodexTurnError(status="failed")
            error.partial_result.usage = _per_turn_usage(first_usage, latest_usage)
            error.partial_result.runtime_session_id = self.session_id
            if isinstance(error, CodexTurnError):
                error.partial_result.text = response.final_response()[0]
            if not runtime_error_emitted:
                yield error.as_event() if isinstance(error, CodexTurnError) else _error_event(str(error))
            raise error from None
        finally:
            if not completed:
                await self.disconnect()

        self.session_id = self._thread.id
        yield RuntimeResult(
            text=final_text,
            usage=_per_turn_usage(first_usage, latest_usage),
            runtime_session_id=self.session_id,
            provider=self.provider,
            structured_output=structured_output,
        )

    async def _ensure_thread(self) -> Any:
        """client (AsyncCodex) と thread を lazy 生成し、2 回目以降は再利用する。"""
        if self._thread is not None:
            return self._thread
        try:
            if self._codex is None:
                self._codex = AsyncCodex(
                    CodexConfig(
                        codex_bin=_resolve_codex_bin(self.config),
                        cwd=self.cwd,
                        config_overrides=self._build_config_overrides(),
                        env={"CODEX_HOME": self.codex_home} if self.codex_home else None,
                    )
                )
            sandbox = _map_sandbox(self.config.get("sandbox"), self.config)
            approval_mode = _map_approval_mode(self.config)
            if self.session_id:
                self._thread = await self._codex.thread_resume(
                    self.session_id,
                    cwd=self.cwd,
                    developer_instructions=self.instructions,
                    sandbox=sandbox,
                    approval_mode=approval_mode,
                )
            else:
                self._thread = await self._codex.thread_start(
                    model=self.model,
                    cwd=self.cwd,
                    developer_instructions=self.instructions,
                    sandbox=sandbox,
                    approval_mode=approval_mode,
                )
        except Exception:
            # thread_start/resume 失敗時に app-server subprocess を孤児化させない
            # (cursor.py _ensure_agent と同型)。
            await self.disconnect()
            raise
        # turn 前に失敗しても resume できるよう、thread 確立の時点で session_id を確定する。
        self.session_id = self._thread.id
        return self._thread

    def _build_config_overrides(self) -> tuple[str, ...]:
        """CodexConfig.config_overrides ('key=value' 文字列群) を組み立てる。

        CLI 時代の --config passthrough と等価の意味論を保つため、値は
        _tomlish_value で TOML 値表現へ変換する (mcp entries は既に変換済み)。
        """
        overrides: list[str] = []
        config_dict = self.config.get("config")
        if isinstance(config_dict, dict):
            for key, value in config_dict.items():
                overrides.extend(_dotted_config_entries(key, value))

        profile = self.config.get("profile")
        if isinstance(profile, str) and profile:
            overrides.append(f"profile={_tomlish_value(profile)}")

        for name, server in self.mcp_servers.items():
            for key, value in _codex_mcp_config_entries(name, server):
                overrides.append(f"{key}={value}")

        overrides.extend(_model_provider_overrides(self.config))
        overrides.extend(self._agent_config_overrides())

        return tuple(overrides)

    def _agent_config_overrides(self) -> list[str]:
        """共通 agent 定義を Codex の role file + config override へ変換する。"""
        if not self.agents:
            return []
        if self._agent_role_dir is None:
            self._agent_role_dir = tempfile.mkdtemp(prefix="kotoyose-codex-agents-")

        overrides: list[str] = []
        for name, agent in self.agents.items():
            if not _PROVIDER_ID_PATTERN.match(name):
                raise ValueError(
                    f"codex agent 名 {name!r} は英数字 / '_' / '-' で指定してください"
                )
            role_file = Path(self._agent_role_dir) / f"{name}.toml"
            sandbox_mode = _codex_agent_sandbox(agent.tools)
            role_file.write_text(
                _codex_agent_role_toml(
                    name,
                    agent.prompt,
                    model=self.agent_models.get(name),
                    sandbox_mode=sandbox_mode,
                ),
                encoding="utf-8",
            )
            overrides.extend(
                [
                    f"agents.{name}.description={_tomlish_value(agent.description)}",
                    f"agents.{name}.config_file={_tomlish_value(str(role_file))}",
                ]
            )
            if agent.tools and sandbox_mode is None:
                log.warning(
                    "codex.agent.tools_unsupported",
                    agent=name,
                    tools=agent.tools,
                )
            if agent.model != "inherit" and name not in self.agent_models:
                log.warning(
                    "codex.agent.model_alias_unsupported",
                    agent=name,
                    model=agent.model,
                )
        return overrides


def build_codex_session(
    preset,
    cwd_override: str | None,
    session_id: str | None,
    request_headers: dict[str, str] | None = None,
) -> CodexRuntimeSession:
    """ModelPreset から CodexRuntimeSession を構築する。

    provider 非依存設定を中間表現へ解決し、Codex SDK の native 機能へ変換する。
    """
    from ..config import get_config  # 循環 import 回避のため関数内 import

    cwd = os.path.expanduser(cwd_override) if cwd_override is not None else preset.cwd
    cfg = get_config()
    translated = translate_runtime_config(cfg, preset, request_headers)
    if translated.plugin_paths:
        log.warning(
            "codex.plugin_partially_translated",
            plugins=list(translated.plugin_paths),
            translated="skills only",
        )
    if translated.unsupported_fields:
        log.warning(
            "codex.common_settings_unsupported",
            fields=list(translated.unsupported_fields),
        )
    codex_config = apply_common_timeout(dict(preset.codex or {}), translated.timeout_seconds)
    codex_config, mcp_servers = _translate_codex_common_settings(
        preset, codex_config, translated.mcp_servers
    )
    for key in _IGNORED_CLI_KEYS & set(codex_config.keys()):
        log.debug("codex.cli_config.ignored", key=key)
    return CodexRuntimeSession(
        model=preset.model,
        cwd=str(Path(cwd).expanduser()),
        config=codex_config,
        session_id=session_id or None,
        instructions=translated.instructions,
        mcp_servers=mcp_servers,
        skills=translated.skills,
        effort=translated.effort,
        output_schema=preset.output_schema,
        agents=translated.agents,
        agent_models=_resolve_codex_agent_models(cfg, translated.agents),
        codex_home=(
            str(Path(preset.codex_home).expanduser()) if preset.codex_home else None
        ),
    )


def _resolve_timeout(config: dict[str, Any]) -> float:
    """config の timeout (秒) を取得する。未指定/不正な型は default にフォールバックする。"""
    timeout = config.get("timeout")
    if isinstance(timeout, (int, float)) and timeout > 0:
        return float(timeout)
    return _DEFAULT_TIMEOUT_SECONDS


def _resolve_codex_bin(config: dict[str, Any]) -> str | None:
    """明示指定がなければ SDK と同じ version の同梱 codex を使う。

    stable SDK は互換 runtime を依存関係として固定している。PATH 上の別 version を
    自動選択すると SDK / app-server protocol のずれを再導入するため、特定のローカル
    binary を意図して試す場合にだけ `codex_bin` を指定する。
    """
    codex_bin = config.get("codex_bin")
    if isinstance(codex_bin, str) and codex_bin:
        return os.path.expanduser(codex_bin)
    return None


def _map_sandbox(value: str | None, config: dict[str, Any]) -> Sandbox | None:
    """CLI 語彙の sandbox 文字列を SDK の Sandbox enum へ変換する。

    dangerously_bypass_approvals_and_sandbox が truthy な場合は sandbox 指定より
    優先して full_access を返す (--dangerously-bypass-approvals-and-sandbox の
    意味論を近似)。不正な値は silent ignore せず ValueError にする (既存方針)。
    """
    if config.get("dangerously_bypass_approvals_and_sandbox"):
        return Sandbox.full_access
    if value is None:
        return None
    if value == "read-only":
        return Sandbox.read_only
    if value == "workspace-write":
        return Sandbox.workspace_write
    if value in ("danger-full-access", "full-access"):
        return Sandbox.full_access
    raise ValueError(f"codex provider は sandbox={value!r} に対応していません")


def _map_approval_mode(config: dict[str, Any]) -> ApprovalMode:
    """escalation (sandbox 外実行) 承認の扱いを決める。

    SDK 既定の auto_review は escalation を LLM reviewer + 自動承認 handler に
    委ねるため、preset の sandbox 境界が実質的に破れる。旧 `codex exec` は
    approval を一切尋ねない (= escalation 拒否) 非対話動作だったので、
    deny_all (AskForApproval never 相当) を既定にして意味論を維持する。
    """
    value = config.get("approval_mode")
    if value is None:
        return ApprovalMode.deny_all
    if value == "deny_all":
        return ApprovalMode.deny_all
    if value == "auto_review":
        return ApprovalMode.auto_review
    raise ValueError(
        f"codex provider は approval_mode={value!r} に対応していません (対応: deny_all, auto_review)"
    )


# route 層 (chat.py `_extract_tokens`) が読む usage dict のキー。
_USAGE_KEYS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


def _per_turn_usage(first: Any, latest: Any) -> dict[str, int] | None:
    """thread 累積の ThreadTokenUsage snapshot 群から当 turn 分の usage を切り出す。

    `total` は thread 全体の累積で、resume を跨いでも増え続けることを実機確認済み。
    そのまま返すと 2 turn 目以降の会話で累積値を毎リクエスト報告してしまい、
    interactions の token 集計が二重三重に膨らむ。turn 内の最初の snapshot が
    「累積 + 当 turn 1 回目の request (= first.last)」である性質を使い、
    latest.total - first.total + first.last で当 turn の全 request 合計を得る。
    (snapshot が 1 回だけの turn では first.last がそのまま当 turn 分になる)
    """
    if latest is None or first is None:
        return None
    usage = {
        key: getattr(latest.total, key) - getattr(first.total, key) + getattr(first.last, key)
        for key in _USAGE_KEYS
    }
    if latest.model_context_window is not None:
        usage["model_context_window"] = latest.model_context_window
    return usage


def _tomlish_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(value, ensure_ascii=False)


def _codex_agent_role_toml(
    name: str,
    prompt: str,
    *,
    model: str | None = None,
    sandbox_mode: str | None = None,
) -> str:
    """Codex agent role file を安全な TOML 文字列として組み立てる。"""
    lines = (
        f"name = {_tomlish_value(name)}\n"
        f"developer_instructions = {_tomlish_value(prompt)}\n"
    )
    if model is not None:
        lines += f"model = {_tomlish_value(model)}\n"
    if sandbox_mode is not None:
        lines += f"sandbox_mode = {_tomlish_value(sandbox_mode)}\n"
    return lines


_READ_ONLY_AGENT_TOOLS = frozenset({"Read", "Grep", "Glob", "WebFetch", "WebSearch"})


def _codex_agent_sandbox(tools: list[str] | None) -> str | None:
    """共通 tool allowlist が読取専用なら Codex の filesystem 境界で強制する。"""
    if tools is not None and set(tools) <= _READ_ONLY_AGENT_TOOLS:
        return "read-only"
    return None


_CODEX_AGENT_ROUTE_NAMES = {
    "haiku": "codex-luna",
    "sonnet": "codex-terra",
    "opus": "codex-sol",
}


def _resolve_codex_agent_models(config: Any, agents: dict[str, Any]) -> dict[str, str]:
    """provider 共通の agent tier を Codex route 定義へ解決する。"""
    resolved: dict[str, str] = {}
    for name, agent in agents.items():
        if agent.model == "inherit":
            continue
        route_name = _CODEX_AGENT_ROUTE_NAMES[agent.model]
        definition = config.routes.get(route_name)
        if definition is not None and definition.driver == "codex.app-server":
            resolved[name] = definition.model
    return resolved


_MCP_DENY_PATTERN = re.compile(r"^mcp__(?P<server>.+?)__(?P<tool>.+)$")


def _translate_codex_common_settings(
    preset: Any,
    codex_config: dict[str, Any],
    mcp_servers: dict[str, dict],
) -> tuple[dict[str, Any], dict[str, dict]]:
    """共通 preset の隔離・MCP deny を Codex native 設定へ変換する。"""
    resolved_config = dict(codex_config)
    config_table = dict(resolved_config.get("config") or {})
    if preset.bare or preset.setting_sources == [] or preset.require_isolation:
        config_table["project_doc_max_bytes"] = 0
    if config_table:
        resolved_config["config"] = config_table

    resolved_mcp = {name: dict(server) for name, server in mcp_servers.items()}
    for denied in preset.deny:
        match = _MCP_DENY_PATTERN.match(denied)
        if match is None:
            continue
        server = resolved_mcp.get(match.group("server"))
        if server is None:
            continue
        disabled = list(server.get("disabled_tools") or [])
        tool = match.group("tool")
        if tool not in disabled:
            disabled.append(tool)
        server["disabled_tools"] = disabled
    return resolved_config, resolved_mcp


def _runtime_event_from_item_notification(
    notification: ItemStartedNotification | ItemCompletedNotification,
) -> RuntimeEvent | None:
    """Codex item lifecycle を provider 共通 event へ変換する。"""
    item = notification.item.root
    if isinstance(item, AgentMessageThreadItem):
        if not isinstance(notification, ItemCompletedNotification):
            return None
        return RuntimeEvent(
            kind="text",
            summary=_summarize(item.text),
            payload={
                "text": _truncate(item.text), "item_id": item.id,
                "phase": item.phase.value if item.phase is not None else None,
            },
        )

    kind = "tool_use" if isinstance(notification, ItemStartedNotification) else "tool_result"
    if isinstance(item, CommandExecutionThreadItem):
        return _tool_event(
            kind,
            tool_name="Bash",
            tool_use_id=item.id,
            input_value={"command": item.command},
            output_value=getattr(item, "aggregated_output", None) or "",
            is_error=_item_is_error(item),
            summary=item.command,
        )
    if isinstance(item, McpToolCallThreadItem):
        return RuntimeEvent(
            **_tool_event_args(
                kind,
                tool_name=f"mcp__{item.server}__{item.tool}",
                tool_use_id=item.id,
                input_value=item.arguments,
                output_value=_first_non_none(getattr(item, "result", None), getattr(item, "error", None)),
                is_error=_item_is_error(item),
                summary=_compact_json(item.arguments),
            )
        )
    if isinstance(item, DynamicToolCallThreadItem):
        tool_name = f"{item.namespace}__{item.tool}" if item.namespace else item.tool
        return _tool_event(
            kind,
            tool_name=tool_name,
            tool_use_id=item.id,
            input_value=item.arguments,
            output_value=getattr(item, "content_items", None),
            is_error=_item_is_error(item),
            summary=_compact_json(item.arguments),
        )
    if isinstance(item, FileChangeThreadItem):
        # Diffs may include secrets and are not needed for lifecycle observability.
        changes = [{"path": change.path, "kind": change.kind.model_dump(mode="json")} for change in item.changes]
        return _tool_event(
            kind, tool_name="FileChange", tool_use_id=item.id,
            input_value={"changes": changes},
            output_value={"status": item.status.value, "change_count": len(item.changes)},
            is_error=_item_is_error(item), summary=f"{len(item.changes)} file changes",
        )
    if isinstance(item, CollabAgentToolCallThreadItem):
        # Keep identities/statuses; never duplicate prompts or subagent messages.
        return _tool_event(
            kind, tool_name=f"collab__{item.tool.value}", tool_use_id=item.id,
            input_value={"sender_thread_id": item.sender_thread_id, "receiver_thread_ids": item.receiver_thread_ids},
            output_value={"status": item.status.value, "agents": {
                thread_id: state.status.value for thread_id, state in item.agents_states.items()
            }},
            is_error=_item_is_error(item), summary=f"{item.tool.value}: {item.status.value}",
        )
    if isinstance(item, WebSearchThreadItem):
        return _tool_event(
            kind, tool_name="WebSearch", tool_use_id=item.id,
            input_value={"query": item.query},
            output_value={"result_count": len(item.results or [])},
            is_error=False, summary=item.query,
        )
    if isinstance(item, ContextCompactionThreadItem):
        return RuntimeEvent(
            kind="context_compaction", summary="Context compaction",
            payload={"item_id": item.id, "status": "started" if kind == "tool_use" else "completed"},
        )
    return None


def _compact_json(value: Any) -> str:
    """tool 引数を timeline 向けの短い 1 行 JSON にする。"""
    if hasattr(value, "model_dump"):
        value = value.model_dump(by_alias=True, exclude_none=True, warnings=False)
    return _truncate(json.dumps(_redact_json(value), ensure_ascii=False, separators=(",", ":"), default=str))


def _redact_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if re.search(r"(?i)(authorization|api[-_]?key|token|password|secret)", str(key)) else _redact_json(child)
            for key, child in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_json(child) for child in value]
    return _redact(value) if isinstance(value, str) else value


def _truncate(value: str) -> str:
    """イベントへ保存する文字列を上限内へ切り詰める。"""
    if guard_enabled():
        value = redact_event(value)
    return _redact(value)[:500]


_SECRET_KEY_PATTERN = re.compile(r"(?i)(authorization|api[-_]?key|access[-_]?token|refresh[-_]?token|password|client[-_]?secret)([\"'\s:=]+)([^\s,;\"'}]+)")


def _redact(value: str) -> str:
    """Best-effort redaction for bounded diagnostics, never a credential store."""
    value = re.sub(r"(?i)\bBearer\s+[^\s\"',;}]+", "Bearer [REDACTED]", value)
    value = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[REDACTED]", value)
    value = _SECRET_KEY_PATTERN.sub(r"\1\2[REDACTED]", value)
    value = re.sub(r"(https?://)[^/@\s]+:[^/@\s]+@", r"\1[REDACTED]@", value)
    return value


def _summarize(value: str) -> str:
    """改行を空白へ畳み、人間可読の 1 行要約を作る。"""
    return _truncate(" ".join(value.split()))


def _error_event(detail: str) -> RuntimeEvent:
    """Claude と同じ error payload を持つ runtime event を作る。"""
    return RuntimeEvent(kind="error", summary=_summarize(detail), payload={"error": _truncate(detail)})


def _item_is_error(item: Any) -> bool:
    """Codex item の失敗 status を provider 共通の bool へ正規化する。"""
    status = getattr(item, "status", "")
    status_value = getattr(status, "value", status)
    exit_code = getattr(item, "exit_code", None)
    return (
        status_value in {"failed", "declined", "error"}
        or getattr(item, "error", None) is not None
        or getattr(item, "success", None) is False
        or (isinstance(exit_code, int) and exit_code != 0)
    )


def _first_non_none(*values: Any) -> Any:
    """空の dict や文字列を保持したまま最初の非 None 値を返す。"""
    return next((value for value in values if value is not None), None)


def _tool_event(
    kind: str,
    *,
    tool_name: str,
    tool_use_id: str,
    input_value: Any,
    output_value: Any,
    is_error: bool,
    summary: str,
) -> RuntimeEvent:
    """Claude と共通の tool event payload を組み立てる。"""
    return RuntimeEvent(**_tool_event_args(
        kind,
        tool_name=tool_name,
        tool_use_id=tool_use_id,
        input_value=input_value,
        output_value=output_value,
        is_error=is_error,
        summary=summary,
    ))


def _tool_event_args(
    kind: str,
    *,
    tool_name: str,
    tool_use_id: str,
    input_value: Any,
    output_value: Any,
    is_error: bool,
    summary: str,
) -> dict[str, Any]:
    """開始・完了で異なる Claude 互換 tool payload を返す。"""
    if kind == "tool_use":
        return {
            "kind": kind,
            "tool_name": tool_name,
            "summary": _summarize(summary),
            "payload": {"tool_use_id": tool_use_id, "input": _compact_json(input_value)},
        }
    output = output_value if isinstance(output_value, str) else _compact_json(output_value)
    status = "error" if is_error else "ok"
    return {
        "kind": kind,
        "tool_name": tool_name,
        "summary": _summarize(f"{status}: {output}"),
        "payload": {
            "tool_use_id": tool_use_id,
            "is_error": is_error,
            "output": _truncate(output),
        },
    }


def _dotted_config_entries(prefix: str, value: Any) -> list[str]:
    """1 つの config 値を `key=value` (TOML overrides) の entry 群へ変換する。

    dict は TOML の inline table として表現できない (json.dumps の `{"a": "b"}`
    は不正な TOML 値) ため、必ず `f"{prefix}.{key}..."` の dotted key へ再帰展開する。
    dict でなければ 1 entry ("{prefix}={_tomlish_value(value)}") を返す。
    """
    if isinstance(value, dict):
        entries: list[str] = []
        for key, child in value.items():
            entries.extend(_dotted_config_entries(f"{prefix}.{key}", child))
        return entries
    return [f"{prefix}={_tomlish_value(value)}"]


# provider id は TOML の dotted key として素通しするため、ドット注入等で意図しない
# key を作られないよう英数字/'_'/'-' のみに限定する。
_PROVIDER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")

# codex stable 0.144.4 で廃止された wire_api 値。エラー文言は本家の廃止告知
# (https://github.com/openai/codex/discussions/7782) に合わせ、代替を明示する。
_UNSUPPORTED_WIRE_API = "chat"


def _model_provider_overrides(config: dict[str, Any]) -> list[str]:
    """preset config の `model_provider` を config_overrides の entry 群へ展開する。

    codex は `model_providers.<id>.*` (base_url 等) + `model_provider = "<id>"` で
    任意の Responses API endpoint を指せる (実機検証済み)。str は config.toml 側で
    定義済みの provider を id で選ぶだけ、dict はその場で custom provider を定義する。
    """
    value = config.get("model_provider")
    if value is None:
        return []
    if isinstance(value, str):
        if not value:
            raise ValueError("codex provider の model_provider (str) は空にできません")
        return [f"model_provider={_tomlish_value(value)}"]
    if not isinstance(value, dict):
        raise TypeError(
            f"codex provider は model_provider={value!r} に対応していません (対応: str, dict)"
        )

    remaining = dict(value)
    provider_id = remaining.pop("id", "custom")
    if not isinstance(provider_id, str) or not _PROVIDER_ID_PATTERN.match(provider_id):
        raise ValueError(
            f"codex provider は model_provider.id={provider_id!r} に対応していません"
            " (使用可能文字: 英数字 / '_' / '-')"
        )
    base_url = remaining.pop("base_url", None)
    if not isinstance(base_url, str) or not base_url:
        raise ValueError(
            "codex provider の model_provider (dict) には base_url が必須です"
            " (組み込み provider を選ぶだけなら文字列で id を指定してください)"
        )
    wire_api = remaining.pop("wire_api", "responses")
    if wire_api == _UNSUPPORTED_WIRE_API:
        raise ValueError(
            'codex stable (0.144.4~) では wire_api = "chat" は廃止されました。'
            ' wire_api = "responses" を指定してください'
            " (参照: https://github.com/openai/codex/discussions/7782)"
        )
    name = remaining.pop("name", provider_id)
    env_key = remaining.pop("env_key", None)

    prefix = f"model_providers.{provider_id}"
    entries = [
        f"{prefix}.name={_tomlish_value(name)}",
        f"{prefix}.base_url={_tomlish_value(base_url)}",
        f"{prefix}.wire_api={_tomlish_value(wire_api)}",
    ]
    if isinstance(env_key, str) and env_key:
        entries.append(f"{prefix}.env_key={_tomlish_value(env_key)}")
    for key, child in remaining.items():
        entries.extend(_dotted_config_entries(f"{prefix}.{key}", child))
    entries.append(f"model_provider={_tomlish_value(provider_id)}")
    return entries


# Claude 専用の request header 注入テンプレート。codex には解決手段が無いため、
# silent ignore せず ValueError で気付かせる (config.py の同名マーカーと同じ構文)。
_HEADER_TEMPLATE_MARKER = "{{header:"


def _codex_mcp_config_entries(name: str, server: dict[str, Any]) -> list[tuple[str, str]]:
    """kotoyose 形式の 1 MCP server 設定を `mcp_servers.<name>.*` の dotted key/value
    群に変換する (CodexConfig.config_overrides の `f"{key}={value}"` として使う)。

    stdio (command/args/env/cwd) と http (url/headers) のみ対応。sse や未知の type は
    codex 側に相当する起動フラグが無いため、silent ignore せず ValueError にする。
    """
    for value in _iter_string_values(server):
        if _HEADER_TEMPLATE_MARKER in value:
            raise ValueError(
                f"mcp_servers['{name}']: codex provider は request header 注入 "
                "({{header:...}}) に未対応です"
            )

    prefix = f"mcp_servers.{name}"
    stype = server.get("type") or ("stdio" if "command" in server else None)
    entries: list[tuple[str, str]] = []

    if stype == "stdio":
        command = server.get("command")
        if isinstance(command, str) and command:
            entries.append((f"{prefix}.command", _tomlish_value(command)))
        args = server.get("args")
        if isinstance(args, list):
            entries.append((f"{prefix}.args", _tomlish_value(args)))
        env = server.get("env")
        if isinstance(env, dict):
            for key, value in env.items():
                entries.append((f"{prefix}.env.{key}", _tomlish_value(value)))
        cwd = server.get("cwd")
        if isinstance(cwd, str) and cwd:
            entries.append((f"{prefix}.cwd", _tomlish_value(cwd)))
    elif stype == "http":
        url = server.get("url")
        if isinstance(url, str) and url:
            entries.append((f"{prefix}.url", _tomlish_value(url)))
        headers = server.get("headers")
        if isinstance(headers, dict):
            for key, value in headers.items():
                entries.append((f"{prefix}.http_headers.{key}", _tomlish_value(value)))
    else:
        raise ValueError(
            f"codex provider は mcp_servers['{name}'].type={stype!r} に対応していません"
            " (対応: stdio, http)"
        )

    for key in (
        "enabled",
        "required",
        "startup_timeout_sec",
        "tool_timeout_sec",
        "enabled_tools",
        "disabled_tools",
        "default_tools_approval_mode",
    ):
        if key in server:
            entries.append((f"{prefix}.{key}", _tomlish_value(server[key])))

    return entries


def _iter_string_values(value: Any) -> Iterator[str]:
    """dict/list 内の文字列値を再帰的に列挙する ({{header:...}} 検出用)。"""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _iter_string_values(v)
    elif isinstance(value, list):
        for v in value:
            yield from _iter_string_values(v)
