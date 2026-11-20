"""Tests for print-CLI profiles, stream parsers, and quota classification."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json

import pytest

from kotoyose.print_cli_events import create_print_cli_parser
from kotoyose.print_cli_profiles import (
    AGY_PROFILE,
    PrintCliLoginSpec,
    get_print_cli_profile,
    get_print_cli_profile_for_provider,
    list_print_cli_profiles,
)
from kotoyose.provider_usage_limits import classify_print_cli_usage_limit, normalize_provider_usage_limit


def test_agy_profile_fields_and_registry() -> None:
    profile = get_print_cli_profile("agy")
    assert profile is AGY_PROFILE
    assert profile in list_print_cli_profiles()
    assert profile.id == "agy"
    assert profile.provider == "antigravity"
    assert profile.binary == "agy"
    assert profile.min_version == (1, 5, 9)
    assert profile.settings_flag == "--settings"
    assert profile.parser == "agy_stream_json_v1"
    assert profile.login_home_env == "HOME"
    assert dict(profile.exit_codes) == {3: "timeout"}
    assert profile.max_prompt_bytes == 120 * 1024
    assert profile.max_concurrent_per_home == 2
    assert profile.quota_command == ("-p", "/usage", "--output-format", "json")
    assert profile.models_command == ("models",)
    assert isinstance(profile.login, PrintCliLoginSpec)
    assert profile.login.command == ("-p", "/usage", "--output-format", "json")
    assert profile.login.cli_timeout_seconds == 60
    assert profile.login.success_exit_code == 0
    assert profile.login.success_json_field == "status"
    assert profile.login.success_json_value == "SUCCESS"
    assert get_print_cli_profile_for_provider("antigravity") is AGY_PROFILE
    assert get_print_cli_profile_for_provider("unknown-provider") is None
    with pytest.raises(TypeError):
        profile.surfaces["read"] = None  # type: ignore[index]


def test_argv_rendering_per_surface_and_settings_file(tmp_path) -> None:
    prompt_with_braces = 'Return {"model": "{model}", "timeout": "{timeout}"}'
    read_settings = AGY_PROFILE.write_settings_file("read", tmp_path)
    assert read_settings is None
    read_argv = AGY_PROFILE.render_argv(
        model="gemini-4-hydrogen-high",
        prompt=prompt_with_braces,
        timeout_seconds=45.0,
        surface="read",
        settings_path=read_settings,
    )
    assert read_argv == [
        "-p",
        prompt_with_braces,
        "--model",
        "gemini-4-hydrogen-high",
        "--output-format",
        "stream-json",
        "--print-timeout",
        "45s",
        "--mode",
        "plan",
    ]

    write_settings = AGY_PROFILE.write_settings_file("write", tmp_path)
    assert write_settings is not None and write_settings.exists()
    settings_json = json.loads(write_settings.read_text(encoding="utf-8"))
    assert settings_json == {
        "permissions": {
            "allow": [
                "command(*)",
                "write_file(*)",
                "edit_file(*)",
                "replace_file_content(*)",
            ]
        }
    }
    write_argv = AGY_PROFILE.render_argv(
        model="gemini-4-hydrogen-high",
        prompt="Edit file",
        timeout_seconds=10.0,
        surface="write",
        settings_path=write_settings,
        add_dirs=[tmp_path / "extra"],
    )
    assert write_argv == [
        "-p",
        "Edit file",
        "--model",
        "gemini-4-hydrogen-high",
        "--output-format",
        "stream-json",
        "--print-timeout",
        "10s",
        "--mode",
        "accept-edits",
        "--add-dir",
        str(tmp_path / "extra"),
        "--settings",
        str(write_settings),
    ]


def test_profile_digest_is_stable_and_reflects_field_changes() -> None:
    digest_a = AGY_PROFILE.profile_digest()
    digest_b = get_print_cli_profile("agy").profile_digest()
    assert len(digest_a) == 64
    assert digest_a == digest_b
    modified = replace(AGY_PROFILE, max_prompt_bytes=64 * 1024)
    assert modified.profile_digest() != digest_a
    modified_login = replace(AGY_PROFILE, login=replace(AGY_PROFILE.login, cli_timeout_seconds=30))
    assert modified_login.profile_digest() != digest_a


def test_parser_normal_stream_with_tools_and_usage() -> None:
    parser = create_print_cli_parser("agy_stream_json_v1")
    lines = [
        json.dumps(
            {
                "event": "init",
                "conversation_id": "conv-123",
                "init": {"model": "gemini-4-hydrogen-high", "cwd": "/tmp/ws", "tools": ["command"], "permission_mode": "accept-edits"},
            }
        ),
        json.dumps(
            {
                "event": "step_update",
                "step_update": {"step_index": 0, "state": "DONE", "step_type": "user_input", "text_delta": "ignored prompt"},
            }
        ),
        json.dumps(
            {
                "event": "step_update",
                "step_update": {"step_index": 1, "state": "DONE", "step_type": "thought", "text_delta": "internal thought"},
            }
        ),
        json.dumps(
            {
                "event": "step_update",
                "step_update": {
                    "step_index": 2,
                    "state": "DONE",
                    "step_type": "tool",
                    "tool_info": {"id": "call-1", "name": "write_file", "command": "secret"},
                },
            }
        ),
        json.dumps(
            {
                "event": "step_update",
                "step_update": {
                    "step_index": 3,
                    "state": "DONE",
                    "step_type": "agent_response",
                    "text_delta": "chunk-1 ",
                    "usage": {"input_tokens": 12, "output_tokens": 4, "total_tokens": 16},
                },
            }
        ),
        json.dumps(
            {
                "event": "result",
                "result": {
                    "conversation_id": "conv-123",
                    "status": "SUCCESS",
                    "response": "final answer",
                    "duration_seconds": 1.2,
                    "num_turns": 2,
                    "usage": {"input_tokens": 20, "output_tokens": 9, "total_tokens": 29},
                },
            }
        ),
    ]
    emitted = []
    for line in lines:
        emitted.extend(parser.feed_line(line))

    outcome = parser.finalize()
    assert outcome.saw_init is True
    assert outcome.saw_result is True
    assert outcome.session_id == "conv-123"
    assert outcome.result_status == "SUCCESS"
    assert outcome.content == "final answer"
    assert outcome.turns == 2
    assert outcome.usage == {"source": "reported", "cost": None, "input_tokens": 20, "output_tokens": 9}
    assert any(ev.kind == "tool" and ev.tool_name == "write_file" and "command" not in (ev.payload or {}) for ev in emitted)


def test_parser_malformed_lines_missing_result_and_non_success() -> None:
    parser = create_print_cli_parser("agy_stream_json_v1")
    parser.feed_line("")
    parser.feed_line("not valid json")
    parser.feed_line("[1, 2, 3]")
    parser.feed_line(
        json.dumps(
            {
                "event": "step_update",
                "step_update": {
                    "step_index": 1,
                    "state": "ACTIVE",
                    "step_type": "agent_response",
                    "text_delta": "partial ",
                    "usage": {"input_tokens": 7, "output_tokens": 3},
                },
            }
        )
    )
    partial = parser.finalize()
    assert partial.saw_result is False
    assert partial.malformed_lines == 2
    assert partial.content == "partial "
    assert partial.usage == {"source": "reported", "cost": None, "input_tokens": 7, "output_tokens": 3}

    err_parser = create_print_cli_parser("agy_stream_json_v1")
    err_parser.feed_line(
        json.dumps(
            {
                "event": "result",
                "result": {
                    "status": "FAILED",
                    "response": "",
                    "num_turns": 1,
                    "error": {"status": "RESOURCE_EXHAUSTED", "code": 429, "message": "Model quota reached"},
                },
            }
        )
    )
    err_outcome = err_parser.finalize()
    assert err_outcome.saw_result is True
    assert err_outcome.result_status == "FAILED"
    assert err_outcome.diagnostics["status"] == "RESOURCE_EXHAUSTED"
    assert err_outcome.diagnostics["api_error_status"] == 429


def test_profile_driven_quota_classification_excludes_auth_failures() -> None:
    now = datetime(2026, 11, 15, 0, 0, tzinfo=timezone.utc)
    quota = classify_print_cli_usage_limit(
        AGY_PROFILE,
        {"status": "RESOURCE_EXHAUSTED", "api_error_status": 429, "retry_after": "120"},
        now,
    )
    assert quota is not None
    assert quota.provider == "print_cli"
    assert quota.limit_window == "rate"
    assert quota.resume == "unavailable"
    assert quota.unavailable_reason == "no_session"
    assert quota.reset_at == datetime(2026, 11, 15, 0, 2, tzinfo=timezone.utc)

    via_norm = normalize_provider_usage_limit(
        "print_cli",
        {"profile": "agy", "stderr_tail": "MODEL_QUOTA_EXHAUSTED: quota will reset later"},
        now,
    )
    assert via_norm is not None
    assert via_norm.provider == "print_cli"
    assert via_norm.limit_window == "unknown"

    auth_err = classify_print_cli_usage_limit(
        AGY_PROFILE,
        {"http_status": 401, "stderr_tail": "Authentication required. Please visit the URL to log in"},
        now,
    )
    assert auth_err is None


def test_agy_egress_allowlist_and_profile_digest_sensitivity() -> None:
    assert AGY_PROFILE.egress_hosts == (
        "daily-cloudcode-pa.googleapis.com",
        "aicode.googleapis.com",
        "www.googleapis.com",
        "play.googleapis.com",
        "lh3.googleusercontent.com",
        "oauth2.googleapis.com",  # agy 自身の token refresh (jail の実地確認で判明)
    )
    assert AGY_PROFILE.egress_ports == (443,)

    baseline = AGY_PROFILE.profile_digest()
    changed_hosts = replace(AGY_PROFILE, egress_hosts=(*AGY_PROFILE.egress_hosts, "example.com"))
    changed_ports = replace(AGY_PROFILE, egress_ports=(443, 8443))
    assert changed_hosts.profile_digest() != baseline
    assert changed_ports.profile_digest() != baseline

