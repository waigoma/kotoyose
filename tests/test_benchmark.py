"""Phase 3 benchmark の replay / API contract 試験。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import asyncpg
import httpx
import pytest
from pydantic import ValidationError

from kotoyose.benchmark import (
    ArtifactPayload,
    BenchmarkManifest,
    BenchmarkRunner,
    CacheConditions,
    DatasetCase,
    FailureClassification,
    HttpNativeExecutionClient,
    ReplayPins,
    SideEffectPolicy,
    _load_manifest,
    main,
)
from kotoyose.app import app
from kotoyose.execution_resources import NativeExecutionConfig
from kotoyose.execution_contract import ExecutionEnvelope
from kotoyose.execution_snapshot import ResolvedAttemptSnapshot
from kotoyose.execution_service import ExecutionService
from kotoyose.execution_store import ExecutionStore
from kotoyose.local_worker import DockerLocalWorker
from kotoyose.execution_resources import content_digest


def test_documented_example_matches_normalized_request_and_cache_contract() -> None:
    """wire default の追加時も、同梱 template / example の identity を古いままにしない。"""
    root = Path(__file__).resolve().parents[1] / "docs/standards"
    for relative in ("templates/benchmark-manifest.json", "examples/research-benchmark.json"):
        assert len(_load_manifest(root / relative).cases) == 1


def test_phase3_evaluation_manifest_examples_load() -> None:
    root = Path(__file__).resolve().parents[1] / "docs/standards/examples"
    expected_case_counts = {
        "code-search-evaluation.json": 3,
        "skill-generation-evaluation.json": 2,
    }
    for relative, expected_case_count in expected_case_counts.items():
        manifest = _load_manifest(root / relative)
        assert len(manifest.cases) == expected_case_count
        assert all(case.pins.replay_constraints for case in manifest.cases)


def _request(key: str, *, isolation: str = "sandbox") -> ExecutionEnvelope:
    return ExecutionEnvelope.model_validate({
        "execution": {
            "schema_version": "1", "kind": "command", "idempotency_key": key,
            "caller_run_ref": "run:bench", "workspace_ref": "workspace:bench:0",
            "execution_grant_ref": "grant:bench", "budgets_ref": "budget:bench",
            "requirements": {"isolation": isolation, "capabilities": ["command_execution"]},
            "preferences": {}, "command_spec_ref": "command:bench", "argv_ref": "argv:bench",
            "cwd_ref": "cwd:bench", "env_refs": [], "fixture_refs": [],
            "timeout_policy_ref": "timeout:bench",
        },
    })


async def _execution(client: httpx.AsyncClient, workspace: Path, argv: list[str], isolation: str) -> dict:
    """専用 test DB 上で command resource / Grant / workspace を登録する。"""
    async def resource(kind: str, **payload: object) -> str:
        response = await client.post("/v1/resources", json={"resource": {"schema_version": "1", "kind": kind, **payload}})
        assert response.status_code == 201, response.text
        return response.json()["resource_ref"]

    registered = await client.post("/v1/workspaces", json={
        "schema_version": "1", "path": str(workspace), "caller_workspace_ref": "benchmark-clone",
    })
    assert registered.status_code == 201, registered.text
    workspace_ref = registered.json()["workspace_ref"]
    command_ref = await resource("command_spec", executable=argv[0])
    argv_ref = await resource("argv", values=argv[1:])
    cwd_ref = await resource("cwd", path=".")
    timeout_ref = await resource("timeout_policy", soft_seconds=1, hard_seconds=20, grace_seconds=1)
    now = datetime.now(timezone.utc)
    budget_ref = await resource(
        "budget", reservation_ref="benchmark-reservation", mode="observed",
        deadline=(now + timedelta(minutes=2)).isoformat(), memory_bytes=64 * 1024 * 1024,
        pids_limit=16, cpus=1, output_limit_bytes=8192,
    )
    grant_ref = await resource(
        "grant", grant_id="benchmark-grant", version=1, caller_run_ref="benchmark-run",
        workspace_ref=workspace_ref, isolation=isolation, network="none", command_spec_refs=[command_ref],
        resource_refs=[argv_ref, cwd_ref, timeout_ref, budget_ref], policy_ref="benchmark-policy",
        not_before=(now - timedelta(seconds=1)).isoformat(), expires_at=(now + timedelta(minutes=2)).isoformat(),
        max_seconds=30, delegation="none",
    )
    return {"execution": {
        "schema_version": "1", "kind": "command", "idempotency_key": "benchmark-http-" + uuid4().hex,
        "caller_run_ref": "benchmark-run", "workspace_ref": workspace_ref,
        "execution_grant_ref": grant_ref, "budgets_ref": budget_ref,
        "command_spec_ref": command_ref, "argv_ref": argv_ref, "cwd_ref": cwd_ref,
        "env_refs": [], "fixture_refs": [], "timeout_policy_ref": timeout_ref,
        "requirements": {"isolation": isolation}, "preferences": {},
    }}


def _case(*requests: ExecutionEnvelope, expected: FailureClassification = FailureClassification.SUCCESS,
          image_digest: str | None = None) -> DatasetCase:
    task = {"name": "small-command", "definition": "write marker"}
    acceptance = {"exit_code": 0, "required_artifacts": ["stdout"]}
    digest = lambda value: content_digest(value)
    pins = {
            "repository_revision": "a" * 40,
            "untracked_inputs_digest": "b" * 64,
            "task_definition_digest": digest(task), "acceptance_digest": digest({
                "exit_code": 0, "required_artifacts": ["stdout"],
                "artifact_digests": {}, "required_json_fields": {},
            }),
            "provider": "fixture-provider", "model": "fixture-model-1",
            "prompt_digest": "a" * 64,
            "profile_digest": "c" * 64, "scope_digest": "d" * 64,
            "agent_definition_digest": "e" * 64, "preset_resolution_digest": "f" * 64,
            "cli_digest": "1" * 64, "runtime_version": "native-fixture/1",
            "image_digest": image_digest or ("sha256:" + "2" * 64), "lockfile_digest": "3" * 64,
            "skill_schema_digests": ["4" * 64], "mcp_schema_digests": [],
            "tool_schema_digests": ["5" * 64], "pack_generator": "fixture-pack/1",
            "memory_snapshot_digest": "6" * 64, "fixture_digest": "7" * 64,
            "external_response_fixture_digest": "8" * 64,
            "unreplayable_inputs": [],
    }
    request_payloads = [request.model_dump(mode="json") for request in requests]
    normalized_pins = ReplayPins.model_validate(pins).model_dump(mode="json")
    key_material = {
        "request_digests": [digest(payload) for payload in request_payloads],
        "pins_digest": digest(normalized_pins), "warmup": "cold", "reuse": "disabled",
    }
    return DatasetCase.model_validate({
        "case_id": "command.fixture",
        "task_definition": task,
        "acceptance": acceptance,
        "expected_failure_classification": expected,
            "requests": request_payloads,
            "pins": pins,
        "cache": {"key_digest": digest(key_material), "key_material": key_material,
                  "warmup": "cold", "reuse": "disabled"},
        "side_effects": {"mode": "isolated_mock", "response_fixture_ref": "fixture:response"},
    })


class FakeKotoyose:
    def __init__(self, snapshots: list[dict], events: list[dict], capabilities: dict | None = None):
        self.snapshots = list(snapshots)
        self.event_payload = events
        self.capability_payload = capabilities or {
            "capabilities": {
                "command_execution": {"status": "supported", "reason": "fixture"},
                "native": {"status": "supported", "reason": "fixture"},
            },
        }
        self.submitted: list[ExecutionEnvelope] = []

    async def capabilities(self):
        return self.capability_payload

    async def submit(self, request: ExecutionEnvelope) -> str:
        self.submitted.append(request)
        return f"attempt:{len(self.submitted)}"

    async def describe(self, attempt_id: str):
        if len(self.snapshots) > 1:
            snapshot = dict(self.snapshots.pop(0))
            snapshot["attempt_id"] = attempt_id
            return snapshot
        if self.snapshots:
            snapshot = dict(self.snapshots[0])
            snapshot["attempt_id"] = attempt_id
            return snapshot
        return {"attempt_id": attempt_id, "status": "succeeded", "finished_at": "2026-09-12T00:00:02Z"}

    async def events(self, attempt_id: str, after: int = 0):
        return {"events": [{**event, "attempt_id": attempt_id} for event in self.event_payload if event["sequence"] > after],
                "next_cursor": max((event["sequence"] for event in self.event_payload), default=after)}

    async def artifact(self, ref: str) -> ArtifactPayload:
        content = b"evidence"
        return ArtifactPayload(content=content, media_type="text/plain", digest=hashlib.sha256(content).hexdigest())


class WrongAttemptEvents(FakeKotoyose):
    async def events(self, attempt_id: str, after: int = 0):
        return {"events": [{"attempt_id": "attempt:other", "sequence": 1, "type": "dispatch"}], "next_cursor": 1}


class BadArtifact(FakeKotoyose):
    async def artifact(self, ref: str) -> ArtifactPayload:
        content = b"tampered"
        return ArtifactPayload(content=content, media_type="text/plain", digest=hashlib.sha256(content).hexdigest())


class SnapshotFake(FakeKotoyose):
    def __init__(self, resolved: ResolvedAttemptSnapshot, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._resolved = resolved

    async def resolved_snapshot(self, attempt_id: str) -> ResolvedAttemptSnapshot:
        return self._resolved


class RetainingFake(FakeKotoyose):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.retained = []

    async def retain(self, reference):
        self.retained.append(reference)
        return {"schema_version": "1", "ref": reference.ref, "state": "active"}


class GapEvents(FakeKotoyose):
    async def events(self, attempt_id: str, after: int = 0):
        return {"events": [
            {"attempt_id": attempt_id, "sequence": 2, "type": "succeeded"},
            {"attempt_id": attempt_id, "sequence": 3, "type": "succeeded"},
        ], "next_cursor": 3}


class DuplicateEvents(FakeKotoyose):
    async def events(self, attempt_id: str, after: int = 0):
        entries = [
            {"attempt_id": attempt_id, "sequence": 1, "type": "dispatch"},
            {"attempt_id": attempt_id, "sequence": 1, "type": "dispatch"},
            {"attempt_id": attempt_id, "sequence": 2, "type": "succeeded"},
        ]
        return {"events": [entry for entry in entries if entry["sequence"] > after], "next_cursor": 2}


def _terminal(status: str = "succeeded", *, reason: str | None = None) -> dict:
    return {
        "schema_version": "1", "attempt_id": "attempt:1", "status": status,
        "caller_run_ref": "run:bench", "workspace_ref": "workspace:bench:0", "workspace_generation": 0,
        "reason": reason, "stop_confirmation": "confirmed", "side_effect_resolution": "resolved",
        "exit_code": 0 if status == "succeeded" else 7, "sequence": 2,
        "usage": {"source": "fixture", "cost": 0.12, "input_tokens": 11, "output_tokens": 7},
        "artifacts": [{"ref": "artifact:stdout", "name": "stdout",
                       "digest": hashlib.sha256(b"evidence").hexdigest(),
                       "size": 8, "media_type": "text/plain"}],
        "artifact_manifest": [], "created_at": "2026-09-12T00:00:00Z",
        "finished_at": "2026-09-12T00:00:02Z",
    }


def test_manifest_pins_task_and_acceptance_and_rejects_unknown_fields() -> None:
    case = _case(_request("bench-1"))
    manifest = BenchmarkManifest.model_validate({
        "schema_version": "1", "suite_id": "fixture-suite", "dataset_revision": "b" * 40,
        "cases": [case.model_dump(mode="json")],
    })
    assert len(manifest.digest) == 64
    assert manifest.model_dump(mode="json")["cases"][0]["pins"]["image_digest"].startswith("sha256:")
    with pytest.raises(ValidationError, match="extra_forbidden"):
        BenchmarkManifest.model_validate({
            "schema_version": "1", "suite_id": "suite", "dataset_revision": "b" * 40,
            "cases": [case.model_dump(mode="json")], "model": "best",
        })
    altered = case.model_dump(mode="json")
    altered["task_definition"]["definition"] = "changed"
    with pytest.raises(ValidationError, match="task_definition_digest"):
        DatasetCase.model_validate(altered)


@pytest.mark.asyncio
async def test_new_pin_and_replay_constraint_are_reported_as_constrained() -> None:
    payload = _case(_request("bench-constrained")).model_dump(mode="json")
    payload["pins"]["runtime_policy_digest"] = "9" * 64
    payload["pins"]["replay_constraints"] = [{
        "pin": "memory_snapshot_digest", "reason": "source ACL cannot be retained",
    }]
    payload["cache"]["key_material"]["pins_digest"] = content_digest(
        ReplayPins.model_validate(payload["pins"]).model_dump(mode="json")
    )
    payload["cache"]["key_digest"] = content_digest(payload["cache"]["key_material"])
    case = DatasetCase.model_validate(payload)
    manifest = BenchmarkManifest(suite_id="suite", dataset_revision="b" * 40, cases=[case])
    report = await BenchmarkRunner(FakeKotoyose([_terminal()], [
        {"sequence": 1, "type": "dispatch"}, {"sequence": 2, "type": "succeeded"},
    ])).run(manifest)
    assert report.runs[0].pin_verification == "constrained"
    assert report.replay[0]["pin_verification"] == "constrained"
    assert report.replay[0]["replay_constraints"][0]["pin"] == "memory_snapshot_digest"
    declared = report.replay[0]["declared_pins"]
    assert {
        "repository_revision", "untracked_inputs_digest", "task_definition_digest",
        "acceptance_digest", "profile_digest", "scope_digest", "agent_definition_digest",
        "preset_resolution_digest", "runtime_policy_digest", "cli_digest", "runtime_version",
        "image_digest", "lockfile_digest", "skill_schema_digests", "mcp_schema_digests",
        "tool_schema_digests", "pack_generator", "memory_snapshot_digest", "fixture_digest",
        "external_response_fixture_digest",
    } <= declared.keys()
    assert report.llm_reproducibility == "not_guaranteed"
    assert report.dataset_summary["expected_failure_classification_distribution"]["success"] == 1
    assert any("only expected-success" in warning for warning in report.warnings)


def test_real_external_side_effect_is_rejected() -> None:
    with pytest.raises(ValidationError, match="isolated_mock or read_only"):
        SideEffectPolicy(mode="github_write")
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        CacheConditions(key_digest="a" * 64, warmup="cold", reuse="disabled", unexpected=True)


def test_model_pin_must_be_explicit() -> None:
    with pytest.raises(ValidationError, match="explicit model identifier"):
        ReplayPins(
            repository_revision="a" * 40, task_definition_digest="a" * 64,
            acceptance_digest="b" * 64, provider="provider", model="best", prompt_digest="c" * 64,
            profile_digest="d" * 64, scope_digest="e" * 64, agent_definition_digest="f" * 64,
            preset_resolution_digest="1" * 64, cli_digest="2" * 64, runtime_version="runtime",
            image_digest="sha256:" + "3" * 64, lockfile_digest="4" * 64,
            pack_generator="pack", fixture_digest="5" * 64,
        )


@pytest.mark.asyncio
async def test_runner_preserves_exact_request_and_reports_usage_events_artifacts() -> None:
    case = _case(_request("bench-1"), _request("bench-2"))
    manifest = BenchmarkManifest(suite_id="suite", dataset_revision="b" * 40, cases=[case])
    events = [
        {"attempt_id": "attempt:1", "sequence": 1, "type": "dispatch", "created_at": "2026-09-12T00:00:00Z"},
        {"attempt_id": "attempt:1", "sequence": 2, "type": "running", "created_at": "2026-09-12T00:00:00.500000Z"},
        {"attempt_id": "attempt:1", "sequence": 3, "type": "succeeded", "created_at": "2026-09-12T00:00:02Z"},
    ]
    fake = FakeKotoyose([dict(_terminal(), sequence=3), dict(_terminal(), sequence=3)], events)
    report = await BenchmarkRunner(fake, wait_timeout=1, poll_seconds=.001).run(manifest)
    assert [request.execution.idempotency_key for request in fake.submitted] == ["bench-1", "bench-2"]
    assert len(report.runs) == 2
    assert report.runs[0].classification == FailureClassification.SUCCESS
    assert report.runs[0].usage["cost"] == .12
    assert report.runs[0].artifacts[0].digest == hashlib.sha256(b"evidence").hexdigest()
    assert report.runs[0].active_seconds == pytest.approx(1.5)
    assert report.runs[0].wait_seconds == pytest.approx(.5)
    assert report.summary["total_runs"] == 2
    assert report.summary["runtime_successes"] == 2
    assert report.summary["acceptance_successes"] == 2
    assert report.summary["certified_runs"] == 0


def test_cache_key_is_derived_from_exact_requests_and_pins() -> None:
    case = _case(_request("bench-cache"))
    payload = case.model_dump(mode="json")
    payload["cache"]["key_material"]["warmup"] = "warm"
    with pytest.raises(ValidationError, match="cache key_material"):
        DatasetCase.model_validate(payload)


@pytest.mark.asyncio
async def test_unsupported_native_is_recorded_without_submit_or_fallback() -> None:
    case = _case(_request("bench-native", isolation="native"), expected=FailureClassification.UNSUPPORTED_CAPABILITY)
    manifest = BenchmarkManifest(suite_id="suite", dataset_revision="b" * 40, cases=[case])
    fake = FakeKotoyose([], [], {"capabilities": {
        "command_execution": {"status": "supported", "reason": "fixture"},
        "native": {"status": "unsupported", "reason": "worker absent"},
    }})
    report = await BenchmarkRunner(fake).run(manifest)
    run = report.runs[0]
    assert run.classification == FailureClassification.UNSUPPORTED_CAPABILITY
    assert run.unsupported is not None
    assert "certified_sandbox_command" in run.unsupported.required_capabilities
    assert fake.submitted == []


@pytest.mark.asyncio
async def test_external_call_without_response_fixture_is_unsupported_before_submit() -> None:
    request = _request("bench-external")
    request_payload = request.model_dump(mode="json")
    request_payload["execution"]["requirements"]["capabilities"].append("external_mcp")
    case_payload = _case(ExecutionEnvelope.model_validate(request_payload)).model_dump(mode="json")
    case_payload["side_effects"] = {
        "mode": "read_only", "response_fixture_ref": None,
        "external_calls": None, "mutation_replay": "forbidden",
    }
    case = DatasetCase.model_validate(case_payload)
    manifest = BenchmarkManifest(suite_id="suite", dataset_revision="b" * 40, cases=[case])
    fake = FakeKotoyose([], [])
    report = await BenchmarkRunner(fake).run(manifest)
    assert report.runs[0].status == "unsupported"
    assert report.runs[0].unsupported.required_capabilities == ("external_response_fixture",)
    assert fake.submitted == []


@pytest.mark.asyncio
async def test_three_repetitions_report_variation_cache_mismatch_and_kotoyose_metrics() -> None:
    payload = _case(
        _request("bench-repeat-1"), _request("bench-repeat-2"), _request("bench-repeat-3"),
    ).model_dump(mode="json")
    conditions = [
        {"warmup": "cold", "reuse": "disabled", "cache_snapshot_digest": None},
        {"warmup": "warm", "reuse": "prewarmed", "cache_snapshot_digest": "a" * 64},
        {"warmup": "cold", "reuse": "disabled", "cache_snapshot_digest": None},
    ]
    payload["cache"]["repetition_conditions"] = conditions
    payload["cache"]["key_material"]["repetition_conditions"] = conditions
    payload["cache"]["key_digest"] = content_digest(payload["cache"]["key_material"])
    case = DatasetCase.model_validate(payload)
    snapshots = []
    for cost in (.1, .2, .3):
        snapshot = _terminal()
        snapshot["sequence"] = 6
        snapshot["usage"] = {
            "cost": cost, "input_tokens": 10, "output_tokens": 5,
            "cache_read_tokens": 2, "cache_write_tokens": 1,
        }
        snapshots.append(snapshot)
    events = [
        {"sequence": 1, "type": "dispatch", "created_at": "2026-09-12T00:00:00Z"},
        {"sequence": 2, "type": "client_tool_result",
         "payload": {"operation_id": "op-1", "state": "unknown"}},
        {"sequence": 3, "type": "client_tool_result",
         "payload": {"operation_id": "op-1", "state": "succeeded"}},
        {"sequence": 4, "type": "infra_retry"},
        {"sequence": 5, "type": "running", "created_at": "2026-09-12T00:00:01Z"},
        {"sequence": 6, "type": "succeeded", "created_at": "2026-09-12T00:00:03Z"},
    ]
    manifest = BenchmarkManifest(suite_id="suite", dataset_revision="b" * 40, cases=[case])
    report = await BenchmarkRunner(FakeKotoyose(snapshots, events)).run(manifest)
    variation = report.case_variation[0]
    assert variation["planned_repetitions"] == variation["observed_repetitions"] == 3
    assert variation["success_rate"] == 1
    assert variation["cost"]["mean"] == pytest.approx(.2)
    assert variation["cost"]["population_stddev"] > 0
    assert variation["cache_conditions_consistent"] is False
    assert report.kotoyose_metrics["runtime_success_rate"] == 1
    assert report.kotoyose_metrics["active_seconds"]["total"] == 6
    assert report.kotoyose_metrics["wait_seconds"]["total"] == 3
    assert report.kotoyose_metrics["cost"]["total"] == pytest.approx(.6)
    assert report.kotoyose_metrics["input_tokens"]["total"] == 30
    assert report.kotoyose_metrics["output_tokens"]["total"] == 15
    assert report.kotoyose_metrics["cache_read_tokens"]["total"] == 6
    assert report.kotoyose_metrics["cache_write_tokens"]["total"] == 3
    assert report.kotoyose_metrics["infra_retry_count"] == 3
    assert report.kotoyose_metrics["infra_retry_observation"] == "durable_attempt_events"
    assert report.kotoyose_metrics["outcome_unknown_operation_resolution_rate"] == 1
    assert report.kotoyose_metrics["outcome_unknown_operation_observation"] == "durable_client_tool_events"
    assert report.kotoyose_metrics["cache_conditions_consistent"] is False
    assert all(value is None for value in report.caller_metrics.values())
    assert any("inconsistent cache/warmup" in warning for warning in report.warnings)


@pytest.mark.asyncio
async def test_timeout_and_event_failure_do_not_guess_terminal_success() -> None:
    case = _case(_request("bench-timeout"), expected=FailureClassification.OUTCOME_UNKNOWN)
    manifest = BenchmarkManifest(suite_id="suite", dataset_revision="b" * 40, cases=[case])
    never = FakeKotoyose([{"attempt_id": "attempt:1", "caller_run_ref": "run:bench",
                      "workspace_ref": "workspace:bench:0", "workspace_generation": 0,
                      "status": "running", "finished_at": None}], [])
    report = await BenchmarkRunner(never, wait_timeout=.01, poll_seconds=.001).run(manifest)
    assert report.runs[0].classification == FailureClassification.OUTCOME_UNKNOWN
    assert report.runs[0].attempt_id == "attempt:1"

    class EventFailure(FakeKotoyose):
        async def events(self, attempt_id: str, after: int = 0):
            raise RuntimeError("event transport unavailable")

    failed = EventFailure([_terminal()], [])
    result = await BenchmarkRunner(failed, wait_timeout=1).run(manifest)
    assert result.runs[0].classification == FailureClassification.OUTCOME_UNKNOWN
    assert result.runs[0].classification_match is False


@pytest.mark.asyncio
async def test_worker_lost_and_contract_error_keep_distinct_failure_classes() -> None:
    worker_lost_case = _case(_request("bench-lost"), expected=FailureClassification.WORKER_LOST)
    worker_lost_manifest = BenchmarkManifest(suite_id="suite", dataset_revision="b" * 40,
                                             cases=[worker_lost_case])
    worker_lost_events = [
        {"attempt_id": "attempt:1", "sequence": 1, "type": "dispatch"},
        {"attempt_id": "attempt:1", "sequence": 2, "type": "worker_lost"},
    ]
    report = await BenchmarkRunner(FakeKotoyose([_terminal("worker_lost")], worker_lost_events), wait_timeout=1).run(worker_lost_manifest)
    assert report.runs[0].classification == FailureClassification.WORKER_LOST
    assert report.dataset_summary["expected_failure_classification_distribution"]["environment_failure"] == 1

    contract_case = _case(_request("bench-contract"), expected=FailureClassification.CONTRACT_ERROR)
    contract_manifest = BenchmarkManifest(suite_id="suite", dataset_revision="b" * 40, cases=[contract_case])
    contract_events = [
        {"attempt_id": "attempt:1", "sequence": 1, "type": "failed"},
        {"attempt_id": "attempt:1", "sequence": 2, "type": "failed"},
    ]
    report = await BenchmarkRunner(FakeKotoyose([_terminal("failed", reason="output_contract_error")], contract_events), wait_timeout=1).run(contract_manifest)
    assert report.runs[0].classification == FailureClassification.CONTRACT_ERROR


@pytest.mark.asyncio
async def test_unconfirmed_success_is_unknown_and_blocks_next_repetition() -> None:
    first = _terminal()
    first["stop_confirmation"] = "unconfirmed"
    first["side_effect_resolution"] = "unresolved"
    case = _case(_request("bench-1"), _request("bench-2"), expected=FailureClassification.OUTCOME_UNKNOWN)
    manifest = BenchmarkManifest(suite_id="suite", dataset_revision="b" * 40, cases=[case])
    events = [
        {"attempt_id": "attempt:1", "sequence": 1, "type": "dispatch"},
        {"attempt_id": "attempt:1", "sequence": 2, "type": "succeeded"},
    ]
    fake = FakeKotoyose([first], events)
    report = await BenchmarkRunner(fake, wait_timeout=1).run(manifest)
    assert len(fake.submitted) == 1
    assert len(report.runs) == 1
    assert report.runs[0].classification == FailureClassification.OUTCOME_UNKNOWN
    assert report.runs[0].runtime_success is True
    assert report.runs[0].acceptance_success is True


@pytest.mark.asyncio
async def test_event_from_another_attempt_is_outcome_unknown() -> None:
    case = _case(_request("bench-event"), expected=FailureClassification.OUTCOME_UNKNOWN)
    manifest = BenchmarkManifest(suite_id="suite", dataset_revision="b" * 40, cases=[case])
    result = await BenchmarkRunner(
        WrongAttemptEvents([_terminal()], []), wait_timeout=1,
    ).run(manifest)
    assert result.runs[0].classification == FailureClassification.OUTCOME_UNKNOWN
    assert result.runs[0].attempt_id == "attempt:1"


@pytest.mark.asyncio
async def test_event_sequence_gap_or_cursor_flyover_is_outcome_unknown() -> None:
    case = _case(_request("bench-gap"), expected=FailureClassification.OUTCOME_UNKNOWN)
    manifest = BenchmarkManifest(suite_id="suite", dataset_revision="b" * 40, cases=[case])
    terminal = dict(_terminal(), sequence=3)
    result = await BenchmarkRunner(GapEvents([terminal], [])).run(manifest)
    assert result.runs[0].classification == FailureClassification.OUTCOME_UNKNOWN


@pytest.mark.asyncio
async def test_identical_event_replay_is_deduplicated() -> None:
    case = _case(_request("bench-duplicate"))
    manifest = BenchmarkManifest(suite_id="suite", dataset_revision="b" * 40, cases=[case])
    result = await BenchmarkRunner(DuplicateEvents([_terminal()], [])).run(manifest)
    assert result.runs[0].classification == FailureClassification.SUCCESS
    assert [event["sequence"] for event in result.runs[0].events] == [1, 2]


@pytest.mark.asyncio
async def test_metadata_only_or_tampered_artifact_cannot_satisfy_acceptance() -> None:
    case = _case(_request("bench-artifact"), expected=FailureClassification.VERIFICATION_FAILED)
    manifest = BenchmarkManifest(suite_id="suite", dataset_revision="b" * 40, cases=[case])
    report = await BenchmarkRunner(BadArtifact([_terminal()], [
        {"attempt_id": "attempt:1", "sequence": 1, "type": "dispatch"},
        {"attempt_id": "attempt:1", "sequence": 2, "type": "succeeded"},
    ])).run(manifest)
    assert report.runs[0].runtime_success is True
    assert report.runs[0].acceptance_success is False
    assert report.runs[0].classification == FailureClassification.VERIFICATION_FAILED


@pytest.mark.asyncio
async def test_resolved_snapshot_is_observed_separately_from_declared_pins() -> None:
    request = _request("bench-certified")
    case = _case(request)
    snapshot = _terminal()
    resolved = ResolvedAttemptSnapshot(
        attempt_id="attempt:1", attempt_sequence=2, request_digest=content_digest(request.execution.model_dump(mode="json")),
        execution_grant_ref=request.execution.execution_grant_ref, grant_digest="1" * 64,
        runtime_kind="command", isolation="sandbox", network="none", worker_id="local-docker",
        worker_capability_state="resolved", worker_os="linux", worker_isolation_modes=("sandbox",),
        external_capabilities=(),
        image_digest=case.pins.image_digest, image_state="resolved", provider=None,
        provider_state="not_applicable", model=None, model_state="not_applicable",
        cli_digest=None, cli_state="not_applicable", sdk_version=None, sdk_state="not_applicable",
        resource_refs={"execution_grant_ref": request.execution.execution_grant_ref},
    )
    manifest = BenchmarkManifest(suite_id="suite", dataset_revision="b" * 40, cases=[case])
    report = await BenchmarkRunner(SnapshotFake(resolved, [snapshot], [
        {"attempt_id": "attempt:1", "sequence": 1, "type": "dispatch"},
        {"attempt_id": "attempt:1", "sequence": 2, "type": "succeeded"},
    ])).run(manifest)
    assert report.runs[0].certification == "certified"
    assert report.runs[0].pin_verification == "unverified"
    assert any("omits declared pins" in note for note in report.runs[0].measurement_notes)
    assert report.runs[0].observed_pins["model"] is None
    assert report.runs[0].observed_pins["model_state"] == "not_applicable"


@pytest.mark.asyncio
async def test_retention_references_apply_existing_apis_and_record_fixture_gap() -> None:
    payload = _case(_request("bench-retention")).model_dump(mode="json")
    payload["retention_references"] = [
        {
            "kind": "artifact", "ref": "artifact:evidence",
            "request": {
                "schema_version": "1", "request_id": "retention:artifact:benchmark",
                "expected_version": 0, "expires_at": "2027-09-15T00:00:00Z",
                "pins": ["benchmark:command.fixture"], "proof_ref": "benchmark:adopted:1",
                "acknowledged_required_action_ref": None,
            },
        },
        {
            "kind": "workspace", "ref": "workspace:bench:0",
            "request": {
                "schema_version": "1", "request_id": "retention:workspace:benchmark",
                "expected_version": 0, "workspace_ref": "workspace:bench:0",
                "expires_at": "2027-09-15T00:00:00Z",
                "pins": ["benchmark:command.fixture"], "proof_ref": "benchmark:adopted:1",
                "recovery": {"mode": "discard", "decision_ref": "benchmark:replayable:1"},
                "acknowledged_required_actions": [],
            },
        },
        {
            "kind": "fixture", "ref": "fixture:public-report",
            "pin": "benchmark:command.fixture", "proof_ref": "benchmark:adopted:1",
        },
    ]
    case = DatasetCase.model_validate(payload)
    manifest = BenchmarkManifest(suite_id="suite", dataset_revision="b" * 40, cases=[case])
    fake = RetainingFake([_terminal()], [
        {"sequence": 1, "type": "dispatch"}, {"sequence": 2, "type": "succeeded"},
    ])
    report = await BenchmarkRunner(fake).run(manifest)
    assert [reference.kind for reference in fake.retained] == ["artifact", "workspace"]
    assert [record["status"] for record in report.retention_requests] == [
        "applied", "applied", "record_only",
    ]
    assert "fixture retention API" in report.retention_requests[2]["reason"]


def test_cli_validation_only_writes_no_credential(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    case = _case(_request("bench-cli"))
    manifest_path = tmp_path / "manifest.json"
    output_path = tmp_path / "report.json"
    manifest_path.write_text(json.dumps({
        "schema_version": "1", "suite_id": "cli-suite", "dataset_revision": "b" * 40,
        "cases": [case.model_dump(mode="json")],
    }), encoding="utf-8")
    monkeypatch.setenv("BENCHMARK_SECRET", "do-not-copy")
    assert main([str(manifest_path), "--output", str(output_path)]) == 0
    output = output_path.read_text(encoding="utf-8")
    assert "validation_only" in output
    assert "do-not-copy" not in output


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("KOTOYOSE_BENCHMARK_HTTP_TESTS") != "1"
    or not os.environ.get("KOTOYOSE_BENCHMARK_TEST_DATABASE_URL")
    or not os.environ.get("KOTOYOSE_DOCKER_TEST_IMAGE"),
    reason="requires explicit isolated benchmark Postgres, Docker image, and opt-in",
)
async def test_runner_real_http_app_registration_dispatch_and_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ASGI route、実 Grant / workspace、Docker command、artifact GET を通す。"""
    database_url = os.environ["KOTOYOSE_BENCHMARK_TEST_DATABASE_URL"]
    image_id = os.environ["KOTOYOSE_DOCKER_TEST_IMAGE"]
    database_name = urlparse(database_url).path.lstrip("/")
    assert database_name.startswith("ahs_benchmark_execution_test")
    pool = await asyncpg.create_pool(database_url, min_size=1, max_size=4, command_timeout=15)
    await pool.execute("DROP SCHEMA IF EXISTS kotoyose CASCADE; CREATE SCHEMA kotoyose")
    workspace = tmp_path / "benchmark-clone"
    workspace.mkdir()
    from kotoyose.config import HarnessConfig
    from tests.fixtures.api_keys import fixture_key_store

    clients = {"benchmark-caller": {
            "workspace_roots": [str(tmp_path)],
            "max_run_seconds": 30, "max_memory_bytes": 64 * 1024 * 1024,
            "max_processes": 16, "max_cpus": 1, "max_concurrent_runs": 2,
        }}
    clients["another-caller"] = {}
    monkeypatch.setattr("kotoyose.config._config", HarnessConfig(clients=clients))
    monkeypatch.setattr(app.state, "api_key_store", await fixture_key_store(**{'benchmark-caller': 'benchmark-http-only', 'another-caller': 'another-caller'}))
    config = NativeExecutionConfig.model_validate({
        "schema_version": "1", "image_id": image_id,
    })
    service = ExecutionService(config, ExecutionStore(pool), DockerLocalWorker(image_id))
    await service.start()
    previous = getattr(app.state, "execution_service", None)
    app.state.execution_service = service
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
            headers={"Authorization": "Bearer kys_benchmark-http-only"},
        ) as http:
            body = await _execution(http, workspace, ["/bin/sh", "-c", "printf evidence"], "sandbox")
            request = ExecutionEnvelope.model_validate(body)
            before = await pool.fetchval("SELECT count(*) FROM kotoyose.execution_attempts")
            invalid_payload = request.model_dump(mode="json")
            invalid_payload["execution"]["idempotency_key"] = "benchmark-http-invalid-" + uuid4().hex
            invalid_payload["execution"]["requirements"]["worker_requirements"] = {"os": "windows"}
            rejected = await http.post("/v1/executions", json=invalid_payload)
            assert rejected.status_code == 501, rejected.text
            after = await pool.fetchval("SELECT count(*) FROM kotoyose.execution_attempts")
            assert after == before
            case = _case(request, image_digest=image_id)
            manifest = BenchmarkManifest(suite_id="http-suite", dataset_revision="b" * 40, cases=[case])
            benchmark_client = HttpNativeExecutionClient("http://test", "kys_benchmark-http-only", transport=httpx.ASGITransport(app=app))
            try:
                report = await BenchmarkRunner(benchmark_client, wait_timeout=30, poll_seconds=.01).run(manifest)
            finally:
                await benchmark_client.aclose()
            run = report.runs[0]
            assert run.classification == FailureClassification.SUCCESS
            assert run.runtime_success is True
            assert run.acceptance_success is True
            assert run.certification == "certified"
            stdout = next(artifact for artifact in run.artifacts if artifact.name == "stdout")
            assert stdout.digest == hashlib.sha256(b"evidence").hexdigest()
            snapshot_response = await http.get(f"/v1/attempts/{run.attempt_id}/snapshot")
            assert snapshot_response.status_code == 200, snapshot_response.text
            snapshot_body = snapshot_response.json()
            assert snapshot_body["runtime_kind"] == "command"
            assert snapshot_body["network"] == "none"
            assert snapshot_body["worker_capability_state"] == "resolved"
            assert snapshot_body["worker_os"] == "linux"
            assert "sandbox" in snapshot_body["worker_isolation_modes"]
            assert snapshot_body["image_digest"] == image_id
            assert snapshot_body["provider"] is None
            assert snapshot_body["provider_state"] == "not_applicable"
            assert all(secret not in snapshot_response.text for secret in ("workspace_path", "cli_binary", "credential", "KOTOYOSE_BENCHMARK_TOKEN"))
            denied = await http.get(
                f"/v1/attempts/{run.attempt_id}/snapshot",
                headers={"Authorization": "Bearer kys_another-caller"},
            )
            assert denied.status_code == 401
    finally:
        await service.close()
        app.state.execution_service = previous
        rows = await pool.fetch("SELECT id,generation FROM kotoyose.execution_attempts")
        for row in rows:
            process = await asyncio.create_subprocess_exec(
                "docker", "rm", "-f", f"kotoyose-{row['id']}-{row['generation']}",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await process.wait()
        await pool.execute("DROP SCHEMA kotoyose CASCADE")
        await pool.close()
