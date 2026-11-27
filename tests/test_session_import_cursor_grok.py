"""Session history import: Cursor and Grok parsers, and Codex subagent tagging."""

import hashlib
import json
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kotoyose.session_import.models import DEFAULT_FALLBACK_EPOCH, KNOWN_SOURCE_KINDS
from kotoyose.session_import.parsers import (
    CodexSessionParser,
    CursorSessionParser,
    GrokSessionParser,
    get_parser,
    list_parser_kinds,
)
from kotoyose.session_import.parsers.base import extract_typed_text

_START = datetime(2026, 1, 2, 3, 4, 0, tzinfo=timezone.utc)


def _utc(second: int) -> datetime:
    return _START + timedelta(seconds=second)


def _ms(second: int) -> int:
    return int(_utc(second).timestamp() * 1000)


def _iso(second: int) -> str:
    return _utc(second).isoformat().replace("+00:00", "Z")


def _text(value: str) -> dict:
    return {"type": "text", "text": value}


def test_all_builtin_parsers_are_registered():
    assert list_parser_kinds() == ("claude", "codex", "antigravity", "cursor", "grok")
    assert set(list_parser_kinds()) == set(KNOWN_SOURCE_KINDS)
    assert all(get_parser(kind).source_kind == kind for kind in list_parser_kinds())


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("plain question", "plain question"),
        ("<rules>R</rules>\n<rules>R2</rules>", ""),
        ("<rules a='1'>R</rules>\n\n typed ", "typed"),
        ("<timestamp>T</timestamp>\n<user_query>\n<rules>mine</rules>\n</user_query>", "<rules>mine</rules>"),
        ("<user_query>quote </user_query> inside</user_query>", "quote </user_query> inside"),
        ("explain <rules>this</rules>", "explain <rules>this</rules>"),
        ("<div>my markup</div>", "<div>my markup</div>"),
        ("<rules>never closed", "<rules>never closed"),
    ],
)
def test_extract_typed_text(text, expected):
    assert extract_typed_text(text, ("rules", "timestamp")) == expected


# --- Cursor ---


def _varint(value: int) -> bytes:
    out = bytearray()
    while value >= 0x80:
        out.append(value & 0x7F | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def _field(number: int, payload: bytes) -> bytes:
    return _varint(number << 3 | 2) + _varint(len(payload)) + payload


def _number_field(number: int, value: int) -> bytes:
    return _varint(number << 3) + _varint(value)


class _Blobs:
    """Content-addressed blobs, as in a Cursor store."""

    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}

    def raw(self, data: bytes) -> str:
        blob_id = hashlib.sha256(data).hexdigest()
        self.data[blob_id] = data
        return blob_id

    def message(self, role: str, content, **extra) -> str:
        return self.raw(json.dumps({"role": role, "content": content, **extra}).encode())

    def node(self, *refs: str | tuple[int, str], extra: bytes = b"") -> str:
        pairs = [ref if isinstance(ref, tuple) else (1, ref) for ref in refs]
        return self.raw(b"".join(_field(number, bytes.fromhex(blob_id)) for number, blob_id in pairs) + extra)


def _store(
    tmp_path: Path,
    blobs: _Blobs,
    root: str | None,
    *,
    meta: dict | None = None,
    sidecar: dict | None = None,
    wal: bool = True,
) -> Path:
    directory = tmp_path / "chats" / "workspace-hash" / "chat-dir"
    directory.mkdir(parents=True)
    path = directory / "store.db"
    values = {"agentId": "agent-1", "latestRootBlobId": root, "createdAt": _ms(0), "blobEncryptionKey": "SECRET-KEY"}
    values |= meta or {}
    connection = sqlite3.connect(path)
    if wal:
        connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")
    connection.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    connection.executemany("INSERT INTO blobs VALUES (?, ?)", list(blobs.data.items()))
    connection.execute("INSERT INTO meta VALUES ('0', ?)", (json.dumps(values).encode().hex(),))
    connection.commit()
    connection.close()
    if sidecar is not None:
        (directory / "meta.json").write_text(json.dumps(sidecar), encoding="utf-8")
    return path


