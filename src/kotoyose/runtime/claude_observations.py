"""Claude SDK telemetry を内容・制御から独立した安全な activity に変換する。"""

from __future__ import annotations

import math
import re

from claude_agent_sdk import (
    MirrorErrorMessage,
    RateLimitEvent,
    TaskNotificationMessage,
    TaskProgressMessage,
    TaskStartedMessage,
    TaskUpdatedMessage,
)

from .base import RuntimeEvent


TASK_MESSAGES = (
    TaskStartedMessage, TaskProgressMessage, TaskNotificationMessage, TaskUpdatedMessage,
)
OBSERVATION_MESSAGES = (RateLimitEvent, MirrorErrorMessage, *TASK_MESSAGES)
_RATE_STATUSES = frozenset({"allowed", "allowed_warning", "rejected"})
_RATE_TYPES = frozenset({"five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet", "overage"})
_TASK_STATUSES = frozenset({"pending", "running", "paused", "completed", "failed", "stopped", "killed"})
_TERMINAL_TASK_STATUSES = frozenset({"completed", "failed", "stopped", "killed"})
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")


def claude_observation(message: object, *, session_id: str | None = None) -> RuntimeEvent | None:
    """SDK observation を allowlisted metadata だけの event に正規化する。

    Args:
        message: SDK が解析した message。未知の型は無視する。
        session_id: SDK init/assistant で確認済みの session ID。異なる ID は無視する。

    Returns:
        Advisory activity。未知の分類・不正 task ID・別 session は None。

    Response、usage、resume identity、provider 操作は変更しない。SDK の raw data、
    description、output path、mirror error text は公開しない。
    """
    reported_session = getattr(message, "session_id", None)
    if session_id and reported_session and reported_session != session_id:
        return None
    if isinstance(message, RateLimitEvent):
        return _rate_limit(message)
    if isinstance(message, MirrorErrorMessage):
        return RuntimeEvent(
            kind="session_store_warning",
            summary="Claude transcript mirror failed; external resume history may be incomplete",
            payload={"source": "claude_sdk", "reason": "mirror_error"},
        )
    if isinstance(message, TASK_MESSAGES):
        return _task_lifecycle(message)
    return None


def _nonnegative_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _rate_limit(message: RateLimitEvent) -> RuntimeEvent | None:
    info = message.rate_limit_info
    if not isinstance(info.status, str) or info.status not in _RATE_STATUSES:
        return None
    payload: dict = {"source": "claude_sdk", "status": info.status}
    if isinstance(info.rate_limit_type, str) and info.rate_limit_type in _RATE_TYPES:
        payload["rate_limit_type"] = info.rate_limit_type
    for name in ("resets_at", "overage_resets_at"):
        value = getattr(info, name)
        if _nonnegative_integer(value):
            payload[name] = value
    if (
        isinstance(info.utilization, (int, float))
        and not isinstance(info.utilization, bool)
        and 0 <= info.utilization <= 1
        and math.isfinite(info.utilization)
    ):
        payload["utilization"] = info.utilization
    if isinstance(info.overage_status, str) and info.overage_status in _RATE_STATUSES:
        payload["overage_status"] = info.overage_status
    window = f" ({info.rate_limit_type})" if "rate_limit_type" in payload else ""
    return RuntimeEvent(
        kind="rate_limit", summary=f"Claude rate limit: {info.status}{window}", payload=payload,
    )


def _task_lifecycle(message: object) -> RuntimeEvent | None:
    task_id = getattr(message, "task_id", None)
    if not isinstance(task_id, str) or not _IDENTIFIER.fullmatch(task_id):
        return None
    if isinstance(message, TaskStartedMessage):
        kind, status = "task_started", "running"
    elif isinstance(message, TaskProgressMessage):
        kind, status = "task_progress", "running"
    elif isinstance(message, TaskNotificationMessage):
        kind, status = "task_notification", message.status
    else:
        assert isinstance(message, TaskUpdatedMessage)
        kind = "task_updated"
        status = message.status or (message.patch.get("status") if isinstance(message.patch, dict) else None)
    if not isinstance(status, str) or status not in _TASK_STATUSES:
        return None
    payload: dict = {
        "source": "claude_sdk", "task_id": task_id,
        "status": "stopped" if status == "killed" else status,
        "terminal": status in _TERMINAL_TASK_STATUSES,
    }
    tool_use_id = getattr(message, "tool_use_id", None)
    if isinstance(tool_use_id, str) and _IDENTIFIER.fullmatch(tool_use_id):
        payload["tool_use_id"] = tool_use_id
    usage = getattr(message, "usage", None)
    if isinstance(usage, dict):
        safe_usage = {
            name: usage[name] for name in ("total_tokens", "tool_uses", "duration_ms")
            if name in usage and _nonnegative_integer(usage[name])
        }
        if safe_usage:
            payload["task_usage"] = safe_usage
    return RuntimeEvent(
        kind=kind, summary=f"Claude task {task_id}: {payload['status']}", payload=payload,
    )
