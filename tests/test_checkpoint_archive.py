"""回復 bytes の完全性、選択範囲、filesystem と credential の境界を検証する。"""

import hashlib
from uuid import uuid4

import pytest
from pydantic import ValidationError

from kotoyose.checkpoint_archive import capture_checkpoint, verify_checkpoint, write_checkpoint
from kotoyose.checkpoint_contract import CheckpointCapture, CheckpointFile
from kotoyose.execution_artifacts import ArtifactCaptureError


def capture_request(files):
    return CheckpointCapture(schema_version='1', request_id=uuid4(), workspace_ref='workspace:test:1',
                             source_attempt_id='attempt-test', files=files)


def entry(path, content=b'', **kwargs):
    return CheckpointFile(path=path, digest=hashlib.sha256(content).hexdigest(), size=len(content),
                          mode='0644', origin='untracked', **kwargs)


@pytest.mark.parametrize('path', ['../outside', '/absolute', 'a/../b', 'a\\b', '.git/config', '.env', 'sub/.env.local', '.ssh/id_rsa', 'config/token.pem', '.aws/credentials', '.npmrc'])
def test_sensitive_or_escaping_paths_are_not_checkpoint_inputs(path):
    with pytest.raises(ValidationError):
        entry(path)


def test_tracked_untracked_and_generated_bytes_roundtrip_without_ambient_files(tmp_path):
    workspace = tmp_path / 'source'
    workspace.mkdir()
    (workspace / 'run.sh').write_bytes(b'echo hello')
    (workspace / 'empty').write_bytes(b'')
    (workspace / '.env').write_text('SECRET')
    request = capture_request([entry('run.sh', b'echo hello').model_copy(update={'mode': '0755', 'origin': 'tracked'}), entry('empty')])
    info = workspace.stat()
    data = capture_checkpoint(workspace, info.st_dev, info.st_ino, request)
    assert b'SECRET' not in data
    manifest, payloads = verify_checkpoint(data, hashlib.sha256(data).hexdigest())
    target = tmp_path / 'restored'
    target.mkdir(mode=0o700)
    write_checkpoint(target, manifest, payloads)
    assert (target / 'run.sh').read_bytes() == b'echo hello'
    assert (target / 'run.sh').stat().st_mode & 0o777 == 0o755
    assert (target / 'empty').read_bytes() == b''
    assert not (target / '.env').exists()
    with pytest.raises(ArtifactCaptureError):
        verify_checkpoint(data + b'changed', hashlib.sha256(data).hexdigest())


def test_symlink_hardlink_changed_bytes_and_replaced_root_cannot_be_saved(tmp_path):
    (tmp_path / 'actual').write_bytes(b'original')
    (tmp_path / 'link').symlink_to('actual')
    (tmp_path / 'hardlink').hardlink_to(tmp_path / 'actual')
    info = tmp_path.stat()
    for filename in ['link', 'hardlink']:
        with pytest.raises(ArtifactCaptureError):
            capture_checkpoint(tmp_path, info.st_dev, info.st_ino, capture_request([entry(filename, b'original')]))
    (tmp_path / 'other').write_bytes(b'changed')
    with pytest.raises(ArtifactCaptureError):
        capture_checkpoint(tmp_path, info.st_dev, info.st_ino, capture_request([entry('other', b'expected')]))
    with pytest.raises(ArtifactCaptureError):
        capture_checkpoint(tmp_path, info.st_dev, info.st_ino + 1, capture_request([entry('other', b'changed')]))


def test_duplicate_and_parent_paths_are_rejected_before_io():
    for files in [[entry('same'), entry('same')], [entry('parent'), entry('parent/child')]]:
        with pytest.raises(ValidationError):
            capture_request(files)
