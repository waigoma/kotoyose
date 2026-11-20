"""復元 intent を先に保存し、新規 workspace を完全性確認後に登録する。"""

from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import NAMESPACE_URL, uuid5
from zipfile import BadZipFile

from .checkpoint_archive import verify_checkpoint, write_checkpoint
from .checkpoint_contract import CHECKPOINT_RETENTION_LOCK, CheckpointRestore
from .checkpoint_store import reject_reserved_restore_path
from .execution_artifacts import ArtifactCaptureError, ArtifactCaptureSpec, ArtifactSpecEntry, collect_execution_artifacts
from .execution_resources import CallerPolicy, ExecutionError, canonical_json, content_digest

if TYPE_CHECKING:
    from .execution_store import ExecutionStore


def restore_path(policy: CallerPolicy, value: str) -> Path:
    """存在する実 parent の下にだけ新規 path を作り、root 自体や symlink を拒む。"""
    original = Path(value)
    try:
        parent = original.parent.resolve(strict=True)
        roots = [root.resolve(strict=True) for root in policy.workspace_roots]
    except OSError as exc:
        raise ExecutionError('invalid_request', 'Restore parent is unavailable') from exc
    if not original.is_absolute() or original.name in {'', '.', '..'} or parent != original.parent or original.is_symlink():
        raise ExecutionError('permission_denied', 'Restore path must have a canonical real parent', 403)
    path = parent / original.name
    if not any(path != root and path.is_relative_to(root) for root in roots):
        raise ExecutionError('permission_denied', 'Restore path is outside caller roots', 403)
    return path


