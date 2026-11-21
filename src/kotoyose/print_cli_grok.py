"""Output hooks for Grok Build streaming JSON, model discovery, and billing."""

from __future__ import annotations

from datetime import datetime, timezone
import math
import re
from typing import TYPE_CHECKING, Any

from .print_cli_events import PrintCliStreamOutcome, _make_runtime_event, extract_usage, parse_json_text

if TYPE_CHECKING:
    from .print_cli_hooks import ParsedQuota
    from .runtime.base import RuntimeEvent


def parse_grok_billing(stdout: bytes, *, stderr: bytes) -> ParsedQuota:
    """Extract the plan and shared weekly usage from a Grok billing result."""
    payload = parse_json_text(stdout.decode("utf-8"))
    if not isinstance(payload, dict):
        return {"auth": "unknown", "quota": [], "last_error": "Invalid Grok billing result"}

    tier = payload.get("subscription_tier")
    parsed: ParsedQuota = {
        "auth": "ok", "quota": [], "last_error": None,
        "plan": tier.strip()[:128] if isinstance(tier, str) and tier.strip() else None,
    }
    config = payload.get("config")
    if not isinstance(config, dict):
        return parsed
    percent = config.get("creditUsagePercent")
    if isinstance(percent, dict):
        percent = percent.get("val")
    if type(percent) not in (float, int) or (isinstance(percent, float) and not math.isfinite(percent)):
        return parsed

    resets_at = None
    period = config.get("currentPeriod")
    end = period.get("end") if isinstance(period, dict) else None
    if isinstance(end, str):
        try:
            reset = datetime.fromisoformat(end)
            if reset.tzinfo is not None:
                resets_at = reset.astimezone(timezone.utc).isoformat()
        except (ValueError, OverflowError):
            pass
    parsed["quota"].append({
        "window": "7d", "label": "Weekly", "used_percent": float(max(0, min(100, percent))),
        "resets_at": resets_at, "source": "grok_acp_billing",
    })
    return parsed

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ERROR_PATTERNS = (
    ("UNAUTHENTICATED", r"not (?:logged in|authenticated)|auth(?:entication)? required|unauthenticated|"
     r"unauthorized|please (?:log|sign) in|grok login|(?:http|status)[: =]+401"),
    ("RESOURCE_EXHAUSTED", r"(?:quota|usage limit) (?:exceeded|exhausted|reached)|resource exhausted"),
    ("RATE_LIMITED", r"rate[ _-]limit|too many requests|(?:http|status)[: =]+429"),
    ("NOT_FOUND", r"unknown model|model\b.*?\b(?:not (?:available|found)|unavailable)"),
)
_MODEL_ROW = re.compile(
    r"^\s*(?:[|│*•-]\s*)?(?P<id>[a-z0-9][a-z0-9._:/-]*)"
    r"(?:\s*(?:\||│|:|—|–)\s*|\s+-\s+|\s{2,}|\t+|\s*$)(?P<label>[^|│]*)(?:[|│].*)?$",
)
_USAGE_KEYS = {
    "inputTokens": "input_tokens",
    "outputTokens": "output_tokens",
    "cachedInputTokens": "cache_read_tokens",
    "cacheReadTokens": "cache_read_tokens",
    "reasoningTokens": "thinking_tokens",
    "totalTokens": "total_tokens",
}


# Grok の ACP は使用量を session/prompt の結果の `_meta.usage` に置く (1.0.89 で実測)。
# costUsdTicks は 1e-10 USD 単位。同じ prompt の `-p` の total_cost_usd と単価で一致を確かめた。
_USD_TICKS = 10**10


def parse_grok_acp_usage(result: dict[str, Any]) -> dict[str, int | float]:
    from .acp_turn import acp_usage

    meta = result.get("_meta")
    usage = meta.get("usage") if isinstance(meta, dict) else None
    found: dict[str, int | float] = dict(acp_usage(usage))
    ticks = usage.get("costUsdTicks") if isinstance(usage, dict) else None
    if type(ticks) is int and 0 <= ticks < 2**63:
        found["cost"] = ticks / _USD_TICKS
    return found


