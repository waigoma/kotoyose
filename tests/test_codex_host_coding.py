"""Model-free host-coding boundary tests. Real jail/cgroup tests are separate host gates."""

import asyncio
from collections import deque
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from kotoyose import native_codex
from kotoyose.codex_coding_policy import HOST_MODE, HOST_TOOL_REF, DEFAULT_CODEX_MODE, TOOL_POLICY_DIGEST, validate_mode, validate_workspace
from kotoyose.codex_model_policy import model_catalog
from kotoyose.codex_rootfs import rootfs_digest, validate_rootfs
from kotoyose.execution_resources import NativeRuntimeProfile
from kotoyose.native_claude import NativeAgentJob
from kotoyose.native_codex import NativeCodexWorker
from kotoyose.provider_jail import build_jail_command, provider_environment


def job(tmp_path, **overrides):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    info = workspace.stat()
    values = dict(attempt_id="attempt-1", model="gpt-6-luna", cli_binary=Path("/usr/bin/false"),
                  credential_name="CODEX_CHATGPT_AUTH", credential_value="", codex_auth_file=tmp_path / "fake-auth.json",
                  codex_auth_identity="fixture", instructions="Original instructions.", prompt="Original task.",
                  output_contract="text", max_turns=8, timeout_seconds=1, grace_seconds=.05, output_limit_bytes=65536,
                  tool_refs=(HOST_TOOL_REF,), codex_execution_mode=HOST_MODE, codex_tool_policy_digest=TOOL_POLICY_DIGEST,
                  workspace=workspace, workspace_device=info.st_dev, workspace_inode=info.st_ino,
                  record_codex_stop=AsyncMock(), generation=7, memory_bytes=128 * 1024 * 1024, pids_limit=32, cpus=.5)
    values.update(overrides)
    return NativeAgentJob(**values)


def profile(**overrides):
    values = dict(provider="codex", model="gpt-6-luna", cli_binary="/usr/bin/false", credential_mode="chatgpt_subscription",
                  credential_name="CODEX_CHATGPT_AUTH", codex_auth_file="/fixture-auth.json")
    values.update(overrides)
    return NativeRuntimeProfile(**values)


def test_mode_is_explicit_and_legacy_profile_serialization_unchanged():
    assert "codex_execution_mode" not in profile().model_dump()
    assert profile(codex_execution_mode=HOST_MODE).model_dump()["codex_execution_mode"] == HOST_MODE
    assert validate_mode(DEFAULT_CODEX_MODE) is False
    assert validate_mode(HOST_MODE) is True
    with pytest.raises(ValueError):
        validate_mode("future-or-unknown")
    with pytest.raises(ValueError):
        profile(codex_execution_mode=HOST_MODE, credential_mode="environment", credential_name="OPENAI_API_KEY",
                credential_env="FIXTURE", codex_auth_file=None)


def test_workspace_cannot_expand_or_change_identity(tmp_path):
    target = tmp_path / "one"
    target.mkdir()
    assert validate_workspace(target) == target
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)
    for bad in (Path("/"), Path("relative"), alias, target / ".."):
        with pytest.raises(ValueError):
            validate_workspace(bad)
    with pytest.raises(ValueError):
        validate_workspace(target, inode=target.stat().st_ino + 1)


def reviewed_tree(tmp_path, coding):
    root = tmp_path / "rootfs"
    root.mkdir(mode=0o755)
    for name in ("tmp", "home", "runtime", "proc", "dev", "usr", "usr/bin"):
        (root / name).mkdir(mode=0o755, parents=True, exist_ok=True)
    paths = ["usr/bin/python3"]
    if coding:
        paths += ["bin/sh", "bin/bash", "usr/bin/git", "runtime/codex-resources/bwrap"]
    for relative in paths:
        path = root / relative
        path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        path.write_text("fixture-only, never executed\n")
        path.chmod(0o755)
    return root


def test_reviewed_coding_toolchain_never_relaxes_legacy_rootfs(tmp_path):
    root = reviewed_tree(tmp_path, True)
    with pytest.raises(ValueError):
        rootfs_digest(root)
    digest = rootfs_digest(root, coding=True)
    assert validate_rootfs(root, digest, coding=True) == root
    (root / "usr/bin/git").write_text("changed")
    with pytest.raises(ValueError):
        validate_rootfs(root, digest, coding=True)
    (root / "home/auth.json").write_text("forbidden")
    with pytest.raises(ValueError):
        rootfs_digest(root, coding=True)


