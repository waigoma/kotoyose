"""管理者設定から ArtifactStore の実装を構成する composition boundary。"""

from pathlib import Path

from .artifact_blob_store import ArtifactBlobRegistry, ArtifactBlobStore
from .artifact_storage_config import ArtifactStorageConfig, LocalArtifactStorageConfig
from .local_artifact_store import LocalArtifactStore


def create_artifact_storage(config: ArtifactStorageConfig, workspace_roots: list[Path]) -> ArtifactBlobRegistry:
    """Local FS を実行 workspace と分離し、S3 の任意依存は選択時だけ解決する。"""
    backends: dict[str, ArtifactBlobStore] = {}
    for name, backend in config.backends.items():
        if isinstance(backend, LocalArtifactStorageConfig):
            resolved = backend.root.resolve()
            if any(resolved.is_relative_to(root.resolve()) or root.resolve().is_relative_to(resolved)
                   for root in workspace_roots):
                raise ValueError("Artifact root and execution workspace roots must not overlap")
            backends[name] = LocalArtifactStore(backend.root)
        else:
            from .s3_artifact_store import S3ArtifactStore
            backends[name] = S3ArtifactStore(backend)
    return ArtifactBlobRegistry(config.default_backend, backends)
