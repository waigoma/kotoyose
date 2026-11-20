"""Execution resource の入力境界と admission の拒否条件を検証する。"""

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from kotoyose.capabilities import CapabilitySupport, capability_manifest
from kotoyose.execution_admission import ExecutionAdmission, validate_image_artifact, workspace_path
from kotoyose.execution_artifacts import ArtifactCaptureSpec, ArtifactSpecEntry
from kotoyose.execution_contract import CommandExecution
from kotoyose.config import Client, HarnessConfig
from kotoyose.execution_resources import (
    ArgumentList,
    CallerPolicy,
    CommandSpec,
    Environment,
    ExecutionBudget,
    ExecutionError,
    ExecutionGrant,
    NativeExecutionConfig,
    WorkerCapabilityConfig,
    ResourceEnvelope,
    TimeoutPolicy,
    WorkingDirectory,
    RemoteWorkerConnection,
    SharedWorkspaceBinding,
    default_worker_capabilities,
)
from kotoyose.remote_worker_contract import RemoteWorkerRef


def _policy(root: Path, **overrides) -> CallerPolicy:
    values = {

        "workspace_roots": [root],
        "env_keys": ["LANG", "LC_ALL"],
        "provenance": ["project", "agent_generated", "external_untrusted", "mixed"],
        "max_seconds": 60.0,
        "max_memory_bytes": 128 * 1024 * 1024,
        "max_pids": 32,
        "max_cpus": 2.0,
        "max_concurrent": 2,
    }
    values.update(overrides)
    return CallerPolicy(**values)


def _resources(now: datetime | None = None) -> dict[str, object]:
    now = now or datetime.now(timezone.utc)
    return {
        "command": CommandSpec(schema_version="1", kind="command_spec", executable="/bin/echo"),
        "argv": ArgumentList(schema_version="1", kind="argv", values=["hello"]),
        "cwd": WorkingDirectory(schema_version="1", kind="cwd", path="."),
        "env": Environment(schema_version="1", kind="env", values={"LANG": "C.UTF-8"}),
        "timeout": TimeoutPolicy(
            schema_version="1", kind="timeout_policy", soft_seconds=5, hard_seconds=20, grace_seconds=2,
        ),
        "budget": ExecutionBudget(
            schema_version="1", kind="budget", reservation_ref="reservation-1", mode="observed",
            deadline=now + timedelta(minutes=1), memory_bytes=64 * 1024 * 1024,
            pids_limit=4, cpus=1.0, output_limit_bytes=1024,
        ),
        "grant": ExecutionGrant(
            schema_version="1", kind="grant", grant_id="grant-1", version=1,
            caller_run_ref="run-1", workspace_ref="workspace-1", isolation="sandbox", network="none",
            command_spec_refs=["command"],
            resource_refs=["argv", "cwd", "env", "budget", "timeout"], policy_ref="policy-1",
            not_before=now - timedelta(minutes=1), expires_at=now + timedelta(minutes=1),
            max_seconds=30, delegation="none",
        ),
    }


def _request(**overrides) -> CommandExecution:
    values = {
        "schema_version": "1", "idempotency_key": "key-1", "caller_run_ref": "run-1",
        "kind": "command", "workspace_ref": "workspace-1", "execution_grant_ref": "grant",
        "budgets_ref": "budget", "command_spec_ref": "command", "argv_ref": "argv",
        "cwd_ref": "cwd", "env_refs": ["env"], "timeout_policy_ref": "timeout",
    }
    values.update(overrides)
    return CommandExecution(**values)


class _MemoryStore:
    def __init__(self, resources: dict[str, object], workspace: dict):
        self.resources = resources
        self.workspace_data = workspace

    async def resource(self, principal: str, ref: str):
        resource = self.resources.get(ref)
        if resource is None:
            raise ExecutionError("invalid_request", "Unknown resource reference", 404)
        return resource

    async def workspace(self, principal: str, ref: str):
        if ref != "workspace-1":
            raise ExecutionError("invalid_request", "Unknown workspace", 404)
        return self.workspace_data


class _ArtifactStore(_MemoryStore):
    def __init__(self, artifact: dict):
        super().__init__({}, {})
        self._artifact = artifact

    async def artifact(self, principal: str, ref: str):
        if principal != "caller-a":
            raise ExecutionError("invalid_request", "Unknown artifact", 404)
        return self._artifact


def _manifest() -> object:
    manifest = capability_manifest()
    for name in ("native_execution", "sandbox", "command_execution"):
        manifest.capabilities[name] = CapabilitySupport(status="supported", reason="test")
    return manifest