def _snapshot(directory: Path) -> dict[str, bytes]:
    return {entry.name: entry.read_bytes() for entry in directory.iterdir()}


def _sidecar(**extra) -> dict:
    return {"cwd": "/srv/alice/work/kotoyose", "createdAtMs": _ms(0), "updatedAtMs": _ms(600)} | extra


def _simple_chat(blobs: _Blobs, count: int = 2) -> list[str]:
    ids = []
    for number in range(count):
        ids.append(blobs.message("user", f"q{number}"))
        ids.append(blobs.message("assistant", [_text(f"a{number}")]))
    return ids


def test_cursor_follows_root_order_through_nested_nodes(tmp_path):
    blobs = _Blobs()
    # Insert in reverse so that row order cannot explain the result.
    second_answer = blobs.message("assistant", "second answer")
    second_question = blobs.message("user", [_text("second question")])
    first_answer = blobs.message("assistant", [_text("first answer")])
    first_question = blobs.message("user", "first question")
    orphan = blobs.message("user", "ORPHAN")
    nested = blobs.node(second_question, second_answer)
    unknown = _field(9, b"x" * 32) + _field(5, b"short") + _number_field(10, 300)
    root = blobs.node(first_question, first_answer, (8, nested), first_question, extra=unknown)
    assert orphan in blobs.data

    turns = CursorSessionParser().parse_file(_store(tmp_path, blobs, root, sidecar=_sidecar()))

    assert [(turn.turn_index, turn.prompt, turn.response) for turn in turns] == [
        (0, "first question", "first answer"),
        (1, "second question", "second answer"),
    ]
    assert {turn.session_id for turn in turns} == {"agent-1"}
    assert {turn.cwd_basename for turn in turns} == {"kotoyose"}
    assert {(turn.source_kind, turn.provider, turn.model) for turn in turns} == {("cursor", "cursor", "unknown")}
    assert "SECRET-KEY" not in repr(turns)


def test_cursor_reads_summarized_archives_before_current_messages(tmp_path):
    blobs = _Blobs()
    system = blobs.message("system", "SYSTEM-PROMPT")
    old_question = blobs.message("user", "old question")
    old_answer = blobs.message("assistant", "old answer")
    summary = blobs.message("user", "SUMMARY-TEXT", providerOptions={"cursor": {"isSummary": True}})
    new_question = blobs.message("user", "new question")
    new_answer = blobs.message("assistant", "new answer")
    archive = blobs.node(old_question, old_answer, (4, summary))
    root = blobs.node(system, summary, new_question, new_answer, (13, archive))

    turns = CursorSessionParser().parse_file(_store(tmp_path, blobs, root))

    assert [(turn.prompt, turn.response) for turn in turns] == [("old question", "old answer"), ("new question", "new answer")]
    assert "SUMMARY-TEXT" not in repr(turns)
    assert "SYSTEM-PROMPT" not in repr(turns)


def test_cursor_terminates_on_cycles_and_skips_missing_or_binary_blobs(tmp_path):
    blobs = _Blobs()
    ids = _simple_chat(blobs, 1)
    # Real ids are content hashes; fixed ids are the only way to build a cycle.
    first, second = "11" * 32, "22" * 32
    blobs.data[first] = _field(1, bytes.fromhex(second)) + _field(1, bytes.fromhex(first))
    blobs.data[second] = _field(1, bytes.fromhex(first)) + b"".join(_field(1, bytes.fromhex(item)) for item in ids)
    garbage = blobs.raw(b"\xff\xff\xff")
    truncated = blobs.raw(_field(1, bytes.fromhex(ids[0]))[:-3])
    broken_json = blobs.raw(b"{not json")
    root = blobs.node(garbage, truncated, broken_json, "33" * 32, first)

    turns = CursorSessionParser().parse_file(_store(tmp_path, blobs, root))

    assert [(turn.prompt, turn.response) for turn in turns] == [("q0", "a0")]


