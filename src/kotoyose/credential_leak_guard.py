"""Shape-only detection for credential-like text in runtime output."""

import re
from dataclasses import dataclass

from .delegation import TOKEN_PATTERN


@dataclass(frozen=True, slots=True)
class Finding:
    """検出種別と開始位置・終了位置。終了位置はマッチに含まれない。"""

    kind: str
    start: int
    end: int


# The short JWT segment minima allow normal compact headers and claims while
# requiring a signature. Bound the two encoded JSON segments so a file scan
# with 128 KiB of overlap covers any match up to the minimum signature length.
# Provider key minima exclude short placeholders.
_PATTERNS = (
    ("jwt", r"eyJ[A-Za-z0-9_-]{13,8192}\.eyJ[A-Za-z0-9_-]{9,65536}\.[A-Za-z0-9_-]{32,}"),
    ("anthropic_oauth_access", r"sk-ant-oat01-[A-Za-z0-9_-]{32,}"),
    ("anthropic_oauth_refresh", r"sk-ant-ort01-[A-Za-z0-9_-]{32,}"),
    ("anthropic_api_key", r"sk-ant-api03-[A-Za-z0-9_-]{32,}"),
    ("openai_project_key", r"sk-proj-[A-Za-z0-9_-]{40,}"),
    # The left boundary excludes identifiers like task-<hex SHA>.
    # The public Codex client treats refresh tokens as opaque strings.
    ("openai_api_key", r"sk-(?<![A-Za-z0-9_]sk-)[A-Za-z0-9]{40,}"),
    ("google_oauth_access", r"ya29\.[A-Za-z0-9_-]{24,}"),
    ("google_oauth_refresh", r"1//0[A-Za-z0-9_-]{24,}"),
    ("kotoyose_delegation_token", TOKEN_PATTERN),
    ("cursor_api_key", r"crsr_[A-Za-z0-9_-]{24,}"),
    ("xai_api_key", r"xai-[A-Za-z0-9_-]{32,}"),
    # A header alone is sufficient, including PKCS#8 and OpenSSH variants.
    ("pem_private_key", r"-----BEGIN (?:[A-Z0-9]+ ){0,4}PRIVATE KEY-----"),
)

_TEXT_RULES = tuple((kind, re.compile(pattern)) for kind, pattern in _PATTERNS)
_BYTE_RULES = tuple((kind, re.compile(pattern.encode("ascii"))) for kind, pattern in _PATTERNS)


def find_credential_shapes(text: str) -> list[Finding]:
    """元の文字列の順に検出種別と文字オフセットを返す。マッチ文字列は保持しない。"""
    return sorted(
        (Finding(kind, *match.span()) for kind, rule in _TEXT_RULES for match in rule.finditer(text)),
        key=lambda finding: finding.start,
    )


def credential_byte_kinds(data: bytes, *, start: int = 0) -> list[str]:
    """Inspect ASCII shapes directly in byte payloads without decoding them."""
    return [kind for kind, rule in _BYTE_RULES for _ in rule.finditer(data, start)]