@pytest.mark.parametrize(
    "payload",
    [
        {"schema_version": "1", "kind": "command_spec", "executable": "/bin/echo", "unexpected": True},
        {"schema_version": "1", "kind": "unknown", "executable": "/bin/echo"},
        {"schema_version": "1", "kind": "command_spec", "executable": "/bin/echo", "principal": "caller-a"},
    ],
)
def test_resource_envelope_rejects_unknown_field_kind_and_principal(payload):
    """Resource kind と identity は固定契約にない入力を受理しない。"""
    with pytest.raises(ValidationError):
        ResourceEnvelope.model_validate({"resource": payload})


def test_artifact_capture_spec_is_a_versioned_resource():
    """artifact spec は通常の content addressed resource と同じ envelope で検証する。"""
    resource = ResourceEnvelope.model_validate({
        "resource": {
            "schema_version": "1", "kind": "artifact_spec",
            "entries": [{"name": "evidence", "path": "evidence.bin",
                         "media_type": "application/octet-stream", "required": True, "max_bytes": 32}],
            "max_total_bytes": 32,
        }
    }).resource
    assert isinstance(resource, ArtifactCaptureSpec)


def test_client_tool_template_validates_as_a_versioned_resource():
    """標準の client tool 雛形は登録用 resource envelope として検証できる。"""
    path = Path(__file__).resolve().parents[1] / "docs/standards/templates/client-tool.json"
    payload = {"resource": json.loads(path.read_text(encoding="utf-8"))}
    resource = ResourceEnvelope.model_validate(payload).resource
    assert resource.kind == "client_tool"


@pytest.mark.parametrize(
    ("media_type", "content"),
    [
        ("image/png", b"\x89PNG\r\n\x1a\nbody"),
        ("image/jpeg", b"\xff\xd8\xffbody"),
        ("image/webp", b"RIFFxxxxWEBPbody"),
    ],
)
def test_image_artifact_requires_real_signature_and_digest(media_type: str, content: bytes):
    """MIME と bytes signature が一致する既存 ArtifactRef だけを通す。"""
    digest = hashlib.sha256(content).hexdigest()
    ref = f"artifact:attempt:image:{digest}"
    assert validate_image_artifact(ref, {"content": content, "media_type": media_type, "digest": digest})["size"] == len(content)
    with pytest.raises(ExecutionError):
        validate_image_artifact(ref, {"content": b"wrong", "media_type": media_type, "digest": digest})
    with pytest.raises(ExecutionError):
        wrong_media = "image/jpeg" if media_type != "image/jpeg" else "image/png"
        validate_image_artifact(ref, {"content": content, "media_type": wrong_media, "digest": digest})


def test_image_artifact_rejects_oversize_and_digest_drift(monkeypatch):
    content = b"\x89PNG\r\n\x1a\nbody"
    digest = hashlib.sha256(content).hexdigest()
    ref = f"artifact:attempt:image:{digest}"
    monkeypatch.setattr("kotoyose.execution_images.MAX_IMAGE_BYTES", len(content) - 1)
    with pytest.raises(ExecutionError, match="size"):
        validate_image_artifact(ref, {"content": content, "media_type": "image/png", "digest": digest})
    monkeypatch.setattr("kotoyose.execution_images.MAX_IMAGE_BYTES", 20 * 1024 * 1024)
    with pytest.raises(ExecutionError, match="digest"):
        validate_image_artifact(ref, {"content": content, "media_type": "image/png", "digest": "0" * 64})


@pytest.mark.asyncio
async def test_image_admission_requires_same_principal_owned_artifact():
    content = b"\x89PNG\r\n\x1a\nbody"
    digest = hashlib.sha256(content).hexdigest()
    ref = f"artifact:attempt:image:{digest}"
    admission = ExecutionAdmission(
        _ArtifactStore({"content": content, "media_type": "image/png", "digest": digest}),
        _manifest(), "sha256:" + "d" * 64,
    )
    assert (await admission._image("caller-a", ref))["digest"] == digest
    with pytest.raises(ExecutionError, match="ArtifactRef"):
        await admission._image("caller-b", ref)


@pytest.mark.parametrize("value", ["", "/tmp", "a/../b", "a\x00b"])
def test_working_directory_is_workspace_relative(value):
    """cwd が host path や traversal を含む場合は resource 化できない。"""
    with pytest.raises(ValidationError):
        WorkingDirectory(schema_version="1", kind="cwd", path=value)


