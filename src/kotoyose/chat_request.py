"""Chat request から runtime 実行前の解決済み context を組み立てる。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from .chat_policy import ChatPolicyViolation, validate_image_input
from .config import ModelPreset
from .conversation_key import derive_lookup_key
from .models import ChatCompletionRequest
from .runtime.acp import AcpPrompt
from .runtime.base import RuntimePrompt, prompt_audit_text, prompt_image_urls, prompt_text


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