def test_cursor_excludes_reasoning_and_tool_payloads(tmp_path):
    blobs = _Blobs()
    reasoning = {"type": "reasoning", "text": "SECRET-THOUGHT", "providerOptions": {"cursor": {"modelName": "grok-test"}}}
    ids = [
        blobs.message("user", "q0"),
        blobs.message(
            "assistant",
            [
                reasoning,
                {"type": "thinking", "thinking": "SECRET-THINKING", "text": "SECRET-THINKING"},
                {"type": "redacted_thinking", "data": "SECRET-REDACTED", "text": "SECRET-REDACTED"},
                _text("Let me look."),
                {"type": "tool-call", "toolName": "Shell", "args": {"command": "SECRET-ARGS"}},
            ],
        ),
        blobs.message("tool", [{"type": "tool-result", "result": "SECRET-RESULT"}]),
        blobs.message("assistant", [_text("Found it.")]),
        blobs.message("user", "q1"),
        blobs.message("assistant", [{"type": "tool_use", "input": "SECRET-INPUT"}, _text("a1")], model="other-model"),
        blobs.message("user", "q2"),
        blobs.message("tool", "SECRET-RESULT-2"),
        blobs.message("assistant", "a2"),
        blobs.message("user", "q3"),
        blobs.message("assistant", "a3"),
    ]

    turns = CursorSessionParser().parse_file(_store(tmp_path, blobs, blobs.node(*ids)))

    assert [turn.response for turn in turns] == ["Let me look.\n\nFound it.", "a1", "a2", "a3"]
    assert [turn.has_tool_calls for turn in turns] == [True, True, True, False]
    assert [turn.model for turn in turns] == ["grok-test", "other-model", "unknown", "unknown"]
    assert "SECRET" not in repr(turns)


def test_cursor_filters_injected_preambles_and_harness_notices(tmp_path):
    blobs = _Blobs()
    preamble = "".join(
        f"<{tag}>\nINJECTED\n</{tag}>\n"
        for tag in ("user_info", "git_status", "rules", "open_and_recently_viewed_files", "agent_skills")
    )
    ids = [
        blobs.message("assistant", "before any prompt"),
        blobs.message("user", preamble),
        blobs.message("user", [_text("<system_reminder>\nINJECTED\n</system_reminder>"), _text("<user_query>\nfirst\n</user_query>")]),
        blobs.message("assistant", "a0"),
        blobs.message(
            "user",
            "<timestamp>x</timestamp>\n<system_notification>INJECTED</system_notification>\n<user_query>INJECTED</user_query>",
        ),
        blobs.message("assistant", "after notice"),
        blobs.message("user", [_text("<user_info>INJECTED</user_info>"), _text("second")]),
        blobs.message("assistant", "a1"),
        blobs.message("user", [{"type": "image", "image": "x"}]),
    ]

    turns = CursorSessionParser().parse_file(_store(tmp_path, blobs, blobs.node(*ids)))

    assert [(turn.prompt, turn.response) for turn in turns] == [("first", "a0\n\nafter notice"), ("second", "a1")]
    assert "INJECTED" not in repr(turns)


