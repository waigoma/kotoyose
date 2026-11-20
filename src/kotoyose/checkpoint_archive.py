"""有限の checkpoint bytes を作成・検査し、専有ディレクトリに展開する。"""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
from zipfile import ZIP_STORED, ZipFile, ZipInfo

from .checkpoint_contract import CheckpointCapture, MAX_CHECKPOINT_ARCHIVE_BYTES
from .execution_artifacts import ArtifactCaptureError, ArtifactCaptureSpec, ArtifactSpecEntry, collect_execution_artifacts
from .execution_resources import canonical_json


def capture_checkpoint(path: Path, device: int, inode: int, capture: CheckpointCapture) -> bytes:
    """既存の fd ベース collector を使い、固定 digest と一致したファイルだけを保存する。"""
    entries = [ArtifactSpecEntry(name=str(i), path=entry.path, media_type='application/octet-stream', required=True,
                                max_bytes=max(entry.size, 1)) for i, entry in enumerate(capture.files)]
    spec = ArtifactCaptureSpec(schema_version='1', kind='artifact_spec', entries=entries,
                               max_total_bytes=sum(max(entry.size, 1) for entry in capture.files))
    result = collect_execution_artifacts(path, device, inode, spec)
    if not result.contract_valid:
        raise ArtifactCaptureError('Checkpoint input is unavailable or unsafe')
    payloads = [result.artifacts[str(i)].content for i in range(len(entries))]
    for entry, data in zip(capture.files, payloads, strict=True):
        if len(data) != entry.size or hashlib.sha256(data).hexdigest() != entry.digest:
            raise ArtifactCaptureError('Checkpoint input changed from the requested manifest')
    stream = io.BytesIO()
    with ZipFile(stream, 'w', compression=ZIP_STORED) as archive:
        archive.writestr(ZipInfo('manifest.json'), canonical_json(capture.model_dump(mode='json')))
        for i, data in enumerate(payloads):
            archive.writestr(ZipInfo(f'files/{i}'), data)
    data = stream.getvalue()
    if len(data) > MAX_CHECKPOINT_ARCHIVE_BYTES:
        raise ArtifactCaptureError('Checkpoint archive exceeds its byte limit')
    return data


def verify_checkpoint(data: bytes, expected_digest: str) -> tuple[CheckpointCapture, list[bytes]]:
    """保存 digest、manifest、各 entry を再検査し、ZIP の path / 圧縮指定には従わない。"""
    if len(data) > MAX_CHECKPOINT_ARCHIVE_BYTES or hashlib.sha256(data).hexdigest() != expected_digest:
        raise ArtifactCaptureError('Checkpoint archive digest or size is invalid')
    with ZipFile(io.BytesIO(data)) as archive:
        infos = archive.infolist()
        if not infos or infos[0].filename != 'manifest.json' or infos[0].file_size > 3 * 1024 * 1024:
            raise ArtifactCaptureError('Checkpoint manifest is invalid')
        if any(info.compress_type != ZIP_STORED or info.flag_bits & 1 or info.compress_size != info.file_size for info in infos):
            raise ArtifactCaptureError('Checkpoint encoding is unsupported')
        manifest = CheckpointCapture.model_validate(json.loads(archive.read(infos[0])))
        if [info.filename for info in infos] != ['manifest.json', *(f'files/{i}' for i in range(len(manifest.files)))]:
            raise ArtifactCaptureError('Checkpoint entries differ from its manifest')
        payloads = []
        for entry, info in zip(manifest.files, infos[1:], strict=True):
            if info.file_size != entry.size:
                raise ArtifactCaptureError('Checkpoint entry size is invalid')
            content = archive.read(info)
            if hashlib.sha256(content).hexdigest() != entry.digest:
                raise ArtifactCaptureError('Checkpoint entry digest is invalid')
            payloads.append(content)
    return manifest, payloads


def write_checkpoint(directory: Path, manifest: CheckpointCapture, payloads: list[bytes]) -> None:
    """Caller からアクセス不能な、新規専有 staging directory 内だけへ復元する。"""
    for entry, content in zip(manifest.files, payloads, strict=True):
        path = directory / entry.path
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with path.open('xb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        path.chmod(int(entry.mode, 8))
