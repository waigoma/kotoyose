"""Postgres に Attempt、workspace writer、event、artifact を原子的に保存する。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Mapping, Sequence
from uuid import uuid4

import asyncpg

from .execution_resources import ExecutionError, Resource, canonical_json, content_digest, RESOURCE_ADAPTER
from .execution_artifacts import CapturedArtifact
from .artifact_file import ArtifactFile
from .local_artifact_store import LocalArtifactStore
from .artifact_blob_store import ArtifactBlob, ArtifactBlobRegistry, ArtifactStorageError
from .input_artifacts import (
    InputArtifactLimits,
    InputArtifactUpload,
)
from .input_artifact_store import InputArtifactRepository
from .client_tool_bridge import ClientToolRepository
from .artifact_retention_store import ArtifactRetentionRepository
from .workspace_retention_store import WorkspaceRetentionRepository
from .model_pool_store import ModelPoolStore
from .model_pools import ModelPools
from .runtime_identity import initialize_runtime_identity
from .workspace_identity import parse_workspace_ref
from .workspace_registration import reject_workspace_overlap
from .execution_operation_guard import authorize_operation
from .mcp_operation_store import McpOperationRepository
from .checkpoint_store import CheckpointRepository, reject_reserved_restore_path
from .agent_spec import mode_requests_room, room_execution_modes
from .execution_contract import ResumeManifest
from .agent_execution_schema import initialize_agent_execution_schema
from .execution_limits import LimitsRepository

if TYPE_CHECKING:
    from .agent_execution_runner import AgentExecutionAdmission
from .execution_resume import unsafe
from .execution_resume_store import lock_predecessor, verify_workspace
from .checkpoint_contract import CHECKPOINT_RETENTION_LOCK
from .admission_quarantine import count_admission_reservations, initialize_admission_quarantine
from .admission_release import initialize_admission_releases


_SCHEMA = """
CREATE TABLE IF NOT EXISTS kotoyose.execution_cancellations (
    principal text NOT NULL, idempotency_key text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (principal, idempotency_key)
);
CREATE TABLE IF NOT EXISTS kotoyose.execution_resources (
    principal text NOT NULL, ref text NOT NULL, payload jsonb NOT NULL,
    revoked_at timestamptz, created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (principal, ref)
);
CREATE UNIQUE INDEX IF NOT EXISTS execution_grant_version_identity
    ON kotoyose.execution_resources(principal,(payload->>'grant_id'),(payload->>'version'))
    WHERE payload->>'kind'='grant';
CREATE TABLE IF NOT EXISTS kotoyose.execution_workspaces (
    id text PRIMARY KEY, principal text NOT NULL, caller_ref text NOT NULL,
    path text NOT NULL UNIQUE, device bigint NOT NULL, inode bigint NOT NULL,
    generation bigint NOT NULL DEFAULT 0, writer_attempt text,
    UNIQUE (principal, caller_ref)
);
CREATE TABLE IF NOT EXISTS kotoyose.execution_attempts (
    id text PRIMARY KEY, principal text NOT NULL, idempotency_key text NOT NULL,
    request_digest text NOT NULL, request jsonb NOT NULL, snapshot jsonb NOT NULL,
    workspace_id text NOT NULL REFERENCES kotoyose.execution_workspaces(id),
    generation bigint NOT NULL, status text NOT NULL, reason text,
    stop_confirmation text NOT NULL DEFAULT 'unconfirmed',
    side_effect_resolution text NOT NULL DEFAULT 'unresolved',
    exit_code integer, sequence bigint NOT NULL DEFAULT 0,
    usage jsonb NOT NULL DEFAULT '{"source":"unknown","cost":null,"input_tokens":null,"output_tokens":null}',
    artifact_manifest jsonb NOT NULL DEFAULT '[]', required_action jsonb,
    created_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz,
    UNIQUE (principal, idempotency_key)
);
ALTER TABLE kotoyose.execution_attempts ADD COLUMN IF NOT EXISTS usage jsonb NOT NULL
    DEFAULT '{"source":"unknown","cost":null,"input_tokens":null,"output_tokens":null}';
ALTER TABLE kotoyose.execution_attempts ADD COLUMN IF NOT EXISTS artifact_manifest jsonb NOT NULL DEFAULT '[]';
ALTER TABLE kotoyose.execution_attempts ADD COLUMN IF NOT EXISTS required_action jsonb;
ALTER TABLE kotoyose.execution_attempts ADD COLUMN IF NOT EXISTS api_operation text NOT NULL DEFAULT 'dispatch';
ALTER TABLE kotoyose.execution_attempts ADD COLUMN IF NOT EXISTS resumed_by text;
ALTER TABLE kotoyose.execution_attempts ADD COLUMN IF NOT EXISTS provider_session_ref text;
ALTER TABLE kotoyose.execution_attempts DROP CONSTRAINT IF EXISTS execution_attempts_principal_idempotency_key_key;
CREATE UNIQUE INDEX IF NOT EXISTS execution_attempt_request_identity
    ON kotoyose.execution_attempts(principal,api_operation,idempotency_key);