def test_cursor_prefers_message_timestamps(tmp_path):
    blobs = _Blobs()
    ids = [
        blobs.message(
            "user",
            [_text("<timestamp>Friday, Jan 2, 2026, 12:05 PM (UTC+9)</timestamp>\n<user_query>tagged</user_query>")],
        ),
        blobs.message("assistant", "a0"),
        blobs.message("user", "explicit", timestamp=_iso(120)),
        blobs.message("assistant", "a1", timestamp=_iso(125)),
        blobs.message("user", "<timestamp>Thursday, Jan 1, 2026, 12:00 AM (UTC-5:30)</timestamp>\n<user_query>earlier</user_query>"),
        blobs.message("assistant", "a2"),
    ]

    turns = CursorSessionParser().parse_file(_store(tmp_path, blobs, blobs.node(*ids), sidecar=_sidecar()))

    assert [(turn.received_at, turn.completed_at) for turn in turns] == [
        (_utc(60), _utc(60)),
        (_utc(120), _utc(125)),
        (_utc(125), _utc(125)),  # never earlier than the previous turn
    ]
    assert not any(turn.timestamp_fallback for turn in turns)


def test_cursor_timestamps_between_creation_and_update_stay_stable_as_the_chat_grows(tmp_path):
    results = []
    for count, updated in ((2, 600), (4, 9000)):
        blobs = _Blobs()
        root = blobs.node(*_simple_chat(blobs, count))
        path = _store(tmp_path / str(count), blobs, root, sidecar=_sidecar(updatedAtMs=_ms(updated)))
        results.append([(turn.received_at, turn.completed_at) for turn in CursorSessionParser().parse_file(path)])

    assert results[0] == [(_utc(0), _utc(0)), (_utc(1), _utc(1))]
    assert results[1][:2] == results[0]
    assert results[1] == sorted(results[1])


def test_cursor_timestamps_are_capped_at_the_last_update(tmp_path):
    blobs = _Blobs()
    root = blobs.node(*_simple_chat(blobs, 3))
    path = _store(tmp_path, blobs, root, sidecar=_sidecar(updatedAtMs=_ms(1)))

    turns = CursorSessionParser().parse_file(path)

    assert [turn.received_at for turn in turns] == [_utc(0), _utc(1), _utc(1)]


def test_cursor_uses_sidecar_creation_time_when_the_store_has_none(tmp_path):
    blobs = _Blobs()
    root = blobs.node(*_simple_chat(blobs, 1))
    path = _store(tmp_path, blobs, root, meta={"createdAt": None}, sidecar=_sidecar(createdAtMs=_ms(30)))

    [turn] = CursorSessionParser().parse_file(path)

    assert turn.received_at == _utc(30)
    assert turn.timestamp_fallback is False


def test_cursor_marks_fallback_when_no_timestamp_exists(tmp_path):
    blobs = _Blobs()
    root = blobs.node(*_simple_chat(blobs))
    path = _store(tmp_path, blobs, root, meta={"createdAt": None, "agentId": None})

    turns = CursorSessionParser().parse_file(path)

    assert [turn.received_at for turn in turns] == [DEFAULT_FALLBACK_EPOCH + timedelta(seconds=n) for n in (1, 2)]
    assert all(turn.timestamp_fallback for turn in turns)
    assert {turn.session_id for turn in turns} == {"chat-dir"}
    assert {turn.cwd_basename for turn in turns} == {None}


@pytest.mark.parametrize("sidecar", ["{broken", "[1]"])
def test_cursor_ignores_a_damaged_sidecar(tmp_path, sidecar):
    blobs = _Blobs()
    path = _store(tmp_path, blobs, blobs.node(*_simple_chat(blobs, 1)))
    (path.parent / "meta.json").write_text(sidecar, encoding="utf-8")

    [turn] = CursorSessionParser().parse_file(path)

    assert (turn.prompt, turn.cwd_basename) == ("q0", None)


@pytest.mark.parametrize("wal", [True, False])
def test_cursor_never_modifies_the_store_or_creates_sidecar_files(tmp_path, wal):
    blobs = _Blobs()
    # Characters that must be escaped in an SQLite URI.
    path = _store(tmp_path / "100% #1?", blobs, blobs.node(*_simple_chat(blobs, 1)), wal=wal)
    before = _snapshot(path.parent)
    assert list(before) == ["store.db"]

    assert len(CursorSessionParser().parse_file(path)) == 1
    assert _snapshot(path.parent) == before


