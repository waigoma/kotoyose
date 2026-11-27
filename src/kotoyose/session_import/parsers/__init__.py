"""Registry of session log parser plugins, keyed by ``source_kind``."""

from __future__ import annotations

from kotoyose.session_import.parsers.antigravity import AntigravitySessionParser
from kotoyose.session_import.parsers.base import SessionParser
from kotoyose.session_import.parsers.claude import ClaudeSessionParser
from kotoyose.session_import.parsers.codex import CodexSessionParser
from kotoyose.session_import.parsers.cursor import CursorSessionParser
from kotoyose.session_import.parsers.grok import GrokSessionParser

__all__ = [
    "AntigravitySessionParser",
    "ClaudeSessionParser",
    "CodexSessionParser",
    "CursorSessionParser",
    "GrokSessionParser",
    "SessionParser",
    "get_parser",
    "list_parser_kinds",
    "register_parser",
]

_REGISTRY: dict[str, SessionParser] = {}


def register_parser(parser: SessionParser) -> None:
    """Register ``parser`` under its ``source_kind``, replacing any previous one."""
    _REGISTRY[parser.source_kind] = parser


def get_parser(source_kind: str) -> SessionParser:
    """Return the parser for ``source_kind``.

    Raises:
        KeyError: if no parser is registered for ``source_kind``.
    """
    try:
        return _REGISTRY[source_kind]
    except KeyError:
        raise KeyError(f"unknown session source kind: {source_kind!r}") from None


def list_parser_kinds() -> tuple[str, ...]:
    """Return the registered source kinds in registration order."""
    return tuple(_REGISTRY)


for _parser in (
    ClaudeSessionParser(),
    CodexSessionParser(),
    AntigravitySessionParser(),
    CursorSessionParser(),
    GrokSessionParser(),
):
    register_parser(_parser)
del _parser
