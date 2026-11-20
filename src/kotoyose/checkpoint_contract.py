"""Caller が選んだ回復対象と、その固定内容を表す契約。"""

from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from .execution_contract import ContractModel, Ref
from .execution_artifacts import ArtifactSpecEntry

MAX_CHECKPOINT_BYTES = 32 * 1024 * 1024
MAX_CHECKPOINT_FILE_BYTES = 8 * 1024 * 1024
MAX_CHECKPOINT_FILES = 2048
MAX_CHECKPOINT_ARCHIVE_BYTES = MAX_CHECKPOINT_BYTES + 4 * 1024 * 1024
CHECKPOINT_RETENTION_LOCK = 'kotoyose-checkpoint-retention'


class CheckpointFile(ContractModel):
    """内容 digest は取得意図を固定し、origin は Caller の分類として保持する。"""

    path: str = Field(min_length=1, max_length=1024)
    digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    size: int = Field(ge=0, le=MAX_CHECKPOINT_FILE_BYTES, strict=True)
    mode: Literal['0644', '0755']
    origin: Literal['tracked', 'untracked', 'generated']

    @field_validator('path')
    @classmethod
    def safe_path(cls, value: str) -> str:
        """Git の設定・認証保存先と portable な危険 path を archive に入れない。"""
        ArtifactSpecEntry.validate_relative_path(value)
        if len(value.split('/')) > 32:
            raise ValueError('Checkpoint path exceeds its depth limit')
        forbidden = {'.git', '.ssh', '.aws', '.azure', '.config', '.gnupg', '.kube', '.npmrc', '.netrc', '.pypirc', 'credentials', 'credentials.json'}
        for component in value.split('/'):
            lowered = component.lower()
            if lowered in forbidden or lowered == '.env' or lowered.startswith('.env.') or lowered.endswith(('.pem', '.key', '.p12', '.pfx')):
                raise ValueError('Checkpoint path is a credential or control configuration location')
        return value


class CheckpointCapture(ContractModel):
    """停止済み Attempt の現在世代から、明示 manifest だけを回収する冪等要求。"""

    schema_version: Literal['1']
    request_id: UUID
    workspace_ref: Ref
    source_attempt_id: Ref
    source_revision: str | None = Field(default=None, max_length=256)
    files: list[CheckpointFile] = Field(min_length=1, max_length=MAX_CHECKPOINT_FILES)

    @model_validator(mode='after')
    def bounded_manifest(self):
        paths = {entry.path for entry in self.files}
        if len(paths) != len(self.files) or sum(entry.size for entry in self.files) > MAX_CHECKPOINT_BYTES:
            raise ValueError('Checkpoint manifest is duplicated or exceeds the aggregate limit')
        if any('/'.join(entry.path.split('/')[:i]) in paths for entry in self.files for i in range(1, len(entry.path.split('/')))):
            raise ValueError('Checkpoint file paths overlap a parent file')
        return self


class CheckpointRestore(ContractModel):
    """新規ディレクトリだけへの復元。古い Grant や Session は引き継がない。"""

    schema_version: Literal['1']
    request_id: UUID
    caller_workspace_ref: Ref
    path: Ref
    expected_digest: str = Field(pattern=r'^[0-9a-f]{64}$')


class CheckpointRelease(ContractModel):
    """回復点への参照保持を明示的に解除する。bytes の削除は通常 retention が判断する。"""

    schema_version: Literal['1']
    request_id: UUID


class CheckpointAbandon(ContractModel):
    """Caller の破棄判断を固定し、未完了の復元先だけを削除する。"""

    schema_version: Literal['1']
    request_id: UUID
    proof_ref: Ref
