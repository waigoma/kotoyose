"""Security tests for session import: credential redaction, UTF-8 truncation, and path containment."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kotoyose.session_import.models import MAX_TEXT_BYTES
from kotoyose.session_import.paths import (
    PathSecurityError,
    configured_allowed_roots,
    default_source_roots,
    hash_source_path,
    iter_safe_source_files,
    is_within_root,
    safe_project_name,
    validate_scan_root,
)
from kotoyose.session_import.redaction import (
    REDACTED_PLACEHOLDER,
    TRUNCATION_SUFFIX,
    redact_credentials,
    sanitize_text,
    truncate_utf8,
)

ANTHROPIC_KEY = "sk-ant-api03-" + "Q" * 40
OPENAI_PROJECT_KEY = "sk-proj-" + "a1B2c3" * 8
JWT = "eyJ" + "hbGciOiJIUzI1NiJ9." + "eyJ" + "zdWIiOiIxMjM0NTY3ODkwIn0." + "x9Y8z7" * 8
PEM_BODY_LINES = [
    "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7",
    "9Zq8fTnKbLmWxR3vY5uJ0pHcQwErTyUiOpAsDfGhJkLz",
    "QwErTyUiOpAsDfGhJkLzXcVbNm1234567890abcdefgh",
]
PEM_HEADER = "-----" + "BEGIN RSA PRIVATE KEY-----"
PEM_FOOTER = "-----" + "END RSA PRIVATE KEY-----"
PEM_BLOCK = PEM_HEADER + "\n" + "\n".join(PEM_BODY_LINES) + "\n" + PEM_FOOTER


def _assert_no_leak(text: str, *fragments: str) -> None:
    for fragment in fragments:
        assert fragment not in text


# --- redact_credentials -------------------------------------------------


def test_redacts_anthropic_api_key():
    text = f"key={ANTHROPIC_KEY} end"
    redacted, was_redacted = redact_credentials(text)
    assert was_redacted is True
    assert redacted == f"key={REDACTED_PLACEHOLDER} end"


def test_redacts_openai_project_key():
    redacted, was_redacted = redact_credentials(f"use {OPENAI_PROJECT_KEY} please")
    assert was_redacted is True
    assert OPENAI_PROJECT_KEY not in redacted
    assert REDACTED_PLACEHOLDER in redacted


def test_redacts_jwt():
    redacted, was_redacted = redact_credentials(f"token: {JWT}")
    assert was_redacted is True
    assert JWT not in redacted


def test_redacts_bearer_token_keeping_label():
    token = "abcdefghijklmnopqrstuvwxyz012345"
    redacted, was_redacted = redact_credentials(f"Authorization: Bearer {token}")
    assert was_redacted is True
    assert redacted == f"Authorization: Bearer {REDACTED_PLACEHOLDER}"


def test_redacts_entire_pem_private_key_body():
    text = f"before\n{PEM_BLOCK}\nafter"
    redacted, was_redacted = redact_credentials(text)
    assert was_redacted is True
    _assert_no_leak(redacted, "BEGIN RSA PRIVATE KEY", "END RSA PRIVATE KEY", *PEM_BODY_LINES)
    assert redacted == f"before\n{REDACTED_PLACEHOLDER}\nafter"


def test_redacts_unclosed_pem_block_to_end_of_string():
    unclosed_header = "-----" + "BEGIN PRIVATE KEY-----"
    text = f"intro\n{unclosed_header}\n" + PEM_BODY_LINES[0] + "\n" + PEM_BODY_LINES[1]
    redacted, was_redacted = redact_credentials(text)
    assert was_redacted is True
    _assert_no_leak(redacted, *PEM_BODY_LINES[:2], "BEGIN PRIVATE KEY")
    assert redacted == f"intro\n{REDACTED_PLACEHOLDER}"


def test_redacts_kotoyose_client_key():
    key = "kys_" + "AbCdEf123_-" * 3
    redacted, was_redacted = redact_credentials(f"client {key}")
    assert was_redacted is True
    assert key not in redacted


def test_redacts_github_tokens():
    classic = "ghp_" + "a1B2c3D4e5F6g7H8i9J0" + "kLmN"
    fine_grained = "github_pat_" + "11ABCDEFG0_" + "x" * 30
    redacted, was_redacted = redact_credentials(f"{classic} and {fine_grained}")
    assert was_redacted is True
    _assert_no_leak(redacted, classic, fine_grained)
    assert redacted == f"{REDACTED_PLACEHOLDER} and {REDACTED_PLACEHOLDER}"


def test_redacts_aws_access_key_id():
    aws_key = "AKIA" + "IOSFODNN7EXAMPLE"
    redacted, was_redacted = redact_credentials(f"aws {aws_key} here")
    assert was_redacted is True
    assert aws_key not in redacted


def test_redacts_slack_token():
    token = "xoxb" + "-1234567890-abcdefghijKLMNOP"
    redacted, was_redacted = redact_credentials(f"slack={token}")
    assert was_redacted is True
    assert token not in redacted


def test_overlapping_patterns_merge_into_single_placeholder():
    text = f"Bearer {ANTHROPIC_KEY}"
    redacted, was_redacted = redact_credentials(text)
    assert was_redacted is True
    assert redacted == f"Bearer {REDACTED_PLACEHOLDER}"


def test_jwt_inside_bearer_merges_into_single_placeholder():
    redacted, _ = redact_credentials(f"Bearer {JWT}")
    assert redacted == f"Bearer {REDACTED_PLACEHOLDER}"


def test_adjacent_secrets_merge_without_leaking_between_them():
    text = f"{ANTHROPIC_KEY}{OPENAI_PROJECT_KEY}"
    redacted, was_redacted = redact_credentials(text)
    assert was_redacted is True
    assert redacted == REDACTED_PLACEHOLDER


def test_clean_text_is_untouched():
    text = "Normal conversation about sk-short and kys_ and Bearer tokens in prose."
    redacted, was_redacted = redact_credentials(text)
    assert redacted == text
    assert was_redacted is False


# --- truncate_utf8 -----------------------------------------------------


def test_truncate_under_limit_is_untouched():
    assert truncate_utf8("hello") == ("hello", False)


def test_truncate_exact_limit_is_untouched():
    text = "a" * MAX_TEXT_BYTES
    assert truncate_utf8(text) == (text, False)


def test_truncate_ascii_over_limit_fits_within_limit():
    text = "a" * (MAX_TEXT_BYTES + 5000)
    truncated, was_truncated = truncate_utf8(text)
    assert was_truncated is True
    assert truncated.endswith(TRUNCATION_SUFFIX)
    assert len(truncated.encode("utf-8")) <= MAX_TEXT_BYTES


def test_truncate_japanese_multibyte_is_clean_and_within_limit():
    text = "日本語のセッション履歴です。" * 10000
    assert len(text.encode("utf-8")) > MAX_TEXT_BYTES
    truncated, was_truncated = truncate_utf8(text)
    assert was_truncated is True
    assert truncated.endswith(TRUNCATION_SUFFIX)
    assert len(truncated.encode("utf-8")) <= MAX_TEXT_BYTES
    # Round-tripping proves no partial code point was left behind.
    assert truncated.encode("utf-8").decode("utf-8") == truncated
    body = truncated[: -len(TRUNCATION_SUFFIX)]
    assert text.startswith(body)


def test_truncate_when_limit_cannot_hold_suffix_returns_raw_prefix():
    truncated, was_truncated = truncate_utf8("日本語", 2)
    assert was_truncated is True
    assert len(truncated.encode("utf-8")) <= 2
    assert truncated == ""


# --- sanitize_text -----------------------------------------------------


def test_sanitize_clean_short_text():
    assert sanitize_text("hello") == ("hello", False, False)


def test_sanitize_redacts_then_truncates():
    # A space separates the key from the tail. Otherwise the greedy token class
    # would also swallow the adjacent alphanumerics.
    text = "x" * (MAX_TEXT_BYTES - 20) + ANTHROPIC_KEY + " " + "y" * 200
    sanitized, was_redacted, was_truncated = sanitize_text(text)
    assert was_redacted is True
    assert was_truncated is True
    assert len(sanitized.encode("utf-8")) <= MAX_TEXT_BYTES
    _assert_no_leak(sanitized, "sk-ant-api03-", "QQQQ")


def test_sanitize_secret_at_boundary_is_redacted_before_cut():
    # The key straddles the byte limit. Redacting first must remove it entirely,
    # so no fragment survives as an unredacted tail.
    prefix = "x" * (MAX_TEXT_BYTES - 10)
    text = prefix + ANTHROPIC_KEY
    sanitized, was_redacted, _ = sanitize_text(text)
    assert was_redacted is True
    _assert_no_leak(sanitized, "sk-ant", "QQQQ")
    assert len(sanitized.encode("utf-8")) <= MAX_TEXT_BYTES


# --- default roots and allowed roots ----------------------------------


def test_default_source_roots(tmp_path: Path):
    roots = default_source_roots(tmp_path)
    assert roots == {
        "claude": tmp_path / ".claude" / "projects",
        "codex": tmp_path / ".codex" / "sessions",
        "antigravity": tmp_path / ".gemini" / "antigravity-cli" / "brain",
        "cursor": tmp_path / ".cursor" / "chats",
        "grok": tmp_path / ".grok" / "sessions",
    }


def test_configured_allowed_roots_includes_env_and_extra_without_duplicates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    env_root = tmp_path / "env-root"
    extra_root = tmp_path / "extra-root"
    monkeypatch.setenv("KOTOYOSE_IMPORT_ALLOWED_ROOTS", os.pathsep.join([str(env_root), str(tmp_path / ".claude" / "projects"), ""]))
    roots = configured_allowed_roots([extra_root, extra_root], home=tmp_path)
    resolved = [root.resolve() for root in roots]
    assert len(resolved) == len(set(resolved))
    assert env_root.resolve() in resolved
    assert extra_root.resolve() in resolved
    assert resolved.count((tmp_path / ".claude" / "projects").resolve()) == 1
    # Default roots come first, preserving order.
    assert roots[0] == tmp_path / ".claude" / "projects"


def test_configured_allowed_roots_without_env_returns_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("KOTOYOSE_IMPORT_ALLOWED_ROOTS", raising=False)
    roots = configured_allowed_roots(home=tmp_path)
    assert roots == tuple(default_source_roots(tmp_path).values())


def test_validate_scan_root_allows_inside(tmp_path: Path):
    allowed = tmp_path / "allowed"
    inside = allowed / "nested" / "dir"
    inside.mkdir(parents=True)
    assert validate_scan_root(inside, [allowed]) == inside.resolve()


def test_validate_scan_root_rejects_outside(tmp_path: Path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    with pytest.raises(PathSecurityError):
        validate_scan_root(outside, [allowed])


def test_validate_scan_root_rejects_symlink_escaping_allowed_root(tmp_path: Path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = allowed / "link"
    link.symlink_to(outside, target_is_directory=True)
    assert link.is_symlink()
    with pytest.raises(PathSecurityError):
        validate_scan_root(link, [allowed])


def test_is_within_root(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.jsonl").write_text("{}\n", encoding="utf-8")
    assert is_within_root(root / "a.jsonl", root) is True
    assert is_within_root(tmp_path / "other.jsonl", root) is False


# --- iter_safe_source_files -------------------------------------------


def test_iter_claude_collects_jsonl_only(tmp_path: Path):
    projects = tmp_path / ".claude" / "projects"
    session_dir = projects / "-srv-work-my-app"
    session_dir.mkdir(parents=True)
    (session_dir / "sess-1.jsonl").write_text("{}\n", encoding="utf-8")
    (session_dir / "notes.txt").write_text("x", encoding="utf-8")
    files = iter_safe_source_files("claude", projects, [projects])
    assert files == [(session_dir / "sess-1.jsonl").resolve()]


def test_iter_codex_collects_nested_jsonl(tmp_path: Path):
    sessions = tmp_path / ".codex" / "sessions"
    day = sessions / "2026" / "11" / "27"
    day.mkdir(parents=True)
    (day / "rollout-a.jsonl").write_text("{}\n", encoding="utf-8")
    (day / "rollout-b.jsonl").write_text("{}\n", encoding="utf-8")
    files = iter_safe_source_files("codex", sessions)
    assert files == sorted([(day / "rollout-a.jsonl").resolve(), (day / "rollout-b.jsonl").resolve()])


def test_iter_antigravity_matches_only_system_generated_logs_transcript(tmp_path: Path):
    brain = tmp_path / ".gemini" / "antigravity-cli" / "brain"
    conv = brain / "conv-1"
    logs = conv / ".system_generated" / "logs"
    logs.mkdir(parents=True)
    (logs / "transcript.jsonl").write_text("{}\n", encoding="utf-8")
    (conv / "other.jsonl").write_text("{}\n", encoding="utf-8")
    steps = conv / ".system_generated" / "steps"
    steps.mkdir()
    (steps / "transcript.jsonl").write_text("{}\n", encoding="utf-8")
    files = iter_safe_source_files("antigravity", brain, [brain])
    assert files == [(logs / "transcript.jsonl").resolve()]


def test_iter_antigravity_single_transcript_file_is_accepted(tmp_path: Path):
    logs = tmp_path / "brain" / "c" / ".system_generated" / "logs"
    logs.mkdir(parents=True)
    transcript = logs / "transcript.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    assert iter_safe_source_files("antigravity", transcript) == [transcript.resolve()]


def test_iter_missing_root_returns_empty(tmp_path: Path):
    assert iter_safe_source_files("claude", tmp_path / "missing", [tmp_path]) == []


def test_iter_rejects_root_outside_allowed_roots(tmp_path: Path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(PathSecurityError):
        iter_safe_source_files("claude", outside, [allowed])


def test_iter_skips_symlinked_file_pointing_outside(tmp_path: Path):
    root = tmp_path / "projects"
    root.mkdir()
    outside = tmp_path / "secret.jsonl"
    outside.write_text('{"secret": true}\n', encoding="utf-8")
    (root / "linked.jsonl").symlink_to(outside)
    (root / "real.jsonl").write_text("{}\n", encoding="utf-8")
    files = iter_safe_source_files("claude", root)
    assert files == [(root / "real.jsonl").resolve()]


def test_iter_does_not_descend_into_symlinked_directory(tmp_path: Path):
    root = tmp_path / "projects"
    root.mkdir()
    outside = tmp_path / "outside-dir"
    outside.mkdir()
    (outside / "leak.jsonl").write_text("{}\n", encoding="utf-8")
    (root / "linked-dir").symlink_to(outside, target_is_directory=True)
    (root / "real.jsonl").write_text("{}\n", encoding="utf-8")
    files = iter_safe_source_files("claude", root)
    assert files == [(root / "real.jsonl").resolve()]


def test_iter_single_non_jsonl_file_is_rejected(tmp_path: Path):
    path = tmp_path / "notes.txt"
    path.write_text("x", encoding="utf-8")
    assert iter_safe_source_files("claude", path) == []


# --- hash_source_path --------------------------------------------------


def test_hash_source_path_format_and_no_plain_path(tmp_path: Path):
    path = tmp_path / "session.jsonl"
    digest = hash_source_path(path)
    assert digest.startswith("sha256:")
    hex_part = digest.removeprefix("sha256:")
    assert len(hex_part) == 64
    assert all(ch in "0123456789abcdef" for ch in hex_part)
    assert str(path) not in digest
    assert hash_source_path(path) == digest


# --- safe_project_name -------------------------------------------------


@pytest.mark.parametrize(
    ("cwd", "expected"),
    [
        ("/srv/work/memory-platform", "memory-platform"),
        ("/srv/work/memory-platform/", "memory-platform"),
        ("C:\\Users\\me\\repo", "repo"),
        ("C:\\Users\\me\\repo\\", "repo"),
        ("/", None),
        ("", None),
        (".", None),
        ("..", None),
        ("/tmp", None),
    ],
)
def test_safe_project_name_from_cwd(cwd: str, expected: str | None):
    result = safe_project_name(cwd)
    assert result == expected
    if result is not None:
        assert "/" not in result and "\\" not in result


def test_safe_project_name_never_returns_full_path_from_claude_encoded_dir(tmp_path: Path):
    encoded = tmp_path / "-srv-work-my-app"
    result = safe_project_name(None, encoded)
    assert result is None


def test_safe_project_name_fallback_plain_name_returns_basename(tmp_path: Path):
    assert safe_project_name(None, tmp_path / "my-app") == "my-app"


def test_safe_project_name_without_inputs_is_none():
    assert safe_project_name() is None