@pytest.mark.parametrize(
    "values",
    [{"BAD-KEY": "x"}, {"PATH": "/tmp"}, {"LANG": "x\x00y"}],
)
def test_environment_rejects_ambient_or_invalid_values(values):
    """ambient provider 環境へ接続する予約名と不正な key/value を拒む。"""
    with pytest.raises(ValidationError):
        Environment(schema_version="1", kind="env", values=values)


def test_timeout_and_grant_intervals_require_ordered_aware_timestamps():
    """停止期限・Grant 期間に曖昧な時刻や空区間を許さない。"""
    with pytest.raises(ValidationError):
        TimeoutPolicy(schema_version="1", kind="timeout_policy", soft_seconds=2, hard_seconds=2, grace_seconds=1)
    with pytest.raises(ValidationError):
        ExecutionBudget(
            schema_version="1", kind="budget", reservation_ref="r", mode="observed",
            deadline=datetime.now(), memory_bytes=16 * 1024 * 1024, pids_limit=1,
            cpus=1, output_limit_bytes=1024,
        )
    values = _resources()["grant"].model_dump()
    values["not_before"] = values["expires_at"]
    with pytest.raises(ValidationError):
        ExecutionGrant(**values)




def test_native_execution_config_rejects_callers_key():
    with pytest.raises(ValueError, match="callers moved to config.yml clients \\(ADR-0011\\)"):
        NativeExecutionConfig.model_validate({
            "schema_version": "1",
            "image_id": "sha256:" + "a" * 64,
            "callers": {"a": {}},
        })




@pytest.mark.asyncio
async def test_admission_rejects_wrong_reference_kind_and_out_of_grant_before_worker(tmp_path):
    """参照 kind / Grant 範囲の不一致は process 起動前に拒否する。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    resources = _resources()
    resources["argv"] = resources["command"]
    store = _MemoryStore(resources, {
        "id": "workspace-id", "path": str(workspace), "device": workspace.stat().st_dev,
        "inode": workspace.stat().st_ino, "generation": 0,
    })
    admission = ExecutionAdmission(store, _manifest(), "sha256:" + "c" * 64)
    with pytest.raises(ExecutionError, match="kind"):
        await admission.resolve("caller-a", _policy(tmp_path), _request())

    resources = _resources()
    resources["grant"].resource_refs.remove("env")
    store = _MemoryStore(resources, {
        "id": "workspace-id", "path": str(workspace), "device": workspace.stat().st_dev,
        "inode": workspace.stat().st_ino, "generation": 0,
    })
    with pytest.raises(ExecutionError, match="outside grant"):
        await ExecutionAdmission(store, _manifest(), "sha256:" + "c" * 64).resolve(
            "caller-a", _policy(tmp_path), _request()
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["expires_at", "caller_run_ref", "workspace_ref", "isolation"])
async def test_admission_rejects_expired_or_scope_mismatched_grant(tmp_path, field):
    """Grant の期限、run、workspace、isolation は request と同じ範囲でなければならない。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    resources = _resources()
    grant_values = resources["grant"].model_dump()
    now = datetime.now(timezone.utc)
    grant_values[field] = {
        "expires_at": now - timedelta(seconds=1),
        "caller_run_ref": "other-run",
        "workspace_ref": "other-workspace",
        "isolation": "native",
    }[field]
    resources["grant"] = ExecutionGrant(**grant_values)
    store = _MemoryStore(resources, {"id": "workspace-id", "path": str(workspace),
                                      "device": workspace.stat().st_dev, "inode": workspace.stat().st_ino,
                                      "generation": 0})
    with pytest.raises(ExecutionError):
        await ExecutionAdmission(store, _manifest(), "sha256:" + "c" * 64).resolve(
            "caller-a", _policy(tmp_path), _request()
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [("max_seconds", 10), ("max_memory_bytes", 16 * 1024 * 1024), ("max_pids", 1), ("max_cpus", 0.5)],
)
async def test_admission_enforces_caller_budget_ceiling(tmp_path, field, value):
    """hard timeout と memory / pids / CPU は Caller 上限を超えられない。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    resources = _resources()
    if field == "max_seconds":
        resources["timeout"] = TimeoutPolicy(schema_version="1", kind="timeout_policy", soft_seconds=5,
                                              hard_seconds=20, grace_seconds=2)
    else:
        budget = resources["budget"].model_copy(update={field: value + (1 if field != "max_cpus" else 0.5)})
        resources["budget"] = budget
    store = _MemoryStore(resources, {"id": "workspace-id", "path": str(workspace),
                                      "device": workspace.stat().st_dev, "inode": workspace.stat().st_ino,
                                      "generation": 0})
    policy = _policy(tmp_path, **{field: value})
    with pytest.raises(ExecutionError, match="ceiling"):
        await ExecutionAdmission(store, _manifest(), "sha256:" + "d" * 64).resolve(
            "caller-a", policy, _request()
        )


@pytest.mark.asyncio
async def test_admission_enforces_allowed_environment_ceiling(tmp_path):
    """Grant が参照しても Caller policy にない環境 key は注入しない。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    resources = _resources()
    store = _MemoryStore(resources, {"id": "workspace-id", "path": str(workspace),
                                      "device": workspace.stat().st_dev, "inode": workspace.stat().st_ino,
                                      "generation": 0})
    with pytest.raises(ExecutionError, match="Environment"):
        await ExecutionAdmission(store, _manifest(), "sha256:" + "d" * 64).resolve(
            "caller-a", _policy(tmp_path, env_keys=[]), _request()
        )


