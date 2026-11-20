"""Best-effort Codex idle observation; never an admission or process-stop proof.

Codex 0.160.0 notifications are observations after dispatch
(measured on 0.160.0; not re-measured on 0.170.0). A new builtin can race an
idle observation and turn/interrupt. The immutable outer hard deadline
and owned process-tree cleanup are the enforcement boundary.
"""

from __future__ import annotations

import math


_WORK_ITEMS = frozenset({"commandExecution", "fileChange"})
_TERMINAL = frozenset({"completed", "failed", "declined"})
_PASSIVE_ITEMS = frozenset({
    "userMessage", "agentMessage", "reasoning", "contextCompaction",
})
_MAX_ITEMS = 512


class CodexSoftStop:
    """Request at most one cooperative interrupt after a known idle observation.

    The caller validates thread/turn identity before observe(), drains the
    notifications queued by turn/start, then calls mark_ready(). It owns the
    sole protocol reader, sends the interrupt, and handles its response. This
    object does not cancel tasks, stop processes, checkpoint, or extend time.
    """

    def __init__(self, deadline_monotonic: float | None) -> None:
        if deadline_monotonic is not None and (
            isinstance(deadline_monotonic, bool)
            or not isinstance(deadline_monotonic, (int, float))
            or not math.isfinite(deadline_monotonic)
        ):
            raise ValueError("invalid_soft_deadline")
        self.deadline_monotonic = deadline_monotonic
        self.interrupt_claimed = False
        self.observation_unknown = False
        self._ready = False
        self._active: dict[str, str] = {}
        self._completed: set[str] = set()

    @property
    def observed_idle(self) -> bool:
        return self._ready and not self.observation_unknown and not self._active

    def mark_ready(self) -> None:
        """Call only after queued early tool notifications have been observed."""
        self._ready = True

    def observe(self, method: str, item: dict) -> None:
        """Observe a scoped item lifecycle; ambiguity disables cooperative idle."""
        if method not in {"item/started", "item/completed"}:
            return
        kind = item.get("type")
        if kind in _PASSIVE_ITEMS:
            return
        if kind not in _WORK_ITEMS:
            self.observation_unknown = True
            return
        item_id, status = item.get("id"), item.get("status")
        if not isinstance(item_id, str) or not item_id:
            self.observation_unknown = True
            return
        if method == "item/started":
            if (status != "inProgress" or item_id in self._completed
                    or item_id in self._active and self._active[item_id] != kind
                    or len(self._active) + len(self._completed) >= _MAX_ITEMS):
                self.observation_unknown = True
                return
            self._active[item_id] = kind
        elif item_id in self._completed:
            # Replayed completion is harmless, but cannot restore unknown state.
            return
        elif status not in _TERMINAL or self._active.get(item_id) != kind:
            self.observation_unknown = True
        else:
            del self._active[item_id]
            self._completed.add(item_id)

    def interrupt_due(self, now: float) -> bool:
        return (self.deadline_monotonic is not None
                and now >= self.deadline_monotonic
                and not self.interrupt_claimed and self.observed_idle)

    def claim_interrupt(self, now: float) -> bool:
        """Reserve the single request. An uncertain send must not be retried."""
        if not self.interrupt_due(now):
            return False
        self.interrupt_claimed = True
        return True

    def wake_timeout(self, now: float) -> float | None:
        """Timer for the sole-reader loop; None means await the next event.

        After the cutoff, active or unknown work must wait for a notification
        or the independent hard deadline, not spin on a zero-second timer.
        """
        if (self.deadline_monotonic is None or self.interrupt_claimed
                or self.observation_unknown or not self._ready):
            return None
        remaining = self.deadline_monotonic - now
        if remaining > 0:
            return remaining
        return 0.0 if self.observed_idle else None
