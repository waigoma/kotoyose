"""Output hooks for the Cursor CLI."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .print_cli_events import (
    PrintCliStreamOutcome,
    _make_runtime_event,
    extract_session_id,
    extract_usage,
    parse_json_text,
)

if TYPE_CHECKING:
    from .runtime.base import RuntimeEvent

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ERROR_PATTERNS = (
    ("UNAUTHENTICATED", r"\b(?:not authenticated|not logged in|authentication required|"
     r"unauthenticated|please (?:log|sign) in|(?:run|use) [`'\" ]*agent login|(?:http|status)[: ]+401)\b"),
    ("RATE_LIMITED", r"\b(?:rate[ _-]limit(?:ed| exceeded)?|too many requests|(?:http|status)[: ]+429)\b"),
    ("RESOURCE_EXHAUSTED", r"\b(?:quota|usage limit)\s+(?:exceeded|exhausted|reached)\b"),
    ("NOT_FOUND", r"\b(?:unknown model|model (?:[^\s]+ )?(?:not (?:available|found)|unavailable))\b"),
)
_MODEL_ROW = re.compile(
    r"^\s*(?:[|│]\s*)?(?P<id>[a-z0-9][a-z0-9._:/-]*)"
    r"(?: +[-–—] +|\s*[|│]\s*|\s{2,}|\t+)(?P<label>[^|│]+)",
)


def parse_cursor_error(stderr: str) -> dict[str, str | int | bool]:
    cleaned = _ANSI.sub("", stderr)
    for status, pattern in _ERROR_PATTERNS:
        if re.search(pattern, cleaned, re.IGNORECASE):
            return {"status": status}
    return {}


def parse_cursor_models(stdout: bytes) -> list[dict[str, str]]:
    models: dict[str, dict[str, str]] = {}
    for line in _ANSI.sub("", stdout.decode("utf-8", errors="replace")).splitlines():
        match = _MODEL_ROW.match(line)
        if match:
            model_id = match["id"]
            label = match["label"].strip()
            if label and model_id not in {"id", "model"}:
                models.setdefault(model_id, {"id": model_id, "name": label, "label": label})
    return list(models.values())


class CursorStreamJsonParser:
    def __init__(self) -> None:
        self.outcome = PrintCliStreamOutcome()

    def feed_line(self, line: str) -> list[RuntimeEvent]:
        if self.outcome.saw_result or not line.strip():
            return []
        event = parse_json_text(line)
        if event is None:
            self.outcome.malformed_lines += 1
            return []
        self.outcome.session_id = extract_session_id(event) or self.outcome.session_id
        kind, subtype = event.get("type"), event.get("subtype")
        if kind == "system" and subtype == "init":
            self.outcome.saw_init = True
        elif kind == "assistant":
            message = event.get("message")
            blocks = message.get("content") if isinstance(message, dict) else None
            if isinstance(blocks, list):
                return [
                    _make_runtime_event(kind="text", summary=block["text"])
                    for block in blocks
                    if isinstance(block, dict) and block.get("type") == "text"
                    and isinstance(block.get("text"), str) and block["text"]
                ]
        elif kind == "tool_call" and subtype in ("started", "completed"):
            call = event.get("tool_call")
            name = next(iter(call), "") if isinstance(call, dict) else ""
            call_id = event.get("call_id")
            return [
                _make_runtime_event(
                    kind="tool", tool_name=name.removesuffix("ToolCall") or None, summary=f"tool {subtype}",
                    payload={"state": subtype, **({"tool_id": call_id} if isinstance(call_id, str) else {})},
                )
            ]
        elif kind == "result":
            self.outcome.saw_result = True
            result = event.get("result")
            if subtype == "success" and event.get("is_error") is not True and isinstance(result, str):
                self.outcome.result_status = "SUCCESS"
                self.outcome.content = result
            else:
                self.outcome.result_status = "ERROR"
                if isinstance(result, str):
                    self.outcome.diagnostics.update(parse_cursor_error(result))
            usage = extract_usage(event) or _cursor_usage(event.get("usage"))
            if usage:
                self.outcome.usage.update(usage, source="cursor_stream_json")
        return []

    def finalize(self) -> PrintCliStreamOutcome:
        return self.outcome


# Cursor's terminal `result.usage` is camelCase (measured 2026-11-19 on 2026.11.13):
# {"inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens", "reasoningTokens"}.
_CURSOR_USAGE_KEYS = {"inputTokens": "input_tokens", "outputTokens": "output_tokens"}


def _cursor_usage(usage: object) -> dict[str, int] | None:
    if not isinstance(usage, dict):
        return None
    found = {
        target: value for source, target in _CURSOR_USAGE_KEYS.items()
        if isinstance(value := usage.get(source), int) and not isinstance(value, bool) and value >= 0
    }
    return found or None