@pytest.mark.parametrize("keep_index", [True, False])
def test_cursor_reads_uncheckpointed_wal_without_touching_the_database(tmp_path, keep_index):
    blobs = _Blobs()
    live = _store(tmp_path / "live", blobs, blobs.node(*_simple_chat(blobs, 1)))
    writer = sqlite3.connect(live)
    try:
        writer.execute("PRAGMA wal_autocheckpoint=0")
        added = _Blobs()
        root = added.node(*_simple_chat(added, 2))
        writer.executemany("INSERT OR IGNORE INTO blobs VALUES (?, ?)", list(added.data.items()))
        meta = json.dumps({"agentId": "agent-1", "latestRootBlobId": root}).encode().hex()
        writer.execute("UPDATE meta SET value = ? WHERE key = '0'", (meta,))
        writer.commit()
        path = live
        if not keep_index:
            # What a crashed writer can leave behind: a WAL without its shared-memory index.
            path = tmp_path / "crashed" / "store.db"
            path.parent.mkdir()
            shutil.copyfile(live, path)
            shutil.copyfile(live.with_name("store.db-wal"), path.with_name("store.db-wal"))
        names = sorted(entry.name for entry in path.parent.iterdir())
        durable = {name: (path.parent / name).read_bytes() for name in names if name != "store.db-shm"}

        turns = CursorSessionParser().parse_file(path)

        assert [turn.prompt for turn in turns] == ["q0", "q1"]
        assert sorted(entry.name for entry in path.parent.iterdir()) == names
        assert {name: (path.parent / name).read_bytes() for name in durable} == durable
    finally:
        writer.close()


def test_cursor_returns_nothing_for_a_locked_store(tmp_path):
    blobs = _Blobs()
    path = _store(tmp_path, blobs, blobs.node(*_simple_chat(blobs, 1)), wal=False)
    writer = sqlite3.connect(path)
    try:
        writer.execute("BEGIN EXCLUSIVE")
        assert CursorSessionParser().parse_file(path) == []
    finally:
        writer.close()


@pytest.mark.parametrize("content", [b"", b"not a database at all" * 100, b"SQLite format 3\x00" + b"\x00" * 200])
def test_cursor_returns_nothing_for_a_corrupted_store(tmp_path, content):
    path = tmp_path / "store.db"
    path.write_bytes(content)

    assert CursorSessionParser().parse_file(path) == []
    assert _snapshot(tmp_path) == {"store.db": content}


def test_cursor_returns_nothing_for_a_missing_store_without_creating_it(tmp_path):
    assert CursorSessionParser().parse_file(tmp_path / "store.db") == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM meta",
        "UPDATE meta SET value = 'zz'",
        "UPDATE meta SET value = '5b315d'",
        "UPDATE meta SET value = NULL",
        "DROP TABLE meta",
        "DROP TABLE blobs",
        "DELETE FROM blobs",
        "UPDATE blobs SET data = NULL",
    ],
)
def test_cursor_returns_nothing_for_unusable_content(tmp_path, statement):
    blobs = _Blobs()
    path = _store(tmp_path, blobs, blobs.node(*_simple_chat(blobs, 1)), wal=False)
    connection = sqlite3.connect(path)
    connection.execute(statement)
    connection.commit()
    connection.close()

    assert CursorSessionParser().parse_file(path) == []


def test_cursor_returns_nothing_without_a_root(tmp_path):
    blobs = _Blobs()
    _simple_chat(blobs)

    assert CursorSessionParser().parse_file(_store(tmp_path, blobs, None)) == []


# --- Grok ---


def _grok_user(*texts: str, **extra) -> dict:
    return {"type": "user", "content": [_text(text) for text in texts]} | extra


def _grok_prompt(text: str, index: int) -> dict:
    return _grok_user(f"<user_query>\n{text}\n</user_query>", prompt_index=index)


def _grok_answer(content, **extra) -> dict:
    return {"type": "assistant", "content": content} | extra


