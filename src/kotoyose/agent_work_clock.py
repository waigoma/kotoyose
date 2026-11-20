"""重複する承認待ちを除いた単調増加の agent 実行時間。"""

from datetime import datetime, timezone
from typing import Literal
import time
import logging

from .execution_limits import ExecutionTime


class AgentWorkClock:
    """起動から停止までの時間を保持し、承認 bridge の重複待機を除外する。"""

    def __init__(self, *, monotonic=None, wall_time=None):
        self._monotonic = monotonic or time.monotonic
        self._wall_time = wall_time or (lambda: datetime.now(timezone.utc))
        self._started_at = None
        self._finished_at = None
        self._segment = None
        self._elapsed = 0.0
        self._waits = 0

    def start(self) -> None:
        """process 起動時に開始する。同じ時計の再開始は拒否する。"""
        if self._started_at is not None:
            raise RuntimeError('clock already started')
        self._started_at = self._wall_time()
        self._segment = self._monotonic()

    def approval_wait_started(self) -> None:
        """承認応答待ちの間だけ計測を停止する。待機は重複できる。"""
        if self._segment is not None and self._waits == 0:
            self._elapsed += self._monotonic() - self._segment
            self._segment = None
        self._waits += 1

    def approval_wait_finished(self) -> None:
        """最後の承認待ちが終わったとき計測を再開する。"""
        if self._waits <= 0:
            raise RuntimeError('approval wait was not started')
        self._waits -= 1
        if self._waits == 0 and self._started_at is not None and self._finished_at is None:
            self._segment = self._monotonic()

    def sample(self) -> ExecutionTime:
        """現在の観測をミリ秒未満切り捨てで返す。"""
        elapsed = self._elapsed
        if self._segment is not None:
            elapsed += self._monotonic() - self._segment
        state = 'ended' if self._finished_at is not None else 'running' if self._started_at else 'not_started'
        return ExecutionTime(state=state, active_ms=max(0, int(elapsed * 1000)),
                             started_at=self._started_at, observed_at=self._wall_time(), finished_at=self._finished_at)

    def stop(self) -> ExecutionTime:
        """停止時刻を固定する。繰り返しても稼働時間は変わらない。"""
        if self._finished_at is None:
            if self._segment is not None:
                self._elapsed += self._monotonic() - self._segment
                self._segment = None
            self._finished_at = self._wall_time()
        return self.sample()


class WorkClockTracker:
    """Persist time fields independently from provider and resource observations."""

    def __init__(self, repository, principal, attempt_id):
        import asyncio
        self.clock = AgentWorkClock()
        self.repository = repository
        self.principal = principal
        self.attempt_id = attempt_id
        self._lock = asyncio.Lock()
        self._task = None
        self._final_observation = None
        self.tool_stop_idle = asyncio.Event()
        self.tool_stop_idle.set()
        self._unconfirmed_tools = set()
        self.tool_operations = set()

    def tool_started(self):
        token = object()
        self._unconfirmed_tools.add(token)
        self.tool_stop_idle.clear()
        return token

    def tool_stopped(self, token):
        self._unconfirmed_tools.remove(token)
        if not self._unconfirmed_tools:
            self.tool_stop_idle.set()

    async def start(self):
        import asyncio
        self.clock.start()
        try:
            await self.checkpoint()
        except Exception as exc:
            logging.getLogger(__name__).warning('work clock checkpoint failed: %s', type(exc).__name__)
        self._task = asyncio.create_task(self._periodic())

    async def checkpoint(self):
        async with self._lock:
            sample = self._final_observation or self.clock.sample()
            if self._final_observation is None and sample.state in ('not_started', 'ended'):
                # 起動通知前と実停止後は、最終証拠が得られるまで断定しない。
                sample = sample.model_copy(update={'state': 'unknown', 'finished_at': None})
            await self.repository.save_time(self.principal, self.attempt_id, sample)

    async def _periodic(self):
        import asyncio
        while True:
            await asyncio.sleep(1)
            try:
                await self.checkpoint()
            except Exception as exc:
                logging.getLogger(__name__).warning('work clock checkpoint failed: %s', type(exc).__name__)

    async def finish(self, *, confirmed: bool, launch_outcome: Literal['not_started', 'started', 'unknown'] = 'unknown'):
        """停止と起動の証拠を保存し、起動不明なら既知の下限を unknown のまま残す。"""
        import asyncio
        import contextlib
        final = self.clock.stop()
        if ((final.started_at is None and launch_outcome != 'not_started')
                or (final.started_at is not None and not confirmed)):
            final = final.model_copy(update={'state': 'unknown', 'finished_at': None})
        # 同一 event loop 内で停止と最終状態の公開を中断せず行う。
        self._final_observation = final
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        async with self._lock:
            await self.repository.save_time(self.principal, self.attempt_id, final)