def test_only_one_workspace_is_mounted_and_no_auth_or_home(tmp_path, monkeypatch):
    root = reviewed_tree(tmp_path, True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sock = tmp_path / "broker.sock"
    # Command construction only. No socket creation or mount is attempted.
    monkeypatch.setattr(Path, "is_socket", lambda self: self == sock)
    catalog = tmp_path / "models.json"
    catalog.write_text("{}")
    command = build_jail_command(Path("/usr/bin/false"), ["app-server"], provider="codex", rootfs=root,
                                 egress_socket=sock, model_catalog=catalog, workspace=workspace)
    binds = [command[i + 1:i + 3] for i, item in enumerate(command) if item == "--bind"]
    assert binds == [[str(workspace), "/workspace"]]
    assert "--share-net" not in command
    assert str(Path.home()) not in command and "auth.json" not in str(command)
    assert [str(root / "runtime/codex-resources/bwrap"), "/runtime/codex-resources/bwrap"] in [
        command[i + 1:i + 3] for i, item in enumerate(command) if item == "--ro-bind"]
    legacy = build_jail_command(Path("/usr/bin/false"), [], provider="codex", rootfs=root,
                                egress_socket=sock, model_catalog=catalog)
    assert "--bind" not in legacy and "/workspace" not in legacy


def test_catalog_and_environment_have_only_reviewed_builtin_authority():
    old = model_catalog("gpt-6-luna")["models"][0]
    new = model_catalog("gpt-6-luna", coding=True)["models"][0]
    assert old["shell_type"] == "disabled" and old["apply_patch_tool_type"] is None
    assert new["shell_type"] == "unified_exec" and new["apply_patch_tool_type"] == "freeform"
    assert {k: v for k, v in new.items() if k not in {"shell_type", "apply_patch_tool_type"}} == {
        k: v for k, v in old.items() if k not in {"shell_type", "apply_patch_tool_type"}}
    env = provider_environment({"KOTOYOSE_CODEX_BROKER_CAPABILITY": "fake-capability", "HOME": "/host", "GH_TOKEN": "secret"}, provider="codex")
    assert env["HOME"] == "/home/kotoyose" and "GH_TOKEN" not in env and "OPENAI_API_KEY" not in env


class ScriptedRPC:
    def __init__(self, events):
        self.pending = deque(events)
        # wire を使わないモデル単体fixture。bytes境界は _Protocol の試験で検証する。
        self.pending_bytes = 0
        self.frame_bytes = 0
        self.limit = 65536
        self.image_uris = ()
        self.turn_id = None
        self.thread_id = None
        self.sequence = 10
        self.sent = []
        self.requests = []

    async def request(self, method, params):
        self.requests.append((method, params))
        if method == "thread/start":
            return {"thread": {"id": "thread"}, "model": "gpt-6-luna", "modelProvider": "kotoyose_native"}
        if method == "turn/start":
            return {"turn": {"id": "turn"}}
        return {}

    async def send(self, value):
        self.sent.append(value)

    async def next(self):
        if self.pending:
            return self.pending.popleft()
        await asyncio.Future()


def event(method, **params):
    return {"method": method, "params": {"threadId": "thread", "turnId": "turn", **params}}


def answer():
    return event("item/completed", item={"type": "agentMessage", "id": "answer", "text": "repaired", "phase": "final_answer"})


async def test_builtin_failure_is_observed_then_sdk_can_repair_without_docker(tmp_path):
    events = [event("item/started", item={"type": "commandExecution", "id": "cmd", "status": "inProgress"}),
              event("item/completed", item={"type": "commandExecution", "id": "cmd", "status": "failed", "exitCode": 1}),
              answer(), event("turn/completed", turn={"id": "turn", "status": "completed"})]
    rpc = ScriptedRPC(events)
    observations = []
    callback = AsyncMock(side_effect=AssertionError("No CommandExecution in host coding"))
    result = await NativeCodexWorker()._run(rpc, job(tmp_path, progress_event=observations.append), asyncio.Event(), callback, {}, {}, {})
    assert result == b"repaired"
    callback.assert_not_awaited()
    assert observations[0].summary == "failed"
    params = dict(rpc.requests)["thread/start"]
    assert params["sandbox"] == "workspace-write" and params["cwd"] == "/workspace"
    assert params["approvalPolicy"] == "never" and params["ephemeral"] is True
    assert "environments" not in params and params["dynamicTools"] == []
    assert params["developerInstructions"] == "Original instructions."


async def test_unknown_approval_is_never_accepted(tmp_path):
    rpc = ScriptedRPC([{"id": "approval", "method": "item/commandExecution/requestApproval",
                        "params": {"threadId": "thread", "turnId": "turn"}}])
    with pytest.raises(ValueError, match="tool_denied"):
        await NativeCodexWorker()._run(rpc, job(tmp_path), asyncio.Event(), AsyncMock(), {}, {}, {})
    assert rpc.sent[-1]["error"]["message"] == "tool_denied" and "result" not in rpc.sent[-1]


@pytest.mark.parametrize("refs", [(), ("builtin:sandbox-exec@1",), (HOST_TOOL_REF, "builtin:sandbox-exec@1")])
async def test_wrong_or_missing_host_ref_never_launches(tmp_path, monkeypatch, refs):
    launch = AsyncMock(side_effect=AssertionError("must reject before process launch"))
    monkeypatch.setattr(native_codex.asyncio, "create_subprocess_exec", launch)
    result = await NativeCodexWorker().execute(job(tmp_path, tool_refs=refs), asyncio.Event(), AsyncMock(), on_process_started=AsyncMock())
    assert result.status == "failed" and result.reason == "invalid_job"
    launch.assert_not_awaited()


class FakeProcess:
    def __init__(self):
        self.pid = 424242
        self.returncode = None
        self.stdin = Mock()
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.killed = False

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        return self.returncode or 0