def _usage(number: int, ended: int, tokens_in: int, tokens_out: int, model: str | None = None) -> dict:
    record = {"turnNumber": number, "endedAt": _iso(ended), "inputTokens": tokens_in, "outputTokens": tokens_out}
    return record | ({"primaryModelId": model} if model else {})


def _summary(**extra) -> dict:
    return {
        "info": {"id": "grok-1", "cwd": "/srv/alice/work/kotoyose"},
        "current_model_id": "grok-default",
        "created_at": _iso(0),
        "updated_at": _iso(900),
    } | extra


def _session(tmp_path: Path, lines: list, summary=None, usage=None, directory: str = "%2Fhome%2Falice%2Fproj") -> Path:
    folder = tmp_path / "sessions" / directory / "session-dir"
    folder.mkdir(parents=True)
    path = folder / "chat_history.jsonl"
    path.write_text("".join((line if isinstance(line, str) else json.dumps(line)) + "\n" for line in lines), encoding="utf-8")
    for name, value in (("summary.json", summary), ("usage.json", usage)):
        if value is not None:
            (folder / name).write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")
    return path


def test_grok_excludes_reasoning_and_tool_payloads(tmp_path):
    lines = [
        {"type": "system", "content": "SECRET-SYSTEM"},
        _grok_prompt("q0", 0),
        {"type": "reasoning", "summary": [_text("SECRET-SUMMARY")], "encrypted_content": "SECRET-ENCRYPTED"},
        _grok_answer("", tool_calls=[{"name": "shell", "arguments": "SECRET-ARGS"}], model_id="grok-used"),
        {"type": "tool_result", "tool_call_id": "t", "content": "SECRET-OUTPUT"},
        _grok_answer([_text("Done."), {"type": "reasoning", "text": "SECRET-BLOCK"}], model_id=""),
        _grok_prompt("q1", 1),
        {"type": "tool_result", "content": "SECRET-OUTPUT-2"},
        _grok_answer("a1"),
        _grok_prompt("q2", 2),
        _grok_answer("a2", tool_calls=[]),
        "{broken",
    ]

    turns = GrokSessionParser().parse_file(_session(tmp_path, lines, summary=_summary()))

    assert [(turn.prompt, turn.response) for turn in turns] == [("q0", "Done."), ("q1", "a1"), ("q2", "a2")]
    assert [turn.has_tool_calls for turn in turns] == [True, True, False]
    assert [turn.model for turn in turns] == ["grok-used", "grok-default", "grok-default"]
    assert {(turn.source_kind, turn.provider, turn.session_id, turn.cwd_basename) for turn in turns} == {
        ("grok", "grok", "grok-1", "kotoyose")
    }
    assert "SECRET" not in repr(turns)


def test_grok_filters_injected_user_lines_and_blocks(tmp_path):
    lines = [
        _grok_user("<user_info>\nINJECTED\n</user_info>\n\n<rules>\nINJECTED\n</rules>"),
        _grok_user("<system-reminder>\nINJECTED\n</system-reminder>", synthetic_reason="system_reminder"),
        _grok_user("INJECTED without a tag", synthetic_reason="other"),
        _grok_user(
            "<environment_context>INJECTED</environment_context>",
            "<INSTRUCTIONS>INJECTED</INSTRUCTIONS>",
            "<user_info>INJECTED</user_info>\nfirst",
        ),
        _grok_answer("a0"),
        _grok_user("<system-reminder>INJECTED</system-reminder>"),
        _grok_answer("still a0"),
        {"type": "user", "content": "second with <user_info>my own tag</user_info>"},
        _grok_answer("a1"),
    ]

    turns = GrokSessionParser().parse_file(_session(tmp_path, lines))

    assert [(turn.prompt, turn.response) for turn in turns] == [
        ("first", "a0\n\nstill a0"),
        ("second with <user_info>my own tag</user_info>", "a1"),
    ]
    assert "INJECTED" not in repr(turns)


