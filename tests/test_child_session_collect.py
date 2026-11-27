"""子の許可境界、転送前秘匿化、保存の副作用、実 mTLS 通信を検証する。"""

import json
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from kotoyose.child_client import child_session, enroll_child
from kotoyose.child_identity import load_policy
from kotoyose.child_protocol import SESSION_COLLECT_POLICY_REFUSAL
from kotoyose.child_registry import ChildRegistry
from kotoyose.child_server import ChildHub
from kotoyose.session_import import (
    SessionImportStore, collect_local_batches, ingest_remote_batches, resolve_effective_origin_hint,
)
from kotoyose.session_import.paths import hash_source_path

SECRET = "ghp_" + "a" * 36


def policy(home, **extra):
    home.mkdir(parents=True, exist_ok=True)
    path = home / "policy.json"
    path.write_text(json.dumps(dict(workspace_roots=[], adapters=[], mcp={}, **extra)))
    return path


def history(home, count=1, response="answer"):
    path = home / ".claude/projects/project/session.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for index in range(count):
        lines.extend([
            dict(type="user", sessionId="session", cwd="/private/full/project",
                 timestamp=f"2026-01-01T00:00:{index * 2:02d}Z", message=dict(content=SECRET)),
            dict(type="assistant", timestamp=f"2026-01-01T00:00:{index * 2 + 1:02d}Z", message=dict(content=[
                dict(type="thinking", thinking="PRIVATE_THINKING"), dict(type="text", text=response),
            ])),
        ])
    path.write_text("\n".join(json.dumps(line) for line in lines))
    return path


def test_policy_backward_compatibility_and_order(tmp_path):
    path = policy(tmp_path)
    old = load_policy(path)
    assert old.session_import_sources == () and old.session_import_origin_hint is None
    policy(tmp_path, session_import_sources=["claude", "all", "claude"], session_import_origin_hint="work")
    current = load_policy(path)
    assert current.session_import_sources == ("claude", "all")
    assert current.session_import_origin_hint == "work"


@pytest.mark.parametrize("extra", [
    {"session_import_sources": "claude"}, {"session_import_sources": ["unknown"]},
    {"session_import_sources": [1]}, {"session_import_sources": None},
    {"session_import_origin_hint": "unknown"}, {"session_import_origin_hint": []},
])
def test_invalid_policy(tmp_path, extra):
    with pytest.raises(ValueError):
        load_policy(policy(tmp_path, **extra))


def test_registry_origin_persistence(tmp_path):
    path = tmp_path / "registry.json"
    registry = ChildRegistry(path, b"x" * 32)
    registry.enroll(name="child", fingerprint="fp", cert_pem="cert")
    record = registry.set_origin_hint("child", "work")
    record["origin_hint"] = "personal"
    assert ChildRegistry(path, b"x" * 32).get("child")["origin_hint"] == "work"
    assert registry.set_origin_hint("child", None)["origin_hint"] is None
    with pytest.raises(ValueError):
        registry.set_origin_hint("child", "bad")
    with pytest.raises(KeyError):
        registry.set_origin_hint("missing", "personal")


def test_collection_sanitizes_chunks_and_skips(tmp_path):
    path = history(tmp_path, count=4)
    batches, stats = collect_local_batches(allowed_sources=["claude"], home=tmp_path, max_batch_bytes=1300)
    assert len(batches) > 1
    serialized = json.dumps(batches)
    assert SECRET not in serialized and "PRIVATE_THINKING" not in serialized
    assert str(path) not in serialized and "/private/full" not in serialized
    assert "[REDACTED]" in serialized
    assert stats["redactions_count"] == 4
    assert all(len(json.dumps(batch, separators=(",", ":")).encode()) <= 1300 for batch in batches)
    stamp = path.stat()
    batches, stats = collect_local_batches(allowed_sources=["all"], home=tmp_path,
                                          known_files={hash_source_path(path): (stamp.st_mtime_ns, stamp.st_size)})
    assert not batches and stats["files_skipped_unchanged"] == 1


