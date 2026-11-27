"""Parser for Codex rollout files (``~/.codex/sessions/**/*.jsonl``)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from kotoyose.session_import.models import ImportedTurn, normalize_session_title
from kotoyose.session_import.parsers.base import (
    TurnDraft,
    add_tokens,
    append_turn,
    as_dict,
    iter_jsonl,
    join_text_blocks,
    non_empty_str,
    parse_timestamp,
)

_USER_BLOCKS = ("input_text", "text")
_ASSISTANT_BLOCKS = ("output_text", "text")
_FINAL_PHASE = "final_answer"
_TOOL_ITEMS = (
    "function_call",
    "function_call_output",
    "custom_tool_call",
    "custom_tool_call_output",
    "web_search_call",
    "local_shell_call",
)
# Text the harness injects with role "user"; it is not something the person typed.
_INJECTED_PREFIXES = (
    "# AGENTS.md instructions",
    "<environment_context>",
    "<INSTRUCTIONS>",
    "<permissions instructions>",
    "<collaboration_mode>",
    "<user_instructions>",
    "<turn_aborted>",
    "<skill>",
)


@dataclass
class _CodexDraft(TurnDraft):
    """Open turn. ``tokens_*`` hold ``token_usage_record`` sums; ``counted_*`` the fallback."""

    final_parts: list[str] = field(default_factory=list)
    last_response: str = ""
    task_message: str = ""
    counted_in: int | None = None
    counted_out: int | None = None

    @property
    def has_response(self) -> bool:
        return bool(self.final_parts or self.last_response)

    def response_text(self) -> str:
        return "\n\n".join(self.final_parts) or self.last_response or self.task_message

    def token_totals(self) -> tuple[int | None, int | None]:
        if self.tokens_in is None and self.tokens_out is None:
            return self.counted_in, self.counted_out
        return self.tokens_in, self.tokens_out


def _is_injected(text: str) -> bool:
    return text.lstrip().startswith(_INJECTED_PREFIXES)


def _is_child_thread(payload: dict[str, Any]) -> bool:
    """Tell whether a ``session_meta`` payload describes a thread spawned by another one."""
    own_id = non_empty_str(payload.get("id"))
    session_id = non_empty_str(payload.get("session_id"))
    if own_id and session_id and own_id != session_id:
        return True
    source = payload.get("source")
    if source == "subagent" or (isinstance(source, dict) and "subagent" in source):
        return True
    return non_empty_str(payload.get("parent_thread_id")) is not None


def _user_text(content: Any) -> str:
    if isinstance(content, list):
        content = [block for block in content if not (isinstance(block, dict) and _is_injected(str(block.get("text", ""))))]
    elif isinstance(content, str) and _is_injected(content):
        return ""
    return join_text_blocks(content, _USER_BLOCKS)


@lru_cache(maxsize=8)
def _session_titles(resolved_path: str, mtime_ns: int, size: int) -> dict[str, str]:
    return {
        session_id: title
        for line in iter_jsonl(Path(resolved_path))
        if (session_id := non_empty_str(line.get("id")))
        and (title := normalize_session_title(line.get("thread_name")))
    }


def _session_title(path: Path, session_id: str) -> str | None:
    try:
        for parent in path.parents[:4]:
            if parent.is_symlink():
                return None
            if parent.name != "sessions":
                continue
            index = parent.parent / "session_index.jsonl"
            if index.is_symlink() or not index.is_file():
                return None
            if any(ancestor.is_symlink() for ancestor in index.parents):
                return None
            stat = index.stat()
            return _session_titles(str(index.resolve()), stat.st_mtime_ns, stat.st_size).get(session_id)
    except OSError:
        return None
    return None


class _RolloutReader:
    """Mutable state for reading one rollout file."""

    def __init__(self, source_kind: str, path: Path) -> None:
        self.source_kind = source_kind
        self.turns: list[ImportedTurn] = []
        self.draft: _CodexDraft | None = None
        self.session_id = Path(path.name.removesuffix(".zst")).stem
        self.session_meta_seen = False
        self.is_subagent = False
        self.agent_origin: str | None = None
        self.cwd: str | None = None
        self.model: str | None = None
        self.response_ids: set[str] = set()
        self.last_total_usage: dict[str, Any] | None = None

    def read(self, line: dict[str, Any]) -> None:
        kind = line.get("type")
        payload = as_dict(line.get("payload"))
        timestamp = parse_timestamp(line.get("timestamp"))
        if kind == "session_meta":
            self._session_meta(payload)
        elif kind == "turn_context":
            self._turn_context(payload)
        elif kind == "token_usage_record":
            self._usage_record(payload)
        elif kind == "event_msg":
            self._event(payload, timestamp)
        elif kind == "response_item":
            self._response_item(payload, timestamp)

    def finish_turn(self) -> None:
        append_turn(self.turns, self.source_kind, self.draft)
        self.draft = None

    def _session_meta(self, payload: dict[str, Any]) -> None:
        # The first record describes this rollout; later ones can be copied from a parent thread.
        if self.session_meta_seen:
            return
        self.session_meta_seen = True
        # Prefer the thread's own id: on child threads session_id can name the parent thread.
        self.session_id = non_empty_str(payload.get("id")) or non_empty_str(payload.get("session_id")) or self.session_id
        self.cwd = non_empty_str(payload.get("cwd")) or self.cwd
        if _is_child_thread(payload):
            self.is_subagent = True
            self.agent_origin = "subagent"

    def _turn_context(self, payload: dict[str, Any]) -> None:
        settings = as_dict(as_dict(payload.get("collaboration_mode")).get("settings"))
        self.model = non_empty_str(payload.get("model")) or non_empty_str(settings.get("model")) or self.model
        self.cwd = non_empty_str(payload.get("cwd")) or self.cwd
        # A turn that already answered keeps the context it answered under.
        if self.draft is not None and not self.draft.has_response:
            self.draft.model = self.model
            self.draft.cwd = self.cwd

    def _usage_record(self, payload: dict[str, Any]) -> None:
        if self.draft is None:
            return
        response_id = non_empty_str(payload.get("response_id"))
        if response_id:
            if response_id in self.response_ids:
                return
            self.response_ids.add(response_id)
        usage = as_dict(payload.get("usage"))
        self.draft.tokens_in = add_tokens(self.draft.tokens_in, usage.get("input_tokens"))
        self.draft.tokens_out = add_tokens(self.draft.tokens_out, usage.get("output_tokens"))

    def _token_count(self, payload: dict[str, Any]) -> None:
        info = as_dict(payload.get("info"))
        total = info.get("total_token_usage")
        if isinstance(total, dict):
            # Codex re-emits the same counts (e.g. on rate limit updates).
            if total == self.last_total_usage:
                return
            self.last_total_usage = total
        if self.draft is None:
            return
        usage = as_dict(info.get("last_token_usage") or info.get("usage") or payload.get("usage"))
        self.draft.counted_in = add_tokens(self.draft.counted_in, usage.get("input_tokens"))
        self.draft.counted_out = add_tokens(self.draft.counted_out, usage.get("output_tokens"))

    def _event(self, payload: dict[str, Any], timestamp: datetime | None) -> None:
        kind = payload.get("type")
        if kind == "token_count":
            self._token_count(payload)
        elif kind == "user_message":
            self._user_message(_user_text(payload.get("message")), timestamp)
        elif kind == "agent_message":
            self._assistant_message(join_text_blocks(payload.get("message"), _ASSISTANT_BLOCKS), payload.get("phase"), timestamp)
        elif kind == "task_complete" and self.draft is not None:
            self.draft.task_message = (non_empty_str(payload.get("last_agent_message")) or "").strip()
            # Prefer the response line's time so that it does not move once the response is written.
            self.draft.completed_at = self.draft.completed_at or parse_timestamp(payload.get("completed_at")) or timestamp
            self.finish_turn()

    def _response_item(self, payload: dict[str, Any], timestamp: datetime | None) -> None:
        kind = payload.get("type")
        if kind == "message":
            role = payload.get("role")
            if role == "user":
                self._user_message(_user_text(payload.get("content")), timestamp)
            elif role == "assistant":
                self._assistant_message(join_text_blocks(payload.get("content"), _ASSISTANT_BLOCKS), payload.get("phase"), timestamp)
        elif kind in _TOOL_ITEMS and self.draft is not None:
            self.draft.has_tool_calls = True

    def _user_message(self, text: str, timestamp: datetime | None) -> None:
        if not text:
            return
        # The same input is logged both as a response_item and as an event_msg.
        if self.draft is not None and not self.draft.has_response and self.draft.prompt == text:
            return
        self.finish_turn()
        self.draft = _CodexDraft(
            session_id=self.session_id,
            prompt=text,
            received_at=timestamp,
            model=self.model,
            cwd=self.cwd,
            is_subagent=self.is_subagent,
            agent_origin=self.agent_origin,
        )

    def _assistant_message(self, text: str, phase: Any, timestamp: datetime | None) -> None:
        if not text or self.draft is None:
            return
        if phase != _FINAL_PHASE:
            self.draft.last_response = text
        elif text not in self.draft.final_parts:
            self.draft.final_parts.append(text)
        self.draft.model = self.draft.model or self.model
        self.draft.completed_at = timestamp or self.draft.completed_at


class CodexSessionParser:
    """Pair each user input with the final assistant answer of that turn."""

    source_kind = "codex"

    def parse_file(self, path: Path) -> list[ImportedTurn]:
        reader = _RolloutReader(self.source_kind, Path(path))
        for line in iter_jsonl(path):
            reader.read(line)
        reader.finish_turn()
        title = _session_title(Path(path), reader.session_id)
        for turn in reader.turns:
            turn.session_title = title
        return reader.turns
