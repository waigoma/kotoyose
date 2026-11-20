#!/usr/bin/env python3
"""Paired Hub/Kotoyose registry + admission fixture, without an Attempt or provider.

Accepts JSON lines {id, method, params}; emits {id, result} or {id, error}.
The only socket permitted is an explicitly acknowledged, initially empty,
loopback PostgreSQL test database. Only this process's owned schema is removed.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import sys
from urllib.parse import unquote, urlsplit
from uuid import uuid4


PRINCIPAL = "pm-selection-pair"
POLICY_REF = "policy:pm-selection-pair"
FORBIDDEN_CALLS = 0


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kotoyose-root", type=Path, required=True)
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--test-database-url-env", required=True)
    parser.add_argument("--acknowledge-empty-test-database", required=True)
    args = parser.parse_args()
    args.kotoyose_root = args.kotoyose_root.resolve(strict=True)
    root = args.workspace_root
    require(root.is_absolute() and root.resolve(strict=True) == root and root.is_dir(), "Workspace root must be a real explicit directory")
    require(root.stat().st_uid == os.getuid() and not root.stat().st_mode & 0o022, "Workspace root must be private to the test owner")
    args.database_url = os.environ[args.test_database_url_env]
    parsed = urlsplit(args.database_url)
    name = unquote(parsed.path.lstrip("/"))
    require(parsed.scheme in {"postgres", "postgresql"} and parsed.hostname in {"127.0.0.1", "::1"}, "Only loopback PostgreSQL is allowed")
    require(not parsed.query and not parsed.fragment, "Database URL overrides are forbidden")
    prefix = "ahs_execution_test_pm_pair_"
    require(name.startswith(prefix) and len(name) > len(prefix) and all(c.isalnum() or c == "_" for c in name),
            "Dedicated test database name with a unique suffix required")
    require(name == args.acknowledge_empty_test_database, "Exact database acknowledgement required")
    args.database_name, args.database_host, args.database_port = name, parsed.hostname, parsed.port or 5432
    return args


def deny(*args, **kwargs):
    global FORBIDDEN_CALLS
    FORBIDDEN_CALLS += 1
    raise AssertionError("Provider execution or unapproved process/network operation is forbidden")


async def deny_async(*args, **kwargs):
    deny()


def install_boundaries(args):
    def audit(event, values):
        if event == "socket.connect":
            address = values[1]
            require(isinstance(address, tuple) and address[0] == args.database_host and address[1] == args.database_port,
                    "Non-test-database socket forbidden")
        elif event in {"subprocess.Popen", "os.posix_spawn", "os.posix_spawnp", "os.system", "os.exec", "os.fork", "os.forkpty"}:
            deny()
    sys.addaudithook(audit)
    # Import no operator configuration or authentication material.
    os.environ["KOTOYOSE_CONFIG"] = os.devnull
    os.environ.pop("KOTOYOSE_COMPATIBILITY_CONFIG", None)
    sys.path.insert(0, str(args.kotoyose_root / "src"))
    import structlog
    structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=sys.stderr))


@asynccontextmanager
async def isolated_store(args, *, allow_disposable_admission=False):
    import asyncpg
    from kotoyose.execution_store import ExecutionStore
    pool = await asyncpg.create_pool(args.database_url, min_size=1, max_size=4, command_timeout=15,
                                    ssl=False, passfile=os.devnull)
    marker = "pm-selection-pair:" + str(uuid4())
    created, keeper = False, None
    try:
        keeper = await pool.acquire()
        require(await keeper.fetchval("SELECT current_database()") == args.database_name, "Wrong database")
        require(await keeper.fetchval("SELECT pg_try_advisory_lock(7168210343510302)"), "Fixture database already owned")
        require(not await keeper.fetchval("SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname='kotoyose')"), "Existing kotoyose schema is forbidden")
        require(not await keeper.fetchval("""
            SELECT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
              WHERE n.nspname NOT IN ('pg_catalog','information_schema')
                AND n.nspname NOT LIKE 'pg_toast%' AND n.nspname NOT LIKE 'pg_temp%'
                AND c.relkind IN ('r','p','v','m','S','f'))
        """), "Database contains existing user relations")
        async with keeper.transaction():
            await keeper.execute("CREATE SCHEMA kotoyose")
            await keeper.execute(f"COMMENT ON SCHEMA kotoyose IS '{marker}'")
        created = True
        store = ExecutionStore(pool)
        await store.initialize()
        if not allow_disposable_admission:
            store.admit = deny_async
        yield store
    finally:
        try:
            if created:
                actual = await keeper.fetchval("SELECT obj_description(oid,'pg_namespace') FROM pg_namespace WHERE nspname='kotoyose'")
                require(actual == marker, "Schema ownership changed; cleanup refused")
                await keeper.execute("DROP SCHEMA kotoyose CASCADE")
        finally:
            if keeper is not None:
                await keeper.execute("SELECT pg_advisory_unlock(7168210343510302)")
                await pool.release(keeper)
            try:
                await asyncio.wait_for(pool.close(), 20)
            except TimeoutError:
                pool.terminate()
                raise


class ForbiddenWorker:
    probe = staticmethod(deny_async)
    execute = staticmethod(deny_async)
    stop = staticmethod(deny_async)


async def no_attempts(store):
    for table in ("execution_attempts", "execution_operations"):
        require(await store.pool.fetchval(f"SELECT count(*) FROM kotoyose.{table}") == 0, "Unexpected execution side effect")


async def run(args):
    import kotoyose.config as registry
    from kotoyose.execution_contract import ExecutionEnvelope
    from kotoyose.execution_resources import (CallerPolicy, NativeExecutionConfig, ResourceEnvelope, WorkspaceRegistration,
        ExecutionError, content_digest, permitted_agent_execution)
    from kotoyose.execution_service import ExecutionService
    from kotoyose.runtime_identity import runtime_identity
    require(Path(registry.__file__).resolve().is_relative_to(args.kotoyose_root / "src"), "Imported another checkout")
    registry._config = registry.HarnessConfig(
        routes={"taskhub-pm": {"driver": "claude.agent-sdk", "model": "cc/claude-sonnet-5-5",
                               "endpoint": "http://127.0.0.1:19999/v1",
                               "credential": "env:NEVER_READ_FIXTURE_TOKEN", "subscription_only": True}},
        models={"taskhub-pm": {"route": "taskhub-pm", "agent": {"place": "isolated", "surface": "none"}}})
    policy = CallerPolicy(workspace_roots=[args.workspace_root], agent_models=["taskhub-pm"],
        provenance=["trusted", "project", "verified_fact", "agent_generated", "external_untrusted", "mixed"],
        max_seconds=600, max_memory_bytes=1024 * 1024 * 1024, max_pids=256, max_cpus=4, max_concurrent=4)
    registry._config = registry._config.model_copy(update={"clients": {PRINCIPAL: registry.Client(
        workspace_roots=[args.workspace_root], models=["taskhub-pm"],
        allowed_provenance=["trusted", "project", "verified_fact", "agent_generated", "external_untrusted", "mixed"],
        max_run_seconds=600, max_memory_bytes=1024 * 1024 * 1024, max_processes=256, max_cpus=4, max_concurrent_runs=4,
    )}})
    settings = NativeExecutionConfig(schema_version="1", image_id="sha256:" + "a" * 64,
                                     drivers={"claude.agent-sdk": {"cli_binary": "/usr/bin/true"}})
    async with isolated_store(args) as store:
        service = ExecutionService(settings, store, ForbiddenWorker())
        service.start = service.dispatch = deny_async
        service._schedule = deny
        snapshots = []
        while line := await asyncio.to_thread(sys.stdin.readline):
            message = json.loads(line)
            ident, method, params = message["id"], message["method"], message.get("params", {})
            try:
                if method == "identity":
                    result = runtime_identity(service, PRINCIPAL)
                elif method == "models":
                    result = {"data": [{"id": alias, "agent_execution": permitted_agent_execution(alias, policy, settings, config=registry._config)}
                                       for alias in registry._config.models]}
                elif method == "registerWorkspace":
                    result = await service.register_workspace_response(PRINCIPAL, policy, WorkspaceRegistration.model_validate(params))
                elif method == "registerResource":
                    resource = ResourceEnvelope.model_validate(params).resource
                    ref = await service.register_resource(PRINCIPAL, policy, resource)
                    result = {"schema_version": "1", "resource_ref": ref}
                elif method == "configureAlias":
                    # Entire fixture-only registry replacement, parsed by real config.
                    registry._config = registry.HarnessConfig.model_validate(params)
                    result = {"configured": True}
                elif method == "validateEnvelope":
                    request = ExecutionEnvelope.model_validate(params).execution
                    plan = await service.admission.resolve(PRINCIPAL, policy, request)
                    snapshots.append(plan)
                    result = {"schema_version": "1", "accepted": True, "attempts": 0,
                              "snapshot_digest": content_digest(plan), "snapshot_index": len(snapshots) - 1,
                              "resolved": {key: plan.get(key) for key in (
                                  "alias", "backend", "provider", "model", "base_url", "workspace_ref", "runtime_billing")}}
                elif method == "snapshot":
                    plan = snapshots[params["index"]]
                    result = {"snapshot_digest": content_digest(plan), "model": plan.get("model")}
                elif method == "stats":
                    result = {"attempts": await store.pool.fetchval("SELECT count(*) FROM kotoyose.execution_attempts"),
                              "operations": await store.pool.fetchval("SELECT count(*) FROM kotoyose.execution_operations"),
                              "forbidden_calls": FORBIDDEN_CALLS}
                elif method == "close":
                    await no_attempts(store)
                    print(json.dumps({"id": ident, "result": {"attempts": 0, "operations": 0}}), flush=True)
                    break
                else:
                    raise ValueError("Unknown fixture operation")
                await no_attempts(store)
                print(json.dumps({"id": ident, "result": result}), flush=True)
            except ExecutionError as exc:
                await no_attempts(store)
                print(json.dumps({"id": ident, "error": {"code": exc.code, "status": exc.status,
                    "reason_code": exc.reason_code, "message": str(exc)}}), flush=True)
            except ValueError as exc:
                await no_attempts(store)
                print(json.dumps({"id": ident, "error": {"code": "invalid_request", "status": 422,
                    "reason_code": None, "message": str(exc)}}), flush=True)
        await no_attempts(store)


if __name__ == "__main__":
    args = arguments()
    install_boundaries(args)
    asyncio.run(run(args))