def test_grok_maps_usage_times_and_tokens_per_turn(tmp_path):
    lines = [_grok_prompt("q0", 0), _grok_answer("a0"), _grok_prompt("q1", 1), _grok_answer("a1", model_id="line-model")]
    usage = {"turns": [_usage(2, 50, 30, 40, "usage-model-2"), _usage(1, 20, 10, 15, "usage-model-1")]}

    turns = GrokSessionParser().parse_file(_session(tmp_path, lines, summary=_summary(), usage=usage))

    assert [(turn.received_at, turn.completed_at) for turn in turns] == [(_utc(0), _utc(20)), (_utc(20), _utc(50))]
    assert [(turn.tokens_in, turn.tokens_out) for turn in turns] == [(10, 15), (30, 40)]
    assert [turn.model for turn in turns] == ["usage-model-1", "line-model"]
    assert not any(turn.timestamp_fallback for turn in turns)


@pytest.mark.parametrize("with_index", [True, False])
def test_grok_keeps_usage_aligned_when_a_prompt_has_no_answer(tmp_path, with_index):
    prompts = [_grok_prompt(f"q{n}", n) for n in range(3)]
    if not with_index:
        for prompt in prompts:
            del prompt["prompt_index"]
    lines = [prompts[0], _grok_answer("a0"), prompts[1], prompts[2], _grok_answer("a2")]
    usage = {"turns": [_usage(1, 20, 1, 2), _usage(2, 30, 3, 4), _usage(3, 40, 5, 6)]}

    turns = GrokSessionParser().parse_file(_session(tmp_path, lines, summary=_summary(), usage=usage))

    assert [turn.turn_index for turn in turns] == [0, 1]
    assert [(turn.received_at, turn.completed_at, turn.tokens_in) for turn in turns] == [
        (_utc(0), _utc(20), 1),
        (_utc(30), _utc(40), 5),
    ]


def test_grok_counts_a_usage_record_once(tmp_path):
    lines = [_grok_prompt("q0", 0), _grok_answer("a0"), _grok_prompt("again", 0), _grok_answer("a1")]

    turns = GrokSessionParser().parse_file(_session(tmp_path, lines, usage={"turns": [_usage(1, 20, 1, 2)]}))

    assert [turn.tokens_in for turn in turns] == [1, None]


def test_grok_falls_back_deterministically_when_usage_is_missing(tmp_path):
    lines = [_grok_prompt("q0", 0), _grok_answer("a0"), _grok_prompt("q1", 1), _grok_answer("a1")]
    path = _session(tmp_path, lines, summary=_summary())

    turns = GrokSessionParser().parse_file(path)

    assert [(turn.received_at, turn.completed_at) for turn in turns] == [(_utc(0), _utc(0)), (_utc(1), _utc(1))]
    assert [turn.timestamp_fallback for turn in turns] == [False, True]
    assert [(turn.tokens_in, turn.tokens_out) for turn in turns] == [(None, None), (None, None)]
    # The session's last update time moves between imports and must not leak into the turns.
    (path.parent / "summary.json").write_text(json.dumps(_summary(updated_at=_iso(5000))), encoding="utf-8")
    assert GrokSessionParser().parse_file(path) == turns


@pytest.mark.parametrize("sidecar", [None, "{broken", "[1]", '{"info": 1, "turns": {}}'])
def test_grok_works_without_usable_sidecars(tmp_path, sidecar):
    lines = [_grok_prompt("q0", 0), _grok_answer("a0")]
    path = _session(tmp_path, lines, summary=sidecar, usage=sidecar)

    [turn] = GrokSessionParser().parse_file(path)

    assert (turn.session_id, turn.cwd_basename, turn.model) == ("session-dir", "proj", "unknown")
    assert turn.received_at == DEFAULT_FALLBACK_EPOCH + timedelta(seconds=1)
    assert turn.timestamp_fallback is True


