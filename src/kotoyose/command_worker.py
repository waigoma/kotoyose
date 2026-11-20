"""実行場所によらない command の開始と停止確認の境界。"""

from __future__ import annotations

import asyncio
from typing import Protocol

from .execution_jobs import CommandJob, StopConfirmation, WorkerResult


class CommandCancelledError(asyncio.CancelledError):
    """同じ execute 呼出しで得た停止確認を、取消のまま controller へ渡す。"""

    def __init__(self, job: CommandJob, stop_confirmation: StopConfirmation) -> None:
        super().__init__()
        self.job = job
        self.stop_confirmation = stop_confirmation


class CommandWorker(Protocol):
    """明示選択した backend の保証を維持し、別 backend へ fallback しない。"""

    async def probe(self) -> bool:
        """実行に必要な隔離機構と resource 制御が利用できるか確認する。"""
        ...

    async def execute(self, job: CommandJob, cancel: asyncio.Event) -> WorkerResult:
        """一意の operation を一度だけ起動し、実停止と結果を分けて返す。"""
        ...

    async def stop(self, attempt_id: str, generation: int, grace_seconds: float) -> str:
        """指定世代の全処理を停止し、証拠が不足する場合は unconfirmed を返す。"""
        ...
