"""Repository Context Pack と Skill の immutable pin / admission を検証する。"""

from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from kotoyose import config as config_module
from kotoyose.capabilities import CapabilitySupport, capability_manifest
from kotoyose.config import AgentRoute, AliasAgent, Connection, HarnessConfig, ModelPreset
from kotoyose.execution_admission import ExecutionAdmission
from kotoyose.execution_contract import AgentExecution, ExecutionInput
from kotoyose.execution_resources import (
    AgentDefinition,
    CallerPolicy,
    DriverInstall,
    ExecutionBudget,
    ExecutionError,
    ExecutionGrant,
    InputText,
    NativeExecutionConfig,
    RepositoryContextPack,
    SkillResource,
    TimeoutPolicy,
    schema_digest_for,
)


def _digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _pinned(cls, kind: str, content: str, **overrides):
    digest = _digest(content)
    values = {
        "schema_version": "1",
        "kind": kind,
        "content": content,
        "content_digest": digest,
        "schema_digest": schema_digest_for(kind),
        "generator": "taskhub-context-generator",
        "version": "1",
        "config_digest": _digest("config-v1"),
        "source_revision": "a" * 40,
        "repository_ref": "repo:example",
        "provenance": "project",
        "dependency_refs": [],
    }
    values.update(overrides)
    return cls(**values)


def test_pinned_resources_require_content_and_schema_digests_and_resolved_revision():
    """本文、schema、commit revision が変わった resource は保存前に拒否する。"""
    pack = _pinned(RepositoryContextPack, "repository_context_pack", "packages\n")
    skill = _pinned(SkillResource, "skill", "Use the pinned checklist.\n")
    assert pack.content_digest == _digest(pack.content)
    assert skill.source_revision == "a" * 40
    with pytest.raises(ValidationError):
        _pinned(RepositoryContextPack, "repository_context_pack", "changed\n", content_digest="0" * 64)
    with pytest.raises(ValidationError):
        _pinned(SkillResource, "skill", "x", schema_digest="0" * 64)
    with pytest.raises(ValidationError):
        _pinned(SkillResource, "skill", "x", source_revision="main")
    with pytest.raises(ValidationError):
        _pinned(SkillResource, "skill", "x", provenance="trusted")


class _MemoryStore:
    def __init__(self, resources, workspace):
        self.resources = resources
        self.workspace_data = workspace

    async def resource(self, principal: str, ref: str):
        value = self.resources.get((principal, ref))
        if value is None:
            raise ExecutionError("invalid_request", "Unknown resource reference", 404)
        return value

    async def workspace(self, principal: str, ref: str):
        return self.workspace_data


def _admission(resources, root: Path, *, grant_refs):
    now = datetime.now(timezone.utc)
    timeout = TimeoutPolicy(
        schema_version="1", kind="timeout_policy", soft_seconds=1, hard_seconds=10, grace_seconds=1,
    )
    budget = ExecutionBudget(
        schema_version="1", kind="budget", reservation_ref="reservation", mode="observed",
        deadline=now + timedelta(minutes=2), memory_bytes=64 * 1024 * 1024, pids_limit=8,
        cpus=1, output_limit_bytes=4096,
    )
    definition = AgentDefinition(
        schema_version="1", kind="agent_definition", instructions="Follow the request.",
        output_contract="text", max_turns=1, timeout_policy_ref="timeout",
    )
    resources.update({
        ("caller-a", "timeout"): timeout,
        ("caller-a", "budget"): budget,
        ("caller-a", "definition"): definition,
    })
    grant = ExecutionGrant(
        schema_version="1", kind="grant", grant_id="grant", version=1,
        caller_run_ref="run-1", workspace_ref="workspace-1", isolation="sandbox", network="none",
        agent_models=["profile"], runtime_profiles=["profile"], resource_refs=grant_refs, policy_ref="policy",
        not_before=now - timedelta(minutes=1), expires_at=now + timedelta(minutes=1),
        max_seconds=20, delegation="none",
    )
    resources[("caller-a", "grant")] = grant
    request = AgentExecution(
        schema_version="1", kind="agent", idempotency_key="key", caller_run_ref="run-1",
        workspace_ref="workspace-1", execution_grant_ref="grant", budgets_ref="budget",
        agent_definition_ref="definition", model="profile",
        inputs=[ExecutionInput(kind="repository_context", provenance="project", content_ref="pack")],
        skill_refs=["skill"],
    )
    policy = CallerPolicy(
        workspace_roots=[root], agent_models=["profile"],
        places=["isolated"], provenance=["project", "agent_generated", "mixed"],
        max_seconds=30, max_memory_bytes=128 * 1024 * 1024, max_pids=16, max_cpus=1,
        max_concurrent=2,
    )
    config_module._config = HarnessConfig(
        connections={"token-conn": Connection(provider="claude", kind="api_key", secret="env:TOKEN", data_policy="no_training")},
        routes={"profile": AgentRoute(
            driver="claude.agent-sdk", model="claude-haiku-4-5-20251001", connection="token-conn",
        )},
        models={"profile": ModelPreset(
            route="profile",
            agent=AliasAgent(place="isolated", surface="none"),
        )},
    )
    settings = NativeExecutionConfig(
        schema_version="1", image_id="sha256:" + "a" * 64,
        drivers={"claude.agent-sdk": DriverInstall(cli_binary=Path("/bin/sh"))},
    )
    manifest = capability_manifest()
    for name in ("native_execution", "sandbox"):
        manifest.capabilities[name] = CapabilitySupport(status="supported", reason="test")
    return ExecutionAdmission(
        _MemoryStore(resources, {
            "id": "workspace-id", "path": str(root / "workspace"),
            "device": (root / "workspace").stat().st_dev,
            "inode": (root / "workspace").stat().st_ino, "generation": 0,
        }),
        manifest, settings.image_id, settings=settings,
    ), policy, request