def test_workspace_path_rejects_root_symlink_and_outside(tmp_path):
    """workspace は root 自体・symlink・許可 root 外の path を公開しない。"""
    root = tmp_path / "root"
    root.mkdir()
    child = root / "child"
    child.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "link"
    link.symlink_to(outside, target_is_directory=True)
    policy = _policy(root)
    assert workspace_path(policy, str(child)) == child
    for value in (str(root), str(link), str(outside)):
        with pytest.raises(ExecutionError):
            workspace_path(policy, value)


@pytest.mark.asyncio
async def test_native_isolation_requires_caller_permission_and_is_pinned(tmp_path):
    """Worker が対応していても Caller 上限にない native は開始せず、許可時は別 Worker を固定する。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    resources = _resources()
    resources["grant"] = resources["grant"].model_copy(update={"isolation": "native"})
    store = _MemoryStore(resources, {"id": "workspace-id", "path": str(workspace),
                                   "device": workspace.stat().st_dev, "inode": workspace.stat().st_ino,
                                   "generation": 0})
    manifest = _manifest()
    manifest.capabilities["native"] = CapabilitySupport(status="supported", reason="test")
    admission = ExecutionAdmission(store, manifest, "sha256:" + "c" * 64)
    request = _request(requirements={"isolation": "native"})
    with pytest.raises(ExecutionError, match="Isolation mode"):
        await admission.resolve("caller-a", _policy(tmp_path), request)
    with pytest.raises(ExecutionError, match="No configured Worker satisfies native"):
        await admission.resolve("caller-a", _policy(tmp_path, places=["host"]), request)


@pytest.mark.asyncio
async def test_admission_pins_artifact_spec_only_when_grant_contains_ref(tmp_path):
    """artifact ref は request と Grant の双方に含まれるときだけ snapshot へ固定する。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    resources = _resources()
    resources["artifact"] = ArtifactCaptureSpec(
        schema_version="1", kind="artifact_spec",
        entries=[ArtifactSpecEntry(name="evidence", path="evidence.bin", media_type="application/octet-stream", required=True, max_bytes=32)],
        max_total_bytes=32,
    )
    resources["grant"] = resources["grant"].model_copy(update={
        "resource_refs": [*resources["grant"].resource_refs, "artifact"]
    })
    request = _request(artifact_spec_ref="artifact")
    snapshot = await ExecutionAdmission(
        _MemoryStore(resources, {"id": "workspace-id", "path": str(workspace),
                                  "device": workspace.stat().st_dev, "inode": workspace.stat().st_ino,
                                  "generation": 0}),
        _manifest(), "sha256:" + "d" * 64,
    ).resolve("caller-a", _policy(tmp_path), request)
    assert snapshot["artifact_spec"]["kind"] == "artifact_spec"


def test_native_caller_permission_requires_explicit_worker_configuration(tmp_path, monkeypatch):
    """local-docker のみが既定 Worker となり、host Client でも native_worker 設定を要求しない。"""
    monkeypatch.setenv("KOTOYOSE_TEST_TOKEN", "test-only")
    monkeypatch.setattr(
        "kotoyose.config._config",
        HarnessConfig(clients={"caller": Client(workspace_roots=[tmp_path], places=["host"])}),
    )
    assert list(default_worker_capabilities()) == ["local-docker"]
    config = NativeExecutionConfig(schema_version="1", image_id="sha256:" + "a" * 64)
    config.validate_client_resources()


