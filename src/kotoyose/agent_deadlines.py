"""native coding の実開始時刻を、固定された有限期限と停止猶予へ収める。"""

import math
from datetime import datetime, timedelta
from typing import Mapping

from .execution_resources import ExecutionError


class AgentStartRejected(ExecutionError):
    """Provider process の起動前に確定した拒否。"""


def validate_agent_start(snapshot: Mapping[str, object], now: datetime) -> None:
    """新 mode の snapshot が hard と grace を収めなければ起動前に拒否する。

    now はタイムゾーン付き現在時刻。旧 mode は検査せず、余地不足は
    AgentStartRejected (grant_expired_before_start) を送出する。
    """
    if snapshot.get("limits_mode") != "agent_work_v1":
        return
    timeout = snapshot["timeout"]
    grant = snapshot["grant"]
    budget = snapshot["budget"]
    hard = timeout["hard_seconds"]
    end = now + timedelta(seconds=hard + timeout["grace_seconds"])
    if (hard > grant["max_seconds"] or datetime.fromisoformat(grant["expires_at"]) < end
            or datetime.fromisoformat(budget["deadline"]) < end):
        raise AgentStartRejected("grant_expired_before_start", "Grant or budget cannot contain the full agent limit", 409)


def coding_deadlines(started: float, remaining: float, timeout: dict, hard_seconds: float) -> tuple[float, float]:
    """queue 待ちも差し引いて soft/hard 比率を保持する。猶予しか残らなければ起動前に拒否する。"""
    hard = min(hard_seconds, remaining - timeout["grace_seconds"])
    soft = hard * timeout["soft_seconds"] / timeout["hard_seconds"]
    if not all(math.isfinite(value) for value in (started, remaining, hard, soft)) or not 0 < soft < hard:
        raise ExecutionError("permission_denied", "Execution deadline has no stop grace remaining", 403)
    return started + hard, started + soft
