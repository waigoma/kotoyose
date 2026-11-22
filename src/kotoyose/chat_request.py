"""Chat request から runtime 実行前の解決済み context を組み立てる。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from .chat_policy import ChatPolicyViolation, validate_image_input
from .config import HarnessConfig, ModelPreset
from .conversation_key import derive_lookup_key
from .execution_resources import ExecutionError
from .models import ChatCompletionRequest, ChatMessage
from .runtime.acp import AcpPrompt
from .runtime.base import RuntimePrompt, RuntimeTextInput, prompt_audit_text, prompt_image_urls, prompt_text
from .runtime.translation import EFFORT_PROVIDERS


@dataclass(frozen=True, slots=True)
class RequestPreparationPorts:
    """Request preparation が session infrastructure を参照するための port。"""

    resolve_session_identity: Callable[
        [str, str, str | None, list[str], dict[str, str]],
        tuple[str, str, str],
    ]
    is_cache_hit: Callable[[str | None, tuple[str, str, str]], bool]


@dataclass(frozen=True, slots=True)
class PreparedChatRequest:
    """Provider 接続と interaction 記録に必要な request 派生値。"""

    prompt: RuntimePrompt
    latest_text: str
    audit_text: str
    lookup_key: str | None
    effective_conversation_key: str | None
    session_identity: tuple[str, str, str]
    cache_hit: bool


def prepare_chat_request(
    request: ChatCompletionRequest,
    headers: Mapping[str, str],
    preset: ModelPreset,
    ports: RequestPreparationPorts,
    *,
    conversation_key: str | None,
    session_identity: tuple[str, str, str] | None = None,
    secret_versions: Mapping[str, int] | None = None,
) -> PreparedChatRequest:
    """Chat request の prompt、lookup key、cache 状態を副作用なしで解決する。

    Args:
        request: Validation 済みの OpenAI 互換 chat request。
        headers: Session identity に使用する request header。
        preset: 解決済み model preset。
        ports: Session identity と cache lookup の抽象境界。
        conversation_key: Header または preset 由来の固定 conversation key。

    Returns:
        Runtime 接続前に必要な派生値一式。

    Raises:
        ChatPolicyViolation: User message 不在または画像入力 policy 違反の場合。
    """
    user_messages = [message for message in request.messages if message.role == "user"]
    if not user_messages:
        raise ChatPolicyViolation("no user message found in messages[]")

    latest_prompt = user_messages[-1].runtime_prompt()
    prompt = latest_prompt
    image_urls = [
        url
        for message in request.messages
        for url in prompt_image_urls(message.runtime_prompt())
    ]
    validate_image_input(preset.provider, image_urls)
    if preset.provider in {"print_cli", "cli_acp"}:
        history = "\n\n".join(
            f"{message.role}: {message.text_content()}" for message in request.messages
        )
        prompt = AcpPrompt(prompt_text(latest_prompt), history) if preset.provider == "cli_acp" else history

    lookup_key = (
        conversation_key
        if conversation_key is not None
        else derive_lookup_key(request.messages)
    )
    session_identity = session_identity or ports.resolve_session_identity(
        preset.provider,
        request.model,
        preset.model,
        preset.required_request_headers,
        dict(headers),
        **({"output_schema": preset.output_schema} if preset.output_schema is not None else {}),
        **({"secret_versions": secret_versions} if secret_versions else {}),
    )
    return PreparedChatRequest(
        prompt=prompt,
        latest_text=prompt_text(latest_prompt),
        audit_text=prompt_audit_text(latest_prompt),
        lookup_key=lookup_key,
        effective_conversation_key=conversation_key or lookup_key,
        session_identity=session_identity,
        cache_hit=ports.is_cache_hit(lookup_key, session_identity),
    )


# SDK runtime は会話を自分で持つ。新規 session だけ、先行発言をプロンプトの先頭に載せる。
_SDK_PROVIDERS = frozenset({"claude", "codex", "cursor", "antigravity"})
_TRANSCRIPT_LIMIT = 200_000


def ensure_reasoning_effort(config: HarnessConfig, alias: str, requested: str | None) -> None:
    """この turn の reasoning_effort を、runtime が受け取れるときだけ通す。

    候補一覧は既存の discovery cache だけを見る。cache が無いときは一覧が不明で、
    ``preset.effort`` と同じく値をそのまま runtime へ渡す。

    Raises:
        ExecutionError: provider が effort を適用しない、または判明している候補の外なら HTTP 422。
    """
    if requested is None:
        return
    preset = config.preset_for(alias)
    if preset.provider not in EFFORT_PROVIDERS:
        raise ExecutionError(
            "invalid_request",
            f"reasoning_effort is not supported for provider '{preset.provider}'",
            422,
        )
    from .model_discovery import cached_reasoning_efforts

    known = cached_reasoning_efforts(config, alias)
    if known is not None and requested not in known["supported"]:
        supported = ", ".join(known["supported"])
        raise ExecutionError(
            "invalid_request",
            f"reasoning_effort {requested!r} is not supported for this model (supported: {supported})",
            422,
        )


def prompt_for_resolved_session(
    messages: list[ChatMessage],
    provider: str,
    latest_prompt: RuntimePrompt,
    *,
    fresh: bool,
) -> RuntimePrompt:
    """新規の SDK session にだけ、先行する発言をプロンプトの先頭へ付ける。

    cache hit、resume、``print_cli`` / ``cli_acp`` は ``prepare_chat_request`` が
    組んだプロンプトのままにする。``fresh`` は session 解決の後に渡す
    (cache hit でもなく、保存済み runtime session id からの resume でもない)。

    Args:
        messages: 最新の user message を含む、リクエストのメッセージ。
        provider: 解決済みの runtime provider。
        latest_prompt: 最新 user message、または CLI 向けに組んだ履歴。
        fresh: この turn が履歴を持たない runtime 会話の開始なら True。

    Returns:
        runtime へ渡すプロンプト。監査用の本文は最新メッセージのままにする。
    """
    if not fresh or provider not in _SDK_PROVIDERS:
        return latest_prompt
    last_user = max((index for index, message in enumerate(messages) if message.role == "user"), default=-1)
    earlier = [
        f"{message.role}: {prompt_audit_text(message.runtime_prompt())}"
        for message in messages[:last_user]
    ]
    if not earlier:
        return latest_prompt
    prefix = _fit_transcript(earlier)
    if isinstance(latest_prompt, str):
        return f"{prefix}\n{latest_prompt}"
    return [RuntimeTextInput(text=prefix), *latest_prompt]


def _fit_transcript(lines: list[str]) -> str:
    """Keep the newest earlier messages inside the transcript character cap."""
    kept = lines
    omitted = False
    while True:
        rendered = _render_transcript(kept, omitted)
        if len(rendered) <= _TRANSCRIPT_LIMIT or not kept:
            return rendered
        kept = kept[1:]
        omitted = True


def _render_transcript(lines: list[str], omitted: bool) -> str:
    body = ["Earlier conversation:"]
    if omitted:
        body.append("[earlier messages omitted]")
    body.extend(lines)
    body.append("---")
    return "\n".join(body)
