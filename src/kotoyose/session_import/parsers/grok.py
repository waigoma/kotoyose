"""Parser for Grok CLI sessions (``~/.grok/sessions/<encoded cwd>/<id>/chat_history.jsonl``)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from kotoyose.session_import.models import ImportedTurn, normalize_session_title
from kotoyose.session_import.parsers.base import (
    TurnDraft,
    append_turn,
    as_dict,
    extract_typed_text,
    iter_jsonl,
    join_text_blocks,
    non_empty_str,
    parse_timestamp,
    token_count,
)

# Blocks Grok puts in front of (or instead of) the typed text.
_INJECTED_TAGS = ("INSTRUCTIONS", "environment_context", "rules", "system-reminder", "user_info")
_ENCODED_SEPARATORS = ("%2f", "%5c")


@dataclass
class _Session:
    """What the sibling ``summary.json`` and ``usage.json`` say about the transcript."""

    session_id: str
    cwd: str | None
    default_model: str | None
    created_at: Any
    session_title: str | None
    usage: dict[int, dict[str, Any]] = field(default_factory=dict)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents) or not path.is_file():
            return {}
        return as_dict(json.loads(path.read_bytes()))
    except (OSError, ValueError, RecursionError):
        return {}


def _cwd_from_directory(name: str) -> str | None:
    if any(separator in name.lower() for separator in _ENCODED_SEPARATORS):
        return unquote(name)
    return None


def _usage_by_turn(usage: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """Index the usage records by their 1-based ``turnNumber``."""
    records = usage.get("turns")
    if not isinstance(records, list):
        return {}
    return {
        number: record
        for record in records
        if isinstance(record, dict) and (number := token_count(record.get("turnNumber"))) is not None
    }


def _read_session(path: Path) -> _Session:
    summary = _read_json(path.parent / "summary.json")
    info = as_dict(summary.get("info"))
    return _Session(
        session_id=non_empty_str(info.get("id")) or path.parent.name or path.stem,
        cwd=non_empty_str(info.get("cwd")) or _cwd_from_directory(path.parent.parent.name),
        default_model=non_empty_str(summary.get("current_model_id")),
        created_at=summary.get("created_at"),
        session_title=normalize_session_title(summary.get("generated_title")),
        usage=_usage_by_turn(_read_json(path.parent / "usage.json")),
    )


def _typed_prompt(content: Any) -> str:
    blocks = [content] if isinstance(content, str) else []
    if isinstance(content, list):
        blocks = [
            block["text"]
            for block in content
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
        ]
    typed = (extract_typed_text(text, _INJECTED_TAGS) for text in blocks)
    return "\n\n".join(text for text in typed if text)


def _turn_number(line: dict[str, Any], previous: int) -> int:
    """Return the 1-based number of a prompt; ``prompt_index`` is 0-based and optional."""
    prompt_index = token_count(line.get("prompt_index"))
    return previous + 1 if prompt_index is None else prompt_index + 1


def _apply_usage(draft: TurnDraft, session: _Session, number: int, usage: dict[str, Any]) -> None:
    """Fill the times, tokens and model of turn ``number``.

    ``summary.updated_at`` is not used: it moves as the session grows, and the
    completion time is part of the request id.
    """
    if number == 1:
        started = session.created_at
    else:
        started = session.usage.get(number - 1, {}).get("endedAt")
    draft.received_at = parse_timestamp(started)
    draft.completed_at = parse_timestamp(usage.get("endedAt"))
    draft.tokens_in = token_count(usage.get("inputTokens"))
    draft.tokens_out = token_count(usage.get("outputTokens"))
    draft.model = draft.model or non_empty_str(usage.get("primaryModelId")) or session.default_model


class GrokSessionParser:
    """Pair typed prompts with visible assistant text; reasoning and tool output are never read."""

    source_kind = "grok"

    def parse_file(self, path: Path) -> list[ImportedTurn]:
        path = Path(path)
        session = _read_session(path)
        turns: list[ImportedTurn] = []
        draft: TurnDraft | None = None
        number = 0
        billed: set[int] = set()

        def finish_turn() -> None:
            if draft is None:
                return
            # A usage record counts once, even if two prompts claim the same number.
            usage = {} if number in billed else session.usage.get(number, {})
            _apply_usage(draft, session, number, usage)
            count = len(turns)
            append_turn(turns, self.source_kind, draft)
            if len(turns) > count:
                billed.add(number)

        for line in iter_jsonl(path):
            kind = line.get("type")
            if kind == "user":
                # Lines with a synthetic_reason were written by the harness.
                prompt = "" if line.get("synthetic_reason") else _typed_prompt(line.get("content"))
                if prompt:
                    finish_turn()
                    number = _turn_number(line, number)
                    draft = TurnDraft(session_id=session.session_id, prompt=prompt, cwd=session.cwd)
            elif draft is None:
                continue
            elif kind == "tool_result":
                draft.has_tool_calls = True
            elif kind == "assistant":
                if line.get("tool_calls"):
                    draft.has_tool_calls = True
                draft.model = non_empty_str(line.get("model_id")) or draft.model
                if text := join_text_blocks(line.get("content"), ("text",)):
                    draft.response_parts.append(text)

        finish_turn()
        for turn in turns:
            turn.session_title = session.session_title
        return turns
