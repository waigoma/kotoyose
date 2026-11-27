"""Parser for Claude Code transcripts (``~/.claude/projects/**/*.jsonl``)."""

from __future__ import annotations

from collections.abc import Hashable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kotoyose.session_import.models import ImportedTurn
from kotoyose.session_import.parsers.base import (
    TurnDraft,
    add_tokens,
    append_turn,
    as_dict,
    iter_jsonl,
    join_text_blocks,
    non_empty_str,
    parse_timestamp,
    token_count,
)

_TOOL_CALL_BLOCKS = ("tool_use", "server_tool_use")
# Claude Code writes this placeholder on locally generated messages; it is not a model name.
_SYNTHETIC_MODEL = "<synthetic>"
# Harness-injected user text: notifications and command output, not something the person typed.
_HARNESS_TEXT_PREFIXES = (
    "<task-notification>",
    "<teammate-message",
    "<bash-stdout>",
    "<bash-stderr>",
    "<local-command-stdout>",
    "<local-command-caveat>",
    "<command-name>",
)
_HARNESS_ORIGIN_KINDS = ("task-notification", "teammate-message", "system", "compact")


@dataclass
class _ClaudeDraft(TurnDraft):
    # Keyed by message id: one API message is split over several lines that repeat its usage.
    usage: dict[Hashable, tuple[int | None, int | None]] = field(default_factory=dict)

    def token_totals(self) -> tuple[int | None, int | None]:
        tokens_in: int | None = None
        tokens_out: int | None = None
        for message_in, message_out in self.usage.values():
            tokens_in = add_tokens(tokens_in, message_in)
            tokens_out = add_tokens(tokens_out, message_out)
        return tokens_in, tokens_out


def _has_block(content: Any, block_types: tuple[str, ...]) -> bool:
    return isinstance(content, list) and any(isinstance(block, dict) and block.get("type") in block_types for block in content)


def _is_harness_notification(line: dict[str, Any], text: str) -> bool:
    origin = line.get("origin")
    if line.get("isCompactSummary") is True:
        return True
    if isinstance(origin, dict) and origin.get("kind") in _HARNESS_ORIGIN_KINDS:
        return True
    return text.lstrip().startswith(_HARNESS_TEXT_PREFIXES)


def _message_usage(usage: dict[str, Any]) -> tuple[int | None, int | None]:
    tokens_in = token_count(usage.get("input_tokens"))
    if tokens_in is not None:
        for key in ("cache_creation_input_tokens", "cache_read_input_tokens"):
            tokens_in += token_count(usage.get(key)) or 0
    return tokens_in, token_count(usage.get("output_tokens"))


class ClaudeSessionParser:
    """Pair user text with the visible assistant text that follows it."""

    source_kind = "claude"

    def parse_file(self, path: Path) -> list[ImportedTurn]:
        turns: list[ImportedTurn] = []
        draft: _ClaudeDraft | None = None
        usage_owners: dict[str, _ClaudeDraft] = {}
        cwd: str | None = None

        for line in iter_jsonl(path):
            kind = line.get("type")
            # Subagent (sidechain) transcripts reuse the parent's sessionId and would collide with its turns.
            if kind not in ("user", "assistant") or line.get("isMeta") is True or line.get("isSidechain") is True:
                continue
            cwd = non_empty_str(line.get("cwd")) or cwd
            message = as_dict(line.get("message"))
            content = message.get("content")
            text = join_text_blocks(content, ("text",))

            if kind == "user":
                if draft is not None and _has_block(content, ("tool_result",)):
                    draft.has_tool_calls = True
                # A line carrying only tool results belongs to the open turn; harness notices do too.
                if text and not _is_harness_notification(line, text):
                    append_turn(turns, self.source_kind, draft)
                    draft = _ClaudeDraft(
                        session_id=non_empty_str(line.get("sessionId")) or Path(path).stem,
                        prompt=text,
                        received_at=parse_timestamp(line.get("timestamp")),
                        cwd=cwd,
                    )
                continue

            if draft is None:
                continue
            if _has_block(content, _TOOL_CALL_BLOCKS):
                draft.has_tool_calls = True
            if text:
                draft.response_parts.append(text)
            model = non_empty_str(message.get("model"))
            if model and model != _SYNTHETIC_MODEL:
                draft.model = model
            draft.completed_at = parse_timestamp(line.get("timestamp")) or draft.completed_at

            usage = message.get("usage")
            if isinstance(usage, dict):
                message_id = non_empty_str(message.get("id"))
                owner = usage_owners.setdefault(message_id, draft) if message_id else draft
                # Later chunks overwrite earlier ones, so the final counts win.
                owner.usage[message_id or object()] = _message_usage(usage)

        append_turn(turns, self.source_kind, draft)
        return turns
