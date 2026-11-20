"""Model-free regression for rootfs validation in the real service startup path."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from kotoyose import config as config_module
from kotoyose import execution_service
from kotoyose.codex_rootfs import rootfs_digest
from kotoyose.config import AgentRoute, AliasAgent, AliasAllow, Connection, HarnessConfig, ModelPreset
from kotoyose.execution_resources import NativeExecutionConfig
from kotoyose.execution_service import ExecutionService


def _rootfs(parent, name, *, coding):
    root = parent / name
    root.mkdir(mode=0o755)
    for directory in ("tmp", "home", "runtime", "proc", "dev", "usr", "usr/bin"):
        (root / directory).mkdir(mode=0o755, parents=True, exist_ok=True)
    executables = ["usr/bin/python3"]
    if coding:
        executables += ["bin/sh", "bin/bash", "usr/bin/git", "runtime/codex-resources/bwrap"]
    for relative in executables:
        path = root / relative
        path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        path.write_text("fixture only; never executed\n")
        path.chmod(0o755)
    return root


@pytest.fixture
def startup(tmp_path, monkeypatch):
    """Keep config/resolution/rootfs checks real; replace only external readiness/DB."""
    monkeypatch.setenv("CODEX_STARTUP_TEST_CALLER_TOKEN", "fixture-only")
    binary = tmp_path / "fixture-cli"
    binary.write_text("fixture only; never executed\n")
    binary.chmod(0o755)
    auth = tmp_path / "unused-fixture-auth.json"
    auth.write_text("{}\n")

    def build(root, digest, surfaces):
        names = [f"codex-{surface}" for surface in surfaces]
        connections, routes, models = {}, {}, {}
        for surface, name in zip(surfaces, names, strict=True):
            if surface == "write":
                conn_name = "codex-chatgpt"
                connections[conn_name] = Connection(provider="codex", kind="subscription", data_policy="no_training", auth_file=auth)
            else:
                conn_name = "codex-env"
                connections[conn_name] = Connection(provider="codex", kind="api_key", data_policy="no_training", secret="env:CODEX_STARTUP_TOKEN")
            routes[name] = AgentRoute(driver="codex.app-server", model="gpt-6-luna", connection=conn_name)
            models[name] = ModelPreset(
                route=name,
                agent=AliasAgent(place="isolated", surface=surface),
            )
        from kotoyose.config import Client
        monkeypatch.setattr(config_module, "_config", HarnessConfig(
            connections=connections, routes=routes, models=models,
            clients={"caller": Client(
                workspace_roots=[tmp_path],
                places=["isolated"], models=names,
                allowed_provenance=["trusted", "project"], max_run_seconds=30,
                max_memory_bytes=128 * 1024 * 1024, max_processes=32, max_cpus=1,
                max_concurrent_runs=1,
            )},
        ))
        cells = {}
        for surface in surfaces:
            kind = "coding" if surface == "write" else "standard"
            cells[kind] = {"rootfs": str(root), "rootfs_digest": digest}
        config = NativeExecutionConfig.model_validate({
            "schema_version": "1", "image_id": "sha256:" + "a" * 64,
            "drivers": {"codex.app-server": {
                "cli_binary": str(binary), "cells": cells,
            }},
        })
        lease = SimpleNamespace(fetchval=AsyncMock(return_value=True), execute=AsyncMock())
        store = SimpleNamespace(
            artifact_retention=SimpleNamespace(), workspace_retention=SimpleNamespace(),
            initialize=AsyncMock(), active=AsyncMock(return_value=[]),
            pool=SimpleNamespace(
                acquire=AsyncMock(return_value=lease), release=AsyncMock(), fetch=AsyncMock(return_value=[])
            ),
        )
        worker = SimpleNamespace(probe=AsyncMock(return_value=True), execute=AsyncMock(), stop=AsyncMock())
        service = ExecutionService(config, store, worker)
        monkeypatch.setattr(service.agent_executor.codex_runtime, "launcher_path", binary)
        monkeypatch.setattr(execution_service, "version", Mock(return_value="0.170.0"))
        monkeypatch.setattr(execution_service.shutil, "which", Mock(return_value=str(binary)))
        return service

    return build


async def _assert_ready(service):
    try:
        await service.start()
        assert service._accepting is True
        service.worker.probe.assert_awaited_once()
        service.store.initialize.assert_awaited_once()
        service.store.pool.acquire.assert_awaited_once()
        service.store.active.assert_awaited_once()
        execution_service.version.assert_called_once_with("openai-codex")
        execution_service.shutil.which.assert_called_once_with("bwrap")
        service.worker.execute.assert_not_awaited()
    finally:
        await service.close()
    assert service._accepting is False
    assert service._lease is None
    service.store.pool.release.assert_awaited_once()


async def _assert_rejected(service):
    try:
        with pytest.raises(ValueError, match="invalid_codex_rootfs"):
            await service.start()
        assert service._accepting is False
        assert service._lease is None
        service.worker.probe.assert_not_awaited()
        service.worker.execute.assert_not_awaited()
        service.store.initialize.assert_not_awaited()
        service.store.pool.acquire.assert_not_awaited()
    finally:
        await service.close()


@pytest.mark.parametrize("surface", ["exec", "write"])
async def test_startup_accepts_rootfs_for_its_surface(tmp_path, startup, surface):
    coding = surface == "write"
    root = _rootfs(tmp_path, "rootfs", coding=coding)
    await _assert_ready(startup(root, rootfs_digest(root, coding=coding), (surface,)))


async def test_startup_logs_only_unresolvable_aliases_with_agent_bindings(tmp_path, startup, monkeypatch):
    root = _rootfs(tmp_path, "rootfs", coding=False)
    service = startup(root, rootfs_digest(root), ("exec",))
    config = config_module.get_config()
    config.models["chat-only"] = ModelPreset(route="codex-exec", place="host", allow=AliasAllow(workspace="none"))
    config.models["broken-agent"] = config.models["codex-exec"].model_copy(update={"route": "missing"})
    config.clients["caller"].models.extend(["chat-only", "broken-agent"])
    logger = Mock()
    monkeypatch.setattr(execution_service, "log", logger)

    await _assert_ready(service)

    logger.error.assert_called_once_with("native.agent.unresolvable", alias="broken-agent", reason="不明な route 'missing' です")


async def test_startup_jail_rejects_coding_toolchain(tmp_path, startup):
    root = _rootfs(tmp_path, "rootfs", coding=True)
    await _assert_rejected(startup(root, rootfs_digest(root, coding=True), ("exec",)))


async def test_startup_coding_mode_requires_coding_executables(tmp_path, startup):
    root = _rootfs(tmp_path, "rootfs", coding=False)
    await _assert_rejected(startup(root, rootfs_digest(root, coding=True), ("write",)))


@pytest.mark.parametrize("surface", ["exec", "write"])
@pytest.mark.parametrize("change", ["digest", "content", "forbidden_path"])
async def test_startup_keeps_digest_and_inventory_checks(tmp_path, startup, surface, change):
    coding = surface == "write"
    root = _rootfs(tmp_path, "rootfs", coding=coding)
    digest = rootfs_digest(root, coding=coding)
    if change == "digest":
        digest = "0" * 64
    elif change == "content":
        (root / "usr/bin/python3").write_text("changed after approval\n")
    else:
        (root / "home/auth.json").write_text("forbidden fixture content\n")
    await _assert_rejected(startup(root, digest, (surface,)))


async def test_coding_rootfs_in_the_standard_cell_is_rejected(tmp_path, startup):
    """standard cell に coding rootfs を置くと、jail の coding=False 検査が拒む。"""
    root = _rootfs(tmp_path, "rootfs", coding=True)
    await _assert_rejected(startup(root, rootfs_digest(root, coding=True), ("exec", "write")))


async def test_each_cell_validates_its_own_rootfs(tmp_path, startup, monkeypatch):
    """jail は standard を coding=False で、coding は coding=True で検査する。"""
    from kotoyose import codex_rootfs

    standard = _rootfs(tmp_path, "standard", coding=False)
    coding = _rootfs(tmp_path, "coding", coding=True)
    seen = []
    real = codex_rootfs.validate_rootfs

    def spy(root, digest, *, coding=False):
        seen.append((Path(root), coding))
        return real(root, digest, coding=coding)

    monkeypatch.setattr(codex_rootfs, "validate_rootfs", spy)
    service = startup(coding, rootfs_digest(coding, coding=True), ("write",))
    service.config.drivers["codex.app-server"].cells["standard"] = type(
        service.config.drivers["codex.app-server"].cells["coding"],
    )(rootfs=standard, rootfs_digest=rootfs_digest(standard, coding=False))
    names = ["codex-exec", "codex-write"]
    connections, routes, models = {}, {}, {}
    from kotoyose.config import AgentRoute, AliasAgent, Client, Connection, HarnessConfig, ModelPreset
    from kotoyose import config as config_module

    auth = tmp_path / "unused-fixture-auth.json"
    connections["codex-env"] = Connection(provider="codex", kind="api_key", data_policy="no_training", secret="env:CODEX_STARTUP_TOKEN")
    connections["codex-chatgpt"] = Connection(provider="codex", kind="subscription", data_policy="no_training", auth_file=auth)
    for surface, name in (("exec", names[0]), ("write", names[1])):
        conn_name = "codex-env" if surface == "exec" else "codex-chatgpt"
        routes[name] = AgentRoute(driver="codex.app-server", model="gpt-6-luna", connection=conn_name)
        models[name] = ModelPreset(
            route=name,
            agent=AliasAgent(place="isolated", surface=surface),
        )
    monkeypatch.setattr(config_module, "_config", HarnessConfig(
        connections=connections, routes=routes, models=models,
        clients={"caller": Client(
            workspace_roots=[tmp_path],
            places=["isolated"], models=names,
            allowed_provenance=["trusted", "project"], max_run_seconds=30,
            max_memory_bytes=128 * 1024 * 1024, max_processes=32, max_cpus=1,
            max_concurrent_runs=1,
        )},
    ))
    await _assert_ready(service)
    assert (standard, False) in seen
    assert (coding, True) in seen
