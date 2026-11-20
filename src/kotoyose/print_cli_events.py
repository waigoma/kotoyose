"""Stream parser registry and shared event/error helpers for print-mode CLIs."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
import json
import re
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from .runtime.base import RuntimeEvent


def _make_runtime_event(
    *,
    kind: str,
    tool_name: str | None = None,
    summary: str | None = None,
    payload: dict[str, Any] | None = None,
) -> RuntimeEvent:
    from .runtime.base import RuntimeEvent

    return RuntimeEvent(kind=kind, tool_name=tool_name, summary=summary, payload=payload)

TERMINAL_FAILURE_STATUSES = frozenset(
    {"ERROR", "FAILED", "CANCELLED", "CANCELED", "TIMEOUT", "MAX_TURNS"}
)
CANONICAL_STATUSES = TERMINAL_FAILURE_STATUSES | frozenset(
    {
        "INVALID_ARGUMENT",
        "DEADLINE_EXCEEDED",
        "NOT_FOUND",
        "ALREADY_EXISTS",
        "PERMISSION_DENIED",
        "RESOURCE_EXHAUSTED",
        "FAILED_PRECONDITION",
        "ABORTED",
        "OUT_OF_RANGE",
        "UNIMPLEMENTED",
        "INTERNAL",
        "UNAVAILABLE",
        "DATA_LOSS",
        "UNAUTHENTICATED",
        "UNKNOWN",
        "RATE_LIMITED",
    }
)
_USAGE_KEYS = frozenset(
    {"input_tokens", "output_tokens", "thinking_tokens", "cache_read_tokens", "total_tokens"}
)
_RESPONSE_STEP_TYPES = frozenset(
    {"", "agent_response", "response", "assistant", "assistant_message", "text"}
)


def parse_json_text(text: str) -> dict[str, Any] | None:
    """Parse a JSON object string, returning None for invalid or non-object payloads."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def extract_session_id(event: dict[str, Any]) -> str | None:
    """Extract a conversation/session identifier from a structured CLI event."""
    for key in (
        "conversation",
        "conversation_id",
        "conversationId",
        "session_id",
        "sessionId",
        "thread_id",
        "run_id",
    ):
        value = event.get(key)
        if isinstance(value, str) and value:
            return value
    for key in ("init", "result", "step_update"):
        nested = event.get(key)
        if isinstance(nested, dict):
            found = extract_session_id(nested)
            if found:
                return found
    return None


def extract_text_delta(event: dict[str, Any]) -> str:
    """Extract response text or text delta from a CLI event object."""
    for key in ("text_delta", "text", "response", "output", "message"):
        value = event.get(key)
        if isinstance(value, str):
            return value
    for key in ("step_update", "result", "content"):
        nested = event.get(key)
        if isinstance(nested, dict):
            found = extract_text_delta(nested)
            if found:
                return found
    return ""


def extract_usage(event: dict[str, Any]) -> dict[str, int] | None:
    """Extract non-negative integer token usage fields from a CLI event."""
    usage = event.get("usage") or event.get("stats")
    if isinstance(usage, dict):
        return {
            key: value
            for key, value in usage.items()
            if key in _USAGE_KEYS
            and isinstance(value, int)
            and not isinstance(value, bool)
            and value >= 0
        } or None
    for key in ("result", "step_update"):
        nested = event.get(key)
        if isinstance(nested, dict):
            found = extract_usage(nested)
            if found:
                return found
    return None


