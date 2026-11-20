"""Chat request preparation service の単体テスト。"""

import pytest
from starlette.datastructures import Headers

from kotoyose.chat_policy import ChatPolicyViolation
from kotoyose.chat_request import RequestPreparationPorts, prepare_chat_request
from kotoyose.config import ModelPreset
from kotoyose.models import ChatCompletionRequest


def _request(messages: list[dict]) -> ChatCompletionRequest:
    return ChatCompletionRequest.model_validate({
        "model": "kotoyose-chat",
        "messages": messages,
    })


def _ports(*, cache_hit: bool = False) -> RequestPreparationPorts:
    return RequestPreparationPorts(
        resolve_session_identity=lambda *args: (
            "claude",
            "kotoyose-chat",
            "digest",
        ),
        is_cache_hit=lambda lookup_key, identity: cache_hit,
    )


def test_prepare_chat_request_rejects_missing_user_message():
    """User message がない request を runtime 接続前に拒否する。"""
    request = _request([{"role": "assistant", "content": "hello"}])

    with pytest.raises(ChatPolicyViolation, match="no user message"):
        prepare_chat_request(
            request,
            Headers(),
            ModelPreset(),
            _ports(),
            conversation_key=None,
        )


def test_prepare_chat_request_uses_resolved_identity_and_cache_result():
    """Lookup key と port の解決結果を prepared request へ引き継ぐ。"""
    request = _request([{"role": "user", "content": "hello"}])

    prepared = prepare_chat_request(
        request,
        Headers(),
        ModelPreset(),
        _ports(cache_hit=True),
        conversation_key="conversation-1",
    )

    assert prepared.lookup_key == "conversation-1"
    assert prepared.session_identity == ("claude", "kotoyose-chat", "digest")
    assert prepared.cache_hit is True
    assert prepared.latest_text == "hello"


def test_prepare_chat_request_respects_cache_port_rejection():
    """Cache 判定 port が拒否した session を miss として扱う。"""
    request = _request([{"role": "user", "content": "hello"}])

    prepared = prepare_chat_request(
        request,
        Headers(),
        ModelPreset(),
        _ports(),
        conversation_key="conversation-1",
    )

    assert prepared.cache_hit is False
