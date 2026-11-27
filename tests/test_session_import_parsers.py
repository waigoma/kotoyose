"""Session history import: parser plugins for Claude Code, Codex and Antigravity logs."""

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kotoyose.session_import import parsers
from kotoyose.session_import.models import DEFAULT_FALLBACK_EPOCH, ImportedTurn
from kotoyose.session_import.parsers import (
    AntigravitySessionParser,
    ClaudeSessionParser,
    CodexSessionParser,
    get_parser,
    list_parser_kinds,
    register_parser,
)
from kotoyose.session_import.parsers.base import extract_cwd_basename, fallback_timestamp, iter_jsonl, parse_timestamp


def _write(path: Path, lines: list) -> Path:
    """Write dicts as JSON lines; strings are written verbatim (for malformed lines)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join((line if isinstance(line, str) else json.dumps(line)) + "\n" for line in lines), encoding="utf-8")
    return path


def _at(second: int) -> str:
    return f"2026-01-02T03:04:{second:02d}.000Z"


def _utc(second: int) -> datetime:
    return datetime(2026, 1, 2, 3, 4, second, tzinfo=timezone.utc)


def _dump(turns: list[ImportedTurn]) -> str:
    return repr(turns)


# --- base helpers ---


@pytest.mark.parametrize(
    "value",
    [
        "2026-01-02T03:04:05Z",
        "2026-01-02T12:04:05+09:00",
        "2026-01-02T03:04:05",
        datetime(2026, 1, 2, 3, 4, 5),
        int(_utc(5).timestamp()),
        int(_utc(5).timestamp() * 1000),
    ],
)
def test_parse_timestamp_normalizes_to_utc(value):
    parsed = parse_timestamp(value)
    assert parsed == _utc(5)
    assert parsed.utcoffset() == timedelta(0)


@pytest.mark.parametrize("value", [None, "", "yesterday", True, [], {}, float("nan"), float("inf"), 10**30, "0001-01-01T00:00:00+09:00"])
def test_parse_timestamp_rejects_unusable_values(value):
    assert parse_timestamp(value) is None


def test_fallback_timestamp_is_deterministic_and_ordered():
    assert fallback_timestamp(0) == DEFAULT_FALLBACK_EPOCH + timedelta(seconds=1)
    assert fallback_timestamp(4) == DEFAULT_FALLBACK_EPOCH + timedelta(seconds=5)
    assert fallback_timestamp(4, _utc(5)) == _utc(6)
    assert fallback_timestamp(4, datetime(2026, 1, 2, 3, 4, 5)) == _utc(6)


@pytest.mark.parametrize(
    ("cwd", "expected"),
    [
        ("/srv/alice/work/kotoyose", "kotoyose"),
        ("/srv/alice/work/kotoyose///", "kotoyose"),
        ("C:\\Users\\alice\\proj\\", "proj"),
        ("C:\\Users/alice\\mixed", "mixed"),
        ("C:\\", None),
        ("/", None),
        ("/tmp", None),
        ("/srv/alice/.", None),
        ("/srv/alice/..", None),
        ("", None),
        (None, None),
    ],
)
def test_extract_cwd_basename(cwd, expected):
    assert extract_cwd_basename(cwd) == expected


def test_iter_jsonl_skips_blank_malformed_and_non_object_lines(tmp_path):
    path = tmp_path / "log.jsonl"
    path.write_bytes(b'{"a": 1}\n\n   \n{broken\n[1, 2]\n"text"\nnull\n' + b"[" * 100_000 + b'\n{"b": "\xff"}\n{"c": 3')
    assert list(iter_jsonl(path)) == [{"a": 1}, {"b": "\ufffd"}]


# --- Claude ---


def _claude_user(content, second=None, **extra):
    line = {"type": "user", "sessionId": "sess-c", "cwd": "/srv/alice/work/kotoyose", "message": {"role": "user", "content": content}}
    if second is not None:
        line["timestamp"] = _at(second)
    return line | extra


def _claude_assistant(content, second=None, message_id=None, usage=None, model="claude-test", **extra):
    message = {"role": "assistant", "content": content, "model": model}
    if message_id:
        message["id"] = message_id
    if usage:
        message["usage"] = usage
    line = {"type": "assistant", "sessionId": "sess-c", "message": message}
    if second is not None:
        line["timestamp"] = _at(second)
    return line | extra


def _text(value: str) -> dict:
    return {"type": "text", "text": value}


def test_claude_multi_turn_excludes_thinking_and_tool_content(tmp_path):
    usage = {"input_tokens": 10, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 1000, "output_tokens": 1}
    path = _write(
        tmp_path / "file-stem.jsonl",
        [
            {"type": "session-owner", "sessionId": "sess-c"},
            {"type": "queue-operation", "content": "QUEUE-NOISE"},
            _claude_user("first question", 1),
            _claude_assistant([{"type": "thinking", "thinking": "SECRET-THOUGHT"}], 2, "msg_1", usage),
            _claude_assistant([{"type": "redacted_thinking", "data": "SECRET-REDACTED"}], 3, "msg_1", usage),
            _claude_assistant([_text("Let me look.")], 4, "msg_1", usage),
            _claude_assistant([{"type": "tool_use", "name": "Bash", "input": {"command": "SECRET-INPUT"}}], 5, "msg_1", usage | {"output_tokens": 50}),
            _claude_user([{"type": "tool_result", "tool_use_id": "t1", "content": "SECRET-DUMP"}], 6),
            {"type": "attachment", "attachment": "ATTACHMENT-NOISE"},
            _claude_assistant([_text("Found it.")], 7, "msg_2", {"input_tokens": 5, "output_tokens": 7}),
            _claude_user([_text("second"), _text("question")], 8),
            _claude_assistant("plain answer", 9, model="claude-other"),
        ],
    )

    turns = ClaudeSessionParser().parse_file(path)

    assert [(turn.turn_index, turn.prompt, turn.response) for turn in turns] == [
        (0, "first question", "Let me look.\n\nFound it."),
        (1, "second\n\nquestion", "plain answer"),
    ]
    first, second = turns
    assert (first.source_kind, first.provider, first.session_id) == ("claude", "claude", "sess-c")
    assert (first.model, second.model) == ("claude-test", "claude-other")
    assert (first.received_at, first.completed_at) == (_utc(1), _utc(7))
    assert (first.tokens_in, first.tokens_out) == (1115, 57)
    assert (second.tokens_in, second.tokens_out) == (None, None)
    assert (first.has_tool_calls, second.has_tool_calls) == (True, False)
    assert not first.timestamp_fallback
    assert first.cwd_basename == first.project == "kotoyose"
    assert "SECRET" not in _dump(turns)
    assert "NOISE" not in _dump(turns)
    assert "/home/alice" not in _dump(turns)


@pytest.mark.parametrize("block_type", ["tool_use", "server_tool_use"])
def test_claude_tool_use_blocks_set_flag(tmp_path, block_type):
    path = _write(tmp_path / "s.jsonl", [_claude_user("q", 1), _claude_assistant([{"type": block_type}, _text("a")], 2)])
    assert ClaudeSessionParser().parse_file(path)[0].has_tool_calls


def test_claude_tool_result_only_line_does_not_start_a_turn(tmp_path):
    path = _write(
        tmp_path / "s.jsonl",
        [
            _claude_user("q", 1),
            _claude_user([{"type": "tool_result", "content": "dump"}], 2),
            _claude_assistant([_text("a")], 3),
        ],
    )
    turns = ClaudeSessionParser().parse_file(path)
    assert [(turn.prompt, turn.response, turn.has_tool_calls) for turn in turns] == [("q", "a", True)]


def test_claude_skips_meta_lines_and_one_sided_turns(tmp_path):
    path = _write(
        tmp_path / "fallback-id.jsonl",
        [
            _claude_assistant([_text("orphan answer")], 1),
            _claude_user("unanswered", 2),
            _claude_user("only thinking", 3),
            _claude_assistant([{"type": "thinking", "thinking": "x"}], 4),
            _claude_user("real", 5, sessionId=None),
            _claude_user("META-PROMPT", 6, isMeta=True),
            _claude_assistant([_text("META-ANSWER")], 7, isMeta=True),
            _claude_assistant([_text("answer")], 8),
            _claude_assistant([_text("note")], 9, model="<synthetic>"),
            _claude_user("trailing", 10),
        ],
    )
    turns = ClaudeSessionParser().parse_file(path)
    assert [(turn.turn_index, turn.session_id, turn.prompt, turn.response, turn.model) for turn in turns] == [
        (0, "fallback-id", "real", "answer\n\nnote", "claude-test")
    ]


def test_claude_sidechain_lines_are_ignored(tmp_path):
    path = _write(
        tmp_path / "s.jsonl",
        [
            _claude_user("q", 1),
            _claude_assistant([_text("SIDE-ANSWER")], 2, isSidechain=True),
            _claude_user("SIDE-PROMPT", 3, isSidechain=True),
            _claude_assistant([_text("a")], 4),
        ],
    )
    turns = ClaudeSessionParser().parse_file(path)
    assert [(turn.prompt, turn.response) for turn in turns] == [("q", "a")]
    assert "SIDE" not in _dump(turns)


@pytest.mark.parametrize(
    "content",
    [
        "<task-notification>\n<task-id>1</task-id>\n</task-notification>",
        '<teammate-message teammate_id="x">hi</teammate-message>',
        "<bash-stdout>out</bash-stdout>",
        "  <bash-stderr>err</bash-stderr>",
        "<local-command-stdout>x</local-command-stdout>",
        "<local-command-caveat>c</local-command-caveat>",
        "<command-name>/clear</command-name>",
    ],
)
def test_claude_harness_notice_does_not_start_a_turn(tmp_path, content):
    path = _write(
        tmp_path / "s.jsonl",
        [_claude_user("q", 1), _claude_user(content, 2), _claude_assistant([_text("a")], 3)],
    )
    turns = ClaudeSessionParser().parse_file(path)
    assert [(turn.prompt, turn.response) for turn in turns] == [("q", "a")]
    assert "task-notification" not in _dump(turns) and "bash-" not in _dump(turns)


def test_claude_origin_and_compact_summary_do_not_start_a_turn(tmp_path):
    path = _write(
        tmp_path / "s.jsonl",
        [
            _claude_user("q", 1),
            _claude_user("NOTICE-ORIGIN", 2, origin={"kind": "task-notification"}),
            _claude_user("SUMMARY-TEXT", 3, isCompactSummary=True),
            _claude_user("TEAMMATE-TEXT", 4, origin={"kind": "teammate-message"}),
            _claude_assistant([_text("a")], 5),
        ],
    )
    turns = ClaudeSessionParser().parse_file(path)
    assert [(turn.prompt, turn.response) for turn in turns] == [("q", "a")]
    assert "SUMMARY-TEXT" not in _dump(turns) and "NOTICE-ORIGIN" not in _dump(turns)


def test_claude_pasted_content_starts_a_normal_turn(tmp_path):
    path = _write(
        tmp_path / "s.jsonl",
        [
            _claude_user("q", 1),
            _claude_assistant([_text("a")], 2),
            _claude_user("<pasted_content>x</pasted_content>", 3),
            _claude_assistant([_text("b")], 4),
        ],
    )
    turns = ClaudeSessionParser().parse_file(path)
    assert [(turn.prompt, turn.response) for turn in turns] == [("q", "a"), ("<pasted_content>x</pasted_content>", "b")]


def test_claude_usage_of_a_message_split_across_turns_is_counted_once(tmp_path):
    usage = {"input_tokens": 3, "output_tokens": 4}
    path = _write(
        tmp_path / "s.jsonl",
        [
            _claude_user("q1", 1),
            _claude_assistant([_text("a1")], 2, "msg_1", usage),
            _claude_user("q2", 3),
            _claude_assistant([_text("a1 tail")], 4, "msg_1", usage),
        ],
    )
    first, second = ClaudeSessionParser().parse_file(path)
    assert (first.tokens_in, first.tokens_out) == (3, 4)
    assert (second.tokens_in, second.tokens_out) == (None, None)


# --- Codex ---


def _codex(kind, payload, second=None):
    line = {"type": kind, "payload": payload}
    if second is not None:
        line["timestamp"] = _at(second)
    return line


def _codex_item(payload, second=None):
    return _codex("response_item", payload, second)


def _codex_event(payload, second=None):
    return _codex("event_msg", payload, second)


def _codex_user(*texts, second=None):
    return _codex_item({"type": "message", "role": "user", "content": [{"type": "input_text", "text": t} for t in texts]}, second)


def _codex_assistant(text, phase=None, second=None):
    payload = {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}
    if phase:
        payload["phase"] = phase
    return _codex_item(payload, second)


def _codex_usage(response_id, tokens_in, tokens_out):
    return _codex("token_usage_record", {"response_id": response_id, "usage": {"input_tokens": tokens_in, "output_tokens": tokens_out}})


def test_codex_full_session(tmp_path):
    path = _write(
        tmp_path / "rollout.jsonl",
        [
            _codex("session_meta", {"session_id": "sess-x", "id": "thread-x", "cwd": "/srv/alice/old"}, 0),
            _codex("session_meta", {"session_id": "parent-session", "cwd": "/srv/alice/parent"}, 0),
            _codex_item({"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "SECRET-DEV"}]}),
            _codex_user("# AGENTS.md instructions for /x\n\n<INSTRUCTIONS>SECRET</INSTRUCTIONS>", "<environment_context>SECRET</environment_context>"),
            _codex("turn_context", {"model": "gpt-test", "cwd": "/srv/alice/work/kotoyose"}),
            _codex_user("fix the bug", second=1),
            _codex_event({"type": "user_message", "message": "fix the bug"}, 1),
            _codex_item({"type": "reasoning", "summary": [{"type": "summary_text", "text": "SECRET-REASONING"}]}),
            _codex_assistant("Looking now.", "commentary", 2),
            _codex_item({"type": "custom_tool_call", "name": "exec", "input": "SECRET-INPUT"}),
            _codex_item({"type": "custom_tool_call_output", "output": "SECRET-DUMP" * 1000}),
            _codex_item({"type": "function_call_output", "output": "SECRET-DUMP" * 1000}),
            _codex_usage("r1", 100, 10),
            _codex_assistant("Fixed.", "final_answer", 3),
            _codex_event({"type": "agent_message", "message": "Fixed.", "phase": "final_answer"}, 3),
            _codex_assistant("Trailing commentary.", "commentary", 4),
            _codex_usage("r2", 200, 20),
            _codex_usage("r2", 200, 20),
            _codex_event({"type": "token_count", "info": {"last_token_usage": {"input_tokens": 9999, "output_tokens": 9999}}}),
            _codex_event({"type": "task_complete", "last_agent_message": "Fixed.", "completed_at": int(_utc(5).timestamp())}, 5),
            _codex_assistant("AFTER-COMPLETE", "final_answer", 6),
            _codex_user("<turn_aborted>\nSECRET\n</turn_aborted>", second=7),
            _codex_user("<permissions instructions>SECRET", "<collaboration_mode>SECRET", "<user_instructions>SECRET", "<skill>SECRET"),
            _codex("turn_context", {"collaboration_mode": {"settings": {"model": "gpt-next"}}}),
            _codex_user("<environment_context>SECRET</environment_context>", "and now?", second=8),
            _codex_assistant("first draft", second=9),
            _codex_assistant("last draft", "commentary", 10),
        ],
    )

    turns = CodexSessionParser().parse_file(path)

    assert [(turn.turn_index, turn.prompt, turn.response) for turn in turns] == [
        (0, "fix the bug", "Fixed."),
        (1, "and now?", "last draft"),
    ]
    first, second = turns
    assert (first.source_kind, first.provider, first.session_id) == ("codex", "codex", "thread-x")
    assert (first.model, second.model) == ("gpt-test", "gpt-next")
    assert first.cwd_basename == "kotoyose"
    assert (first.tokens_in, first.tokens_out) == (300, 30)
    assert (second.tokens_in, second.tokens_out) == (None, None)
    assert (first.has_tool_calls, second.has_tool_calls) == (True, False)
    assert (first.received_at, first.completed_at) == (_utc(1), _utc(4))
    assert "SECRET" not in _dump(turns)
    assert "/home/alice" not in _dump(turns)


@pytest.mark.parametrize(
    "item_type",
    ["function_call", "function_call_output", "custom_tool_call", "custom_tool_call_output", "web_search_call", "local_shell_call"],
)
def test_codex_tool_items_set_flag_without_content(tmp_path, item_type):
    path = _write(
        tmp_path / "r.jsonl",
        [_codex_user("q", second=1), _codex_item({"type": item_type, "output": "SECRET", "input": "SECRET"}), _codex_assistant("a", second=2)],
    )
    turns = CodexSessionParser().parse_file(path)
    assert turns[0].has_tool_calls
    assert "SECRET" not in _dump(turns)


def test_codex_session_meta_prefers_own_id_over_session_id(tmp_path):
    path = _write(
        tmp_path / "rollout-stem.jsonl",
        [
            _codex("session_meta", {"id": "child-thread", "session_id": "parent-thread"}),
            _codex_user("q", second=1),
            _codex_assistant("a", "final_answer", 2),
        ],
    )
    turn = CodexSessionParser().parse_file(path)[0]
    assert turn.session_id == "child-thread"


def test_codex_task_complete_message_is_used_when_no_response_item(tmp_path):
    path = _write(
        tmp_path / "rollout-stem.jsonl",
        [
            _codex("session_meta", {"id": "sess-id"}),
            _codex_user("q", second=1),
            _codex_event({"type": "task_complete", "last_agent_message": "from task", "completed_at": int(_utc(9).timestamp())}, 10),
        ],
    )
    turn = CodexSessionParser().parse_file(path)[0]
    assert (turn.response, turn.session_id, turn.model) == ("from task", "sess-id", "unknown")
    assert (turn.received_at, turn.completed_at) == (_utc(1), _utc(9))


def test_codex_legacy_event_messages(tmp_path):
    path = _write(
        tmp_path / "rollout-stem.jsonl",
        [
            _codex_event({"type": "user_message", "message": "<environment_context>x</environment_context>"}, 1),
            _codex_event({"type": "user_message", "message": "continue"}, 2),
            _codex_event({"type": "agent_message", "message": "step one"}, 3),
            _codex_event({"type": "token_count", "info": None}),
            _codex_event({"type": "token_count", "info": {"total_token_usage": {"n": 1}, "last_token_usage": {"input_tokens": 5, "output_tokens": 2}}}),
            _codex_event({"type": "token_count", "info": {"total_token_usage": {"n": 1}, "last_token_usage": {"input_tokens": 5, "output_tokens": 2}}}),
            # The same text again after an answer is a new turn, in either logging order.
            _codex_event({"type": "user_message", "message": "continue"}, 4),
            _codex_user("continue", second=4),
            _codex_event({"type": "token_count", "usage": {"input_tokens": 7, "output_tokens": 3}}),
            _codex_event({"type": "agent_message", "message": "step two"}, 5),
            _codex_event({"type": "user_message", "message": "unanswered"}, 6),
        ],
    )
    turns = CodexSessionParser().parse_file(path)
    assert [(turn.prompt, turn.response, turn.tokens_in, turn.tokens_out) for turn in turns] == [
        ("continue", "step one", 5, 2),
        ("continue", "step two", 7, 3),
    ]
    assert turns[0].session_id == "rollout-stem"


# --- Antigravity ---


def _agy(kind, second=None, **fields):
    line = {"type": kind} | fields
    if second is not None:
        line["created_at"] = _at(second)
    return line


def _agy_path(tmp_path: Path) -> Path:
    return tmp_path / "brain" / "uuid-1234" / ".system_generated" / "logs" / "transcript.jsonl"


def test_antigravity_turns(tmp_path):
    path = _write(
        _agy_path(tmp_path),
        [
            _agy("CONVERSATION_HISTORY", 0, content="SECRET-HISTORY"),
            _agy("USER_INPUT", 1, content="  hello  "),
            _agy(
                "PLANNER_RESPONSE",
                2,
                thinking="SECRET-THINKING",
                thought="SECRET-THOUGHT",
                tool_calls=[{"args": "SECRET-ARGS"}],
                input_tokens=10,
                output_tokens=1,
            ),
            _agy("GENERIC", 3, content="SECRET-TOOL-DUMP"),
            _agy("SYSTEM_MESSAGE", 4, content="SECRET-SYSTEM"),
            _agy("CHECKPOINT", 5, content="SECRET-CHECKPOINT", input_tokens=5000, output_tokens=5000),
            _agy("PLANNER_RESPONSE", 6, content="Part one.", model="gemini-test", input_tokens=20, output_tokens=2),
            _agy("MODEL_RESPONSE", 7, content="Part two.", thinking="SECRET-THINKING", input_tokens=True),
            _agy("PLANNER_RESPONSE", 8, thinking="SECRET-THINKING"),
            _agy("USER_INPUT", 9, content="thinking only"),
            _agy("PLANNER_RESPONSE", 10, thinking="SECRET-THINKING", content=""),
            _agy("USER_INPUT", 11, content="<USER_REQUEST>\nbye\n</USER_REQUEST>\n<ADDITIONAL_METADATA>\nSECRET-METADATA\n</ADDITIONAL_METADATA>"),
            _agy("PLANNER_RESPONSE", 12, content="Bye."),
        ],
    )

    turns = AntigravitySessionParser().parse_file(path)

    assert [(turn.turn_index, turn.prompt, turn.response) for turn in turns] == [
        (0, "hello", "Part one.\n\nPart two."),
        (1, "bye", "Bye."),
    ]
    first, second = turns
    assert (first.source_kind, first.provider, first.session_id) == ("antigravity", "antigravity", "uuid-1234")
    assert (first.model, second.model) == ("gemini-test", "unknown")
    assert (first.tokens_in, first.tokens_out) == (30, 3)
    assert (second.tokens_in, second.tokens_out) == (None, None)
    assert (first.has_tool_calls, second.has_tool_calls) == (True, False)
    assert (first.received_at, first.completed_at) == (_utc(1), _utc(7))
    assert "SECRET" not in _dump(turns)


def test_antigravity_session_id_outside_brain_layout(tmp_path):
    path = _write(tmp_path / "session-dir" / "other.jsonl", [_agy("USER_INPUT", 1, content="q"), _agy("PLANNER_RESPONSE", 2, content="a")])
    assert AntigravitySessionParser().parse_file(path)[0].session_id == "session-dir"


# --- timestamps, robustness and registry ---


def _pair(kind: str, prompt: str, second_in: int | None = None, second_out: int | None = None) -> list[dict]:
    if kind == "claude":
        return [_claude_user(prompt, second_in), _claude_assistant([_text("a")], second_out)]
    if kind == "codex":
        return [_codex_user(prompt, second=second_in), _codex_assistant("a", second=second_out)]
    return [_agy("USER_INPUT", second_in, content=prompt), _agy("PLANNER_RESPONSE", second_out, content="a")]


@pytest.mark.parametrize("kind", ["claude", "codex", "antigravity"])
def test_missing_timestamps_use_deterministic_fallback_not_mtime(tmp_path, kind):
    path = _write(tmp_path / "s.jsonl", _pair(kind, "q0") + _pair(kind, "q1", 20, 21) + _pair(kind, "q2") + _pair(kind, "q3"))
    mtime = datetime(2030, 5, 5, tzinfo=timezone.utc)
    os.utime(path, (mtime.timestamp(), mtime.timestamp()))

    turns = get_parser(kind).parse_file(path)

    assert [turn.timestamp_fallback for turn in turns] == [True, False, True, True]
    assert [turn.completed_at for turn in turns] == [fallback_timestamp(0), _utc(21), _utc(22), _utc(23)]
    assert all(turn.received_at <= turn.completed_at and turn.completed_at != mtime for turn in turns)
    os.utime(path, (0, 0))
    assert get_parser(kind).parse_file(path) == turns


@pytest.mark.parametrize("kind", ["claude", "codex", "antigravity"])
@pytest.mark.parametrize(
    ("second_in", "second_out", "expected"),
    [(5, None, (5, 5)), (None, 6, (6, 6)), (9, 4, (9, 9))],
)
def test_one_sided_or_inverted_timestamps(tmp_path, kind, second_in, second_out, expected):
    turn = get_parser(kind).parse_file(_write(tmp_path / "s.jsonl", _pair(kind, "q", second_in, second_out)))[0]
    assert (turn.received_at, turn.completed_at) == (_utc(expected[0]), _utc(expected[1]))
    assert not turn.timestamp_fallback


@pytest.mark.parametrize("kind", ["claude", "codex", "antigravity"])
def test_malformed_lines_and_unexpected_shapes_do_not_crash(tmp_path, kind):
    junk = [
        "{not json",
        "[]",
        {"type": ["user"]},
        {"type": "user", "message": "not-a-dict"},
        {"type": "assistant", "message": {"content": [None, 1, {"type": "text", "text": 5}]}},
        {"type": "response_item", "payload": "not-a-dict"},
        {"type": "response_item", "payload": {"type": ["message"], "role": "user"}},
        {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [None, {"type": "input_text", "text": None}]}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": {"not": "text"}}},
        {"type": "turn_context", "payload": {"collaboration_mode": "x"}},
        {"type": "USER_INPUT", "content": ["not", "text"]},
    ]
    pair = _pair(kind, "q", 1, 2)
    turns = get_parser(kind).parse_file(_write(tmp_path / "s.jsonl", junk + pair[:1] + junk[:2] + pair[1:] + ['{"truncated": ']))
    assert [(turn.prompt, turn.response) for turn in turns] == [("q", "a")]


def test_builtin_parsers_are_registered():
    assert list_parser_kinds() == ("claude", "codex", "antigravity", "cursor", "grok")
    assert all(get_parser(kind).source_kind == kind for kind in list_parser_kinds())
    with pytest.raises(KeyError):
        get_parser("nope")


def test_registry_accepts_custom_parser(monkeypatch):
    monkeypatch.setattr(parsers, "_REGISTRY", dict(parsers._REGISTRY))

    class CustomParser:
        source_kind = "custom-cli"

        def parse_file(self, path: Path) -> list[ImportedTurn]:
            return []

    custom = CustomParser()
    register_parser(custom)

    assert get_parser("custom-cli") is custom
    assert "custom-cli" in list_parser_kinds()


def test_codex_parser_and_discovery_support_zstd_rollouts(tmp_path):
    import zstandard

    from kotoyose.session_import.paths import iter_safe_source_files

    plain = _write(tmp_path / "2026/11/24/rollout-plain.jsonl", _pair("codex", "hello plain", 1, 2))
    raw_zst = "\n".join(json.dumps(line) for line in _pair("codex", "hello zstd", 3, 4)).encode("utf-8") + b"\n"
    compressed = tmp_path / "2026/09/11/rollout-zst.jsonl.zst"
    compressed.parent.mkdir(parents=True, exist_ok=True)
    compressed.write_bytes(zstandard.ZstdCompressor().compress(raw_zst))

    corrupt = tmp_path / "2026/09/11/rollout-corrupt.jsonl.zst"
    corrupt.write_bytes(b"not valid zstd bytes")

    found = iter_safe_source_files("codex", tmp_path)
    assert found == sorted([plain, compressed, corrupt])

    turns = CodexSessionParser().parse_file(compressed)
    assert [(turn.prompt, turn.response, turn.session_id) for turn in turns] == [
        ("hello zstd", "a", "rollout-zst"),
    ]
    assert CodexSessionParser().parse_file(corrupt) == []


@pytest.mark.parametrize("value,expected", [(None, None), (42, None), (" \n\t", None), (" a\n b\t c ", "a b c"), ("x" * 161, "x" * 160)])
def test_normalize_session_title(value, expected):
    from kotoyose.session_import.models import normalize_session_title

    assert normalize_session_title(value) == expected


@pytest.mark.parametrize("records,expected", [
    ([{"type": "summary", "summary": "first"}, {"type": "summary", "title": "last"}], "last"),
    ([{"type": "ai-title", "aiTitle": "AI"}, {"type": "summary", "summary": "summary"}], "AI"),
    ([{"type": "custom-title", "customTitle": "custom"}, {"type": "ai-title", "aiTitle": "AI"},
      {"type": "summary", "summary": "summary"}, {"type": "custom-title", "title": " final\n name "}], "final name"),
    ([{"type": "ai-title", "title": "first"}, {"type": "ai-title", "aiTitle": "last"}], "last"),
    ([{"type": "summary", "summary": "fallback"}, {"type": "custom-title", "customTitle": 123}], "fallback"),
])
def test_claude_native_titles_apply_to_all_turns_at_eof(tmp_path, records, expected):
    path = _write(tmp_path / "session.jsonl", [
        _claude_user("q1"), _claude_assistant("a1"),
        _claude_user("q2"), _claude_assistant("a2"), *records,
    ])
    turns = ClaudeSessionParser().parse_file(path)
    assert len(turns) == 2
    assert [turn.session_title for turn in turns] == [expected, expected]


def test_codex_native_title_index_cache_and_refresh(tmp_path, monkeypatch):
    from kotoyose.session_import.parsers import codex

    path = _write(tmp_path / "sessions/2026/01/02/rollout.jsonl", [
        _codex("session_meta", {"id": "thread-1"}), _codex_user("q"), _codex_assistant("a"),
    ])
    index = _write(tmp_path / "session_index.jsonl", ["{broken", {"id": "other", "thread_name": "other"},
                                                         {"id": "thread-1", "thread_name": " native\n title "}])
    original = codex.iter_jsonl
    reads = []

    def tracked(source):
        if Path(source) == index:
            reads.append(source)
        yield from original(source)

    monkeypatch.setattr(codex, "iter_jsonl", tracked)
    for _ in range(3):
        assert CodexSessionParser().parse_file(path)[0].session_title == "native title"
    assert len(reads) == 1
    _write(index, [{"id": "thread-1", "thread_name": "updated title"}])
    assert CodexSessionParser().parse_file(path)[0].session_title == "updated title"
    assert len(reads) == 2
    index.unlink()
    outside = _write(tmp_path / "outside.jsonl", [{"id": "thread-1", "thread_name": "private"}])
    index.symlink_to(outside)
    assert CodexSessionParser().parse_file(path)[0].session_title is None


@pytest.mark.parametrize("title,expected,redacted,truncated", [
    (" title\n ghp_" + "a" * 36, "title [REDACTED]", True, False),
    ("x" * 161, "x" * 160, False, True),
    ("\n ", None, False, False),
])
def test_imported_title_storage_sanitizes_and_combines_flags(title, expected, redacted, truncated):
    from kotoyose.session_import.importer import build_interaction_row

    turn = ImportedTurn("claude", "session", 0, _utc(0), _utc(1), "q", "a", session_title=title)
    row, r, t = build_interaction_row(turn, source_machine="test", source_path_hash="hash")
    assert row["metadata"].get("session_title") == expected
    assert r is redacted and t is truncated
    assert row["metadata"]["redacted"] is redacted
    assert row["metadata"]["truncated"] is truncated