def test_worker_requirements_are_typed_and_unknown_fields_are_rejected():
    """OS / GPU / locality の条件を自由形式 metadata として扱わない。"""
    request = _request(requirements={
        "isolation": "sandbox",
        "worker_requirements": {
            "os": "linux", "applications": ["sh"], "gpu": "none",
            "workspace_locality": "local", "native": "forbidden",
        },
    })
    assert request.requirements.worker_requirements.os == "linux"
    with pytest.raises(ValidationError):
        _request(requirements={"worker_requirements": {"os": "linux", "kernel_escape": True}})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "worker_requirements, message",
    [
        ({"os": "windows"}, "os=windows"),
        ({"gpu": "nvidia"}, "gpu=nvidia"),
        ({"applications": ["blender"]}, "application"),
        ({"workspace_locality": "remote"}, "workspace_locality=remote"),
    ],
)
async def test_worker_hard_requirement_mismatch_rejects_before_admission_snapshot(
    tmp_path, worker_requirements, message,
):
    """Local Ubuntu Worker と一致しない条件で worker fallback を行わない。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    resources = _resources()
    store = _MemoryStore(resources, {
        "id": "workspace-id", "path": str(workspace),
        "device": workspace.stat().st_dev, "inode": workspace.stat().st_ino, "generation": 0,
    })
    admission = ExecutionAdmission(store, _manifest(), "sha256:" + "c" * 64)
    request = _request(requirements={"worker_requirements": worker_requirements})
    with pytest.raises(ExecutionError, match=message):
        await admission.resolve("caller-a", _policy(tmp_path), request)


@pytest.mark.asyncio
async def test_worker_capability_is_admin_selected_and_persisted_in_snapshot(tmp_path):
    """管理者設定した Local capability だけを選択結果へ保存する。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    resources = _resources()
    store = _MemoryStore(resources, {
        "id": "workspace-id", "path": str(workspace),
        "device": workspace.stat().st_dev, "inode": workspace.stat().st_ino, "generation": 0,
    })
    worker = WorkerCapabilityConfig(
        worker_id="local-docker", os="linux", applications=[],
        isolation_modes=["sandbox"], workspace_locality="local",
    )
    admission = ExecutionAdmission(
        store, _manifest(), "sha256:" + "c" * 64,
        worker_capabilities={"local-docker": worker},
    )
    request = _request(requirements={"worker_requirements": {"os": "linux"}})
    snapshot = await admission.resolve("caller-a", _policy(tmp_path), request)
    assert snapshot["worker_id"] == "local-docker"
    assert snapshot["worker_capabilities"] == worker.model_dump(mode="json")


def test_unverified_local_worker_capability_and_registry_key_are_rejected(tmp_path):
    """未検証の app / GPU と registry identity の差替えを管理者設定から許可しない。"""
    with pytest.raises(ValueError, match="application inventory"):
        WorkerCapabilityConfig(
            worker_id="local-docker", os="linux", applications=["blender"],
            isolation_modes=["sandbox"],
        )
    with pytest.raises(ValueError, match="Linux local CPU"):
        WorkerCapabilityConfig(
            worker_id="local-docker", os="linux", gpu="nvidia", isolation_modes=["sandbox"],
        )
    with pytest.raises(ValueError, match="Unknown Worker ID"):
        WorkerCapabilityConfig(worker_id="remote-1", os="linux", isolation_modes=["sandbox"])
    worker = WorkerCapabilityConfig(worker_id="local-docker", os="linux", isolation_modes=["sandbox"])
    with pytest.raises(ValueError, match="registry key"):
        NativeExecutionConfig(
            schema_version="1", image_id="sha256:" + "a" * 64,
            worker_capabilities={"wrong-key": worker},
        )