@pytest.mark.asyncio
async def test_admission_pins_pack_and_skill_body_without_extending_tool_surface(tmp_path):
    """Grant 内の pack / Skill だけを provenance と opaque ref 付きで Agent input に固定する。"""
    (tmp_path / "workspace").mkdir()
    resources = {}
    pack = _pinned(RepositoryContextPack, "repository_context_pack", "routes: /v1\n")
    skill = _pinned(SkillResource, "skill", "Review the listed routes.\n")
    resources.update({("caller-a", "pack"): pack, ("caller-a", "skill"): skill})
    admission, policy, request = _admission(
        resources, tmp_path, grant_refs=["timeout", "budget", "definition", "pack", "skill"],
    )
    snapshot = await admission.resolve("caller-a", policy, request)
    assert snapshot["inputs"][0]["kind"] == "repository_context"
    assert snapshot["inputs"][0]["provenance"] == "project"
    assert snapshot["inputs"][0]["content_ref"] == "pack"
    assert snapshot["inputs"][0]["text"] == pack.content
    assert snapshot["inputs"][0]["dependency_closure"][0]["content_digest"] == pack.content_digest
    assert snapshot["skills"][0]["resource_ref"] == "skill"
    assert snapshot["skills"][0]["content"] == skill.content
    assert snapshot["skills"][0]["dependency_closure"][0]["schema_digest"] == skill.schema_digest
    assert snapshot["tool_refs"] == []


@pytest.mark.asyncio
async def test_admission_rejects_missing_or_cyclic_pinned_dependencies_before_start(tmp_path):
    """依存 ref の欠落・循環を worker 実行前に拒否する。"""
    (tmp_path / "workspace").mkdir()
    pack = _pinned(RepositoryContextPack, "repository_context_pack", "pack", dependency_refs=["missing"])
    resources = {("caller-a", "pack"): pack}
    admission, policy, request = _admission(resources, tmp_path, grant_refs=["timeout", "budget", "definition", "pack", "skill", "missing"])
    with pytest.raises(Exception, match="Unknown resource"):
        await admission.resolve("caller-a", policy, request)

    first = _pinned(RepositoryContextPack, "repository_context_pack", "first", dependency_refs=["second"])
    second = _pinned(SkillResource, "skill", "second", dependency_refs=["first"])
    resources = {("caller-a", "pack"): first, ("caller-a", "skill"): second,
                 ("caller-a", "first"): first, ("caller-a", "second"): second}
    admission, policy, request = _admission(resources, tmp_path, grant_refs=["timeout", "budget", "definition", "pack", "skill", "first", "second"])
    request = request.model_copy(update={"skill_refs": ["second"]})
    with pytest.raises(Exception, match="cycle"):
        await admission.resolve("caller-a", policy, request)


