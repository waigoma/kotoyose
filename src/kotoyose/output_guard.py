"""Failure and redaction policy for outbound runtime output."""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import structlog

from .credential_leak_guard import credential_byte_kinds, find_credential_shapes

if TYPE_CHECKING:
    from .runtime.base import RuntimeEvent

log = structlog.get_logger()
LEAK_REASON = "credential_leak_suspected"
LEAK_MESSAGE = "Output blocked because it contained a suspected credential."


class CredentialLeakSuspected(RuntimeError):
    def __init__(self):
        super().__init__(LEAK_MESSAGE)


def guard_enabled() -> bool:
    from .config import get_config

    return get_config().credential_leak_guard


def report_findings(kinds: list[str], *, path: str | None = None) -> None:
    log.warning(LEAK_REASON, counts=dict(Counter(kinds)), path=redact_event(path))


def check_answer(answer: str, *, start: int = 0) -> None:
    findings = [finding for finding in find_credential_shapes(answer) if finding.start >= start]
    if findings:
        report_findings([finding.kind for finding in findings])
        raise CredentialLeakSuspected


def redact_event(value: Any) -> Any:
    if isinstance(value, str):
        # Withhold the entire field, including any PEM body following a header.
        return "[redacted: suspected credential]" if find_credential_shapes(value) else value
    if isinstance(value, bytes):
        return "[redacted: suspected credential]" if credential_byte_kinds(value) else value
    if isinstance(value, dict):
        return {redact_event(key): redact_event(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_event(item) for item in value]
    return value


def redact_runtime_event(event: RuntimeEvent) -> RuntimeEvent:
    return replace(
        event,
        kind=redact_event(event.kind),
        tool_name=redact_event(event.tool_name),
        summary=redact_event(event.summary),
        payload=redact_event(event.payload),
    )
