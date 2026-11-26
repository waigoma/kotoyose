"""Admission, binary-drift, route/connection config, and Hub runner tests for `cli.print`."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
import textwrap
from types import SimpleNamespace
from uuid import uuid4

import pytest

from kotoyose.agent_spec import HOST_CODING
from kotoyose.capabilities import CapabilitySupport, capability_manifest
from kotoyose.config import Client, HarnessConfig, get_client, get_config
from kotoyose.execution_admission import ExecutionAdmission, validate_print_request
from kotoyose.execution_agent import AgentExecutor
from kotoyose.execution_contract import AgentExecution
from kotoyose.execution_resources import (
    AgentContainerConfig,
    AgentDefinition,
    ClientToolSpec,
    DriverCell,
    DriverInstall,
    ExecutionBudget,
    ExecutionError,
    ExecutionGrant,
    InputText,
    NativeExecutionConfig,
    TimeoutPolicy,
    WorkerCapabilityConfig,
    default_worker_capabilities,
    resolve_agent_model,
)
from kotoyose.execution_snapshot import resolved_attempt_snapshot
from kotoyose.agent_execution_digest import agent_utc, agent_work_digest
from kotoyose.agent_execution_runner import AgentExecutionRunner
from kotoyose.print_cli_agent import _HOME_SEMAPHORES
from kotoyose.print_cli_profiles import AGY_PROFILE
from kotoyose.routes.agent_executions import agent_execution_error_response


def _write_fake_agy(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body), encoding="utf-8")
    path.chmod(0o755)
    return path


def _make_harness_config(*, login_home: Path, driver: str = "cli.print", profile: str | None = "agy",
                         surface: str = "write") -> HarnessConfig:
    return HarnessConfig(
        connections={
            "agy-home-1": {
                "provider": "antigravity",
                "kind": "subscription", "data_policy": "no_training",
                "config_dir": str(login_home),
                "profile": "agy",
            }
        },
        routes={
            "agy-print": {
                "driver": driver,
                "model": "gemini-4-hydrogen-high",
                "connection": "agy-home-1",
                **({"profile": profile} if profile is not None else {}),
            }
        },
        models={
            "agy-coder": {
                "route": "agy-print",
                "agent": {"place": "host", "surface": surface},
            }
        },
    )


def _make_native_config(root: Path, fake_agy: Path) -> NativeExecutionConfig:
    from kotoyose import config as config_module

    ws_root = root / "workspaces"
    ws_root.mkdir(parents=True, exist_ok=True)
    current = config_module.get_config()
    client = Client(

        workspace_roots=[ws_root],
        models=["agy-coder"],
        places=["host"],
        max_surface="write",
        allowed_env_vars=[],
        allowed_provenance=["trusted", "project"],
        max_run_seconds=30,
        max_memory_bytes=128 * 1024 * 1024,
        max_processes=32,
        max_cpus=2,
        max_concurrent_runs=4,
    )
    config_module._config = current.model_copy(update={"clients": {**current.clients, "caller-a": client}})
    return NativeExecutionConfig(
        schema_version="1",
        image_id="sha256:" + ("0" * 64),
        drivers={"cli.print": DriverInstall(profile_binaries={"agy": fake_agy})},
        worker_capabilities={
            **default_worker_capabilities(),
            "local-print-cli": WorkerCapabilityConfig(
                worker_id="local-print-cli",
                os="linux",
                isolation_modes=["native"],
                native=True,
            ),
        },
    )


class _FakeLimits:
    def __init__(self, attempts: dict[str, dict]) -> None:
        self._attempts = attempts

    async def start(self, principal: str, attempt_id: str, observed_at: datetime, active_ms: int) -> None:
        att = self._attempts[attempt_id]
        att["limits_state"] = "running"
        att["limits_started_at"] = att["limits_started_at"] or observed_at
        att["limits_observed_at"] = observed_at
        att["limits_active_ms"] = active_ms

    async def heartbeat(self, principal: str, attempt_id: str, observed_at: datetime, active_ms: int) -> None:
        att = self._attempts[attempt_id]
        att["limits_observed_at"] = observed_at
        att["limits_active_ms"] = active_ms

    async def finish(self, principal: str, attempt_id: str, observed_at: datetime, active_ms: int, confirmed: bool, launch_outcome: str) -> None:
        att = self._attempts[attempt_id]
        att["limits_state"] = "ended" if confirmed and launch_outcome == "started" else "not_started"
        att["limits_observed_at"] = observed_at
        att["limits_finished_at"] = observed_at
        att["limits_active_ms"] = active_ms

    async def save_time(self, principal: str, attempt_id: str, sample) -> None:
        att = self._attempts[attempt_id]
        att["limits_state"] = sample.state
        att["limits_started_at"] = sample.started_at
        att["limits_observed_at"] = sample.observed_at
        att["limits_finished_at"] = sample.finished_at
        att["limits_active_ms"] = sample.active_ms


class _MemoryStore:
    def __init__(self, workspace_path: Path) -> None:
        self.resources: dict[tuple[str, str], object] = {}
        self.workspaces: dict[tuple[str, str], dict] = {}
        self.attempts: dict[str, dict] = {}
        self.operations_by_attempt: dict[str, list[dict]] = {}
        self.events_by_attempt: dict[str, list[tuple[str, dict]]] = {}
        self.artifacts_by_ref: dict[str, dict] = {}
        self.limits = _FakeLimits(self.attempts)
        stat = workspace_path.stat()
        self.workspaces[("caller-a", "ws:1")] = {
            "id": "ws-uuid",
            "generation": 0,
            "workspace_ref": "ws:1",
            "path": str(workspace_path),
            "device": stat.st_dev,
            "inode": stat.st_ino,
            "caller_ref": "caller-a",
        }

    def put(self, ref: str, obj: object, principal: str = "caller-a") -> str:
        self.resources[(principal, ref)] = obj
        return ref

    async def workspace(self, principal: str, ref: str) -> dict:
        return self.workspaces[(principal, ref)]

    async def resource(self, principal: str, ref: str):
        val = self.resources.get((principal, ref))
        if val is None:
            raise ExecutionError("not_found", f"missing resource {ref}", 404)
        return val

    async def operations(self, attempt_id: str) -> list[dict]:
        return list(self.operations_by_attempt.get(attempt_id, []))

    async def prepare_operation(self, attempt_id: str, kind: str) -> str:
        op_id = f"op:{uuid4().hex}"
        self.operations_by_attempt.setdefault(attempt_id, []).append(
            {"id": op_id, "kind": kind, "state": "prepared", "result": None}
        )
        return op_id

    async def record_process(self, op_id: str, result: dict) -> None:
        for ops in self.operations_by_attempt.values():
            for op in ops:
                if op["id"] == op_id:
                    op["result"] = result

    async def resolve_operation(self, op_id: str, state: str) -> None:
        for ops in self.operations_by_attempt.values():
            for op in ops:
                if op["id"] == op_id:
                    op["state"] = state

    async def attempt(self, principal: str, attempt_id: str) -> dict:
        return deepcopy(self.attempts[attempt_id])

    async def authorize_provider_request(self, attempt_id: str, principal: str) -> None:
        return None

    async def append_event(self, attempt_id: str, kind: str, payload: dict) -> None:
        self.events_by_attempt.setdefault(attempt_id, []).append((kind, payload))

    async def artifact(self, principal: str, ref: str, *, attempt_id: str | None = None) -> dict:
        return self.artifacts_by_ref[ref]


def _setup_admission_Fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    tool_refs: tuple[str, ...] = (),
    mcp_refs: tuple[str, ...] = (),
    capabilities: tuple[str, ...] = (),
    extra_inputs: list[dict] | None = None,
    prompt_text: str = "Analyze workspace cleanly.",
    output_contract: str = "text",
    alias_surface: str = "write",
) -> tuple[AgentExecution, ExecutionAdmission, _MemoryStore, NativeExecutionConfig, HarnessConfig, Path, Path]:
    login_home = tmp_path / "agy-login-home"
    login_home.mkdir(parents=True, exist_ok=True)
    fake_agy = _write_fake_agy(
        tmp_path / "bin" / "agy",
        """
        import json
        print(json.dumps({"event": "init", "conversation_id": "conv-1", "init": {"model": "gemini-4-hydrogen-high"}}))
        print(json.dumps({"event": "result", "result": {"conversation_id": "conv-1", "status": "SUCCESS", "response": "ok", "num_turns": 1, "usage": {"input_tokens": 10, "output_tokens": 5}}}))
        """,
    )
    hcfg = _make_harness_config(login_home=login_home, surface=alias_surface)
    monkeypatch.setattr("kotoyose.config._config", hcfg)
    ncfg = _make_native_config(tmp_path, fake_agy)
    hcfg = get_config()
    monkeypatch.setattr("kotoyose.config.get_config", lambda: hcfg)
    ws_path = tmp_path / "workspaces" / "proj"
    ws_path.mkdir(parents=True, exist_ok=True)
    store = _MemoryStore(ws_path)

    now = datetime.now(timezone.utc)
    store.put("res:timeout", TimeoutPolicy(schema_version="1", kind="timeout_policy", soft_seconds=2, hard_seconds=10, grace_seconds=1))
    store.put("res:budget", ExecutionBudget(
        schema_version="1", kind="budget", reservation_ref="r1", mode="observed",
        deadline=now + timedelta(minutes=2), memory_bytes=128 * 1024 * 1024, pids_limit=32, cpus=2, output_limit_bytes=32768,
    ))
    store.put("res:def", AgentDefinition(
        schema_version="1", kind="agent_definition", instructions="Follow rules.",
        output_contract=output_contract, max_turns=2, timeout_policy_ref="res:timeout",
    ))
    store.put("res:in", InputText(schema_version="1", kind="text", text=prompt_text, provenance="trusted"))
    extra_refs: list[str] = []
    if "res:client-tool" in tool_refs:
        store.put("res:client-tool", ClientToolSpec(
            schema_version="1", kind="client_tool", name="ping", description="Ping",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            effect="read", timeout_seconds=5,
        ))
    grant_tools = list(tool_refs)
    store.put("res:grant", ExecutionGrant(
        schema_version="1", kind="grant", grant_id="g1", version=1,
        caller_run_ref="run-1", workspace_ref="ws:1", isolation="native", network="none",
        command_spec_refs=[], agent_models=["agy-coder"],
        resource_refs=["res:def", "res:in", "res:budget", "res:timeout", *grant_tools, *extra_refs],
        policy_ref="pol-1", not_before=now - timedelta(seconds=1), expires_at=now + timedelta(minutes=2),
        max_seconds=30, delegation="none",
    ))
    inputs = [{"kind": "task_request", "provenance": "trusted", "content_ref": "res:in"}, *(extra_inputs or [])]
    req = AgentExecution.model_validate({
        "schema_version": "1",
        "kind": "agent",
        "idempotency_key": f"k-{uuid4().hex}",
        "caller_run_ref": "run-1",
        "workspace_ref": "ws:1",
        "execution_grant_ref": "res:grant",
        "budgets_ref": "res:budget",
        "agent_definition_ref": "res:def",
        "model": "agy-coder",
        "inputs": inputs,
        "tool_refs": list(tool_refs),
        "mcp_refs": list(mcp_refs),
        "client_key": "ck" if "res:client-tool" in tool_refs else None,
        "requirements": {"isolation": "native", "capabilities": list(capabilities)},
    })
    manifest = capability_manifest()
    manifest.capabilities["native_execution"] = CapabilitySupport(status="supported", reason="enabled")
    admission = ExecutionAdmission(
        store=store,
        manifest=manifest,
        image_id=ncfg.image_id,
        worker_capabilities=ncfg.worker_capabilities,
        settings=ncfg,
    )
    return req, admission, store, ncfg, hcfg, fake_agy, login_home


def test_config_route_and_connection_validation(tmp_path: Path) -> None:
    login_home = tmp_path / "login-home"
    login_home.mkdir()
    cfg = _make_harness_config(login_home=login_home)
    assert cfg.routes["agy-print"].profile == "agy"
    assert cfg.connections["agy-home-1"].config_dir == login_home

    # Route and subscription connection must each declare a profile.
    with pytest.raises(ValueError, match="profile is required"):
        _make_harness_config(login_home=login_home, profile=None)

    connection = cfg.connections["agy-home-1"].model_dump(exclude={"profile"})
    with pytest.raises(ValueError, match=r"set connections\.agy-home-1\.profile to 'agy'"):
        HarnessConfig(connections={"agy-home-1": connection}, routes=cfg.routes)

    with pytest.raises(ValueError, match=r"drivers\.cli\.print\.profile_binaries"):
        NativeExecutionConfig(
            schema_version="1",
            image_id="sha256:" + ("0" * 64),
            drivers={"cli.print": DriverInstall(cli_binary=tmp_path / "agy")},
        )

    # Unknown profile fails
    with pytest.raises(ValueError, match="unknown print CLI profile"):
        _make_harness_config(login_home=login_home, profile="nonexistent")

    # Missing config_dir on antigravity subscription fails for cli.print route
    with pytest.raises(ValueError, match="requires config_dir"):
        HarnessConfig(
            connections={"c": {"provider": "antigravity", "kind": "subscription", "data_policy": "no_training"}},
            routes={"r": {"driver": "cli.print", "model": "m", "connection": "c", "profile": "agy"}},
        )

    # Claude subscription semantics preserved
    with pytest.raises(ValueError, match="requires secret or config_dir"):
        HarnessConfig(connections={"c": {"provider": "claude", "kind": "subscription", "data_policy": "no_training"}})
    with pytest.raises(ValueError, match="cannot specify config_dir"):
        HarnessConfig(connections={"c": {"provider": "codex", "kind": "subscription", "data_policy": "no_training", "config_dir": str(login_home)}})


async def test_admission_accepts_no_tools_and_host_coding_and_pins_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    req_read, admission, _, ncfg, hcfg, fake_agy, login_home = _setup_admission_Fixture(tmp_path, monkeypatch, tool_refs=())
    resolved = resolve_agent_model("agy-coder", ncfg, config=hcfg)
    assert resolved.backend == "print_cli"
    assert resolved.login_home == login_home

    policy = get_client("caller-a")
    plan_read = await admission.resolve("caller-a", policy, req_read)
    assert plan_read["backend"] == "print_cli"
    assert plan_read["provider"] == "print_cli"
    assert plan_read["print_cli_profile"] == "agy"
    assert plan_read["profile_digest"] == AGY_PROFILE.profile_digest()
    assert plan_read["surface"] == "read"
    assert plan_read["connection_id"] == "agy-home-1"
    assert plan_read["login_home"] == str(login_home)
    assert plan_read["worker_id"] == "local-print-cli"
    expected_digest = hashlib.sha256(fake_agy.read_bytes()).hexdigest()
    assert plan_read["cli_digest"] == expected_digest

    # Public snapshot projects cleanly without leaking login_home path.
    att = {
        "id": "att-1",
        "sequence": 0,
        "request_digest": "a" * 64,
        "generation": 0,
        "request": req_read.model_dump(mode="json"),
        "snapshot": plan_read,
    }
    snap = resolved_attempt_snapshot(att)
    assert snap.backend == "print_cli"
    assert snap.cli_digest == expected_digest
    assert str(login_home) not in snap.model_dump_json()

    # host-coding@1 accepted -> write surface
    req_write, admission_w, _, ncfg_w, _, _, _ = _setup_admission_Fixture(
        tmp_path / "w", monkeypatch, tool_refs=(HOST_CODING,)
    )
    plan_write = await admission_w.resolve("caller-a", get_client("caller-a"), req_write)
    assert plan_write["surface"] == "write"


async def test_admission_rejects_client_tools_mcp_images_and_oversize_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Client tools -> 501
    req_ct, adm_ct, _, ncfg_ct, _, _, _ = _setup_admission_Fixture(
        tmp_path / "ct", monkeypatch, tool_refs=("res:client-tool",), capabilities=("client_tools",)
    )
    with pytest.raises(ExecutionError) as e_ct:
        await adm_ct.resolve("caller-a", get_client("caller-a"), req_ct)
    assert e_ct.value.status == 501

    # MCP -> 501
    req_mcp, adm_mcp, _, ncfg_mcp, _, _, _ = _setup_admission_Fixture(
        tmp_path / "mcp", monkeypatch, mcp_refs=("res:mcp",)
    )
    with pytest.raises(ExecutionError) as e_mcp:
        await adm_mcp.resolve("caller-a", get_client("caller-a"), req_mcp)
    assert e_mcp.value.status == 501

    # Images -> 501
    req_img, adm_img, _, ncfg_img, _, _, _ = _setup_admission_Fixture(
        tmp_path / "img",
        monkeypatch,
        extra_inputs=[{"kind": "image", "provenance": "project", "content_ref": "artifact:img1"}],
    )
    with pytest.raises(ExecutionError) as e_img:
        await adm_img.resolve("caller-a", get_client("caller-a"), req_img)
    assert e_img.value.status == 501

    # Oversize prompt -> 413 payload_too_large (and agent_execution_error_response returns 413)
    req_big, adm_big, _, ncfg_big, _, _, _ = _setup_admission_Fixture(
        tmp_path / "big", monkeypatch, prompt_text="A" * (125 * 1024)
    )
    with pytest.raises(ExecutionError) as e_big:
        await adm_big.resolve("caller-a", get_client("caller-a"), req_big)
    assert e_big.value.status == 413
    assert e_big.value.code == "payload_too_large"
    resp = agent_execution_error_response(e_big.value)
    assert resp.status_code == 413
    assert json.loads(resp.body)["error"]["code"] == "payload_too_large"


async def test_executor_detects_binary_change_with_409_before_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    req, admission, store, ncfg, _, fake_agy, _ = _setup_admission_Fixture(tmp_path, monkeypatch)
    plan = await admission.resolve("caller-a", get_client("caller-a"), req)
    attempt = {
        "id": "att-drift",
        "principal": "caller-a",
        "status": "running",
        "generation": 0,
        "request": req.model_dump(mode="json"),
        "snapshot": plan,
    }
    store.attempts["att-drift"] = attempt
    # Tamper with pinned CLI binary after admission
    fake_agy.write_text("#!/bin/sh\necho tampered\n", encoding="utf-8")
    executor = AgentExecutor(ncfg, store, object())
    with pytest.raises(ExecutionError) as exc_info:
        await executor.execute(attempt, asyncio.Event())
    assert exc_info.value.status == 409
    assert exc_info.value.code == "request_conflict"


def _setup_agent_execution_harness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    plan: dict,
    mem_store: _MemoryStore,
    ncfg: NativeExecutionConfig,
):
    eid = uuid4()
    nonce = "act:1"
    now = datetime.now(timezone.utc)
    plan.setdefault("budgets_ref", "res:budget")
    work = {
        "work_kind": "coding",
        "role": None,
        "automatic": False,
        "workspace_ref": "ws:1",
        "agent_definition_ref": "res:def",
        "artifact_spec_ref": None,
        "policy_ref": "pol-1",
        "scope_grant_ref": "res:grant",
        "resource_refs": ["res:budget"],
        "model": "agy-coder",
        "runtime_profile": None,
        "required_authentication_mode": None,
        "expected_authentication_binding_digest": None,
        "required_provider": None,
        "required_billing_mode": None,
        "inputs": [
            {
                "kind": "task_request",
                "provenance": "trusted",
                "content_ref": "res:in",
                "expected_revocation_epoch": None,
            }
        ],
        "tool_refs": [HOST_CODING],
        "skill_refs": [],
        "mcp_refs": [],
        "requirements": {
            "isolation": "native",
            "capabilities": [],
            "worker_requirements": {
                "os": None,
                "applications": [],
                "gpu": "none",
                "workspace_locality": "any",
                "native": "allowed",
            },
        },
        "result_contract": plan["agent_result_contract"],
        "approval_bindings": [],
        "continuation_mode": "fresh",
        "client_key": None,
    }
    money = {
        "mode": "fixed_units",
        "basis": "subscription_execution_units_v1",
        "allocation_units": "1.0",
        "run_units_ceiling": "5.0",
    }
    time_cfg = {"run_hard_ms": 10000, "grace_ms": 1000}
    resources = {"max_turns": 4, "max_output_bytes": 32768}
    recovery = {"provider_wait": "enabled", "fault_retry_limit": 1, "fault_retries_used": 0}
    work_digest = agent_work_digest(
        {
            "execution": {
                "work": work,
                "money": money,
                "predecessor": None,
                "time": time_cfg,
                "resources": resources,
                "recovery": recovery,
                "authority_ceilings": [],
            }
        }
    )
    auth_body = {
        "decision": "allow",
        "revision": 1,
        "activation_nonce": nonce,
        "work_digest": work_digest,
        "caller_run_ref": "run-1",
        "issued_at": agent_utc(now),
        "not_before": agent_utc(now - timedelta(seconds=1)),
        "expires_at": agent_utc(now + timedelta(minutes=2)),
        "allowed_active_ms": 10000,
        "stop_limit": "run_seconds",
        "charge": {"charge_ref": "charge:1", "reservation_ref": "resv:1", "allocation_units": "1.0"},
        "authority_ceiling_at": None,
        "approval": None,
        "settled_receipt_cursor": 0,
    }
    body = {
        "schema_version": "agent_execution_v1",
        "kind": "agent",
        "idempotency_key": f"exec-k-{uuid4().hex}",
        "caller_run_ref": "run-1",
        "work_digest": work_digest,
        "work": work,
        "money": money,
        "time": time_cfg,
        "resources": resources,
        "recovery": recovery,
        "predecessor": None,
        "authority_ceilings": [],
        "initial_authorization": auth_body,
    }

    exec_row = {
        "id": eid,
        "principal": "caller-a",
        "state": "queued",
        "current_nonce": nonce,
        "authorization_revision": 1,
        "workspace_id": "ws-uuid",
        "workspace_generation": 0,
        "workspace_ref": "workspace:ws-uuid:0",
        "work_digest": work_digest,
        "pinned_plan": plan,
        "request": {"execution": body},
        "cancel_requested": False,
        "admission_closed": False,
        "probe_step": 0,
        "sequence": 0,
        "pause": None,
        "provider_limit": None,
        "terminal": None,
    }
    act_row = {
        "execution_id": eid,
        "nonce": nonce,
        "ordinal": 1,
        "reason": "initial",
        "unit_ordinal": 1,
        "state": "awaiting",
        "launch": "not_started",
        "measurement": "none",
        "active_ms": 0,
        "turns": 0,
        "output_bytes": 0,
        "attempt_id": None,
        "approval": None,
        "interruption": None,
        "started_at": None,
        "finished_at": None,
        "provider_rejected_only": False,
    }
    receipts: list[dict] = []

    class _FakeConn:
        def transaction(self):
            class _Tx:
                async def __aenter__(self): return None
                async def __aexit__(self, *args): return False
            return _Tx()

        async def fetchrow(self, sql: str, *args):
            if "FROM kotoyose.agent_execution_activations" in sql:
                return act_row
            if "FROM kotoyose.agent_execution_authorizations" in sql:
                return {"body": auth_body}
            if "FROM kotoyose.agent_executions" in sql:
                return exec_row
            return None

        async def fetchval(self, sql: str, *args):
            if "sum(turns)" in sql:
                return 0
            if "sum(output_bytes)" in sql:
                return 0
            if "sum(active_ms)" in sql:
                return act_row["active_ms"]
            if "SELECT EXISTS" in sql:
                return False
            return None

        async def execute(self, sql: str, *args):
            if "UPDATE kotoyose.agent_execution_activations SET state='ended'" in sql:
                _, _, launch, meas, active_ms, turns, out_bytes, rej_only, started_at, finished_at = args
                act_row.update(
                    state="ended", launch=launch, measurement=meas, active_ms=active_ms,
                    turns=turns, output_bytes=out_bytes, provider_rejected_only=rej_only,
                    started_at=started_at, finished_at=finished_at,
                )
            elif "UPDATE kotoyose.agent_executions SET workspace_generation" in sql:
                exec_row["workspace_generation"] = args[1]
                exec_row["workspace_ref"] = args[2]

    class _FakePool:
        def acquire(self):
            class _Ctx:
                async def __aenter__(self): return _FakeConn()
                async def __aexit__(self, *args): return False
            return _Ctx()

        async def fetchrow(self, sql: str, *args):
            return await _FakeConn().fetchrow(sql, *args)

    async def lock_execution(conn, execution_id):
        return exec_row

    async def close_open_units(conn, execution_id, at):
        return None

    async def finish_terminal(conn, execution_id, terminal, at):
        receipt = {
            "cursor": 1,
            "receipt_id": f"rcpt:{uuid4().hex}",
            "digest": hashlib.sha256(json.dumps(terminal, sort_keys=True).encode()).hexdigest(),
            "terminal": terminal,
            "activation": deepcopy(act_row),
        }
        receipts.append(receipt)
        exec_row["state"] = "terminal"

    class _FakeLeases:
        def assert_startable(self, key):
            return None

        def lease_expires_at(self, key):
            return datetime.now(timezone.utc) + timedelta(minutes=2)

        def remaining_seconds(self, key, active_ms):
            return 10.0

        def forget(self, key):
            return None

    class _FakeExecutionService:
        def __init__(self) -> None:
            self.config = ncfg
            self.store = mem_store
            self.agent_execution_leases = _FakeLeases()
            self.executor = AgentExecutor(ncfg, mem_store, object())
            self.executor.agent_execution_leases = self.agent_execution_leases
            self._tasks: dict[str, asyncio.Task] = {}

        async def admit_for_agent_execution(self, principal, req_dump, snapshot, max_conc, agent_execution):
            att_id = "att-exec-1"
            att = {
                "id": att_id,
                "principal": principal,
                "status": "running",
                "generation": snapshot["generation"],
                "request": req_dump,
                "snapshot": snapshot,
                "limits_state": "not_started",
                "limits_started_at": None,
                "limits_observed_at": None,
                "limits_finished_at": None,
                "limits_active_ms": 0,
                "limits_turns": None,
                "limits_output_bytes": None,
                "limits_provider": None,
                "finished_at": None,
                "artifacts": [],
                "stop_confirmation": "confirmed",
                "side_effect_resolution": "resolved",
                "reason": "completed",
            }
            mem_store.attempts[att_id] = att
            act_row["attempt_id"] = att_id
            act_row["state"] = "running"
            return att, True

        def _schedule(self, attempt, *, after):
            cancel = asyncio.Event()

            async def _run():
                result = await self.executor.execute(attempt, cancel)
                att = mem_store.attempts[attempt["id"]]
                att["status"] = result.status
                att["reason"] = result.reason
                att["stop_confirmation"] = result.stop_confirmation
                att["limits_turns"] = result.turns
                att["limits_output_bytes"] = result.output_bytes
                att["finished_at"] = datetime.now(timezone.utc)
                if result.content:
                    mem_store.artifacts_by_ref["art:out"] = {"content": result.content}
                    att["artifacts"] = [{"name": "output", "ref": "art:out"}]
                await after(attempt)

            task = asyncio.create_task(_run())
            self._tasks[attempt["id"]] = task
            return cancel

    exec_svc = _FakeExecutionService()
    mem_store.admit = exec_svc.admit_for_agent_execution  # type: ignore[attr-defined]
    fake_agent_execution_store = SimpleNamespace(
        pool=_FakePool(),
        lock_execution=lock_execution,
        close_open_units=close_open_units,
        finish_terminal=finish_terminal,
    )
    hub = SimpleNamespace(
        store=fake_agent_execution_store,
        execution=SimpleNamespace(config=ncfg, store=mem_store, _schedule=exec_svc._schedule),
        leases=exec_svc.agent_execution_leases,
        clock=lambda: datetime.now(timezone.utc),
    )
    return eid, exec_row, act_row, receipts, exec_svc, AgentExecutionRunner(hub)


async def test_agent_execution_runner_produces_complete_receipt_for_print_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    req, admission, mem_store, ncfg, _, _, _ = _setup_admission_Fixture(
        tmp_path, monkeypatch, tool_refs=(HOST_CODING,), output_contract="json"
    )
    plan = await admission.resolve("caller-a", get_client("caller-a"), req)
    plan["limits_mode"] = "agent_work_v1"
    plan["agent_result_contract"] = {
        "format": "json",
        "json_schema": {
            "type": "object",
            "properties": {"status": {"type": "string"}},
            "required": ["status"],
        },
    }
    fake_agy_path = Path(plan["cli_binary"])
    _write_fake_agy(
        fake_agy_path,
        """
        import json
        print(json.dumps({"event": "init", "conversation_id": "conv-exec", "init": {"model": "gemini-4-hydrogen-high"}}))
        print(json.dumps({"event": "step_update", "step_update": {"step_index": 0, "state": "DONE", "step_type": "agent_response", "text_delta": "{\\"status\\": \\"ok\\"}", "usage": {"input_tokens": 14, "output_tokens": 6}}}))
        print(json.dumps({"event": "result", "result": {"conversation_id": "conv-exec", "status": "SUCCESS", "response": "{\\"status\\": \\"ok\\"}", "num_turns": 1, "usage": {"input_tokens": 14, "output_tokens": 6}}}))
        """,
    )
    plan["cli_digest"] = hashlib.sha256(fake_agy_path.read_bytes()).hexdigest()

    eid, exec_row, _, receipts, exec_svc, runner = _setup_agent_execution_harness(
        tmp_path, monkeypatch, plan=plan, mem_store=mem_store, ncfg=ncfg
    )
    started = await runner.start(eid)
    assert started is True
    await exec_svc._tasks["att-exec-1"]

    assert len(receipts) == 1
    rcpt = receipts[0]
    assert rcpt["terminal"] == {"kind": "succeeded"}
    assert rcpt["activation"]["state"] == "ended"
    assert rcpt["activation"]["launch"] == "started"
    assert rcpt["activation"]["measurement"] == "complete"
    assert rcpt["activation"]["turns"] == 1
    assert rcpt["activation"]["output_bytes"] == len(b'{"status":"ok"}')
    assert exec_row["state"] == "terminal"


async def test_agent_execution_runner_semaphore_leaves_activation_waiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    req, admission, mem_store, ncfg, _, _, login_home = _setup_admission_Fixture(
        tmp_path, monkeypatch, tool_refs=(HOST_CODING,), output_contract="json"
    )
    plan = await admission.resolve("caller-a", get_client("caller-a"), req)
    plan["limits_mode"] = "agent_work_v1"
    plan["agent_result_contract"] = {"format": "json", "json_schema": {"type": "object"}}

    eid, exec_row, act_row, receipts, _, runner = _setup_agent_execution_harness(
        tmp_path, monkeypatch, plan=plan, mem_store=mem_store, ncfg=ncfg
    )
    sem = _HOME_SEMAPHORES.get("agy", login_home, 2)
    held = 0
    try:
        while not sem.locked():
            await sem.acquire()
            held += 1
        started = await runner.start(eid)
        assert started is False
        assert act_row["state"] == "awaiting"
        assert act_row["attempt_id"] is None
        assert exec_row["state"] == "queued"
        assert receipts == []
    finally:
        for _ in range(held):
            sem.release()


async def test_admission_rejects_host_coding_when_alias_ceiling_is_below_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """host-coding は編集とコマンドの allow-rule (write) を選ぶ。alias の上限が read なら 403。"""
    req, adm, _, ncfg, _, _, _ = _setup_admission_Fixture(
        tmp_path / "ro", monkeypatch, tool_refs=(HOST_CODING,), alias_surface="read"
    )
    with pytest.raises(ExecutionError) as err:
        await adm.resolve("caller-a", get_client("caller-a"), req)
    assert err.value.status == 403
    assert err.value.code == "permission_denied"

    # 道具なしなら read の alias でも通り、surface は read のまま。
    req_ro, adm_ro, _, ncfg_ro, _, _, _ = _setup_admission_Fixture(
        tmp_path / "ro2", monkeypatch, tool_refs=(), alias_surface="read"
    )
    plan = await adm_ro.resolve("caller-a", get_client("caller-a"), req_ro)
    assert plan["surface"] == "read"


def _make_isolated_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    with_agent_container: bool = True,
    with_coding_cell: bool = True,
    driver: str = "cli.print",
):
    login_home = tmp_path / "agy-login-home"
    login_home.mkdir(parents=True, exist_ok=True)
    fake_agy = _write_fake_agy(
        tmp_path / "bin" / "agy",
        """
        import json
        print(json.dumps({"event": "init", "conversation_id": "conv-iso", "init": {"model": "gemini-4-hydrogen-high"}}))
        print(json.dumps({"event": "result", "result": {"conversation_id": "conv-iso", "status": "SUCCESS", "response": "ok", "num_turns": 1, "usage": {"input_tokens": 10, "output_tokens": 5}}}))
        """,
    )
    hcfg = HarnessConfig(
        connections={
            "agy-home-1": {
                "provider": "antigravity",
                "kind": "subscription", "data_policy": "no_training",
                "config_dir": str(login_home),
                "profile": "agy",
            }
        },
        routes={
            "agy-print": {
                "driver": driver,
                "model": "gemini-4-hydrogen-high",
                "connection": "agy-home-1",
                "profile": "agy",
            }
        },
        models={
            "agy-iso": {
                "route": "agy-print",
                "agent": {"place": "isolated", "surface": "write"},
                **({"tools": ["Edit"], "deny": ["Bash"]} if driver == "cli.acp" else {}),
            },
            "agy-host": {
                "route": "agy-print",
                "agent": {"place": "host", "surface": "write"},
            },
        },
    )
    ws_root = tmp_path / "workspaces"
    ws_root.mkdir(parents=True, exist_ok=True)
    ws_path = ws_root / "proj"
    ws_path.mkdir(parents=True, exist_ok=True)
    hcfg = hcfg.model_copy(update={
        "clients": {
            "caller-a": Client(

                workspace_roots=[ws_root],
                models=["agy-iso", "agy-host"],
                places=["host", "isolated"],
                max_surface="write",
                allowed_env_vars=[],
                allowed_provenance=["trusted", "project"],
                max_run_seconds=30,
                max_memory_bytes=128 * 1024 * 1024,
                max_processes=32,
                max_cpus=2,
                max_concurrent_runs=4,
            ),
        },
    })
    monkeypatch.setattr("kotoyose.config._config", hcfg)
    monkeypatch.setattr("kotoyose.config.get_config", lambda: hcfg)

    image_id = "sha256:" + ("e" * 64)
    ac_cfg = None
    if with_agent_container:
        agent_root = tmp_path / "agent-root"
        home_root = tmp_path / "home-root"
        agent_root.mkdir(parents=True, exist_ok=True)
        home_root.mkdir(parents=True, exist_ok=True)
        ac_cfg = AgentContainerConfig(
            agent_root=str(agent_root),
            home_root=str(home_root),
            workspace_memory_bytes=256 * 1024 * 1024,
            cpus=2.0,
            pids_limit=64,
            min_free_disk_bytes=0,
            max_containers=4,
            egress_socket_root="/tmp",
        )
    cells = {"coding": DriverCell(image_id=image_id)} if with_coding_cell else {}
    ncfg = NativeExecutionConfig(
        schema_version="1",
        image_id="sha256:" + ("0" * 64),
        drivers={"cli.print": DriverInstall(profile_binaries={"agy": fake_agy}, cells=cells)},
        agent_container=ac_cfg,
        worker_capabilities={
            **default_worker_capabilities(),
            "local-print-cli": WorkerCapabilityConfig(
                worker_id="local-print-cli",
                os="linux",
                isolation_modes=["native"],
                native=True,
            ),
        },
    )
    store = _MemoryStore(ws_path)
    now = datetime.now(timezone.utc)
    store.put("res:timeout", TimeoutPolicy(schema_version="1", kind="timeout_policy", soft_seconds=2, hard_seconds=10, grace_seconds=1))
    store.put("res:budget", ExecutionBudget(
        schema_version="1", kind="budget", reservation_ref="r1", mode="observed",
        deadline=now + timedelta(minutes=2), memory_bytes=128 * 1024 * 1024, pids_limit=32, cpus=2, output_limit_bytes=32768,
    ))
    store.put("res:def", AgentDefinition(
        schema_version="1", kind="agent_definition", instructions="Follow rules.",
        output_contract="text", max_turns=2, timeout_policy_ref="res:timeout",
    ))
    store.put("res:in", InputText(schema_version="1", kind="text", text="Inspect repo.", provenance="trusted"))
    store.put("res:grant-iso", ExecutionGrant(
        schema_version="1", kind="grant", grant_id="g-iso", version=1,
        caller_run_ref="run-1", workspace_ref="ws:1", isolation="sandbox", network="none",
        command_spec_refs=[], agent_models=["agy-iso"],
        resource_refs=["res:def", "res:in", "res:budget", "res:timeout", HOST_CODING],
        policy_ref="pol-1", not_before=now - timedelta(seconds=1), expires_at=now + timedelta(minutes=2),
        max_seconds=30, delegation="none",
    ))
    store.put("res:grant-host", ExecutionGrant(
        schema_version="1", kind="grant", grant_id="g-host", version=1,
        caller_run_ref="run-2", workspace_ref="ws:1", isolation="native", network="none",
        command_spec_refs=[], agent_models=["agy-host"],
        resource_refs=["res:def", "res:in", "res:budget", "res:timeout", HOST_CODING],
        policy_ref="pol-1", not_before=now - timedelta(seconds=1), expires_at=now + timedelta(minutes=2),
        max_seconds=30, delegation="none",
    ))
    manifest = capability_manifest()
    manifest.capabilities["native_execution"] = CapabilitySupport(status="supported", reason="enabled")
    admission = ExecutionAdmission(
        store=store,
        manifest=manifest,
        image_id=ncfg.image_id,
        worker_capabilities=ncfg.worker_capabilities,
        settings=ncfg,
    )
    req_iso = AgentExecution.model_validate({
        "schema_version": "1",
        "kind": "agent",
        "idempotency_key": f"k-{uuid4().hex}",
        "caller_run_ref": "run-1",
        "workspace_ref": "ws:1",
        "execution_grant_ref": "res:grant-iso",
        "budgets_ref": "res:budget",
        "agent_definition_ref": "res:def",
        "model": "agy-iso",
        "inputs": [{"kind": "task_request", "provenance": "trusted", "content_ref": "res:in"}],
        "tool_refs": [HOST_CODING],
        "requirements": {"isolation": "sandbox", "capabilities": []},
    })
    req_host = AgentExecution.model_validate({
        "schema_version": "1",
        "kind": "agent",
        "idempotency_key": f"k-{uuid4().hex}",
        "caller_run_ref": "run-2",
        "workspace_ref": "ws:1",
        "execution_grant_ref": "res:grant-host",
        "budgets_ref": "res:budget",
        "agent_definition_ref": "res:def",
        "model": "agy-host",
        "inputs": [{"kind": "task_request", "provenance": "trusted", "content_ref": "res:in"}],
        "tool_refs": [HOST_CODING],
        "requirements": {"isolation": "native", "capabilities": []},
    })
    return admission, store, ncfg, image_id, req_iso, req_host


async def test_isolated_admission_requires_agent_container_and_coding_image_and_keeps_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 1. With agent_container + coding image configured: isolated succeeds and host still works.
    adm_ok, _, ncfg_ok, image_id, req_iso, req_host = _make_isolated_setup(tmp_path / "ok", monkeypatch)
    plan_iso = await adm_ok.resolve("caller-a", get_client("caller-a"), req_iso)
    assert plan_iso["backend"] == "print_cli"
    assert plan_iso["isolation"] == "sandbox"
    assert plan_iso["execution_environment"] == "isolated"
    assert plan_iso["worker_id"] == "local-docker"
    assert plan_iso["image_id"] == image_id
    assert plan_iso["profile_digest"] == AGY_PROFILE.profile_digest()

    plan_host = await adm_ok.resolve("caller-a", get_client("caller-a"), req_host)
    assert plan_host["backend"] == "print_cli"
    assert plan_host["isolation"] == "native"
    assert plan_host["execution_environment"] == "host"
    assert plan_host["worker_id"] == "local-print-cli"
    assert plan_host["image_id"] is None

    # 2. Without agent_container configured: isolated rejected cleanly.
    adm_no_ac, _, ncfg_no_ac, _, req_iso_2, _ = _make_isolated_setup(
        tmp_path / "no-ac", monkeypatch, with_agent_container=False
    )
    with pytest.raises(ExecutionError) as err_no_ac:
        await adm_no_ac.resolve("caller-a", get_client("caller-a"), req_iso_2)
    assert err_no_ac.value.status == 501
    assert err_no_ac.value.code == "unsupported_capability"

    profile = resolve_agent_model("agy-iso", ncfg_ok)
    with pytest.raises(ExecutionError) as err_validate:
        validate_print_request(req_iso, profile, agent_container=False)
    assert err_validate.value.status == 501
    validate_print_request(req_iso, profile, agent_container=True)

    # 3. Without drivers["cli.print"].cells["coding"].image_id: isolated fails alias resolution.
    adm_no_img, _, ncfg_no_img, _, req_iso_3, _ = _make_isolated_setup(
        tmp_path / "no-img", monkeypatch, with_coding_cell=False
    )
    with pytest.raises(ExecutionError) as err_no_img:
        await adm_no_img.resolve("caller-a", get_client("caller-a"), req_iso_3)
    assert err_no_img.value.status == 422
    assert err_no_img.value.code == "unsupported_capability"


async def test_executor_runs_isolated_print_cli_and_records_container_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kotoyose.agent_container import AgentContainer, CONTAINER_ALIAS_SKILLS
    from kotoyose.config import SkillDefinition, get_config

    _HOME_SEMAPHORES.clear()
    adm, store, ncfg, image_id, req_iso, _ = _make_isolated_setup(tmp_path / "exec", monkeypatch)
    plugin = tmp_path / "plugin"
    manifest = plugin / ".claude-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text('{"name":"fixture"}')
    skill = plugin / "skills" / "guide"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("Read the guide.")
    asset = skill / "guide.bin"
    asset.write_bytes(b"pinned")
    cfg = get_config()
    cfg.skill_definitions["guide"] = SkillDefinition(plugin=str(plugin), skill="guide")
    cfg.models["agy-iso"].skill_refs = ["guide"]
    plan = await adm.resolve("caller-a", get_client("caller-a"), req_iso)
    asset.write_bytes(b"changed after admission")

    executor = AgentExecutor(ncfg, store, worker=SimpleNamespace())

    class _FakeExecContainer:
        instance = "test-inst"

        async def create(self, spec) -> str:
            self.staged = spec.ro_dirs[CONTAINER_ALIAS_SKILLS]
            assert (self.staged / "guide/guide.bin").read_bytes() == b"pinned"
            assert not self.staged.is_relative_to(spec.workspace)
            assert f"type=bind,src={self.staged},dst={CONTAINER_ALIAS_SKILLS},readonly" in AgentContainer._extra_mount_args(spec)
            return f"cid-{spec.job_id}"

        async def exec(self, job_id: str, argv: list[str], *, env: dict[str, str], limit: int):
            assert any(
                f"This skill's files are at \"{CONTAINER_ALIAS_SKILLS}/guide\"" in arg and "guide.bin" in arg
                for arg in argv
            )
            assert all(str(self.staged) not in arg for arg in argv)
            return await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                (
                    "import json; "
                    'print(json.dumps({"event": "init", "conversation_id": "c1", "init": {"model": "m"}})); '
                    'print(json.dumps({"event": "result", "result": {"conversation_id": "c1", "status": "SUCCESS", "response": "done"}}))'
                ),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=limit,
            )

        async def remove(self, job_id: str, grace_seconds: int = 0) -> str:
            return "confirmed"

    container = _FakeExecContainer()
    executor.print_cli_runtime.container_runtime = (container, ncfg.agent_container, image_id)
    attempt = {
        "id": "att-exec-iso-1",
        "principal": "caller-a",
        "status": "running",
        "request": req_iso.model_dump(mode="json"),
        "snapshot": plan,
    }
    store.attempts[attempt["id"]] = attempt
    result = await executor._execute_print_cli(
        attempt,
        asyncio.Event(),
        runtime_entry=SimpleNamespace(entered=False),
    )
    assert result.status == "succeeded"
    assert result.content == b"done"
    assert not container.staged.exists()
    ops = store.operations_by_attempt[attempt["id"]]
    assert len(ops) == 1
    assert ops[0]["state"] == "confirmed"
    assert ops[0]["result"]["agent_container"]["instance"] == "test-inst"
    assert ops[0]["result"]["agent_container"]["id"].startswith("cid-")



@pytest.mark.asyncio
async def test_training_policy_refuses_and_admits_print_cli_execution(tmp_path, monkeypatch):
    from kotoyose.config import TrainingAck
    from kotoyose.data_policy import training_digest

    request, admission, _, _, cfg, _, _ = _setup_admission_Fixture(tmp_path, monkeypatch)
    cfg.connections["agy-home-1"].data_policy = "trains"
    caller = get_client("caller-a")
    with pytest.raises(ExecutionError) as refused:
        await admission.resolve("caller-a", caller, request)
    assert (refused.value.status, refused.value.reason_code) == (403, "data_policy_restricted")
    capabilities = ["tools", "workspace"]
    cfg.training_acks.append(TrainingAck(
        alias="agy-coder", digest=training_digest(cfg, "agy-coder", capabilities),
        capabilities=capabilities, reasons=["trains"], note="Disposable workspace",
        acknowledged_by="test-owner", at="2026-11-18T10:00:00Z",
    ))
    plan = await admission.resolve("caller-a", caller, request)
    assert plan["backend"] == "print_cli"
    assert plan["surface"] == "read"
    cfg.routes[cfg.models["agy-coder"].route].model = "updated-model"
    with pytest.raises(ExecutionError) as stale:
        await admission.resolve("caller-a", caller, request)
    assert stale.value.reason_code == "data_policy_restricted"


async def test_acp_isolated_admission_pins_protocol_and_alias_policy(tmp_path, monkeypatch):
    admission, _, _, image_id, request, _ = _make_isolated_setup(tmp_path, monkeypatch, driver="cli.acp")
    plan = await admission.resolve("caller-a", get_client("caller-a"), request)
    assert plan["backend"] == "print_cli"
    assert plan["acp"] is True
    assert plan["profile_digest"] == AGY_PROFILE.profile_digest(acp=True)
    assert plan["acp_tools"] == ["Edit"]
    assert plan["acp_deny"] == ["Bash"]
    assert plan["image_id"] == image_id
    assert plan["execution_environment"] == "isolated"
