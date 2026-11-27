"""Credential redaction and UTF-8 safe truncation for imported session text."""

from __future__ import annotations

import re
from typing import NamedTuple

from ..credential_leak_guard import find_credential_shapes
from .models import MAX_TEXT_BYTES

REDACTED_PLACEHOLDER = "[REDACTED]"
TRUNCATION_SUFFIX = "\n...[truncated]"

# A complete PEM private key block. When the END line is missing, the block runs
# to the end of the input so that no key body line survives redaction.
_PEM_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN (?:[A-Z0-9]+ ){0,4}PRIVATE KEY-----"
    r"[\s\S]*?"
    r"(?:-----END (?:[A-Z0-9]+ ){0,4}PRIVATE KEY-----|\Z)"
)
# Only the token (group 2) is redacted so the "Bearer " label stays readable.
_BEARER_TOKEN = re.compile(r"(?i)(Bearer\s+)([A-Za-z0-9._~+/=-]{16,})")
_EXTRA_TOKEN_PATTERNS = (
    re.compile(r"kys_[A-Za-z0-9_-]{24,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{16,}"),
)


class _Span(NamedTuple):
    start: int
    end: int


def _collect_spans(text: str) -> list[_Span]:
    spans = [_Span(finding.start, finding.end) for finding in find_credential_shapes(text)]
    spans.extend(_Span(match.start(), match.end()) for match in _PEM_PRIVATE_KEY_BLOCK.finditer(text))
    spans.extend(_Span(*match.span(2)) for match in _BEARER_TOKEN.finditer(text))
    for pattern in _EXTRA_TOKEN_PATTERNS:
        spans.extend(_Span(match.start(), match.end()) for match in pattern.finditer(text))
    return spans


def _merge_spans(spans: list[_Span]) -> list[_Span]:
    """Merge overlapping and adjacent spans into disjoint, ordered spans."""
    merged: list[_Span] = []
    for span in sorted(spans):
        if merged and span.start <= merged[-1].end:
            if span.end > merged[-1].end:
                merged[-1] = _Span(merged[-1].start, span.end)
        else:
            merged.append(span)
    return merged


def redact_credentials(text: str) -> tuple[str, bool]:
    """Replace credential-like spans with ``[REDACTED]``.

    Returns ``(redacted_text, was_redacted)``. Clean text is returned unchanged
    with ``was_redacted=False``.
    """
    spans = _merge_spans(_collect_spans(text))
    if not spans:
        return text, False
    parts: list[str] = []
    cursor = 0
    for span in spans:
        parts.append(text[cursor : span.start])
        parts.append(REDACTED_PLACEHOLDER)
        cursor = span.end
    parts.append(text[cursor:])
    return "".join(parts), True


def truncate_utf8(text: str, max_bytes: int = MAX_TEXT_BYTES) -> tuple[str, bool]:
    """Truncate ``text`` so its UTF-8 encoding fits in ``max_bytes``.

    The cut always lands on a character boundary. When there is room, the
    result ends with ``TRUNCATION_SUFFIX`` and the whole result fits in
    ``max_bytes``. When ``max_bytes`` cannot hold the suffix, the raw prefix
    is returned without the suffix.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False
    suffix_bytes = len(TRUNCATION_SUFFIX.encode("utf-8"))
    if max_bytes <= suffix_bytes:
        prefix = encoded[: max(max_bytes, 0)].decode("utf-8", errors="ignore")
        return prefix, True
    budget = max_bytes - suffix_bytes
    # errors="ignore" drops only a trailing partial multi-byte sequence.
    prefix = encoded[:budget].decode("utf-8", errors="ignore")
    return prefix + TRUNCATION_SUFFIX, True


def sanitize_text(text: str, max_bytes: int = MAX_TEXT_BYTES) -> tuple[str, bool, bool]:
    """Redact credentials first, then truncate.

    Redacting first means a secret that straddles the byte limit is fully
    replaced before any cut happens.
    Returns ``(text, was_redacted, was_truncated)``.
    """
    redacted_text, was_redacted = redact_credentials(text)
    truncated_text, was_truncated = truncate_utf8(redacted_text, max_bytes)
    return truncated_text, was_redacted, was_truncated
