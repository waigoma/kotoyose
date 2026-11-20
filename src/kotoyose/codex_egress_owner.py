"""egress だけが未確認の停止で、egress proxy を持っていた Kotoyose process が居ないことを判断する。

egress proxy (``CodexEgress``) は attempt を実行した Kotoyose process の中の asyncio task として動く。
attempt を実行できるのは coordinator lock を持つ process だけなので、次のどれかを肯定できたときだけ
「生きている egress task はもう無い」と判断する。どれも肯定できなければ None を返す (fail closed)。

- previous_boot: provider を起動した boot と今の boot が違う。前の boot の process は全て終わっている。
- previous_coordinator: attempt は今の process が lock を取る前に作られた (前の coordinator が実行した)。
  前の coordinator は lock を失うと期限付きの cleanup の後に os._exit するので、取得から
  ``PREDECESSOR_EXIT_BOUND_SECONDS`` が過ぎていれば、もう居ない。
- settled_in_process: attempt は今の process が実行し、その egress の task が全て終わっている。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
import time

from .codex_stop_evidence import EgressBasis
from .coordinator_lease import PREDECESSOR_EXIT_BOUND_SECONDS
from .process_identity import ProcessIdentity


def egress_owner_absent(attempt: dict, ownership: dict, *, coordinator, process: ProcessIdentity,
                        settled_in_process: Callable[[str], bool],
                        monotonic: Callable[[], float] = time.monotonic) -> tuple[EgressBasis, dict] | None:
    """所有 process の不在を肯定できたときだけ (basis, 判断した process の identity) を返す。"""
    recovered_by = {"pid": process.pid, "start_ticks": process.start_ticks, "boot_id": process.boot_id}
    launched_boot = ownership.get("boot_id")
    if isinstance(launched_boot, str) and launched_boot and launched_boot != process.boot_id:
        return "previous_boot", recovered_by
    if (coordinator is None or getattr(coordinator, "state", None) not in ("starting", "held")
            or getattr(coordinator, "connection", None) is None):
        return None
    acquired_at = getattr(coordinator, "acquired_at", None)
    acquired_monotonic = getattr(coordinator, "acquired_monotonic", None)
    created_at = attempt.get("created_at")
    if (not isinstance(acquired_at, datetime) or not isinstance(created_at, datetime)
            or not isinstance(acquired_monotonic, (int, float))):
        return None
    if created_at < acquired_at:
        if monotonic() - acquired_monotonic >= PREDECESSOR_EXIT_BOUND_SECONDS:
            return "previous_coordinator", recovered_by
        return None
    if settled_in_process(attempt["id"]):
        return "settled_in_process", recovered_by
    return None
