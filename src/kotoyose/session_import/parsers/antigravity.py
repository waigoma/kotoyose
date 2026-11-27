"""Parser for Antigravity CLI transcripts (``brain/<uuid>/.system_generated/logs/transcript.jsonl``)."""

from __future__ import annotations

import re
from pathlib import Path

from kotoyose.session_import.models import ImportedTurn
from kotoyose.session_import.parsers.base import (
    TurnDraft,
    add_tokens,
    append_turn,
    iter_jsonl,
    non_empty_str,
    parse_timestamp,
)

_RESPONSE_TYPES = ("PLANNER_RESPONSE", "MODEL_RESPONSE")
# The CLI wraps the typed text and appends harness metadata blocks after the closing tag.
_USER_REQUEST = re.compile(r"\s*<USER_REQUEST>(.*)</USER_REQUEST>", re.DOTALL)


def _session_id(path: Path) -> str:
    parents = path.parents
    if path.name == "transcript.jsonl" and len(parents) > 2 and parents[0].name == "logs" and parents[1].name == ".system_generated":
        return parents[2].name or path.stem
    return path.parent.name or path.stem


def _user_text(content: object) -> str:
    if not isinstance(content, str):
        return ""
    wrapped = _USER_REQUEST.match(content)
    return (wrapped.group(1) if wrapped else content).strip()


class AntigravitySessionParser:
    """Pair user inputs with the model's visible replies; ``thinking`` is never read."""

    source_kind = "antigravity"

    def parse_file(self, path: Path) -> list[ImportedTurn]:
        session_id = _session_id(Path(path))
        turns: list[ImportedTurn] = []
        draft: TurnDraft | None = None

        for line in iter_jsonl(path):
            kind = line.get("type")
            timestamp = parse_timestamp(line.get("created_at") or line.get("timestamp"))

            if kind == "USER_INPUT":
                prompt = _user_text(line.get("content"))
                if prompt:
                    append_turn(turns, self.source_kind, draft)
                    draft = TurnDraft(session_id=session_id, prompt=prompt, received_at=timestamp)
            elif kind in _RESPONSE_TYPES and draft is not None:
                if line.get("tool_calls"):
                    draft.has_tool_calls = True
                draft.model = non_empty_str(line.get("model")) or draft.model
                draft.tokens_in = add_tokens(draft.tokens_in, line.get("input_tokens"))
                draft.tokens_out = add_tokens(draft.tokens_out, line.get("output_tokens"))
                content = non_empty_str(line.get("content"))
                if content:
                    draft.response_parts.append(content.strip())
                    draft.completed_at = timestamp or draft.completed_at

        append_turn(turns, self.source_kind, draft)
        return turns
