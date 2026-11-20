#!/usr/bin/env python3
"""Opt-in real PostgreSQL admission/receipt replay without worker startup.

Uses the same explicit empty-DB acknowledgement as pm_selection_kotoyose.py. Unlike
that pre-Attempt helper, this creates one disposable Attempt and its prepared
local_execution ledger entry, but never schedules a worker/provider. It is a
receipt/replay test of an owner-configured subscription-only route, not an
independent verification of upstream billing.
"""

import asyncio
from datetime import datetime, timedelta, timezone
import json
import tempfile

from pm_selection_kotoyose import (PRINCIPAL, POLICY_REF, ForbiddenWorker, arguments, deny_async,
                              install_boundaries, isolated_store, require)


async def run_replay(args):
    import kotoyose.config as registry
    from kotoyose.execution_contract import ExecutionEnvelope
    from kotoyose.execution_resources import (AgentDefinition, CallerPolicy, ExecutionBudget, ExecutionError,
        ExecutionGrant, InputText, NativeExecutionConfig, TimeoutPolicy, WorkspaceRegistration, content_digest)
    from kotoyose.execution_service import ExecutionService
    from kotoyose.execution_snapshot import resolved_attempt_snapshot
    registry._config = registry.HarnessConfig(
        routes={"taskhub-pm": {"driver": "claude.agent-sdk", "model": "cc/claude-sonnet-5-5",
                               "endpoint": "http://127.0.0.1:19999/v1",
                               "credential": "env:NEVER_READ_FIXTURE_TOKEN", "subscription_only": False}},
        models={"taskhub-pm": {"route": "taskhub-pm", "agent": {"place": "isolated", "surface": "none"}}})
    policy = CallerPolicy(workspace_roots=[args.workspace_root], agent_models=["taskhub-pm"],
        provenance=["trusted"], max_seconds=60, max_memory_bytes=128 * 1024 * 1024, max_pids=32, max_cpus=1, max_concurrent=1)
    registry._config = registry._config.model_copy(update={"clients": {PRINCIPAL: registry.Client(
        workspace_roots=[args.workspace_root], models=["taskhub-pm"],
        allowed_provenance=["trusted"], max_run_seconds=60, max_memory_bytes=128 * 1024 * 1024,
        max_processes=32, max_cpus=1, max_concurrent_runs=1,
    )}})
    settings = NativeExecutionConfig(schema_version="1", image_id="sha256:" + "a" * 64,
                                     drivers={"claude.agent-sdk": {"cli_binary": "/usr/bin/true"}})
    with tempfile.TemporaryDirectory(prefix="pm-replay-", dir=args.workspace_root) as leaf:
        async with isolated_store(args, allow_disposable_admission=True) as store:
            service = ExecutionService(settings, store, ForbiddenWorker())
            service.start = deny_async
            scheduled = []
            service._schedule = lambda attempt: scheduled.append(attempt["id"])
            # Service startup would acquire leases/probe workers; this fixture
            # deliberately enables only dispatch's admission path instead.
            service._accepting = True
            workspace = await service.register_workspace_response(PRINCIPAL, policy, WorkspaceRegistration(
                schema_version="1", path=leaf, caller_workspace_ref="paired-replay-workspace"))
            async def register(resource):
                return await service.register_resource(PRINCIPAL, policy, resource)
            timeout = await register(TimeoutPolicy(schema_version="1", kind="timeout_policy",
                soft_seconds=1, hard_seconds=20, grace_seconds=1))
            now = datetime.now(timezone.utc)
            budget = await register(ExecutionBudget(schema_version="1", kind="budget", reservation_ref="fixture-replay",
                mode="observed", deadline=now + timedelta(minutes=5), memory_bytes=64 * 1024 * 1024,
                pids_limit=16, cpus=1, output_limit_bytes=8192))
            definition = await register(AgentDefinition(schema_version="1", kind="agent_definition", instructions="Never execute.",
                max_turns=2, output_contract="text", timeout_policy_ref=timeout))
            content = await register(InputText(schema_version="1", kind="text", text="Synthetic receipt test only.", provenance="trusted"))
            grant = await register(ExecutionGrant(schema_version="1", kind="grant", grant_id="fixture-replay-grant", version=1,
                caller_run_ref="fixture-replay-run", workspace_ref=workspace["workspace_ref"], isolation="sandbox", network="none",
                resource_refs=[timeout, budget, definition, content], agent_models=["taskhub-pm"], policy_ref=POLICY_REF,
                not_before=now - timedelta(seconds=1), expires_at=now + timedelta(minutes=5), max_seconds=30, delegation="none"))
            body = {"execution": {"schema_version": "1", "kind": "agent", "model": "taskhub-pm", "required_provider": "claude",
                "required_billing_mode": "subscription",
                "idempotency_key": "fixture-replay-key", "caller_run_ref": "fixture-replay-run", "workspace_ref": workspace["workspace_ref"],
                "execution_grant_ref": grant, "budgets_ref": budget, "agent_definition_ref": definition,
                "inputs": [{"kind": "instructions", "provenance": "trusted", "content_ref": content}]}}
            request = ExecutionEnvelope.model_validate(body).execution
            strict = request.model_copy(update={"idempotency_key": "unproved-subscription", "required_billing_mode": "subscription"})
            try:
                await service.dispatch(PRINCIPAL, policy, strict)
            except ExecutionError as exc:
                require(exc.reason_code == "billing_policy_unavailable", "Unexpected billing rejection")
            else:
                raise AssertionError("Unestablished subscription route was admitted")
            require(await store.pool.fetchval("SELECT count(*) FROM kotoyose.execution_attempts") == 0, "Rejected request created an Attempt")
            registry._config.routes["taskhub-pm"].subscription_only = True
            identifier = await service.dispatch(PRINCIPAL, policy, request)
            admitted = await store.attempt(PRINCIPAL, identifier)
            original = content_digest(admitted["snapshot"])
            receipt = resolved_attempt_snapshot(admitted).model_dump(mode="json")
            require(receipt["runtime_billing"]["mode"] == "subscription", "Missing owner-policy receipt")
            require(receipt["runtime_billing"]["metered_fallback"] == "forbidden", "Fallback was relaxed")
            require("NEVER_READ_FIXTURE_TOKEN" not in str(receipt), "Credential source leaked into receipt")
            require(admitted["snapshot"]["model"] == "cc/claude-sonnet-5-5", "Admission lost the selected model")
            require(admitted["request"]["required_provider"] == "claude", "Request constraint was not saved")
            require(admitted["snapshot"]["base_url"] == "http://127.0.0.1:19999/v1", "Admission lost the selected route")
            require(admitted["request"]["required_billing_mode"] == "subscription", "Billing requirement was not saved")
            registry._config.routes["taskhub-pm"].subscription_only = False
            try:
                await service.dispatch(PRINCIPAL, policy, request.model_copy(update={"idempotency_key": "new-after-policy-removal"}))
            except ExecutionError as exc:
                require(exc.reason_code == "billing_policy_unavailable", "New Attempt did not recheck owner policy")
            else:
                raise AssertionError("New Attempt reused removed route trust")
            registry._config.models.clear()
            registry._config.routes.clear()
            service.admission.resolve = deny_async
            service._accepting = False
            replayed = await service.dispatch(PRINCIPAL, policy, request)
            require(replayed == identifier, "Replay produced another Attempt")
            require(scheduled == [identifier], "Replay scheduled new work")
            repeated = await store.attempt(PRINCIPAL, identifier)
            require(content_digest(repeated["snapshot"]) == original, "Replay changed the immutable snapshot")
            require(resolved_attempt_snapshot(repeated).model_dump(mode="json") == receipt, "Replay changed the saved receipt")
            conflicting = request.model_copy(update={"required_provider": "codex"})
            try:
                await service.dispatch(PRINCIPAL, policy, conflicting)
            except ExecutionError as exc:
                require(exc.code == "request_conflict" and exc.status == 409, "Wrong idempotency conflict")
            else:
                raise AssertionError("Changed request reused an idempotency key")
            require(await store.pool.fetchval("SELECT count(*) FROM kotoyose.execution_attempts") == 1, "Unexpected Attempt count")
            operations = await store.pool.fetch("SELECT kind,state FROM kotoyose.execution_operations")
            require([(r["kind"], r["state"]) for r in operations] == [("local_execution", "prepared")], "Worker/provider operation started")
            print(json.dumps({"passed": True, "real_store_admission": True, "receipt_replayed": True,
                "snapshot_unchanged": True, "conflicting_request_rejected": True, "rejected_subscription_attempts": 0,
                "disposable_attempts": 1, "prepared_local_operations": 1, "provider_operations": 0,
                "worker_invocations": 0, "owner_configured_subscription_policy_admitted": True,
                "upstream_billing_independently_verified": False}), flush=True)


if __name__ == "__main__":
    args = arguments()
    install_boundaries(args)
    asyncio.run(run_replay(args))
