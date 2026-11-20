"""Resume の停止証拠と互換性を照合する。fresh Handoff の選択は Caller に委ねる。"""

from .execution_contract import ResumeManifest, ResumeReason, ResumeRequest
from .execution_resources import ExecutionError, content_digest, pinned_codex_egress_limits
from .runtime.resume import ResumeSupport
from .codex_coding_policy import HOST_MODE, progress_only_policy_transition


TERMINAL = frozenset({"succeeded", "failed", "cancelled", "timeout", "worker_lost", "budget_exceeded", "approval_required"})


def unsafe(reason: ResumeReason) -> ExecutionError:
    """機械判定可能な拒否理由を既存 error envelope に付加する。"""
    return ExecutionError("resume_unsafe", "Resume safety condition failed: " + reason, 409,
                          reason_code=reason)


def resume_request_digest(attempt_id: str, request: ResumeRequest) -> str:
    """同じ key の別 predecessor への付け替えも request conflict にする。"""
    return content_digest({"predecessor_attempt_id": attempt_id, **request.model_dump(mode="json")})


def check_predecessor(previous: dict) -> None:
    """終端と実停止・副作用確定を独立に検査し、同じ旧 Attempt の分岐再開を拒否する。"""
    conditions: tuple[tuple[bool, ResumeReason], ...] = (
        (previous["finished_at"] is not None and previous["status"] in TERMINAL, "attempt_not_terminal"),
        (previous["stop_confirmation"] == "confirmed", "stop_unconfirmed"),
        (previous["side_effect_resolution"] == "resolved", "effects_unresolved"),
        (previous.get("resumed_by") is None, "already_resumed"),
    )
    for valid, reason in conditions:
        if not valid:
            raise unsafe(reason)


def operations_digest(operations: list[dict]) -> str:
    """Operation の確定内容を証拠とし、更新時刻だけの変化に依存しない。"""
    if any(item["state"] != "confirmed" for item in operations):
        raise unsafe("operation_unknown")
    return content_digest([
        {key: item[key] for key in ("id", "kind", "state", "result")}
        for item in sorted(operations, key=lambda item: item["id"])
    ])


def build_resume_manifest(principal: str, previous: dict, operations: list[dict],
                          request: ResumeRequest, snapshot: dict, support: ResumeSupport, *, run_bound: bool = False) -> ResumeManifest:
    """解決済み設定と停止証拠から manifest を作る。

    run_bound は呼び出し側が predecessor と Run の関係を検証した場合だけ指定する。
    このとき caller_run_ref 以外の安全検査は維持する。
    """
    old = previous["snapshot"]
    try:
        pinned_codex_egress_limits(old)
    except ValueError as exc:
        raise unsafe("snapshot_mismatch") from exc
    try:
        pinned_codex_egress_limits(snapshot)
    except ValueError as exc:
        raise unsafe("runtime_mismatch") from exc
    execution = request.execution
    if old.get("kind") != "agent" or execution.kind != "agent":
        raise unsafe("execution_kind_unsupported")
    sandbox_matches = (snapshot.get("codex_sandbox") == old.get("codex_sandbox")
                       and ("codex_sandbox" in snapshot) == ("codex_sandbox" in old))
    # Only the historical host mode hard-coded workspace-write. Preserve that
    # effective policy on a new resume without mutating the old snapshot, or
    # treating other missing/explicit policies and today's alias as equivalent.
    if ("codex_sandbox" not in old and old.get("codex_execution_mode") == "host_native_coding_v1"
            and snapshot.get("codex_execution_mode") == "host_native_coding_v1"
            and snapshot.get("codex_sandbox") == "workspace-write"):
        sandbox_matches = True
    tool_policy_matches = snapshot.get("codex_tool_policy_digest") == old.get("codex_tool_policy_digest")
    if (not tool_policy_matches and previous.get("id") is not None
            and snapshot.get("grant", {}).get("resume_predecessor_ref") == previous["id"]
            and snapshot.get("codex_execution_mode") == old.get("codex_execution_mode") == HOST_MODE):
        tool_policy_matches = progress_only_policy_transition(
            old.get("codex_tool_policy_digest"), snapshot.get("codex_tool_policy_digest"))
    conditions: tuple[tuple[bool, ResumeReason], ...] = (
        (content_digest(old) == request.predecessor_snapshot_digest, "snapshot_mismatch"),
        (run_bound or previous["request"]["caller_run_ref"] == execution.caller_run_ref
         or snapshot["grant"].get("resume_predecessor_ref") == previous["id"], "run_mismatch"),
        (snapshot["workspace_id"] == previous["workspace_id"]
         and snapshot["generation"] == previous["generation"] + 1, "workspace_mismatch"),
        (snapshot["provider"] == old["provider"], "provider_mismatch"),
        (sandbox_matches, "runtime_mismatch"),
        (tool_policy_matches, "runtime_mismatch"),
        (all(snapshot.get(key) == old.get(key) for key in (
            "runtime_profile", "alias", "backend", "model", "base_url", "cli_digest", "sdk_version",
            "credential_mode", "credential_env", "credential_name", "compression_policy",
            "isolation", "image_id", "worker_id", "worker_capabilities",
            "codex_rootfs", "codex_rootfs_digest", "codex_preferences",
            "codex_execution_mode",
            "execution_environment", "codex_sandbox_policy", "codex_protected_workspace",
            "preset_prompt_snapshot", "codex_egress_limits",
            "codex_auth_file", "codex_auth_identity",
            "provider_session_storage",
        )), "runtime_mismatch"),
    )
    for valid, reason in conditions:
        if not valid:
            raise unsafe(reason)
    session_ref = previous.get("provider_session_ref") if "native" in support.modes else None
    mode = "native" if session_ref else "durable_state"
    if mode not in support.modes or (getattr(request, "required_resume_mode", None) == "native" and mode != "native"):
        raise unsafe("adapter_unsupported")
    grant = snapshot["grant"]
    return ResumeManifest(
        predecessor_attempt_id=previous["id"], predecessor_generation=previous["generation"],
        predecessor_status=previous["status"], predecessor_sequence=previous["sequence"],
        stop_confirmation="confirmed", side_effect_resolution="resolved",
        operations_digest=operations_digest(operations),
        predecessor_snapshot_digest=request.predecessor_snapshot_digest,
        provider_session_ref=session_ref,
        provider_session_reason=None if session_ref else support.reason,
        provider=snapshot["provider"], runtime_profile=execution.runtime_profile, model=execution.model, mode=mode,
        workspace_ref=execution.workspace_ref, workspace_generation=snapshot["generation"],
        checkpoint_ref=request.checkpoint_ref, checkpoint_digest=request.checkpoint_digest,
        execution_grant_ref=execution.execution_grant_ref, grant_version=grant["version"],
        grant_digest=content_digest(grant), principal=principal, caller_run_ref=execution.caller_run_ref,
        resume_request_key=execution.idempotency_key,
        request_digest=resume_request_digest(previous["id"], request),
    )
