"""Codex coding / Codex sandbox / Claude jail / Claude room の admission snapshot を固定する。

揮発する host 値 (inode、CLI digest、SDK 版、tmp path、認証 binding digest) だけを
置き換える。それ以外の bytes は今の受付結果と一致する。
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from kotoyose import config as config_module
from kotoyose.capabilities import CapabilityManifest, CapabilitySupport
from kotoyose.codex_coding_policy import HOST_TOOL_REF
from kotoyose.codex_rootfs import rootfs_digest
from kotoyose.config import AgentRoute, AliasAgent, Connection, HarnessConfig, ModelPreset
from kotoyose.execution_admission import ExecutionAdmission
from kotoyose.execution_contract import AgentExecution
from kotoyose.execution_resources import (
    AgentContainerConfig, AgentDefinition, CallerPolicy, DriverCell, DriverInstall, ExecutionBudget,
    ExecutionGrant, InputText, NativeExecutionConfig, NativeRuntimeProfile, TimeoutPolicy, canonical_json,
    codex_authentication_binding, codex_profile_authentication,
)
from tests.codex_subscription_fixture import subscription_auth
from tests.test_codex_host_coding import reviewed_tree

FIXTURE = Path(__file__).parent / "fixtures" / "agent_snapshots"
NOT_BEFORE = datetime(2020, 1, 1, tzinfo=timezone.utc)
EXPIRES = datetime(2099, 1, 1, tzinfo=timezone.utc)
IMAGE_ID = "sha256:" + "ab" * 32
CLI = "/usr/bin/false"
_HEX = re.compile(r"^[a-f0-9]{64}$")


def _manifest() -> CapabilityManifest:
    return CapabilityManifest(capabilities={
        name: CapabilitySupport(status="supported", reason="fixture")
        for name in ("native_execution", "sandbox")
    })


def _resources(tool_refs: list[str]) -> dict:
    grant = ExecutionGrant(
        schema_version="1", kind="grant", grant_id="grant-1", version=1,
        caller_run_ref="run-1", workspace_ref="workspace:fixture:0", isolation="sandbox", network="none",
        agent_models=["alias"], resource_refs=["definition", "budget", "timeout", "input", *tool_refs],
        policy_ref="policy", not_before=NOT_BEFORE, expires_at=EXPIRES, max_seconds=60, delegation="none",
    )
    return {
        "grant": grant,
        "definition": AgentDefinition(
            schema_version="1", kind="agent_definition", instructions="Original instructions.",
            output_contract="text", max_turns=8, timeout_policy_ref="timeout",
        ),
        "timeout": TimeoutPolicy(schema_version="1", kind="timeout_policy", soft_seconds=20, hard_seconds=30, grace_seconds=1),
        "budget": ExecutionBudget(
            schema_version="1", kind="budget", reservation_ref="reservation", mode="observed",
            deadline=EXPIRES, memory_bytes=128 * 1024 * 1024, pids_limit=32, cpus=.5, output_limit_bytes=65536,
        ),
        "input": InputText(schema_version="1", kind="text", text="Original task.", provenance="project"),
    }


def _store(workspace: Path, resources: dict):
    info = workspace.stat()

    async def resource(principal, ref):
        return resources[ref]

    return SimpleNamespace(
        resource=resource,
        workspace=AsyncMock(return_value={
            "id": "fixture", "path": str(workspace), "device": info.st_dev, "inode": info.st_ino,
            "generation": 0, "caller_ref": "repo-1",
        }),
    )


def _policy(root: Path) -> CallerPolicy:
    return CallerPolicy(
        workspace_roots=[root], agent_models=["alias"],
        places=["isolated"], max_seconds=60, max_memory_bytes=128 * 1024 * 1024,
        max_pids=32, max_cpus=1, max_concurrent=1,
    )


def _request(tool_refs: list[str], **extra) -> AgentExecution:
    return AgentExecution(
        schema_version="1", kind="agent", idempotency_key="dispatch-1", caller_run_ref="run-1",
        workspace_ref="workspace:fixture:0", execution_grant_ref="grant", budgets_ref="budget",
        agent_definition_ref="definition", model="alias", tool_refs=tool_refs,
        inputs=[{"kind": "task_request", "provenance": "project", "content_ref": "input"}],
        **extra,
    )


def _route_for(profile: NativeRuntimeProfile) -> tuple[str, str, Connection]:
    """profile が持っていた pin を、route と connection に移す。"""
    if profile.provider == "claude":
        return "claude.agent-sdk", "claude-oauth", Connection(provider="claude", kind="subscription", data_policy="no_training", secret="claude-oauth")
    if profile.credential_mode == "chatgpt_subscription":
        return "codex.app-server", "codex-chatgpt", Connection(
            provider="codex", kind="subscription", data_policy="no_training", auth_file=profile.codex_auth_file,
        )
    return "codex.app-server", "openai-env", Connection(provider="codex", kind="api_key", data_policy="no_training", secret="env:OPENAI_API_KEY")


def _settings(root: Path, policy: CallerPolicy, profile: NativeRuntimeProfile, *, container: bool):
    driver, _conn_id, _conn = _route_for(profile)
    cells = {}
    if profile.provider == "codex" and profile.codex_rootfs is not None:
        kind = "coding" if profile.codex_execution_mode == "host_native_coding_v1" else "standard"
        payload = {"rootfs": profile.codex_rootfs, "rootfs_digest": profile.codex_rootfs_digest}
        if profile.codex_egress_limits is not None:
            payload["codex_egress_limits"] = profile.codex_egress_limits
        cells[kind] = DriverCell(**payload)
    values = dict(
        schema_version="1", image_id=IMAGE_ID,
        drivers={driver: DriverInstall(cli_binary=profile.cli_binary, cells=cells)},
    )
    if container:
        values["agent_container"] = AgentContainerConfig(agent_root="/var/lib/kotoyose-agent", home_root="/var/lib/kotoyose-agent-home")
    return NativeExecutionConfig(**values)


async def _admit(tmp_path: Path, name: str, *, provider: str, model: str, profile: NativeRuntimeProfile,
                 tool_refs: list[str], surface: str, container: bool, preset: dict, request_extra: dict,
                 monkeypatch) -> dict:
    root = tmp_path / name
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    if provider == "codex" and surface == "write":
        for name in (".codex", ".agents"):
            (workspace / name).mkdir()
    policy = _policy(root)
    resources = _resources(tool_refs)
    driver, conn_id, conn = _route_for(profile)
    monkeypatch.setattr(
        config_module, "_config",
        HarnessConfig(
            connections={conn_id: conn},
            routes={"alias-route": AgentRoute(driver=driver, model=model, connection=conn_id)},
            models={"alias": ModelPreset(
                route="alias-route",
                agent=AliasAgent(place="isolated", surface=surface), **preset,
            )},
        ),
    )
    settings = _settings(root, policy, profile, container=container)
    admission = ExecutionAdmission(
        _store(workspace, resources), _manifest(), settings.image_id, settings=settings,
    )
    return await admission.resolve("fixture", policy, _request(tool_refs, **request_extra))


def _codex_profile(root: Path, *, coding: bool) -> NativeRuntimeProfile:
    tree = reviewed_tree(root, coding)
    if coding:
        auth, _ = subscription_auth(root, account="fixture-account", expiry=1893456000)
        return NativeRuntimeProfile(
            provider="codex", model="gpt-6-luna", cli_binary=CLI, codex_execution_mode="host_native_coding_v1",
            codex_rootfs=tree, codex_rootfs_digest=rootfs_digest(tree, coding=True),
            credential_mode="chatgpt_subscription", credential_name="CODEX_CHATGPT_AUTH", codex_auth_file=auth,
        )
    return NativeRuntimeProfile(
        provider="codex", model="gpt-6-luna", cli_binary=CLI, codex_rootfs=tree,
        codex_rootfs_digest=rootfs_digest(tree, coding=False),
        credential_mode="environment", credential_name="OPENAI_API_KEY", credential_env="OPENAI_API_KEY",
    )


async def representative_snapshots(tmp_path: Path, monkeypatch) -> dict[str, dict]:
    """4 種の受付 snapshot。umask を固定し、rootfs digest が実行ごとに揺れないようにする。"""
    previous = os.umask(0o022)
    try:
        coding_root = tmp_path / "codex-coding"
        coding_root.mkdir()
        coding_profile = _codex_profile(coding_root, coding=True)
        binding = codex_authentication_binding(codex_profile_authentication(coding_profile))
        coding = await _admit(
            tmp_path, "codex-coding", provider="codex", model="gpt-6-luna", profile=coding_profile,
            tool_refs=[HOST_TOOL_REF], surface="write", container=True, preset={"effort": "max"},
            request_extra={
                "required_provider": "codex", "required_billing_mode": "subscription",
                "required_authentication_mode": "chatgpt_subscription",
                "expected_authentication_binding_digest": binding["binding_digest"],
            },
            monkeypatch=monkeypatch,
        )
        sandbox_root = tmp_path / "codex-sandbox"
        sandbox_root.mkdir()
        sandbox = await _admit(
            tmp_path, "codex-sandbox", provider="codex", model="gpt-6-luna",
            profile=_codex_profile(sandbox_root, coding=False),
            tool_refs=["builtin:sandbox-exec@1"], surface="exec", container=False, preset={}, request_extra={},
            monkeypatch=monkeypatch,
        )
        jail = await _admit(
            tmp_path, "claude-jail", provider="claude", model="claude-sonnet-4-5",
            profile=NativeRuntimeProfile(
                provider="claude", model="unused", cli_binary=CLI, credential_mode="runtime_managed",
            ),
            tool_refs=[], surface="none", container=False, preset={}, request_extra={},
            monkeypatch=monkeypatch,
        )
        room = await _admit(
            tmp_path, "claude-room", provider="claude", model="claude-sonnet-4-5",
            profile=NativeRuntimeProfile(
                provider="claude", model="unused", cli_binary=CLI, credential_mode="runtime_managed",
            ),
            tool_refs=[HOST_TOOL_REF], surface="write", container=True, preset={}, request_extra={},
            monkeypatch=monkeypatch,
        )
    finally:
        os.umask(previous)
    return {
        "codex-coding": coding,
        "codex-sandbox": sandbox,
        "claude-jail": jail,
        "claude-room": room,
    }


def stabilize(snapshot: dict, tmp_path: Path) -> dict:
    """比較に使わない host 固有値だけを置き換える。決定に使うフィールドは残す。"""
    def walk(value):
        if isinstance(value, dict):
            return {key: walk(item) for key, item in value.items()}
        if isinstance(value, list):
            return [walk(item) for item in value]
        if isinstance(value, str) and str(tmp_path) in value:
            return value.replace(str(tmp_path), "<tmp>")
        return value

    stable = walk(snapshot)
    assert stable["workspace_device"] != 0 and stable["workspace_inode"] != 0
    stable["workspace_device"] = 0
    stable["workspace_inode"] = 0
    assert _HEX.fullmatch(stable["cli_digest"])
    stable["cli_digest"] = "<cli_digest>"
    assert isinstance(stable["sdk_version"], str) and stable["sdk_version"]
    stable["sdk_version"] = "<sdk_version>"
    assert stable["cli_binary"] == str(Path(CLI).resolve())
    stable["cli_binary"] = "<cli>"
    protected = stable.get("codex_protected_workspace")
    if isinstance(protected, list):
        for entry in protected:
            assert type(entry["device"]) is int and entry["device"] != 0
            assert type(entry["inode"]) is int and entry["inode"] != 0
            entry["device"] = 0
            entry["inode"] = 0
    if "expected_authentication_binding_digest" in stable:
        assert _HEX.fullmatch(stable["expected_authentication_binding_digest"])
        stable["expected_authentication_binding_digest"] = "<binding_digest>"
    # policy_digest は auth file の path を含む binding の hash なので、tmp ごとに変わる。
    billing = stable.get("runtime_billing")
    if isinstance(billing, dict) and "policy_digest" in billing:
        assert _HEX.fullmatch(billing["policy_digest"])
        billing["policy_digest"] = "<policy_digest>"
    return stable


async def test_representative_admission_snapshots_match_pins(tmp_path, monkeypatch):
    """4 種の snapshot が pin と byte 一致すること。"""
    snapshots = await representative_snapshots(tmp_path, monkeypatch)
    for name, snapshot in snapshots.items():
        path = FIXTURE / f"{name}.json"
        actual = canonical_json(stabilize(snapshot, tmp_path)).encode()
        assert path.read_bytes() == actual, name
