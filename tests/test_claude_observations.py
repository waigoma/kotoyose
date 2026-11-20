"""Installed SDK telemetry contracts; no authentication or provider calls."""

from dataclasses import asdict

import pytest
from claude_agent_sdk import MirrorErrorMessage, RateLimitEvent, RateLimitInfo
from claude_agent_sdk._internal.message_parser import parse_message

from kotoyose.runtime.claude_observations import claude_observation


def task_message(subtype, **overrides):
    fields = {
        "type": "system", "subtype": subtype, "task_id": "task-1",
        "uuid": "uuid-1", "session_id": "session-1", "tool_use_id": "toolu-1",
        "description": "SECRET description", "summary": "SECRET summary",
        "output_file": "/SECRET/private-output", "task_type": "SECRET type",
        "status": "completed", "patch": {"status": "killed", "error": "SECRET patch"},
        "usage": {"total_tokens": 7, "tool_uses": 2, "duration_ms": 3, "raw": "SECRET usage"},
    }
    fields.update(overrides)
    return parse_message(fields)


@pytest.mark.parametrize("subtype, status, terminal", [
    ("task_started", "running", False),
    ("task_progress", "running", False),
    ("task_notification", "completed", True),
    ("task_updated", "stopped", True),
])
def test_real_sdk_task_lifecycle_preserves_identity_but_never_content(subtype, status, terminal):
    message = task_message(subtype)
    event = claude_observation(message, session_id="session-1")
    assert event.kind == subtype
    assert event.payload["task_id"] == "task-1"
    assert event.payload["status"] == status
    assert event.payload["terminal"] is terminal
    assert "SECRET" not in repr(asdict(event))
    if subtype == "task_progress":
        assert event.payload["task_usage"] == {"total_tokens": 7, "tool_uses": 2, "duration_ms": 3}


@pytest.mark.parametrize("status", ["pending", "running", "paused", "completed", "failed", "killed"])
def test_update_only_terminal_status_needs_no_notification(status):
    event = claude_observation(task_message("task_updated", patch={"status": status}))
    assert event.payload["terminal"] is (status in {"completed", "failed", "killed"})
    assert event.payload["status"] == ("stopped" if status == "killed" else status)


@pytest.mark.parametrize("overrides", [
    {"task_id": "/SECRET/private"}, {"task_id": "x" * 129},
    {"patch": {"status": "SECRET unknown"}}, {"patch": []},
])
def test_malformed_and_unknown_task_metadata_are_ignored(overrides):
    assert claude_observation(task_message("task_updated", **overrides)) is None


def test_rate_limit_allowlist_and_foreign_session_fence():
    message = RateLimitEvent(
        RateLimitInfo(
            status="allowed_warning", rate_limit_type="five_hour", resets_at=123,
            utilization=0.9, overage_status="rejected", overage_resets_at=234,
            overage_disabled_reason="SECRET reason", raw={"token": "SECRET raw"},
        ), "uuid-1", "session-1",
    )
    event = claude_observation(message, session_id="session-1")
    assert event.payload == {
        "source": "claude_sdk", "status": "allowed_warning", "rate_limit_type": "five_hour",
        "resets_at": 123, "utilization": 0.9, "overage_status": "rejected", "overage_resets_at": 234,
    }
    assert "SECRET" not in repr(event)
    assert claude_observation(message, session_id="different-session") is None
    assert claude_observation(task_message("task_started"), session_id="different-session") is None


@pytest.mark.parametrize("value", [True, -1, 2, float("inf"), float("nan"), 10**1000, "SECRET"])
def test_rate_limit_invalid_numeric_fields_are_absent(value):
    message = RateLimitEvent(
        RateLimitInfo("allowed", utilization=value, resets_at=value), "uuid-1", "session-1",
    )
    event = claude_observation(message)
    assert "utilization" not in event.payload
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        assert "resets_at" not in event.payload


def test_unknown_rate_fields_and_mirror_errors_never_dump_raw_sdk_data():
    message = RateLimitEvent(
        RateLimitInfo("allowed", rate_limit_type=["SECRET"], overage_status={"SECRET": True}),
        "uuid-1", "session-1",
    )
    assert claude_observation(message).payload == {"source": "claude_sdk", "status": "allowed"}
    message.rate_limit_info.status = ["SECRET"]
    assert claude_observation(message) is None
    event = claude_observation(MirrorErrorMessage("mirror_error", {"SECRET": "raw"}, error="SECRET token"))
    assert event.kind == "session_store_warning"
    assert event.payload == {"source": "claude_sdk", "reason": "mirror_error"}
    assert "SECRET" not in repr(event)
    assert claude_observation(object()) is None