@pytest.mark.parametrize("allowed,requested", [([], "all"), (["codex"], "claude")])
def test_collection_policy_refuses(tmp_path, allowed, requested):
    with pytest.raises(PermissionError, match=SESSION_COLLECT_POLICY_REFUSAL):
        collect_local_batches(allowed_sources=allowed, source_kind=requested, home=tmp_path)


def test_collection_symlink_and_partial_ledger(tmp_path):
    history(tmp_path, count=2)
    outside = tmp_path / "outside.jsonl"
    outside.write_text("secret")
    (tmp_path / ".claude/projects/escape.jsonl").symlink_to(outside)
    batches, stats = collect_local_batches(allowed_sources=["claude"], home=tmp_path, max_turns=1)
    assert stats["files_scanned"] == 1
    assert len(batches[0]["turns"]) == 1 and batches[0]["file_skipped_since"] > 0


@pytest.mark.parametrize("hints,expected", [((None,), None), (("personal", None), "personal"),
                                          (("personal", "work"), "work")])
def test_effective_origin(hints, expected):
    assert resolve_effective_origin_hint(*hints) == expected
    with pytest.raises(ValueError):
        resolve_effective_origin_hint("work", "invalid")


async def test_remote_ingest_defense_dedup_and_dry_run(tmp_path):
    history(tmp_path, count=3)
    batches, stats = collect_local_batches(allowed_sources=["claude"], home=tmp_path)
    batches[0]["turns"][0]["prompt"] = SECRET
    batches[0]["turns"][1]["truncated"] = True
    store = SessionImportStore(None)
    stats["child_origin_hint"] = "work"
    dry = await ingest_remote_batches(batches, source_machine="child", origin_hint="personal",
                                      remote_stats=stats, dry_run=True, store=store)
    assert dry.turns_imported == 3
    assert not store._interactions and not store._files and not store._runs
    result = await ingest_remote_batches(batches, source_machine="child", origin_hint="personal",
                                         remote_stats=stats, store=store)
    assert result.turns_imported == 3 and result.origin_hint == "work"
    rows = list(store._interactions.values())
    assert all(row["metadata"]["source_machine"] == "child" for row in rows)
    assert all(row["metadata"]["origin_hint"] == "work" for row in rows)
    assert SECRET not in json.dumps(rows, default=str)
    assert rows[0]["metadata"]["redacted"] and rows[1]["metadata"]["truncated"]
    again = await ingest_remote_batches(batches, source_machine="child", store=store)
    assert again.turns_imported == 0 and again.turns_skipped_duplicate == 3
    ledger = await store.get_file_ledger("claude", "child", batches[0]["source_path_hash"])
    assert ledger.imported_count == 3
    assert await store.get_file_ledger("claude", "other", batches[0]["source_path_hash"]) is None
    snapshot = deepcopy((store._interactions, store._files, store._runs))
    dry = await ingest_remote_batches(batches, source_machine="child", dry_run=True, store=store)
    assert dry.turns_imported == 0 and dry.turns_skipped_duplicate == 3
    assert snapshot == (store._interactions, store._files, store._runs)
    store.find_live_session_ids = AsyncMock(return_value={"session"})
    live = await ingest_remote_batches(batches, source_machine="second", store=store)
    assert live.turns_imported == 0 and live.turns_skipped_duplicate == 3


@pytest.mark.parametrize("enabled", [True, False])
async def test_loopback_mtls_collection(tmp_path, monkeypatch, enabled):
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    history(tmp_path / "user")
    home = tmp_path / "child"
    policy(home, session_import_sources=["claude"] if enabled else [], session_import_origin_hint="work")
    hub = ChildHub(tmp_path / "host", heartbeat_interval=0.1)
    await hub.start("127.0.0.1", 0)
    try:
        code = hub.issue_enrollment_code(f"127.0.0.1:{hub.port}")
        await enroll_child(code, home, name="box")
        async with child_session(home, heartbeat_interval=0.1):
            entry = hub.session_collect_children()[0]
            assert entry["connected"] and entry["supports_session_collect"]
            assert entry["effective_origin_hint"] == "work"
            if not enabled:
                with pytest.raises(PermissionError, match=SESSION_COLLECT_POLICY_REFUSAL):
                    await hub.collect_sessions("box")
                return
            batches, stats, hint = await hub.collect_sessions("box")
            assert batches and SECRET not in json.dumps(batches)
            store = SessionImportStore(None)
            result = await ingest_remote_batches(batches, source_machine="box", origin_hint=hint,
                                                 remote_stats=stats, store=store)
            assert result.turns_imported == 1 and result.origin_hint == "work"
            assert next(iter(store._interactions.values()))["metadata"]["source_machine"] == "box"
            policy(home)
            with pytest.raises(PermissionError, match=SESSION_COLLECT_POLICY_REFUSAL):
                await hub.collect_sessions("box")
    finally:
        await hub.stop()