@pytest.mark.asyncio
async def test_remote_worker_requires_admin_shared_binding_before_admission(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    challenge = workspace / ".kotoyose-binding"
    challenge.write_bytes(b"nonce")
    resources = _resources()
    store = _MemoryStore(resources, {
        "id": "workspace-id", "path": str(workspace), "caller_ref": "caller-workspace",
        "device": workspace.stat().st_dev, "inode": workspace.stat().st_ino, "generation": 0,
    })
    ref = RemoteWorkerRef(
        worker_id="remote-1", generation=1, principal="gateway", certificate_fingerprint="a" * 64,
        server_certificate_fingerprint="b" * 64,
    )
    connection = RemoteWorkerConnection(
        worker=ref, endpoint="https://127.0.0.1:9443", ca_file=tmp_path / "ca",
        certificate_file=tmp_path / "client", key_file=tmp_path / "client.key", journal_path=tmp_path / "remote.sqlite",
    )
    worker = WorkerCapabilityConfig(
        worker_id="remote-1", os="linux", isolation_modes=["sandbox"], workspace_locality="remote",
        remote_connection=connection,
        workspace_bindings=[SharedWorkspaceBinding(
            caller_workspace_ref="caller-workspace", remote_workspace_ref="remote-workspace", local_path=workspace,
            challenge_relative=".kotoyose-binding", challenge_digest=hashlib.sha256(b"nonce").hexdigest(),
        )],
    )
    admission = ExecutionAdmission(store, _manifest(), "sha256:" + "c" * 64, worker_capabilities={"remote-1": worker})
    request = _request(requirements={"worker_requirements": {"workspace_locality": "remote"}})
    snapshot = await admission.resolve("caller-a", _policy(tmp_path), request)
    assert snapshot["worker_id"] == "remote-1"
    assert snapshot["remote_binding"]["remote_workspace_ref"] == "remote-workspace"
    challenge.write_bytes(b"changed")
    with pytest.raises(ExecutionError, match="binding"):
        await admission.resolve("caller-a", _policy(tmp_path), request.model_copy(update={"idempotency_key": "remote-key-2"}))
    challenge.unlink()
    outside = tmp_path / "outside-nonce"
    outside.write_bytes(b"nonce")
    challenge.symlink_to(outside)
    with pytest.raises(ExecutionError, match="binding"):
        await admission.resolve("caller-a", _policy(tmp_path), request.model_copy(update={"idempotency_key": "remote-key-3"}))
    challenge.unlink()
    challenge.write_bytes(b"x" * 4097)
    with pytest.raises(ExecutionError, match="binding"):
        await admission.resolve("caller-a", _policy(tmp_path), request.model_copy(update={"idempotency_key": "remote-key-4"}))


@pytest.mark.asyncio
async def test_worker_selection_skips_remote_candidate_without_workspace_binding(tmp_path):
    """先頭 remote の binding 不一致で、後続の適合 Worker を隠さない。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    challenge = workspace / ".kotoyose-binding"
    challenge.write_bytes(b"nonce")

    def remote(worker_id: str, local_path: Path) -> WorkerCapabilityConfig:
        ref = RemoteWorkerRef(
            worker_id=worker_id, generation=1, principal="gateway",
            certificate_fingerprint="a" * 64, server_certificate_fingerprint="b" * 64,
        )
        connection = RemoteWorkerConnection(
            worker=ref, endpoint="https://127.0.0.1:9443", ca_file=tmp_path / "ca",
            certificate_file=tmp_path / "client", key_file=tmp_path / "client.key",
            journal_path=tmp_path / f"{worker_id}.sqlite",
        )
        return WorkerCapabilityConfig(
            worker_id=worker_id, os="linux", isolation_modes=["sandbox"], workspace_locality="remote",
            remote_connection=connection,
            workspace_bindings=[SharedWorkspaceBinding(
                caller_workspace_ref="caller-workspace", remote_workspace_ref=worker_id,
                local_path=local_path, challenge_relative=".kotoyose-binding",
                challenge_digest=hashlib.sha256(b"nonce").hexdigest(),
            )],
        )

    resources = _resources()
    store = _MemoryStore(resources, {
        "id": "workspace-id", "path": str(workspace), "caller_ref": "caller-workspace",
        "device": workspace.stat().st_dev, "inode": workspace.stat().st_ino, "generation": 0,
    })
    admission = ExecutionAdmission(
        store, _manifest(), "sha256:" + "c" * 64,
        worker_capabilities={"remote-1": remote("remote-1", other), "remote-2": remote("remote-2", workspace)},
    )
    snapshot = await admission.resolve(
        "caller-a", _policy(tmp_path),
        _request(requirements={"worker_requirements": {"workspace_locality": "remote"}}),
    )
    assert snapshot["worker_id"] == "remote-2"


def test_machine_config_rejects_legacy_bearer_setting():
    with pytest.raises(ValidationError, match="legacy_bearer") as exc:
        NativeExecutionConfig.model_validate({
            "schema_version": "1", "image_id": "sha256:" + "a" * 64,
            "legacy_bearer": False,
        })
    assert exc.value.errors()[0]["type"] == "extra_forbidden"