@pytest.mark.asyncio
async def test_admission_allows_diamond_dependencies_and_rejects_mixed_pack(tmp_path):
    """共有 dependency は再訪問し、Pack の mixed provenance は受け付けない。"""
    (tmp_path / "workspace").mkdir()
    leaf = _pinned(SkillResource, "skill", "leaf")
    left = _pinned(SkillResource, "skill", "left", dependency_refs=["leaf"])
    right = _pinned(SkillResource, "skill", "right", dependency_refs=["leaf"])
    root = _pinned(RepositoryContextPack, "repository_context_pack", "root", dependency_refs=["left", "right"])
    resources = {
        ("caller-a", "pack"): root, ("caller-a", "skill"): leaf, ("caller-a", "leaf"): leaf,
        ("caller-a", "left"): left, ("caller-a", "right"): right,
    }
    admission, policy, request = _admission(
        resources, tmp_path,
        grant_refs=["timeout", "budget", "definition", "pack", "skill", "left", "right", "leaf"],
    )
    snapshot = await admission.resolve("caller-a", policy, request)
    assert [item["resource_ref"] for item in snapshot["inputs"][0]["dependency_closure"]] == [
        "pack", "left", "leaf", "right",
    ]

    mixed_dependency = _pinned(SkillResource, "skill", "mixed dep", provenance="agent_generated")
    root = _pinned(RepositoryContextPack, "repository_context_pack", "root mixed", dependency_refs=["mixed"])
    resources = {("caller-a", "pack"): root, ("caller-a", "skill"): leaf, ("caller-a", "mixed"): mixed_dependency}
    admission, policy, request = _admission(
        resources, tmp_path, grant_refs=["timeout", "budget", "definition", "pack", "skill", "mixed"],
    )
    with pytest.raises(Exception, match="Mixed provenance"):
        await admission.resolve("caller-a", policy, request)

    mixed_root = _pinned(
        RepositoryContextPack,
        "repository_context_pack",
        "root mixed",
        dependency_refs=["mixed"],
        provenance="mixed",
    )
    resources[("caller-a", "pack")] = mixed_root
    request = request.model_copy(update={"inputs": [ExecutionInput(
        kind="repository_context", provenance="mixed", content_ref="pack",
    )]})
    with pytest.raises(Exception, match="project provenance"):
        await admission.resolve("caller-a", policy, request)


@pytest.mark.asyncio
async def test_admission_rejects_non_pinned_dependency_even_when_grant_allows_it(tmp_path):
    """InputText の trusted provenance を pinned closure へ混ぜて ceiling を迂回させない。"""
    (tmp_path / "workspace").mkdir()
    pack = _pinned(
        RepositoryContextPack,
        "repository_context_pack",
        "pack",
        dependency_refs=["trusted-text"],
    )
    trusted_text = InputText(
        schema_version="1", kind="text", text="do not elevate", provenance="trusted",
    )
    resources = {
        ("caller-a", "pack"): pack,
        ("caller-a", "trusted-text"): trusted_text,
    }
    admission, policy, request = _admission(
        resources,
        tmp_path,
        grant_refs=["timeout", "budget", "definition", "pack", "skill", "trusted-text"],
    )
    with pytest.raises(Exception, match="must be repository packs or skills"):
        await admission.resolve("caller-a", policy, request)


@pytest.mark.asyncio
async def test_admission_rejects_foreign_pinned_ref_and_trusted_context_claim(tmp_path):
    """別 principal の resource と trusted 昇格を grant / policy の境界で拒否する。"""
    (tmp_path / "workspace").mkdir()
    pack = _pinned(RepositoryContextPack, "repository_context_pack", "pack")
    skill = _pinned(SkillResource, "skill", "skill")
    resources = {("caller-b", "pack"): pack, ("caller-a", "skill"): skill}
    admission, policy, request = _admission(resources, tmp_path, grant_refs=["timeout", "budget", "definition", "pack", "skill"])
    with pytest.raises(Exception, match="Unknown resource"):
        await admission.resolve("caller-a", policy, request)

    resources = {("caller-a", "pack"): pack, ("caller-a", "skill"): skill}
    admission, policy, request = _admission(resources, tmp_path, grant_refs=["timeout", "budget", "definition", "pack", "skill"])
    request = request.model_copy(update={"inputs": [ExecutionInput(
        kind="repository_context", provenance="trusted", content_ref="pack",
    )]})
    with pytest.raises(Exception, match="provenance"):
        await admission.resolve("caller-a", policy, request)