CREATE TABLE IF NOT EXISTS kotoyose.execution_events (
    attempt_id text NOT NULL REFERENCES kotoyose.execution_attempts(id),
    sequence bigint NOT NULL, type text NOT NULL, payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (attempt_id, sequence)
);
CREATE TABLE IF NOT EXISTS kotoyose.execution_artifacts (
    ref text PRIMARY KEY, attempt_id text NOT NULL REFERENCES kotoyose.execution_attempts(id),
    name text NOT NULL, digest text NOT NULL, content bytea NOT NULL,
    media_type text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (attempt_id, name)
);
ALTER TABLE kotoyose.execution_artifacts ALTER COLUMN content DROP NOT NULL;
ALTER TABLE kotoyose.execution_artifacts ADD COLUMN IF NOT EXISTS storage_backend text NOT NULL DEFAULT 'postgres';
ALTER TABLE kotoyose.execution_artifacts ADD COLUMN IF NOT EXISTS storage_identity text;
ALTER TABLE kotoyose.execution_artifacts ADD COLUMN IF NOT EXISTS storage_key text;
ALTER TABLE kotoyose.execution_artifacts ADD COLUMN IF NOT EXISTS byte_size bigint;
CREATE TABLE IF NOT EXISTS kotoyose.execution_operations (
    id text PRIMARY KEY, attempt_id text NOT NULL REFERENCES kotoyose.execution_attempts(id),
    kind text NOT NULL, state text NOT NULL, result jsonb,
    created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS kotoyose.execution_input_artifacts (
    ref text PRIMARY KEY, principal text NOT NULL, digest text NOT NULL,
    content bytea, media_type text NOT NULL, byte_size bigint NOT NULL,
    storage_backend text NOT NULL DEFAULT 'postgres', storage_identity text,
    storage_key text, created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL, retention_pin boolean NOT NULL DEFAULT false,
    state text NOT NULL DEFAULT 'active', pin_count integer NOT NULL DEFAULT 0,
    deleted_at timestamptz, tombstone_reason text
);
ALTER TABLE kotoyose.execution_input_artifacts
    DROP CONSTRAINT IF EXISTS execution_input_artifacts_principal_digest_key;
ALTER TABLE kotoyose.execution_input_artifacts ADD COLUMN IF NOT EXISTS state text NOT NULL DEFAULT 'active';
ALTER TABLE kotoyose.execution_input_artifacts ADD COLUMN IF NOT EXISTS pin_count integer NOT NULL DEFAULT 0;
ALTER TABLE kotoyose.execution_input_artifacts ADD COLUMN IF NOT EXISTS deleted_at timestamptz;
ALTER TABLE kotoyose.execution_input_artifacts ADD COLUMN IF NOT EXISTS tombstone_reason text;
CREATE TABLE IF NOT EXISTS kotoyose.execution_attempt_input_artifacts (
    attempt_id text NOT NULL REFERENCES kotoyose.execution_attempts(id),
    artifact_ref text NOT NULL REFERENCES kotoyose.execution_input_artifacts(ref),
    principal text NOT NULL, released_at timestamptz,
    PRIMARY KEY (attempt_id, artifact_ref)
);
"""


async def count_unfinished_agent_rooms(conn) -> int:
    """終了していない agent container の attempt を数える。container が立つ前の連打も枠に入れる。"""
    return int(await conn.fetchval(
        "SELECT count(*) FROM kotoyose.execution_attempts "
        "WHERE finished_at IS NULL AND snapshot->>'codex_execution_mode' = ANY($1::text[])",
        sorted(room_execution_modes()),
    ) or 0)


def _decoded(row: asyncpg.Record) -> dict:
    result = dict(row)
    for key in ("request", "snapshot", "payload", "result", "usage", "artifact_manifest"):
        if isinstance(result.get(key), str):
            result[key] = json.loads(result[key])
    return result


class ExecutionStore:
    """No-op fallback を持たない実行台帳。全書き込みは caller と世代で限定する。"""

    def __init__(self, pool: asyncpg.Pool, artifacts: ArtifactBlobRegistry | None = None):
        self.pool = pool
        self.limits = LimitsRepository(pool)
        self.runtime_store_id: str | None = None
        self.artifact_storage = artifacts or ArtifactBlobRegistry("postgres", {})
        self.input_artifacts = InputArtifactRepository(self.pool, self.artifact_storage)
        self.client_tools = ClientToolRepository(self.pool, self._event)
        self.artifact_retention = ArtifactRetentionRepository(self.pool, self.artifact_storage)
        self.mcp_calls = McpOperationRepository(self.pool, self._event)
        self.checkpoints = CheckpointRepository(self)
        self.workspace_retention = WorkspaceRetentionRepository(self)
        self.model_pools = ModelPools(ModelPoolStore(pool))
        # ExecutionService が agent_container 設定のあるときだけ入れる。無ければ部屋の枠は見ない。
        self.agent_room_capacity = None

    async def initialize(self) -> None:
        """既存 kotoyose schema に独立した実行台帳を追加する。"""
        await self.pool.execute(_SCHEMA)
        await self.limits.initialize()
        await initialize_admission_quarantine(self.pool)
        await initialize_admission_releases(self.pool)
        self.runtime_store_id = await initialize_runtime_identity(self.pool)
        await self.client_tools.initialize()
        await self.artifact_retention.initialize()
        await self.mcp_calls.initialize()
        await self.checkpoints.initialize()
        await self.workspace_retention.initialize()
        await self.model_pools.store.initialize()
        await initialize_agent_execution_schema(self.pool)

    async def put_resource(self, principal: str, resource: Resource) -> str:
        """同内容は同じ ref に収束し、保存済み内容や取消状態を上書きしない。"""
        payload = resource.model_dump(mode="json")
        ref = "resource:sha256:" + content_digest(payload)
        inserted = await self.pool.fetchval(
            "INSERT INTO kotoyose.execution_resources(principal,ref,payload) VALUES($1,$2,$3::jsonb) "
            "ON CONFLICT DO NOTHING RETURNING ref", principal, ref, canonical_json(payload),
        )
        if inserted is None and not await self.pool.fetchval(
            "SELECT EXISTS(SELECT 1 FROM kotoyose.execution_resources WHERE principal=$1 AND ref=$2)", principal, ref,
        ):
            raise ExecutionError("request_conflict", "Grant version already has different immutable content", 409)
        return ref

    async def put_input_artifact(
        self, principal: str, upload: InputArtifactUpload, limits: InputArtifactLimits,
    ) -> dict:
        """input repository へ処理を委譲する。"""
        self.input_artifacts.artifact_storage = self.artifact_storage
        return await self.input_artifacts.put_input_artifact(principal, upload, limits)

    async def gc_expired_input_artifacts(self, *, limit: int = 100) -> int:
        """期限切れ input の bounded GC を委譲する。"""
        self.input_artifacts.artifact_storage = self.artifact_storage
        return await self.input_artifacts.gc_expired_input_artifacts(limit=limit)

    async def reconcile_input_artifacts(self, *, limit: int = 100) -> int:
        """input の pending / deleting 状態の recovery を委譲する。"""
        self.input_artifacts.artifact_storage = self.artifact_storage
        return await self.input_artifacts.reconcile_input_artifacts(limit=limit)

    async def resource(self, principal: str, ref: str) -> Resource:
        """他 principal の ref や取消済み Grant を解決しない。"""
        row = await self.pool.fetchrow(
            "SELECT payload,revoked_at FROM kotoyose.execution_resources WHERE principal=$1 AND ref=$2",
            principal, ref,
        )
        if row is None:
            raise ExecutionError("invalid_request", "Unknown resource reference", 404)
        if row["revoked_at"] is not None:
            raise ExecutionError("permission_denied", "Resource was revoked", 403)
        return RESOURCE_ADAPTER.validate_json(row["payload"])

    async def revoke_grant(self, principal: str, ref: str) -> list[str]:
        """取消記録と対象 Attempt の取消要求を一つの transaction に保存する。"""
        async with self.pool.acquire() as conn, conn.transaction():
            changed = await conn.fetchval(
                "UPDATE kotoyose.execution_resources SET revoked_at=COALESCE(revoked_at,now()) "
                "WHERE principal=$1 AND ref=$2 AND payload->>'kind'='grant' RETURNING ref", principal, ref,
            )
            if changed is None:
                raise ExecutionError("invalid_request", "Unknown grant", 404)
            rows = await conn.fetch(
                "SELECT id FROM kotoyose.execution_attempts WHERE principal=$1 "
                "AND request->>'execution_grant_ref'=$2 AND finished_at IS NULL FOR UPDATE", principal, ref,
            )
            for row in rows:
                await self._cancel(conn, row["id"], "policy_revoked")
        return [row["id"] for row in rows]

    async def register_workspace(self, principal: str, caller_ref: str, path: Path) -> dict:
        """同じ実体・親子ディレクトリを二重の可変 workspace に登録しない。"""
        info = path.stat()
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('kotoyose-workspaces',0))")
            await reject_reserved_restore_path(conn, str(path))
            prior = await conn.fetchrow(
                "SELECT * FROM kotoyose.execution_workspaces WHERE principal=$1 AND caller_ref=$2 FOR UPDATE",
                principal, caller_ref,
            )
            if prior is not None:
                if prior["lifecycle_state"] != "active":
                    raise ExecutionError("request_conflict", "Workspace deletion has already started", 409)
                if (prior["path"], prior["device"], prior["inode"]) != (str(path), info.st_dev, info.st_ino):
                    raise ExecutionError("request_conflict", "Workspace registration changed", 409)
                return _decoded(prior)
            await reject_workspace_overlap(conn, path, info)
            row = await conn.fetchrow(
                "INSERT INTO kotoyose.execution_workspaces(id,principal,caller_ref,path,device,inode) "
                "VALUES($1,$2,$3,$4,$5,$6) RETURNING *",
                str(uuid4()), principal, caller_ref, str(path), info.st_dev, info.st_ino,
            )
            await self.workspace_retention.register(conn, row["id"])
            return _decoded(row)

    async def workspace(self, principal: str, ref: str) -> dict:
        """Workspace ref の世代と所有を照合する。writer の獲得は admission で行う。"""
        workspace_id, generation_number = parse_workspace_ref(ref)
        row = await self.pool.fetchrow(
            "SELECT * FROM kotoyose.execution_workspaces WHERE id=$1 AND principal=$2",
            workspace_id, principal,
        )
        if row is None:
            raise ExecutionError("invalid_request", "Unknown workspace", 404)
        if row["lifecycle_state"] != "active":
            raise ExecutionError("request_conflict", "Workspace deletion has already started", 409)
        if row["generation"] != generation_number:
            raise ExecutionError("request_conflict", "Workspace generation is stale", 409)
        return _decoded(row)

    async def replay(self, principal: str, key: str, digest: str, *, operation: str = "dispatch") -> dict | None:
        """期限切れや終端後でも、既に受理した同一 dispatch の受理証を返す。"""
        row = await self.pool.fetchrow(
            "SELECT * FROM kotoyose.execution_attempts WHERE principal=$1 AND idempotency_key=$2 AND api_operation=$3",
            principal, key, operation,
        )
        if row is not None and row["request_digest"] != digest:
            raise ExecutionError("request_conflict", "Idempotency key has a different request", 409)
        return _decoded(row) if row is not None else None

    async def admit(self, principal: str, request: dict, snapshot: dict, max_concurrent: int, *, agent_execution: AgentExecutionAdmission | None = None) -> tuple[dict, bool]:
        """排他・取消・期限を再照合し、dispatch intent と最初の event を同時に保存する。"""
        resume = ResumeManifest.model_validate(snapshot["resume_manifest"]) if "resume_manifest" in snapshot else None
        operation = "agent_activation" if agent_execution is not None else "resume" if resume else "dispatch"
        digest = resume.request_digest if resume else content_digest(request)
        async with self.pool.acquire() as conn, conn.transaction():
            # principal 単位で予約数と key を直列化し、別 workspace の過剰 admission も防ぐ。
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))", "kotoyose-caller:" + principal)
            if agent_execution is not None:
                await agent_execution.check(conn)
            prior = await conn.fetchrow(
                "SELECT * FROM kotoyose.execution_attempts WHERE principal=$1 AND idempotency_key=$2 AND api_operation=$3",
                principal, request["idempotency_key"], operation,
            )
            if prior is not None:
                if prior["request_digest"] != digest:
                    raise ExecutionError("request_conflict", "Idempotency key has a different request", 409)
                return _decoded(prior), False
            if operation == "dispatch" and await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM kotoyose.execution_cancellations "
                "WHERE principal=$1 AND idempotency_key=$2)", principal, request["idempotency_key"],
            ):
                raise ExecutionError("request_conflict", "Dispatch admission was cancelled", 409)
            if resume and resume.checkpoint_ref:
                await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))", CHECKPOINT_RETENTION_LOCK)
            grant = await conn.fetchrow(
                "SELECT payload,revoked_at,(payload->>'expires_at')::timestamptz > now() "
                "AND (payload->>'not_before')::timestamptz <= now() AS valid "
                "FROM kotoyose.execution_resources WHERE principal=$1 AND ref=$2 FOR UPDATE",
                principal, request["execution_grant_ref"],
            )
            if grant is None or grant["revoked_at"] is not None or (agent_execution is None and not grant["valid"]):
                if resume:
                    raise unsafe("grant_mismatch")
                raise ExecutionError("permission_denied", "Grant is no longer valid", 403)
            if resume:
                if (resume.principal != principal or content_digest(json.loads(grant["payload"])) != resume.grant_digest
                        or (agent_execution is None and datetime.fromisoformat(snapshot["deadline"]) <= datetime.now(timezone.utc))):
                    raise unsafe("grant_mismatch")
                await lock_predecessor(conn, resume)
            workspace = await conn.fetchrow(
                "SELECT * FROM kotoyose.execution_workspaces WHERE id=$1 AND principal=$2 FOR UPDATE",
                snapshot["workspace_id"], principal,
            )
            if workspace is None or workspace["lifecycle_state"] != "active" or workspace["generation"] != snapshot["generation"] or workspace["writer_attempt"]:
                if resume:
                    raise unsafe("workspace_mismatch")
                raise ExecutionError("request_conflict", "Workspace writer or generation changed", 409)
            if resume:
                await verify_workspace(conn, resume, workspace)
            active = await count_admission_reservations(conn, principal)
            if active >= max_concurrent:
                # 満杯は「待てば通る」断り。呼び手が待ちとして扱えるよう、他の食い違い (409) と分けて返す。
                raise ExecutionError("capacity_exhausted", "Caller concurrency reservation is full", 429,
                                     reason_code="caller_concurrency")
            if getattr(self, "agent_room_capacity", None) is not None and mode_requests_room(snapshot.get("codex_execution_mode")):
                # 新しい attempt 行と container より前に断る。再開も同じ admit を通る。
                await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))", "kotoyose-agent-containers")
                await self.agent_room_capacity(
                    workspace["path"], reserved=await count_unfinished_agent_rooms(conn),
                )
            attempt_id = str(uuid4())
            await conn.execute(
                "INSERT INTO kotoyose.execution_attempts(id,principal,idempotency_key,request_digest,request,"
                "snapshot,workspace_id,generation,status,api_operation) VALUES($1,$2,$3,$4,$5::jsonb,$6::jsonb,$7,$8,'dispatched',$9)",
                attempt_id, principal, request["idempotency_key"], digest, canonical_json(request),
                canonical_json(snapshot), workspace["id"], workspace["generation"], operation,
            )
            if request.get("limits_mode") == "agent_work_v1":
                await conn.execute(
                    "UPDATE kotoyose.execution_attempts SET limits_state='not_started',limits_active_ms=0,"
                    "limits_observed_at=now(),limits_sequence=1 WHERE id=$1", attempt_id,
                )
            if resume:
                await conn.execute("UPDATE kotoyose.execution_attempts SET resumed_by=$2 WHERE id=$1", resume.predecessor_attempt_id, attempt_id)
                await conn.execute("UPDATE kotoyose.execution_client_connections SET state='revoked' WHERE attempt_id=$1 AND state='active'", resume.predecessor_attempt_id)
            input_refs = sorted({
                item.get("content_ref") for item in request.get("inputs", [])
                if isinstance(item, dict) and str(item.get("content_ref", "")).startswith("artifact:input:")
            })
            for artifact_ref in input_refs:
                artifact = await conn.fetchrow(
                    "SELECT ref,state,expires_at FROM kotoyose.execution_input_artifacts "
                    "WHERE ref=$1 AND principal=$2 FOR UPDATE", artifact_ref, principal,
                )
                pinned = (await conn.fetchval('''SELECT EXISTS(SELECT 1 FROM kotoyose.agent_execution_input_pins
                    WHERE execution_id=$1 AND artifact_ref=$2 AND principal=$3 AND released_at IS NULL)''',
                    agent_execution.execution_id, artifact_ref, principal)) if agent_execution is not None else False
                if (artifact is None or artifact["state"] != "active"
                        or (agent_execution is not None and not pinned)
                        or (agent_execution is None and artifact["expires_at"] <= datetime.now(timezone.utc))):
                    raise ExecutionError("permission_denied", "Input artifact is unavailable for this Attempt", 403)
                await conn.execute(
                    "INSERT INTO kotoyose.execution_attempt_input_artifacts(attempt_id,artifact_ref,principal) "
                    "VALUES($1,$2,$3)", attempt_id, artifact_ref, principal,
                )
                await conn.execute(
                    "UPDATE kotoyose.execution_input_artifacts SET pin_count=pin_count+1 WHERE ref=$1", artifact_ref,
                )
            await conn.execute(
                "UPDATE kotoyose.execution_workspaces SET writer_attempt=$2 WHERE id=$1",
                workspace["id"], attempt_id,
            )
            await conn.execute(
                "INSERT INTO kotoyose.execution_operations(id,attempt_id,kind,state) VALUES($1,$2,'local_execution','prepared')",
                "execution:" + attempt_id, attempt_id,
            )
            await self._event(conn, attempt_id, "dispatched", {
                "workspace_ref": request["workspace_ref"],
                **({"memory_snapshots": snapshot["memory_snapshots"]}
                   if snapshot.get("memory_snapshots") else {}),
                **({"predecessor_attempt_id": resume.predecessor_attempt_id,
                    "resume_manifest_digest": snapshot["resume_manifest_digest"], "resume_mode": resume.mode} if resume else {}),
            })
            if agent_execution is not None:
                await agent_execution.bind(conn, attempt_id)
            row = await conn.fetchrow("SELECT * FROM kotoyose.execution_attempts WHERE id=$1", attempt_id)
            return _decoded(row), True

    async def cancel_dispatch(self, principal: str, key: str) -> str | None:
        """同じ admission lock 内で取消を保存し、遅延 POST の新規実行を防ぐ。"""
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1,0))", "kotoyose-caller:" + principal,
            )
            await conn.execute(
                "INSERT INTO kotoyose.execution_cancellations(principal,idempotency_key) "
                "VALUES($1,$2) ON CONFLICT DO NOTHING", principal, key,
            )
            row = await conn.fetchrow(
                "SELECT id FROM kotoyose.execution_attempts WHERE principal=$1 AND idempotency_key=$2 AND api_operation='dispatch' FOR UPDATE",
                principal, key,
            )
            if row is None:
                return None
            await self._cancel(conn, row["id"], "user_cancelled")
            return row["id"]

    async def attempt(self, principal: str, attempt_id: str) -> dict:
        """所有者だけに Attempt と不変 artifact の metadata を返す。"""
        row = await self.pool.fetchrow(
            "SELECT * FROM kotoyose.execution_attempts WHERE principal=$1 AND id=$2", principal, attempt_id,
        )
        if row is None:
            raise ExecutionError("invalid_request", "Unknown attempt", 404)
        result = _decoded(row)
        result["artifacts"] = [dict(item) for item in await self.pool.fetch(
            "SELECT b.ref,b.name,b.digest,COALESCE(b.byte_size,octet_length(b.content)) AS size,b.media_type, "
            "COALESCE(r.state,'active') AS availability FROM kotoyose.execution_artifacts b "
            "LEFT JOIN kotoyose.execution_artifact_retention r ON r.artifact_ref=b.ref WHERE b.attempt_id=$1",
            attempt_id,
        )]
        # 既存 v1 client の strict metadata codec を維持し、利用不能時だけ追加状態を返す。
        for artifact in result['artifacts']:
            if artifact['availability'] == 'active':
                artifact.pop('availability')
        return result

    async def active(self) -> list[dict]:
        """起動時に実行中または停止未確認の実体を照合する。"""
        rows = await self.pool.fetch(
            "SELECT * FROM kotoyose.execution_attempts WHERE finished_at IS NULL OR stop_confirmation='unconfirmed'",
        )
        return [_decoded(row) for row in rows]

    async def container_attempt_owners(self, ids: list[str]) -> dict[str, tuple[str, bool, int]]:
        """親、永続停止証拠に基づく回収可否、実行時の世代を返す。"""
        rows = await self.pool.fetch(
            "SELECT candidate.id,owner.id AS owner_id,owner.generation,"
            "(owner.finished_at IS NOT NULL AND owner.stop_confirmation='confirmed' AND "
            "CASE WHEN operation.id IS NOT NULL THEN "
            "attempt.id IS NULL AND operation.kind='sandbox_command' AND operation.state='confirmed' "
            "ELSE local_operation.kind='local_execution' AND local_operation.state='confirmed' "
            "END) AS cleanup_ready "
            "FROM unnest($1::text[]) AS candidate(id) "
            "LEFT JOIN kotoyose.execution_operations operation ON operation.id=candidate.id "
            "LEFT JOIN kotoyose.execution_attempts attempt ON attempt.id=candidate.id "
            "LEFT JOIN kotoyose.execution_operations local_operation "
            "ON local_operation.id='execution:' || attempt.id AND local_operation.attempt_id=attempt.id "
            "LEFT JOIN kotoyose.execution_attempts owner "
            "ON owner.id=COALESCE(operation.attempt_id,attempt.id)", ids,
        )
        return {
            row["id"]: (row["owner_id"], row["cleanup_ready"] is True, row["generation"])
            for row in rows if row["owner_id"] is not None
        }

    async def mark_running(self, attempt_id: str) -> bool:
        """開始済みを確定する前に cancel と writer 世代を再確認する。"""
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT a.* FROM kotoyose.execution_attempts a JOIN kotoyose.execution_workspaces w ON w.id=a.workspace_id "
                "WHERE a.id=$1 AND a.status='dispatched' AND a.generation=w.generation AND w.writer_attempt=a.id FOR UPDATE OF a", attempt_id,
            )
            if row is None:
                return False
            await conn.execute(
                "UPDATE kotoyose.execution_attempts SET status='running', "
                "limits_state=CASE WHEN limits_state='not_started' AND snapshot::jsonb->>'kind' IS DISTINCT FROM 'command' THEN 'unknown' ELSE limits_state END, "
                "limits_observed_at=CASE WHEN limits_state='not_started' AND snapshot::jsonb->>'kind' IS DISTINCT FROM 'command' THEN now() ELSE limits_observed_at END, "
                "limits_sequence=CASE WHEN limits_state='not_started' AND snapshot::jsonb->>'kind' IS DISTINCT FROM 'command' THEN COALESCE(limits_sequence, 0) + 1 ELSE limits_sequence END WHERE id=$1",
                attempt_id,
            )
            await conn.execute(
                "UPDATE kotoyose.execution_operations SET state='sent',updated_at=now() WHERE attempt_id=$1", attempt_id,
            )
            await self._event(conn, attempt_id, "running", {})
            return True

    async def cancel(self, principal: str, attempt_id: str, reason: str = "user_cancelled") -> None:
        """取消要求を保存する。terminal 記録を停止確認に読み替えない。"""
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT id,finished_at FROM kotoyose.execution_attempts WHERE id=$1 AND principal=$2 FOR UPDATE", attempt_id, principal,
            )
            if row is None:
                raise ExecutionError("invalid_request", "Unknown attempt", 404)
            if row["finished_at"] is None:
                await self._cancel(conn, attempt_id, reason)

    async def _cancel(self, conn: asyncpg.Connection, attempt_id: str, reason: str) -> None:
        changed = await conn.fetchval(
            "UPDATE kotoyose.execution_attempts SET status='cancelling',reason=$2 WHERE id=$1 "
            "AND finished_at IS NULL AND status!='cancelling' RETURNING id", attempt_id, reason,
        )
        if changed:
            await self._event(conn, attempt_id, "cancelling", {"reason": reason})

    async def append_event(self, attempt_id: str, event_type: str, payload: dict) -> None:
        """Warning や progress を terminal 前にだけ追記する。"""
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow("SELECT finished_at FROM kotoyose.execution_attempts WHERE id=$1 FOR UPDATE", attempt_id)
            if row is not None and row["finished_at"] is None:
                await self._event(conn, attempt_id, event_type, payload)

    async def prepare_operation(self, attempt_id: str, kind: str) -> str:
        """Agent tool / provider の起動 intent を、取消・世代照合の後に保存する。"""
        async with self.pool.acquire() as conn, conn.transaction():
            await authorize_operation(conn, attempt_id)
            operation_id = str(uuid4())
            await conn.execute(
                "INSERT INTO kotoyose.execution_operations(id,attempt_id,kind,state) VALUES($1,$2,$3,'prepared')",
                operation_id, attempt_id, kind,
            )
            await self._event(conn, attempt_id, "operation_prepared", {"operation_id": operation_id, "kind": kind})
            return operation_id

    async def authorize_provider_request(self, attempt_id: str, principal: str) -> None:
        """推論要求直前にも共通 Grant・workspace 世代/writer・未解決操作を照合する。"""
        async with self.pool.acquire() as conn, conn.transaction():
            await authorize_operation(conn, attempt_id, principal)

    async def record_process(self, operation_id: str, identity: dict) -> None:
        """Provider に task を送る前に、停止照合できる process identity を記録する。"""
        await self.pool.execute(
            "UPDATE kotoyose.execution_operations SET state='sent',result=$2::jsonb,updated_at=now() "
            "WHERE id=$1 AND kind='provider_process' AND state='prepared'", operation_id, canonical_json(identity),
        )

    async def record_codex_stop(self, operation_id: str, evidence: dict) -> None:
        """観測中の停止証拠と安全な診断を原子的に保存する。既存の異なる証拠は上書きしない。"""
        from .codex_stop_evidence import stop_evidence_context, validate_stop_evidence
        async with self.pool.acquire() as conn, conn.transaction():
            attempt = await self._lock_codex_provider_stop(conn, operation_id)
            ownership = dict(attempt["result"] or {})
            previous = ownership.pop("codex_stop_evidence", None)
            recovered = ownership.pop("codex_namespace_stop_evidence", None)
            egress = ownership.pop("codex_egress_stop_evidence", None)
            if (recovered is not None or egress is not None) and previous is None:
                raise ExecutionError("request_conflict", "Provider stop source is missing", 409)
            try:
                proof = validate_stop_evidence(evidence, stop_evidence_context(attempt), ownership)
            except (ValueError, TypeError, KeyError) as exc:
                raise ExecutionError("request_conflict", "Provider stop evidence is invalid", 409) from exc
            if previous is not None:
                if previous != proof.model_dump():
                    raise ExecutionError("request_conflict", "Provider stop evidence is immutable", 409)
                return
            await conn.execute(
                "UPDATE kotoyose.execution_operations SET result=$2::jsonb,updated_at=now() WHERE id=$1",
                operation_id, canonical_json({**ownership, "codex_stop_evidence": proof.model_dump()}),
            )
            await self._event(conn, attempt["id"], "codex_stop_diagnostic", proof.diagnostic())

    async def record_codex_namespace_stop(self, operation_id: str, evidence: dict) -> None:
        """終端の namespace 終了観測を追記する。元の部分証拠と verdict は保持する。"""
        from .codex_stop_evidence import stop_evidence_context, validate_namespace_stop_evidence
        async with self.pool.acquire() as conn, conn.transaction():
            attempt = await self._lock_codex_provider_stop(conn, operation_id)
            if attempt["finished_at"] is None:
                raise ExecutionError("request_conflict", "Namespace recovery requires a terminal attempt", 409)
            result = dict(attempt["result"] or {})
            ownership = dict(result)
            source = ownership.pop("codex_stop_evidence", None)
            previous = ownership.pop("codex_namespace_stop_evidence", None)
            if ownership.pop("codex_egress_stop_evidence", None) is not None:
                raise ExecutionError("request_conflict", "Provider stop was already recovered", 409)
            try:
                proof = validate_namespace_stop_evidence(evidence, stop_evidence_context(attempt), ownership, source)
            except (ValueError, TypeError, KeyError) as exc:
                raise ExecutionError("request_conflict", "Namespace stop evidence is invalid", 409) from exc
            if previous is not None:
                if previous != proof.model_dump():
                    raise ExecutionError("request_conflict", "Namespace stop evidence is immutable", 409)
                return
            await conn.execute(
                "UPDATE kotoyose.execution_operations SET result=$2::jsonb,updated_at=now() WHERE id=$1",
                operation_id, canonical_json({**result, "codex_namespace_stop_evidence": proof.model_dump()}),
            )
            await self._event(conn, attempt["id"], "codex_stop_diagnostic", {
                "schema_version": "1", "stage": "complete", "reason": "confirmed", "pending_tasks": 0,
            })

    async def record_codex_egress_stop(self, operation_id: str, evidence: dict) -> None:
        """egress だけが未確認の終端に、所有 process の不在の観測を追記する。元の部分証拠は保持する。"""
        from .codex_stop_evidence import stop_evidence_context, validate_egress_stop_evidence
        async with self.pool.acquire() as conn, conn.transaction():
            attempt = await self._lock_codex_provider_stop(conn, operation_id)
            if attempt["finished_at"] is None:
                raise ExecutionError("request_conflict", "Egress recovery requires a terminal attempt", 409)
            result = dict(attempt["result"] or {})
            ownership = dict(result)
            source = ownership.pop("codex_stop_evidence", None)
            if ownership.pop("codex_namespace_stop_evidence", None) is not None:
                raise ExecutionError("request_conflict", "Provider stop was already recovered", 409)
            previous = ownership.pop("codex_egress_stop_evidence", None)
            try:
                proof = validate_egress_stop_evidence(evidence, stop_evidence_context(attempt), ownership, source)
            except (ValueError, TypeError, KeyError) as exc:
                raise ExecutionError("request_conflict", "Egress stop evidence is invalid", 409) from exc
            if previous is not None:
                if previous != proof.model_dump():
                    raise ExecutionError("request_conflict", "Egress stop evidence is immutable", 409)
                return
            await conn.execute(
                "UPDATE kotoyose.execution_operations SET result=$2::jsonb,updated_at=now() WHERE id=$1",
                operation_id, canonical_json({**result, "codex_egress_stop_evidence": proof.model_dump()}),
            )
            await self._event(conn, attempt["id"], "codex_stop_diagnostic", {
                "schema_version": "1", "stage": "complete", "reason": "confirmed", "pending_tasks": 0,
            })

    async def _lock_codex_provider_stop(self, conn, operation_id: str) -> dict:
        row = await conn.fetchrow(
            "SELECT a.*,o.result,o.kind AS operation_kind,w.generation AS workspace_generation,w.writer_attempt "
            "FROM kotoyose.execution_attempts a JOIN kotoyose.execution_operations o ON o.attempt_id=a.id "
            "JOIN kotoyose.execution_workspaces w ON w.id=a.workspace_id WHERE o.id=$1 FOR UPDATE OF a,o,w",
            operation_id,
        )
        if row is None:
            raise ExecutionError("request_conflict", "Provider operation is unavailable", 409)
        attempt = _decoded(row)
        if (attempt["operation_kind"] != "provider_process" or attempt["resumed_by"] is not None
                or not mode_requests_room(attempt["snapshot"].get("codex_execution_mode"))
                or attempt["writer_attempt"] != attempt["id"]
                or attempt["workspace_generation"] != attempt["generation"]):
            raise ExecutionError("request_conflict", "Provider stop ownership changed", 409)
        return attempt

    async def resolve_operation(self, operation_id: str, stop: str) -> None:
        """操作の停止確認を記録し、unknown のまま次の tool が始まることを防ぐ。"""
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT a.resumed_by FROM kotoyose.execution_attempts a JOIN kotoyose.execution_operations o "
                "ON o.attempt_id=a.id WHERE o.id=$1 FOR UPDATE OF a", operation_id,
            )
            if row is not None and row["resumed_by"] is None:
                await conn.execute(
                    "UPDATE kotoyose.execution_operations SET state=$2,updated_at=now() WHERE id=$1 AND state IS DISTINCT FROM $2",
                    operation_id, "confirmed" if stop == "confirmed" else "unknown",
                )

    async def operations(self, attempt_id: str) -> list[dict]:
        """復旧処理が実体を照合するための controller-issued 操作一覧を返す。"""
        return [_decoded(row) for row in await self.pool.fetch(
            "SELECT * FROM kotoyose.execution_operations WHERE attempt_id=$1 ORDER BY created_at,id", attempt_id,
        )]

    async def finish(
        self,
        attempt_id: str,
        status: str,
        reason: str,
        stop: str,
        exit_code: int | None,
        artifacts: Mapping[str, bytes | CapturedArtifact],
        usage: dict | None = None,
        artifact_manifest: Sequence[dict] = (),
        required_action: dict | None = None,
        provider_session_ref: str | None = None,
        limits_result: tuple | None = None,
        launch_outcome: Literal['not_started', 'started', 'unknown'] | None = None,
    ) -> tuple[str, str | None, str | None] | None:
        """証拠と終端を原子的に保存し、未起動の証拠がない時間を unknown に保つ。"""
        identity = await self.pool.fetchrow("SELECT principal FROM kotoyose.execution_attempts WHERE id=$1", attempt_id)
        if identity is not None:
            await self.client_tools.cancel_attempt(identity["principal"], attempt_id)
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow("SELECT * FROM kotoyose.execution_attempts WHERE id=$1 FOR UPDATE", attempt_id)
            if row is None:
                return None
            if row["finished_at"] is not None:
                return row["status"], row["reason"], row["stop_confirmation"]
            if row['limits_state'] == 'running':
                await conn.execute("UPDATE kotoyose.execution_attempts SET limits_state='unknown', "
                    "limits_observed_at=now(),limits_finished_at=NULL,limits_sequence=COALESCE(limits_sequence,0)+1 WHERE id=$1", attempt_id)
            if (row['limits_state'] in ('not_started', 'unknown') and row['limits_started_at'] is None
                    and reason != 'coordinator_restarted' and json.loads(row['snapshot']).get('kind') == 'agent'):
                if launch_outcome in ('started', 'unknown'):
                    await conn.execute("UPDATE kotoyose.execution_attempts SET limits_state='unknown', "
                        "limits_observed_at=now(),limits_finished_at=NULL,limits_sequence=COALESCE(limits_sequence,0)+1 WHERE id=$1", attempt_id)
                elif row['limits_state'] == 'not_started' or launch_outcome == 'not_started':
                    await conn.execute("UPDATE kotoyose.execution_attempts SET limits_state='ended', limits_active_ms=0, "
                        "limits_observed_at=now(),limits_finished_at=now(),limits_sequence=COALESCE(limits_sequence,0)+1 WHERE id=$1", attempt_id)
            await self.mcp_calls.cancel_in_transaction(conn, row['principal'], attempt_id)
            if row["status"] == "cancelling" and row["reason"] == "coordinator_restarted":
                status, reason = "worker_lost", row["reason"]
            elif row["status"] == "cancelling" and stop == "confirmed":
                status, reason = "cancelled", row["reason"]
            if limits_result is not None:
                usage_limits, provider_limit = limits_result
                await self.limits.save_result(row['principal'], attempt_id, usage_limits,
                    provider_limit if reason == 'provider_usage_limit' else None, connection=conn)
            execution_outcome = {"status": status, "reason": reason, "exit_code": exit_code}
            manifest = {item['name']: dict(item) for item in artifact_manifest}
            for name, value in artifacts.items():
                if isinstance(value, CapturedArtifact):
                    content, media_type = value.content, value.media_type
                else:
                    content, media_type = value, "text/plain; charset=utf-8"
                try:
                    await self.save_artifact(conn, attempt_id, name, CapturedArtifact(content, media_type))
                except ArtifactStorageError:
                    # 保存先の不調で、実停止や元の失敗理由を失わない。原本は削除しない。
                    manifest[name] = {'name': name, 'state': 'error', 'reason': 'artifact storage unavailable'}
                    if status == 'succeeded':
                        status, reason = 'failed', 'artifact_storage_error'
            artifact_manifest = list(manifest.values())
            effects_pending = await conn.fetchval("SELECT EXISTS(SELECT 1 FROM kotoyose.execution_operations WHERE attempt_id=$1 AND kind IN ('client_tool','mcp_tool') AND state <> 'confirmed')", attempt_id)
            resolution = "resolved" if stop == "confirmed" and not effects_pending else "unresolved"
            await conn.execute(
                "UPDATE kotoyose.execution_attempts SET status=$2,reason=$3,stop_confirmation=$4,"
                "side_effect_resolution=$5,exit_code=$6,artifact_manifest=$7::jsonb,required_action=$8::jsonb,finished_at=now() WHERE id=$1",
                attempt_id, status, reason, stop, resolution, exit_code,
                canonical_json(list(artifact_manifest)),
                canonical_json(required_action) if required_action is not None else None,
            )
            if usage is not None:
                await conn.execute("UPDATE kotoyose.execution_attempts SET usage=$2::jsonb WHERE id=$1", attempt_id, canonical_json(usage))
            if provider_session_ref is not None:
                await conn.execute("UPDATE kotoyose.execution_attempts SET provider_session_ref=$2 WHERE id=$1", attempt_id, provider_session_ref)
            await conn.execute(
                "UPDATE kotoyose.execution_operations SET state=$2,result=$3::jsonb,updated_at=now() "
                "WHERE attempt_id=$1 AND kind='local_execution'",
                attempt_id, "confirmed" if stop == "confirmed" else "unknown", canonical_json({"status": status, "exit_code": exit_code}),
            )
            if resolution == "resolved":
                await self._release_input_pins(conn, attempt_id)
                await conn.execute(
                    "UPDATE kotoyose.execution_workspaces SET writer_attempt=NULL,generation=generation+1 "
                    "WHERE id=$1 AND writer_attempt=$2 AND generation=$3", row["workspace_id"], attempt_id, row["generation"],
                )
            await self._event(conn, attempt_id, status, {
                "stop_confirmation": stop, "side_effect_resolution": resolution, "exit_code": exit_code,
                "reason": reason,
                "execution_outcome": execution_outcome,
                "artifact_manifest": list(artifact_manifest),
                "required_action": required_action,
                "previous_workspace_ref": json.loads(row["request"])["workspace_ref"],
                "workspace_ref": f"workspace:{row['workspace_id']}:{row['generation'] + (resolution == 'resolved')}",
            })
            return status, reason, stop

    async def events(self, principal: str, attempt_id: str, after: int, limit: int) -> list[dict]:
        """Cursor より後の永続 event を順序付きで再送する。"""
        await self.attempt(principal, attempt_id)
        return [_decoded(row) for row in await self.pool.fetch(
            "SELECT * FROM kotoyose.execution_events WHERE attempt_id=$1 AND sequence>$2 ORDER BY sequence LIMIT $3",
            attempt_id, after, limit,
        )]

    async def context_observation(self, attempt_id: str) -> dict:
        """既に所有確認した Attempt の最後の観測を返し、欠測を明示する。"""
        row = await self.pool.fetchrow("SELECT payload,created_at FROM kotoyose.execution_events WHERE attempt_id=$1 AND type='context_usage' ORDER BY sequence DESC LIMIT 1", attempt_id)
        return {'observation': json.loads(row['payload']) if row else {'state': 'unobserved', 'source': 'provider_runtime', 'reason': 'no_sample_recorded'},
                'observed_at': row['created_at'].isoformat() if row else None}

    async def confirm_stop(self, attempt_id: str) -> None:
        """process 停止と外部副作用を分離し、全条件の確定時だけ writer を解放する。"""
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow("SELECT * FROM kotoyose.execution_attempts WHERE id=$1 FOR UPDATE", attempt_id)
            if row is None or row["finished_at"] is None or (row["stop_confirmation"] == "confirmed" and row["side_effect_resolution"] == "resolved"):
                return
            await conn.execute("UPDATE kotoyose.execution_operations SET state='confirmed',updated_at=now() WHERE attempt_id=$1 AND kind NOT IN ('client_tool','mcp_tool')", attempt_id)
            pending = await conn.fetchval("SELECT EXISTS(SELECT 1 FROM kotoyose.execution_operations WHERE attempt_id=$1 AND state <> 'confirmed')", attempt_id)
            resolution = "unresolved" if pending else "resolved"
            await conn.execute("UPDATE kotoyose.execution_attempts SET stop_confirmation='confirmed',side_effect_resolution=$2 WHERE id=$1", attempt_id, resolution)
            if pending:
                if row["stop_confirmation"] != "confirmed":
                    await self._event(conn, attempt_id, "process_stop_confirmed", {"stop_confirmation": "confirmed", "side_effect_resolution": "unresolved"})
                return
            await conn.execute("UPDATE kotoyose.execution_workspaces SET writer_attempt=NULL,generation=generation+1 WHERE id=$1 AND writer_attempt=$2 AND generation=$3", row["workspace_id"], attempt_id, row["generation"])
            await self._release_input_pins(conn, attempt_id)
            await self._event(conn, attempt_id, "stop_confirmed", {
                "status": row["status"], "source": "worker_inspection", "stop_confirmation": "confirmed",
                "side_effect_resolution": "resolved",
                "previous_workspace_ref": json.loads(row["request"])["workspace_ref"],
                "workspace_ref": f"workspace:{row['workspace_id']}:{row['generation'] + 1}",
            })

    async def _release_input_pins(self, conn: asyncpg.Connection, attempt_id: str) -> None:
        """停止確認まで保持した input pin を同じ transaction で解放する。"""
        rows = await conn.fetch(
            "SELECT artifact_ref FROM kotoyose.execution_attempt_input_artifacts "
            "WHERE attempt_id=$1 AND released_at IS NULL FOR UPDATE", attempt_id,
        )
        for row in rows:
            await conn.execute(
                "UPDATE kotoyose.execution_input_artifacts SET pin_count=GREATEST(pin_count-1,0) "
                "WHERE ref=$1", row["artifact_ref"],
            )
        await conn.execute(
            "UPDATE kotoyose.execution_attempt_input_artifacts SET released_at=now() "
            "WHERE attempt_id=$1 AND released_at IS NULL", attempt_id,
        )

    async def save_artifact(self, conn: asyncpg.Connection, attempt_id: str, name: str, artifact: CapturedArtifact) -> dict:
        """保存確認後に bytes と初期 retention pin を同じ transaction へ追加する。"""
        content, media_type = artifact.content, artifact.media_type
        digest = content.digest if isinstance(content, ArtifactFile) else hashlib.sha256(content).hexdigest()
        ref = f"artifact:{attempt_id}:{name}:{digest}"
        blob = (ArtifactBlob(hashlib.sha256(ref.encode()).hexdigest(), digest, content.size, media_type)
                if isinstance(content, ArtifactFile) else ArtifactBlob.from_content(ref, content, media_type))
        backend_name = self.artifact_storage.default
        identity = None
        stored_content = content
        if backend_name != "postgres":
            backend = self.artifact_storage.resolve(backend_name)
            # 外部保存は rollback できない。immutable key の孤児は許容し、read-back 前の成功確定は許さない。
            if isinstance(content, ArtifactFile):
                if not isinstance(backend, LocalArtifactStore):
                    raise ArtifactStorageError("Streaming capture requires local artifact storage")
                await backend.put_file(blob, content, getattr(self, 'artifact_minimum_free_bytes', 5_000_000_000))
            else:
                await backend.put(blob, content)
            identity, stored_content = backend.identity, None
        elif isinstance(content, ArtifactFile):
            raise ArtifactStorageError("Large artifacts cannot be stored inline")
        await conn.execute(
            "INSERT INTO kotoyose.execution_artifacts(ref,attempt_id,name,digest,content,media_type,"
            "storage_backend,storage_identity,storage_key,byte_size) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)",
            ref, attempt_id, name, digest, stored_content, media_type,
            backend_name, identity, blob.key if identity else None, blob.size,
        )
        await self.artifact_retention.register(conn, ref)
        return {'ref': ref, 'name': name, 'digest': digest, 'size': blob.size, 'media_type': media_type}

    async def artifact_record(self, principal: str, ref: str, *, attempt_id: str | None = None) -> dict:
        """所有とretentionを照合した内部metadata。backend locatorはHTTPへ返さない。"""
        row = await self.pool.fetchrow(
            "SELECT r.*,p.state AS retention_state FROM kotoyose.execution_artifacts r JOIN kotoyose.execution_attempts a ON a.id=r.attempt_id "
            "LEFT JOIN kotoyose.execution_artifact_retention p ON p.artifact_ref=r.ref "
            "WHERE r.ref=$1 AND a.principal=$2", ref, principal,
        )
        if row is None and attempt_id is not None:
            row = await self.pool.fetchrow(
                "SELECT i.* FROM kotoyose.execution_input_artifacts i "
                "JOIN kotoyose.execution_attempt_input_artifacts p ON p.artifact_ref=i.ref "
                "JOIN kotoyose.execution_attempts a ON a.id=p.attempt_id AND a.principal=$2 "
                "WHERE i.ref=$1 AND i.principal=$2 AND p.attempt_id=$3 "
                "AND p.principal=$2 AND p.released_at IS NULL AND i.state='active'",
                ref, principal, attempt_id,
            )
        if row is None and attempt_id is None:
            row = await self.pool.fetchrow(
                "SELECT * FROM kotoyose.execution_input_artifacts WHERE ref=$1 AND principal=$2 "
                "AND state='active' AND expires_at > now()", ref, principal,
            )
        if row is None:
            # Caller が所有している metadata が残る場合だけ期限 / tombstone を知らせる。
            expired = await self.pool.fetchrow(
                "SELECT state,expires_at FROM kotoyose.execution_input_artifacts "
                "WHERE ref=$1 AND principal=$2", ref, principal,
            )
            if expired is not None and (
                expired["state"] == "tombstone" or expired["expires_at"] <= datetime.now(timezone.utc)
            ):
                raise ExecutionError("artifact_expired", "Artifact has expired or was deleted", 410)
            raise ExecutionError("invalid_request", "Unknown artifact", 404)
        result = dict(row)
        if result.get('retention_state') == 'tombstone':
            raise ExecutionError('artifact_expired', 'Artifact bytes were deleted', 410)
        if result.get('retention_state') == 'deleting':
            raise ExecutionError('artifact_storage_error', 'Artifact deletion is being reconciled', 503)
        return result

    async def artifact(self, principal: str, ref: str, *, attempt_id: str | None = None) -> dict:
        """小さい内容だけをメモリへ返す。大容量の取得にはartifact_streamを使う。"""
        result = await self.artifact_record(principal, ref, attempt_id=attempt_id)
        size = result["byte_size"]
        if size is None and result["content"] is not None:
            size = len(result["content"])
        if not isinstance(size, int) or size < 0:
            raise ExecutionError("artifact_storage_error", "Artifact size metadata is invalid", 503)
        if size > 64 * 1024 * 1024:
            raise ExecutionError("artifact_storage_error", "Large artifacts require streaming reads", 413)
        blob = ArtifactBlob(result["storage_key"] or hashlib.sha256(ref.encode()).hexdigest(),
                            result["digest"], size, result["media_type"])
        try:
            if result["storage_backend"] == "postgres":
                if result["content"] is None:
                    raise ArtifactStorageError("Inline artifact content is absent")
                result["content"] = blob.verify(result["content"])
            else:
                if not result["storage_identity"] or not result["storage_key"]:
                    raise ArtifactStorageError("Artifact storage locator is incomplete")
                backend = self.artifact_storage.resolve(result["storage_backend"], result["storage_identity"])
                result["content"] = await backend.read(blob)
        except ArtifactStorageError as exc:
            raise ExecutionError("artifact_storage_error", "Saved artifact location, digest, or size could not be verified", 503) from exc
        if "attempt_id" not in result:
            result["attempt_id"] = None
        return {key: value for key, value in result.items() if not key.startswith("storage_")}

    async def artifact_stream(self, principal: str, ref: str):
        """既存の所有・retention検査を通した有限chunk列と公開metadataを返す。"""
        record = await self.artifact_record(principal, ref)
        size = record['byte_size']
        if size is None:
            size = len(record['content'])
        public = {'digest': record['digest'], 'media_type': record['media_type'], 'size': size}
        if record['storage_backend'] == 'postgres':
            artifact = await self.artifact(principal, ref)
            async def inline():
                yield artifact['content']
            return public, inline()
        try:
            backend = self.artifact_storage.resolve(record['storage_backend'], record['storage_identity'])
            if isinstance(backend, LocalArtifactStore):
                blob = ArtifactBlob(record['storage_key'], record['digest'], size, record['media_type'])
                return public, backend.stream(blob)
            artifact = await self.artifact(principal, ref)
            async def bounded():
                yield artifact['content']
            return public, bounded()
        except ArtifactStorageError as exc:
            raise ExecutionError('artifact_storage_error', 'Saved artifact backend is unavailable', 503) from exc

    async def _event(self, conn: asyncpg.Connection, attempt_id: str, event_type: str, payload: dict) -> None:
        sequence = await conn.fetchval(
            "UPDATE kotoyose.execution_attempts SET sequence=sequence+1 WHERE id=$1 RETURNING sequence", attempt_id,
        )
        await conn.execute(
            "INSERT INTO kotoyose.execution_events(attempt_id,sequence,type,payload) VALUES($1,$2,$3,$4::jsonb)",
            attempt_id, sequence, event_type, canonical_json(payload),
        )