def runtime_event_from_step(step: dict[str, Any]) -> RuntimeEvent | None:
    """Convert a tool/checkpoint/subagent step into a sanitized RuntimeEvent."""
    step_type = str(step.get("step_type") or "").lower()
    if step_type not in {"tool", "checkpoint", "subagent"} and not step.get("subagent_info"):
        return None

    tool_info = step.get("tool_info")
    tool_name = step.get("tool_name")
    if not isinstance(tool_name, str) and isinstance(tool_info, dict):
        candidate = tool_info.get("name") or tool_info.get("tool_name") or tool_info.get("toolName")
        tool_name = candidate if isinstance(candidate, str) else None
    if not isinstance(tool_name, str):
        tool_name = None

    state = step.get("state")
    summary_parts = [part for part in (step_type, str(state) if state else None) if part]
    payload: dict[str, Any] = {}
    for key in ("step_index", "state", "duration_seconds"):
        value = step.get(key)
        if value is not None:
            payload[key] = value

    if isinstance(tool_info, dict):
        # Do not persist command lines, arguments, paths, or tool results into the
        # generic activity event: they may contain source code or credentials.
        for key in ("id", "status", "server_name", "serverName"):
            value = tool_info.get(key)
            if isinstance(value, (str, int, float, bool)):
                payload[f"tool_{key}"] = value

    subagent = step.get("subagent_info")
    if isinstance(subagent, dict):
        # Transcript / prompt payloads may contain user code or secrets. Only keep identifiers.
        for key in ("name", "agent_name", "status", "conversation_id"):
            value = subagent.get(key)
            if isinstance(value, (str, int, float, bool)):
                payload[f"subagent_{key}"] = value

    return _make_runtime_event(
        kind="tool" if step_type == "tool" else ("subagent" if subagent else step_type),
        tool_name=tool_name,
        summary=" ".join(summary_parts) or None,
        payload=payload or None,
    )


async def read_stderr_tail(reader: asyncio.StreamReader, *, max_bytes: int = 32768) -> str:
    """Bound retained diagnostics even when the child emits large stderr output."""
    tail = bytearray()
    while chunk := await reader.read(65536):
        tail.extend(chunk)
        if len(tail) > max_bytes:
            del tail[:-max_bytes]
    return tail.decode("utf-8", errors="replace")


def structured_error_fields(payload: dict[str, Any]) -> dict[str, str | int | bool]:
    """Allow typed classification fields, never error messages or arbitrary data."""
    result: dict[str, str | int | bool] = {}
    status = (
        payload.get("status")
        or payload.get("canonical_status")
        or payload.get("canonicalStatus")
    )
    if isinstance(status, str) and status.upper() in CANONICAL_STATUSES:
        result["status"] = status.upper()
    code = payload.get("code")
    if isinstance(code, int) and not isinstance(code, bool) and 0 <= code <= 599:
        result["api_error_status"] = code
    retryable = payload.get("retryable")
    if isinstance(retryable, bool):
        result["retryable"] = retryable
    error_id = payload.get("error_id") or payload.get("errorId")
    if isinstance(error_id, str) and re.fullmatch(r"[0-9a-fA-F-]{8,64}", error_id):
        result["error_id"] = error_id
    return result


@dataclass
class PrintCliStreamOutcome:
    """Normalized final state accumulated from a print-CLI stdout stream."""

    saw_init: bool = False
    saw_result: bool = False
    session_id: str | None = None
    result_status: str | None = None
    content: str = ""
    usage: dict[str, Any] = field(
        default_factory=lambda: {
            "source": "unknown",
            "cost": None,
            "input_tokens": None,
            "output_tokens": None,
        }
    )
    turns: int | None = None
    malformed_lines: int = 0
    diagnostics: dict[str, Any] = field(default_factory=dict)
    raw_error_texts: list[str] = field(default_factory=list)


class PrintCliStreamParser(Protocol):
    """Stateful line-by-line parser for a print-CLI stdout protocol."""

    def feed_line(self, line: str) -> list[RuntimeEvent]:
        """Consume one stdout line and return zero or more sanitized progress events."""
        ...

    def finalize(self) -> PrintCliStreamOutcome:
        """Return the accumulated outcome once stdout reaches EOF."""
        ...


def create_print_cli_parser(kind: str) -> PrintCliStreamParser:
    """Instantiate the stream parser registered under this name."""
    from .print_cli_hooks import get_parser

    return get_parser(kind)()


def _values_from_payload(payload: Any) -> list[Any]:
    """JSON payload (dict または list) から model 候補の生値一覧を取り出す。

    dict の場合は "models" または "data" キー、list の場合はそれ自身を対象にする。

    Args:
        payload: json.loads 済みの値。

    Returns:
        model ID 抽出前の生値一覧 (文字列または dict の混在を許す)。
    """
    if isinstance(payload, dict):
        return payload.get("models") or payload.get("data") or []
    if isinstance(payload, list):
        return payload
    return []