async def test_since_truncation_and_chunked_ledger(tmp_path):
    path = history(tmp_path, count=3, response="あ" * 70000)
    batches, stats = collect_local_batches(allowed_sources=["claude"], home=tmp_path, max_batch_bytes=200000)
    assert len(batches) > 1 and stats["truncations_count"] == 3
    assert all(turn["truncated"] for batch in batches for turn in batch["turns"])
    store = SessionImportStore(None)
    summary = await ingest_remote_batches(batches, source_machine="child", store=store)
    assert summary.turns_imported == 3 and summary.files_scanned == 1
    ledger = await store.get_file_ledger("claude", "child", hash_source_path(path))
    assert ledger.imported_count == 3 and ledger.last_turn_index == 2
    from kotoyose.session_import import parse_since

    batches, stats = collect_local_batches(allowed_sources=["claude"], home=tmp_path,
                                          since=parse_since("2026-01-01T00:00:03Z"))
    assert stats["turns_skipped_since"] == 1
    filtered = SessionImportStore(None)
    summary = await ingest_remote_batches(batches, source_machine="child", store=filtered, remote_stats=stats)
    assert summary.turns_imported == 2 and summary.turns_skipped_since == 1
    assert not filtered._files


async def test_remote_session_title_passthrough_and_host_redaction(tmp_path):
    path = history(tmp_path, count=2)
    with path.open("a") as handle:
        handle.write("\n" + json.dumps({"type": "custom-title", "customTitle": "Title\n" + SECRET}))
    batches, stats = collect_local_batches(allowed_sources=["claude"], home=tmp_path)
    assert all(turn["session_title"] == "Title [REDACTED]" for batch in batches for turn in batch["turns"])
    assert SECRET not in json.dumps(batches)
    batches[0]["turns"][1]["session_title"] = "Host\n" + SECRET + " " + "x" * 200
    store = SessionImportStore(None)
    result = await ingest_remote_batches(batches, source_machine="child", remote_stats=stats, store=store)
    assert result.turns_imported == 2
    rows = list(store._interactions.values())
    assert rows[0]["metadata"]["session_title"] == "Title [REDACTED]"
    assert rows[1]["metadata"]["session_title"].startswith("Host [REDACTED] ")
    assert len(rows[1]["metadata"]["session_title"]) == 160
    assert rows[1]["metadata"]["redacted"] and rows[1]["metadata"]["truncated"]
    assert SECRET not in json.dumps(rows, default=str)


def test_collection_normalizes_and_limits_title_before_transfer(tmp_path, monkeypatch):
    from kotoyose.session_import import remote
    from kotoyose.session_import.parsers import ClaudeSessionParser

    path = history(tmp_path)
    turns = ClaudeSessionParser().parse_file(path)
    turns[0].prompt = "q"
    turns[0].session_title = "Title\n" + SECRET + " " + "x" * 200

    class Parser:
        def parse_file(self, path):
            return turns

    monkeypatch.setattr(remote, "get_parser", lambda kind: Parser())
    batches, stats = collect_local_batches(allowed_sources=["claude"], home=tmp_path)
    turn = batches[0]["turns"][0]
    assert turn["session_title"].startswith("Title [REDACTED] ")
    assert len(turn["session_title"]) == 160
    assert turn["redacted"] and turn["truncated"]
    assert stats["redactions_count"] == stats["truncations_count"] == 1
