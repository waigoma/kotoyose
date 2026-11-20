"""Provider limit observations and durable probe scheduling."""
from datetime import datetime, timedelta

from .agent_execution_digest import agent_utc, parse_agent_utc

KNOWN_RESET_PAD_MS = 5_000
UNKNOWN_BACKOFF_MS = (60_000, 120_000, 240_000, 480_000, 900_000)
TRY_NOW_DEDUP_MS = 30_000


def next_probe_at(limit: dict, step: int, now: datetime) -> datetime:
    reset = parse_agent_utc(limit['reset_at']) if limit['reset_at'] else None
    detected = parse_agent_utc(limit['detected_at'])
    if reset is not None and reset > detected:
        pad = timedelta(milliseconds=KNOWN_RESET_PAD_MS)
        return max(now + pad, reset + pad)
    return now + timedelta(milliseconds=UNKNOWN_BACKOFF_MS[min(step, len(UNKNOWN_BACKOFF_MS) - 1)])


def provider_limit_wire(attempt_row: dict) -> dict:
    reset = attempt_row['limits_reset_at']
    detected = attempt_row['limits_detected_at']
    if reset is not None and reset <= detected:
        reset = None
    return {'provider': attempt_row['limits_provider'], 'limit_window': attempt_row['limits_window'],
        'reset_at': agent_utc(reset) if reset else None,
        'detected_at': agent_utc(detected), 'resume': attempt_row['limits_resume'],
        'unavailable_reason': attempt_row['limits_unavailable_reason']}
