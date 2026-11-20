"""Model-free tests of cooperative timing, not actual-CLI stop conformance."""

import math

import pytest

from kotoyose.codex_soft_stop import CodexSoftStop


def item(item_id="command-1", kind="commandExecution", status="inProgress"):
    return {"id": item_id, "type": kind, "status": status}


def ready(deadline=10.0):
    state = CodexSoftStop(deadline)
    state.mark_ready()
    return state


@pytest.mark.parametrize("deadline", [True, False, "10", math.inf, -math.inf, math.nan])
def test_rejects_invalid_deadline(deadline):
    with pytest.raises(ValueError, match="invalid_soft_deadline"):
        CodexSoftStop(deadline)


def test_disabled_has_no_timer_or_interrupt():
    state = ready(None)
    assert state.wake_timeout(100) is None
    assert not state.claim_interrupt(100)


def test_idle_waits_for_cutoff_then_claims_once():
    state = ready()
    assert state.wake_timeout(9) == 1
    assert not state.claim_interrupt(9)
    assert state.wake_timeout(10) == 0
    assert state.claim_interrupt(10)
    assert not state.claim_interrupt(11)
    assert state.wake_timeout(11) is None


def test_early_events_must_be_drained_before_ready():
    state = CodexSoftStop(10)
    assert not state.observed_idle
    assert not state.claim_interrupt(20)
    assert state.wake_timeout(20) is None
    state.observe("item/started", item())
    state.mark_ready()
    assert not state.claim_interrupt(20)
    state.observe("item/completed", item(status="completed"))
    assert state.claim_interrupt(20)


@pytest.mark.parametrize("kind", ["commandExecution", "fileChange"])
@pytest.mark.parametrize("terminal", ["completed", "failed", "declined"])
def test_inflight_work_is_not_interrupted_at_soft_cutoff(kind, terminal):
    state = ready()
    state.observe("item/started", item(kind=kind))
    assert not state.claim_interrupt(10)
    assert state.wake_timeout(10) is None
    state.observe("item/completed", item(kind=kind, status=terminal))
    assert state.claim_interrupt(11)


def test_all_parallel_work_must_complete():
    state = ready()
    state.observe("item/started", item("one"))
    state.observe("item/started", item("two", "fileChange"))
    state.observe("item/completed", item("one", status="completed"))
    assert not state.claim_interrupt(20)
    state.observe("item/completed", item("two", "fileChange", "completed"))
    assert state.claim_interrupt(20)


def test_hung_command_waits_for_external_hard_deadline():
    state = ready()
    state.observe("item/started", item())
    assert not state.claim_interrupt(1000)
    assert state.wake_timeout(1000) is None
    # No change to deadline, no invented completion or process-stop proof.
    assert state.deadline_monotonic == 10
    assert not state.observed_idle


@pytest.mark.parametrize("method, bad_item", [
    ("item/started", item(status="completed")),
    ("item/started", item(item_id="")),
    ("item/started", item(kind="mcpToolCall")),
    ("item/completed", item(status="completed")),
    ("item/completed", item(status="inProgress")),
])
def test_ambiguous_lifecycle_never_becomes_known_idle(method, bad_item):
    state = ready()
    state.observe(method, bad_item)
    assert state.observation_unknown
    assert not state.claim_interrupt(20)
    assert state.wake_timeout(20) is None
    state.mark_ready()
    assert not state.claim_interrupt(20)


def test_duplicate_events_do_not_double_count():
    state = ready()
    state.observe("item/started", item())
    state.observe("item/started", item())
    state.observe("item/completed", item(status="completed"))
    state.observe("item/completed", item(status="completed"))
    assert state.claim_interrupt(20)


def test_reused_completed_identity_is_unknown():
    state = ready()
    state.observe("item/started", item())
    state.observe("item/completed", item(status="completed"))
    state.observe("item/started", item())
    assert state.observation_unknown
    assert not state.claim_interrupt(20)


def test_mismatched_completion_is_unknown():
    state = ready()
    state.observe("item/started", item())
    state.observe("item/completed", item(kind="fileChange", status="completed"))
    assert state.observation_unknown
    assert not state.claim_interrupt(20)


def test_passive_agent_output_does_not_hide_idle():
    state = ready()
    state.observe("item/started", {"type": "agentMessage", "id": "a"})
    assert state.claim_interrupt(20)


def test_observation_storage_is_bounded():
    state = ready()
    for index in range(513):
        state.observe("item/started", item(str(index)))
    assert state.observation_unknown
    assert len(state._active) == 512
    assert not state.claim_interrupt(20)


def test_soft_observation_is_not_tool_admission_control():
    state = ready()
    assert state.claim_interrupt(10)
    # A builtin can dispatch before its started notification arrives. This is
    # explicitly an accepted race, not a failed security/admission assertion.
    state.observe("item/started", item("racing-command"))
    assert not state.observed_idle
    assert state.interrupt_claimed
    assert not state.claim_interrupt(11)


def test_further_tools_after_soft_cutoff_are_observed_not_denied():
    state = ready()
    state.observe("item/started", item("original"))
    assert not state.claim_interrupt(10)
    state.observe("item/started", item("later"))
    state.observe("item/completed", item("original", status="completed"))
    assert not state.claim_interrupt(11)
    state.observe("item/completed", item("later", status="completed"))
    assert state.claim_interrupt(12)