@pytest.mark.parametrize(
    ("directory", "expected"),
    [("%2Fhome%2Falice%2Fmy%20app", "my app"), ("C%3A%5Cwork%5Capp", "app"), ("plain-name", None)],
)
def test_grok_takes_the_cwd_from_the_encoded_directory(tmp_path, directory, expected):
    lines = [_grok_prompt("q0", 0), _grok_answer("a0")]

    [turn] = GrokSessionParser().parse_file(_session(tmp_path, lines, directory=directory))

    assert turn.cwd_basename == expected


# --- Codex child threads ---


def _rollout(tmp_path: Path, meta: dict) -> Path:
    lines = [{"type": "session_meta", "payload": meta}]
    for number in range(2):
        for role, kind, text in (("user", "input_text", f"q{number}"), ("assistant", "output_text", f"a{number}")):
            payload = {"type": "message", "role": role, "content": [{"type": kind, "text": text}]}
            lines.append({"type": "response_item", "timestamp": _iso(number), "payload": payload})
    # A copy of the parent's metadata must not change what this thread is.
    lines.append({"type": "session_meta", "payload": {"id": "parent", "session_id": "parent"}})
    path = tmp_path / "rollout.jsonl"
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "meta",
    [
        {"id": "child", "session_id": "parent"},
        {"id": "child", "source": {"subagent": {"thread_spawn": {"parent_thread_id": "parent"}}}},
        {"id": "child", "source": "subagent"},
        {"id": "child", "parent_thread_id": "parent"},
    ],
)
def test_codex_tags_every_turn_of_a_child_thread(tmp_path, meta):
    turns = CodexSessionParser().parse_file(_rollout(tmp_path, meta))

    assert [(turn.session_id, turn.is_subagent, turn.agent_origin) for turn in turns] == [("child", True, "subagent")] * 2


@pytest.mark.parametrize(
    "meta",
    [
        {"id": "own"},
        {"id": "own", "session_id": "own", "source": "cli", "parent_thread_id": None},
        {"session_id": "own", "source": {"custom": "ide"}, "parent_thread_id": ""},
    ],
)
def test_codex_leaves_top_level_threads_untagged(tmp_path, meta):
    turns = CodexSessionParser().parse_file(_rollout(tmp_path, meta))

    assert [(turn.session_id, turn.is_subagent, turn.agent_origin) for turn in turns] == [("own", False, None)] * 2


@pytest.mark.parametrize("name,expected", [
    (" native\n title ", "native title"), ("New Agent", None), ("NEW CHAT", None),
    ("Untitled", None), ("", None), (None, None), (123, None),
])
def test_cursor_native_title(tmp_path, name, expected):
    blobs = _Blobs()
    path = _store(tmp_path, blobs, blobs.node(*_simple_chat(blobs)), meta={"name": name})
    turns = CursorSessionParser().parse_file(path)
    assert len(turns) == 2
    assert all(turn.session_title == expected for turn in turns)


@pytest.mark.parametrize("summary,expected", [
    ({"generated_title": " native\n title "}, "native title"),
    ({"generated_title": "  "}, None), ({"generated_title": 123}, None),
    ("{broken", None), ([], None),
])
def test_grok_native_title(tmp_path, summary, expected):
    path = _session(tmp_path, [_grok_user("q"), _grok_answer("a"), _grok_user("q2"), _grok_answer("a2")],
                    summary=summary, directory="project")
    turns = GrokSessionParser().parse_file(path)
    assert len(turns) == 2
    assert all(turn.session_title == expected for turn in turns)


def test_grok_native_title_ignores_symlink(tmp_path):
    path = _session(tmp_path, [_grok_user("q"), _grok_answer("a")], directory="project")
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"generated_title": "private title"}))
    (path.parent / "summary.json").symlink_to(outside)
    assert GrokSessionParser().parse_file(path)[0].session_title is None