def parse_grok_error(stderr: str) -> dict[str, str | int | bool]:
    for status, pattern in _ERROR_PATTERNS:
        if re.search(pattern, _ANSI.sub("", stderr), re.IGNORECASE):
            return {"status": status}
    return {}


def parse_grok_models(stdout: bytes) -> list[dict[str, str]]:
    models: dict[str, dict[str, str]] = {}
    for line in _ANSI.sub("", stdout.decode(errors="replace")).splitlines():
        match = _MODEL_ROW.match(line.strip().rstrip("|│"))
        if not match:
            continue
        model_id = match["id"].rstrip(":")
        if model_id != "auto" and not any(c in model_id for c in "-._:/"):
            continue
        label = match["label"].strip() or model_id
        models.setdefault(model_id, {"id": model_id, "name": label, "label": label})
    return list(models.values())


def _text(event: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = event.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


class GrokStreamingJsonParser:
    def __init__(self) -> None:
        self.outcome = PrintCliStreamOutcome()
        self._chunks: list[str] = []
        self._handlers = {
            "thought": self._thought,
            "text": self._delta,
            "tool_call": self._tool,
            "tool_call_update": self._tool,
            "usage": self._usage,
            "end": self._end,
        }

    def feed_line(self, line: str) -> list[RuntimeEvent]:
        if self.outcome.saw_result or not line.strip():
            return []
        event = parse_json_text(line)
        if event is None:
            self.outcome.malformed_lines += 1
            return []
        handler = self._handlers.get(str(event.get("type")))
        return handler(event) if handler else []

    @staticmethod
    def _thought(event: dict) -> list[RuntimeEvent]:
        return []

    def _delta(self, event: dict) -> list[RuntimeEvent]:
        # Measured on 1.0.89 (2026-11-19): {"type": "text", "data": "<text>"}.
        text = _text(event, ("data", "text", "delta", "content"))
        if not text:
            return []
        self._chunks.append(text)
        return [_make_runtime_event(kind="text", summary=text)]

    @staticmethod
    def _tool(event: dict) -> list[RuntimeEvent]:
        call = event.get("toolCall", event.get("tool_call"))
        call = call if isinstance(call, dict) else event
        name = _text(call, ("toolName", "name", "tool"))
        call_id = _text(call, ("toolCallId", "callId", "id"))
        state = "updated" if event["type"] == "tool_call_update" else "started"
        return [_make_runtime_event(
            kind="tool", tool_name=name or None, summary=f"tool {state}",
            payload={"state": state, **({"tool_id": call_id} if call_id else {})},
        )]

    def _usage(self, event: dict) -> list[RuntimeEvent]:
        usage = event.get("usage", event)
        if isinstance(usage, dict):
            normalized = extract_usage({"usage": usage}) or {}
            normalized.update({
                target: value for source, target in _USAGE_KEYS.items()
                if type(value := usage.get(source)) is int and value >= 0
            })
            if normalized:
                self.outcome.usage.update(normalized, source="grok_streaming_json")
        return []

    def _end(self, event: dict) -> list[RuntimeEvent]:
        self.outcome.saw_result = True
        self.outcome.session_id = _text(event, ("sessionId", "session_id")) or None
        reason = _text(event, ("stopReason", "stop_reason"))
        error = event.get("error")
        self.outcome.result_status = (
            "ERROR" if error or reason.lower() in {"error", "failed", "cancelled", "canceled"} else "SUCCESS"
        )
        if isinstance(error, dict):
            error = _text(error, ("message", "code"))
        self.outcome.diagnostics.update(parse_grok_error(error if isinstance(error, str) else reason))
        self.outcome.content = _text(event, ("text", "answer", "result", "content")) or "".join(self._chunks)
        if "usage" in event:
            self._usage(event)
        cost = event.get("total_cost_usd")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0:
            self.outcome.usage["cost"] = float(cost)
        return []

    def finalize(self) -> PrintCliStreamOutcome:
        if not self.outcome.saw_result:
            self.outcome.content = "".join(self._chunks)
        return self.outcome
