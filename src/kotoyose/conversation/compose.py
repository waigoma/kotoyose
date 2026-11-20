"""明示設定だけを会話 provider に接続する。SDK 認証への自動 fallback は行わない。"""

import os
from pathlib import Path
from urllib.parse import urlsplit

from .. import process_env
from ..config import get_config
from ..execution_resources import ExecutionError
from ..secret_store import SecretStoreError, resolve_secret_sync
from .contracts import Config, Start
from .ports import Message, Provider
from .provider import AnthropicProvider
from .service import Conversations


def create_conversations() -> Conversations | None:
    """KOTOYOSE_CONVERSATION_CONFIG がなければ API は未対応。設定に秘密を保存しない。"""
    path = os.environ.get("KOTOYOSE_CONVERSATION_CONFIG")
    if path is None:
        return None
    config = Config.model_validate_json(Path(path).read_text())
    kotoyose = get_config()
    for alias in config.models:
        if alias not in kotoyose.models or kotoyose.preset_for(alias).provider != "claude":
            raise ValueError("Conversation alias must resolve to a configured Claude Messages API model")

    def provider(request: Start) -> Provider:
        model = config.models.get(request.model)
        if model is None or request.model not in get_config().models:
            raise ExecutionError("model_not_found", f"Model '{request.model}' does not exist", 404)
        if request.max_output_tokens > model.max_output_tokens:
            raise ExecutionError("agent_contract_unsupported", "Conversation model or output budget is unsupported", 422)
        kotoyose = get_config()
        preset = kotoyose.preset_for(request.model)
        base_url = preset.anthropic_base_url or kotoyose.resolve_setting("anthropic_base_url", "ANTHROPIC_BASE_URL")
        if preset.anthropic_auth_token_secret:
            try:
                token = resolve_secret_sync(preset.anthropic_auth_token_secret)
            except SecretStoreError as exc:
                raise ExecutionError("agent_contract_unsupported", "Explicit Messages API secret is unavailable", 503) from exc
        else:
            token = (
                process_env.get(preset.anthropic_auth_token_env)
                if preset.anthropic_auth_token_env
                else kotoyose.resolve_setting("anthropic_auth_token", "ANTHROPIC_AUTH_TOKEN")
            )
        if not preset.model or not base_url or not token:
            raise ExecutionError("agent_contract_unsupported", "Explicit Messages API credentials are unavailable", 503)
        parsed = urlsplit(base_url)
        if (parsed.username or parsed.password or parsed.query or parsed.fragment or
                parsed.scheme not in {"http", "https"} or
                parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}):
            raise ExecutionError("agent_contract_unsupported", "Conversation provider URL is invalid", 503)
        url = base_url.rstrip("/")
        if not url.endswith("/v1"):
            url += "/v1"
        return AnthropicProvider(url=url, headers={"authorization": "Bearer " + token,
                                  "anthropic-version": "2023-06-01", "content-type": "application/json"},
                                 model=preset.model, instructions=request.instructions,
                                 messages=tuple(Message(item.role, item.content) for item in request.messages),
                                 tools=[tool.model_dump() for tool in request.tools],
                                 timeout=config.limits.stream_timeout_seconds,
                                 max_bytes=config.limits.max_output_bytes,
                                 input_token_budget=model.input_token_budget,
                                 response_tool_name_suffix=model.response_tool_name_suffix)

    return Conversations(config.limits, provider, config.models, frozenset(config.caller_ids))