class CheckpointRestorer:
    """途中の復元を通常登録から除外し、同一要求の成功 receipt だけを再利用する。"""

    def __init__(self, store: ExecutionStore):
        self.store = store
        self.pool = store.pool

    async def restore(self, principal: str, policy: CallerPolicy, ref: str, request: CheckpointRestore) -> dict:
        """停止済み checkpoint を新しい writer 世代へ登録する。破損・曖昧な途中状態では停止する。"""
        identity = str(uuid5(NAMESPACE_URL, f'kotoyose-restore:{principal}:{request.request_id}'))
        digest = content_digest({'checkpoint_ref': ref, **request.model_dump(mode='json')})
        async with self.pool.acquire() as conn:
            await conn.execute('SELECT pg_advisory_lock(hashtextextended($1,0))', f'checkpoint-restore:{identity}')
            try:
                prior = await conn.fetchrow('SELECT * FROM kotoyose.execution_checkpoint_restores WHERE principal=$1 AND request_id=$2', principal, str(request.request_id))
                if prior and prior['request_digest'] != digest:
                    raise ExecutionError('request_conflict', 'Restore request ID changed', 409)
                if prior and prior['state'] == 'confirmed':
                    return json.loads(prior['result'])
                if prior and prior['state'] != 'prepared':
                    raise ExecutionError('request_conflict', 'Restore has been abandoned or is being abandoned', 409)
                path = restore_path(policy, request.path)
                checkpoint = await self.store.checkpoints.get(principal, ref)
                artifact = checkpoint['artifact']
                if artifact['digest'] != request.expected_digest:
                    raise ExecutionError('request_conflict', 'Restore digest differs from the saved checkpoint', 409)
                if prior is None:
                    await self._prepare(conn, identity, principal, ref, digest, request, path, artifact['ref'])
                    # mkdir は既存内容を再利用しない。完了前の crash は予約を残して後続登録を防ぐ。
                    path.mkdir(mode=0o700)
                    info = path.stat()
                    await conn.execute('UPDATE kotoyose.execution_checkpoint_restores SET device=$2,inode=$3 WHERE id=$1', identity, info.st_dev, info.st_ino)
                    prior = await conn.fetchrow('SELECT * FROM kotoyose.execution_checkpoint_restores WHERE id=$1', identity)
                info = path.lstat()
                if path.is_symlink() or not path.is_dir() or (info.st_dev, info.st_ino) != (prior['device'], prior['inode']):
                    raise ExecutionError('request_conflict', 'Restore directory identity is unknown or changed', 409)
                saved = await self.store.artifact(principal, artifact['ref'])
                manifest, payloads = verify_checkpoint(saved['content'], request.expected_digest)
                # 既存 file は上書きしない。途中状態は全 bytes が一致する時だけ復旧できる。
                if not any(path.iterdir()):
                    write_checkpoint(path, manifest, payloads)
                self._verify_restored(path, info.st_dev, info.st_ino, manifest)
                async with conn.transaction():
                    await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('kotoyose-workspaces',0))")
                    await reject_reserved_restore_path(conn, str(path), restore_id=identity)
                    current = path.lstat()
                    if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                        raise ExecutionError('request_conflict', 'Restore directory changed before registration', 409)
                    await conn.execute('INSERT INTO kotoyose.execution_workspaces(id,principal,caller_ref,path,device,inode) VALUES($1,$2,$3,$4,$5,$6)',
                                       identity, principal, request.caller_workspace_ref, str(path), info.st_dev, info.st_ino)
                    await self.store.workspace_retention.register(conn, identity)
                    result = {'schema_version': '1', 'checkpoint_ref': ref, 'workspace_ref': f'workspace:{identity}:0',
                              'request_id': str(request.request_id), 'caller_workspace_ref': request.caller_workspace_ref, 'checkpoint_digest': request.expected_digest,
                              'workspace_generation': 0, 'source_workspace_ref': checkpoint['workspace_ref'],
                              'recovery_point': {'source_attempt_id': checkpoint['source_attempt_id'], 'source_revision': checkpoint['source_revision'],
                                                 'coverage': 'selected_files', 'session_state': 'excluded', 'later_changes': 'not_restored'}}
                    await conn.execute("UPDATE kotoyose.execution_checkpoint_restores SET state='confirmed',result=$2::jsonb,finished_at=now() WHERE id=$1", identity, canonical_json(result))
                    await self.store._event(conn, checkpoint['source_attempt_id'], 'checkpoint_restored', {'checkpoint_ref': ref, 'workspace_ref': result['workspace_ref']})
                return result
            except (OSError, ArtifactCaptureError, ValueError, BadZipFile) as exc:
                raise ExecutionError('request_conflict', 'Checkpoint restore is incomplete or failed verification; its reserved directory was retained', 409) from exc
            finally:
                await conn.execute('SELECT pg_advisory_unlock(hashtextextended($1,0))', f'checkpoint-restore:{identity}')

    async def _prepare(self, conn, identity, principal, ref, digest, request, path, artifact_ref):
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('kotoyose-workspaces',0))")
            await conn.execute('SELECT pg_advisory_xact_lock(hashtextextended($1,0))', CHECKPOINT_RETENTION_LOCK)
            await reject_reserved_restore_path(conn, str(path))
            overlap = await conn.fetchval("SELECT EXISTS(SELECT 1 FROM kotoyose.execution_workspaces WHERE path=$1 OR starts_with(path,$1||'/') OR starts_with($1,path||'/') OR (principal=$2 AND caller_ref=$3))", str(path), principal, request.caller_workspace_ref)
            if overlap or path.exists():
                raise ExecutionError('request_conflict', 'Restore requires an unused workspace path and caller reference', 409)
            retention = await conn.fetchrow('SELECT state FROM kotoyose.execution_artifact_retention WHERE artifact_ref=$1 FOR UPDATE', artifact_ref)
            if retention is None or retention['state'] != 'active':
                raise ExecutionError('artifact_expired', 'Checkpoint bytes are no longer available', 410)
            await conn.execute('INSERT INTO kotoyose.execution_checkpoint_restores(id,principal,request_id,request_digest,checkpoint_id,caller_ref,path) VALUES($1,$2,$3,$4,$5,$6,$7)',
                               identity, principal, str(request.request_id), digest, ref.removeprefix('checkpoint:'), request.caller_workspace_ref, str(path))

    @staticmethod
    def _verify_restored(path, device, inode, manifest):
        expected = {entry.path for entry in manifest.files}
        expected_directories = {'/'.join(entry.path.split('/')[:i]) for entry in manifest.files for i in range(1, len(entry.path.split('/')))}
        actual = set()
        for parent, directories, files in os.walk(path, followlinks=False):
            if any((Path(parent) / directory).is_symlink() or (Path(parent) / directory).relative_to(path).as_posix() not in expected_directories for directory in directories):
                raise ArtifactCaptureError('Restore contains an unexpected directory or link')
            actual.update((Path(parent) / filename).relative_to(path).as_posix() for filename in files)
            if actual - expected:
                raise ArtifactCaptureError('Restore contains unexpected files')
        if expected != actual:
            raise ArtifactCaptureError('Restored files differ from the checkpoint manifest')
        spec = ArtifactCaptureSpec(schema_version='1', kind='artifact_spec',
            entries=[ArtifactSpecEntry(name=str(i), path=entry.path, media_type='application/octet-stream', required=True, max_bytes=max(entry.size, 1)) for i, entry in enumerate(manifest.files)],
            max_total_bytes=sum(max(entry.size, 1) for entry in manifest.files))
        captured = collect_execution_artifacts(path, device, inode, spec)
        if not captured.contract_valid:
            raise ArtifactCaptureError('Restored files are unreadable')
        for i, entry in enumerate(manifest.files):
            content = captured.artifacts[str(i)].content
            if len(content) != entry.size or hashlib.sha256(content).hexdigest() != entry.digest or (path / entry.path).stat().st_mode & 0o777 != int(entry.mode, 8):
                raise ArtifactCaptureError('Restored files differ from the saved bytes or mode')
