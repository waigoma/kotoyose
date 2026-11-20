"""明示設定された Native execution をアプリの寿命に組み込む。"""

import os
from pathlib import Path

from . import db
from .config_paths import resolve_execution_config_path
from .artifact_storage_factory import create_artifact_storage
from .config import get_clients
from .execution_resources import NativeExecutionConfig
from .execution_service import ExecutionService
from .execution_store import ExecutionStore
from .local_worker import DockerLocalWorker
from .execution_jobs import WorkerResult
import asyncio


async def start_execution_service() -> ExecutionService | None:
    """設定なしは direct chatを維持し、設定不備は起動時に失敗させる。"""
    config_path = resolve_execution_config_path()
    if config_path is None:
        return None
    config = NativeExecutionConfig.model_validate_json(Path(config_path).read_text())
    if db._pool is None:
        raise RuntimeError("Native execution requires DATABASE_URL and a durable Postgres store")
    storage = create_artifact_storage(config.artifact_storage, [
        root for client in get_clients().values() for root in client.workspace_roots
    ])
    local_worker = DockerLocalWorker(config.image_id) if "local-docker" in config.worker_capabilities else _UnavailableWorker()
    service = ExecutionService(config, ExecutionStore(db._pool, storage), local_worker)
    await service.start()
    return service


class _UnavailableWorker:
    """remote-only 構成で host Docker を暗黙起動しない placeholder。"""

    async def probe(self) -> bool:
        return False

    async def execute(self, job, cancel: asyncio.Event) -> WorkerResult:
        raise RuntimeError("local Docker worker is not configured")

    async def stop(self, attempt_id: str, generation: int, grace_seconds: float) -> str:
        return "unconfirmed"
