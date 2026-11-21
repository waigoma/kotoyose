"""Provider usage を interaction と OpenAI 互換 response 向けに正規化する。"""

from __future__ import annotations

from .models import ChatCompletionUsage, CompletionTokenDetails


def extract_tokens(
    usage: dict | None,
    prompt: str,
    response: str,
) -> tuple[int, int, str]:
    """SDK token 数、または文字数ベースの安全な近似値を返す。

    Args:
        usage: Provider が返した usage dictionary。
        prompt: 最新の user prompt。
        response: 完全な assistant response。

    Returns:
        Input token、output token、値の由来を表す tuple。
    """
    if usage and ("input_tokens" in usage or "output_tokens" in usage):
        return (
            int(usage.get("input_tokens", 0) or 0),
            int(usage.get("output_tokens", 0) or 0),
            "sdk",
        )
    return (
        max(1, len(prompt) // 4),
        max(1, len(response) // 4),
        "estimated",
    )


def build_response_usage(
    usage: dict | None,
    tokens_in: int,
    tokens_out: int,
) -> ChatCompletionUsage:
    """Provider ごとの差を OpenAI 互換 usage field へ変換する。

    Args:
        usage: Provider 固有の usage dictionary。
        tokens_in: SDK 値がない場合に使用する input token 数。
        tokens_out: SDK 値がない場合に使用する output token 数。

    Returns:
        OpenAI 互換の ChatCompletionUsage。
    """
    usage = usage or {}

    def integer(*keys: str) -> int | None:
        for key in keys:
            value = usage.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return max(0, int(value))
        return None

    # Claude の chat runtime は cost_usd、実行の usage 契約と print CLI / ACP の hook は cost を使う。
    cost = usage.get("cost_usd", usage.get("cost"))
    cost_usd = (
        float(cost)
        if isinstance(cost, (int, float)) and not isinstance(cost, bool)
        else None
    )
    prompt_tokens = integer("prompt_tokens", "input_tokens")
    completion_tokens = integer("completion_tokens", "output_tokens")
    return ChatCompletionUsage(
        prompt_tokens=tokens_in if prompt_tokens is None else prompt_tokens,
        completion_tokens=tokens_out if completion_tokens is None else completion_tokens,
        cache_read_tokens=integer("cache_read_tokens", "cache_read_input_tokens", "cached_input_tokens") or 0,
        cache_write_tokens=integer("cache_write_tokens", "cache_creation_input_tokens") or 0,
        cost_usd=cost_usd,
        completion_tokens_details=(CompletionTokenDetails(reasoning_tokens=integer("reasoning_tokens", "reasoning_output_tokens") or 0)
                                   if integer("reasoning_tokens", "reasoning_output_tokens") is not None else None),
    )

