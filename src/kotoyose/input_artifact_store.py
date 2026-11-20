"""Principal-scoped input Artifact の保存、pin、retention を担当する。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import asyncpg

from .artifact_blob_store import ArtifactBlob, ArtifactBlobRegistry, ArtifactMissingError, ArtifactStorageError
from .input_artifacts import (
    InputArtifactLimits, InputArtifactUpload, decode_input_upload, input_artifact_ref,
)


class InputArtifactRepository:
    """ExecutionStore から分離した input bytes の durable repository。"""

    def __init__(self, pool: asyncpg.Pool, artifact_storage: ArtifactBlobRegistry):
        self.pool = pool
        self.artifact_storage = artifact_storage

    async def put_input_artifact(
        self, principal: str, upload: InputArtifactUpload,
        limits: InputArtifactLimits,
    ) -> dict:
        """Attempt と無関係な principal-scoped input Artifact を immutable に保存する。"""
        if upload.retention_pin and not limits.allow_retention_pin:
            raise ValueError("retention pin requires administrator authorization")
        now = datetime.now(timezone.utc)
        if upload.expires_at > now + timedelta(seconds=limits.max_lifetime_seconds):
            raise ValueError("input artifact lifetime exceeds administrator limit")
        content, digest = decode_input_upload(upload, limits)
        ref = input_artifact_ref(
            principal, digest, upload.media_type, upload.expires_at, upload.retention_pin,
        )
        blob = ArtifactBlob.from_content(ref, content, upload.media_type)
        backend_name = self.artifact_storage.default
        backend = None
        identity = None
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1,0))", "kotoyose-input-artifacts:" + principal,
            )
            prior = await conn.fetchrow(
                "SELECT ref,digest,media_type,byte_size,content,storage_backend,storage_identity,storage_key,"
                "created_at,expires_at,retention_pin,state "
                "FROM kotoyose.execution_input_artifacts WHERE ref=$1 AND principal=$2",
                ref, principal,
            )
            if prior is not None:
                if prior["state"] == "pending":
                    await self._verify_input_blob(prior)
                    await conn.execute(
                        "UPDATE kotoyose.execution_input_artifacts SET state='active' WHERE ref=$1 AND state='pending'",
                        prior["ref"],
                    )
                    prior = await conn.fetchrow(
                        "SELECT ref,digest,media_type,byte_size,content,storage_backend,storage_identity,storage_key,"
                        "created_at,expires_at,retention_pin,state FROM kotoyose.execution_input_artifacts WHERE ref=$1",
                        prior["ref"],
                    )
                if prior["state"] != "active":
                    raise ValueError("input artifact was deleted or is still being reconciled")
                if (prior["media_type"], prior["expires_at"], prior["retention_pin"]) != (
                    upload.media_type, upload.expires_at, upload.retention_pin,
                ):
                    raise ArtifactStorageError("Input artifact identity metadata changed")
                await self._verify_input_blob(prior)
                return self._input_artifact_result(prior)
            count, total = await conn.fetchrow(
                "SELECT count(*)::int AS count, COALESCE(sum(byte_size),0)::bigint AS total "
                "FROM kotoyose.execution_input_artifacts WHERE principal=$1 AND state IN ('active','pending','deleting')",
                principal,
            )
            if count >= limits.max_count or total + len(content) > limits.max_total_bytes:
                raise ValueError("input artifact registration limit exceeded")
            stored_content = content
            if backend_name != "postgres":
                backend = self.artifact_storage.resolve(backend_name)
                identity, stored_content = backend.identity, None
            row = await conn.fetchrow(
                "INSERT INTO kotoyose.execution_input_artifacts("
                "ref,principal,digest,content,media_type,byte_size,storage_backend,storage_identity,storage_key,expires_at,retention_pin,state) "
                "VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12) RETURNING *",
                ref, principal, digest, stored_content, upload.media_type, blob.size,
                backend_name, identity, blob.key if identity else None, upload.expires_at, upload.retention_pin,
                "pending" if backend_name != "postgres" else "active",
            )
        if backend is not None:
            try:
                await backend.put(blob, content)
            except ArtifactStorageError:
                raise
            async with self.pool.acquire() as conn, conn.transaction():
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1,0))", "kotoyose-input-artifacts:" + principal,
                )
                row = await conn.fetchrow(
                    "UPDATE kotoyose.execution_input_artifacts SET state='active' "
                    "WHERE ref=$1 AND principal=$2 AND state='pending' RETURNING *", ref, principal,
                )
                if row is None:
                    raise ArtifactStorageError("Input artifact pending state changed during provider upload")
        return self._input_artifact_result(row)

    @staticmethod
    def _input_artifact_result(row: asyncpg.Record | dict) -> dict:
        """内部 SQL 列名を upload route の安定した metadata へ変換する。"""
        return {"ref": row["ref"], "digest": row["digest"], "media_type": row["media_type"],
                "size": row["byte_size"], "created_at": row["created_at"],
                "expires_at": row["expires_at"], "retention_pin": row["retention_pin"]}

    async def _verify_input_blob(self, row: asyncpg.Record | dict) -> None:
        """既存 input の bytes を replay 前にも照合し、破損を成功扱いにしない。"""
        size = row["byte_size"]
        blob = ArtifactBlob(row["storage_key"] or hashlib.sha256(row["ref"].encode()).hexdigest(),
                            row["digest"], size, row["media_type"])
        if row["storage_backend"] == "postgres":
            if row["content"] is None:
                raise ArtifactStorageError("Inline input artifact content is absent")
            blob.verify(row["content"])
            return
        if not row["storage_identity"] or not row["storage_key"]:
            raise ArtifactStorageError("Input artifact storage locator is incomplete")
        backend = self.artifact_storage.resolve(row["storage_backend"], row["storage_identity"])
        blob.verify(await backend.read(blob))

    async def gc_expired_input_artifacts(self, *, limit: int = 100) -> int:
        """期限切れで未 pin の input bytes を削除し、ref は tombstone として残す。"""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 0 < limit <= 1000:
            raise ValueError("GC limit must be between 1 and 1000")
        async with self.pool.acquire() as conn, conn.transaction():
            rows = await conn.fetch(
                "SELECT i.* FROM kotoyose.execution_input_artifacts i "
                "WHERE i.state='active' AND i.expires_at <= now() AND i.pin_count=0 "
                "AND i.retention_pin=false "
                "AND NOT EXISTS (SELECT 1 FROM kotoyose.execution_attempt_input_artifacts p "
                "WHERE p.artifact_ref=i.ref AND p.released_at IS NULL) "
                "ORDER BY i.expires_at LIMIT $1 FOR UPDATE SKIP LOCKED", limit,
            )
            refs = [row["ref"] for row in rows]
            if refs:
                await conn.execute(
                    "UPDATE kotoyose.execution_input_artifacts SET state='deleting' WHERE ref=ANY($1::text[])", refs,
                )
        deleted = 0
        for row in rows:
            try:
                blob = ArtifactBlob(row["storage_key"] or hashlib.sha256(row["ref"].encode()).hexdigest(),
                                    row["digest"], row["byte_size"], row["media_type"])
                if row["storage_backend"] == "postgres":
                    pass
                else:
                    backend = self.artifact_storage.resolve(row["storage_backend"], row["storage_identity"])
                    delete = getattr(backend, "delete", None)
                    if not callable(delete):
                        raise ArtifactStorageError("Artifact backend does not support deletion")
                    await delete(blob)
                await self.pool.execute(
                    "UPDATE kotoyose.execution_input_artifacts SET state='tombstone',content=NULL,"
                    "deleted_at=now(),tombstone_reason='expired' WHERE ref=$1 AND state='deleting'", row["ref"],
                )
                deleted += 1
            except ArtifactMissingError:
                await self.pool.execute(
                    "UPDATE kotoyose.execution_input_artifacts SET state='tombstone',content=NULL,"
                    "deleted_at=now(),tombstone_reason='storage_missing' "
                    "WHERE ref=$1 AND state='deleting'", row["ref"],
                )
                deleted += 1
            except (ArtifactStorageError, OSError):
                # deleting は未知状態として残し、reconcile/次回 GC で再確認する。
                continue
        return deleted

    async def reconcile_input_artifacts(self, *, limit: int = 100) -> int:
        """削除途中の external input を readback して tombstone へ収束させる。"""
        rows = await self.pool.fetch(
            "SELECT * FROM kotoyose.execution_input_artifacts WHERE state IN ('pending','deleting') "
            "ORDER BY created_at LIMIT $1", limit,
        )
        reconciled = 0
        for row in rows:
            try:
                blob = ArtifactBlob(row["storage_key"] or hashlib.sha256(row["ref"].encode()).hexdigest(),
                                    row["digest"], row["byte_size"], row["media_type"])
                if row["storage_backend"] == "postgres":
                    if row["content"] is not None:
                        blob.verify(row["content"])
                    if row["state"] == "deleting":
                        await self.pool.execute(
                            "UPDATE kotoyose.execution_input_artifacts SET state='tombstone',content=NULL,"
                            "deleted_at=now(),tombstone_reason='expired' WHERE ref=$1 AND state='deleting'",
                            row["ref"],
                        )
                        reconciled += 1
                        continue
                elif row["state"] == "pending":
                    backend = self.artifact_storage.resolve(row["storage_backend"], row["storage_identity"])
                    await backend.read(blob)
                if row["state"] == "pending":
                    # provider upload 後の DB 応答消失は、readback 成功で active へ収束する。
                    await self.pool.execute(
                        "UPDATE kotoyose.execution_input_artifacts SET state='active' WHERE ref=$1 AND state='pending'",
                        row["ref"],
                    )
                else:
                    backend = self.artifact_storage.resolve(row["storage_backend"], row["storage_identity"])
                    delete = getattr(backend, "delete", None)
                    if not callable(delete):
                        raise ArtifactStorageError("Artifact backend does not support deletion")
                    await delete(blob)
                    await self.pool.execute(
                        "UPDATE kotoyose.execution_input_artifacts SET state='tombstone',content=NULL,"
                        "deleted_at=now(),tombstone_reason='expired' WHERE ref=$1 AND state='deleting'",
                        row["ref"],
                    )
                    reconciled += 1
            except ArtifactMissingError:
                if row["state"] != "deleting":
                    continue
                try:
                    await self.pool.execute(
                        "UPDATE kotoyose.execution_input_artifacts SET state='tombstone',content=NULL,"
                        "deleted_at=now(),tombstone_reason='storage_missing' "
                        "WHERE ref=$1 AND state='deleting'", row["ref"],
                    )
                    reconciled += 1
                except (OSError, asyncpg.PostgresError):
                    continue
            except ArtifactStorageError:
                # 認証・network・identity mismatch は不在の証明ではない。
                continue
        return reconciled
