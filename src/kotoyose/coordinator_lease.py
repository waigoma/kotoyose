"""同一 PostgreSQL session の所有監視と、喪失時の process 再起動境界。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
import os
import time
from typing import NoReturn

import asyncpg
import structlog

from .execution_shutdown import CloseTiming, LEASE_LOST_CLOSE_TIMING

log = structlog.get_logger()

HEARTBEAT_INTERVAL_SECONDS = 5
HEARTBEAT_TIMEOUT_SECONDS = 5
HEARTBEAT_FRESHNESS_SECONDS = 10
LEASE_LOST_EXIT_CODE = 75

_LOCK_KEY = "hashtextextended('kotoyose-local-worker',0)"
# bigint advisory lock の上位・下位 32 bit と objsubid=1 を照合する。
# pg_try_advisory_lock は再入可能なため heartbeat で呼んではいけない。
_OWNERSHIP_QUERY = f"""
SELECT EXISTS (
    SELECT 1 FROM pg_locks
    WHERE locktype = 'advisory' AND pid = pg_backend_pid() AND granted
      AND database = (SELECT oid FROM pg_database WHERE datname = current_database())
      AND classid = (({_LOCK_KEY} >> 32) & 4294967295)::oid
      AND objid = ({_LOCK_KEY} & 4294967295)::oid AND objsubid = 1
)
"""


# lock を失った (または返した) 旧 coordinator が、新しい coordinator の取得後になお生きていられる上限。
# 喪失の検出 (heartbeat の間隔と timeout) と、喪失後 cleanup の全体期限の後に os._exit する。
PREDECESSOR_EXIT_BOUND_SECONDS = (HEARTBEAT_INTERVAL_SECONDS + HEARTBEAT_TIMEOUT_SECONDS
                                  + LEASE_LOST_CLOSE_TIMING.deadline_seconds + 10)


def exit_for_restart() -> NoReturn:
    """uvicorn の正常 shutdown を経ず、Restart=on-failure に固定コードを渡す。"""
    os._exit(LEASE_LOST_EXIT_CODE)


class CoordinatorLease:
    """取得 session を監視し、所有喪失時は受付閉鎖・cleanup 後に必ず異常終了する。"""

    def __init__(self, pool: asyncpg.Pool, close_admission: Callable[[], None],
                 cleanup: Callable[[CloseTiming], Awaitable[None]]):
        self.pool = pool
        self.connection: asyncpg.Connection | None = None
        self.watchdog: asyncio.Task | None = None
        self.last_success: float | None = None
        self.state = "starting"
        self._close_admission = close_admission
        self._cleanup = cleanup
        self._loss_error_type: str | None = None
        # lock を取得した時点。DB の時計 (attempt の created_at と同じ時計) と、経過時間用の単調時計。
        self.acquired_at = None
        self.acquired_monotonic: float | None = None

    async def acquire(self) -> None:
        """起動時だけ lock を取得する。他 coordinator の所有中は起動を拒否する。"""
        self.connection = await self.pool.acquire(timeout=HEARTBEAT_TIMEOUT_SECONDS)
        try:
            async with asyncio.timeout(HEARTBEAT_TIMEOUT_SECONDS):
                acquired = await self.connection.fetchval(f"SELECT pg_try_advisory_lock({_LOCK_KEY})")
            if not acquired:
                raise RuntimeError("Another Kotoyose coordinator owns the LocalWorker")
            async with asyncio.timeout(HEARTBEAT_TIMEOUT_SECONDS):
                self.acquired_at = await self.connection.fetchval("SELECT clock_timestamp()")
            self.acquired_monotonic = time.monotonic()
        except BaseException:
            await self.release()
            raise

    async def start(self) -> None:
        """旧 worker の照合後に所有を検証し、監視を開始する。未確認なら受付しない。"""
        await self._verify()
        self.state = "held"
        self.watchdog = asyncio.create_task(self._watch(), name="execution-coordinator-lease")

    async def _verify(self) -> None:
        async with asyncio.timeout(HEARTBEAT_TIMEOUT_SECONDS):
            if self.connection is None or not await self.connection.fetchval(_OWNERSHIP_QUERY):
                raise RuntimeError("Coordinator lease ownership was lost")
        self.last_success = time.monotonic()

    def heartbeat_age(self) -> float | None:
        """成功した所有確認からの秒数を返す。未確認なら None。時計補正の影響は受けない。"""
        return None if self.last_success is None else max(0, time.monotonic() - self.last_success)

    def readiness_reason(self, accepting: bool) -> str | None:
        """DB 操作なしで readiness の不成立理由を返す。成立時だけ None。"""
        if self.state != "held":
            return {
                "starting": "coordinator_starting", "lost": "coordinator_lease_lost",
                "stopping": "coordinator_stopping",
            }[self.state]
        if self.connection is None or self.last_success is None:
            return "coordinator_lease_unverified"
        if not accepting:
            return "coordinator_admission_closed"
        if self.watchdog is None or self.watchdog.done():
            return "coordinator_watchdog_dead"
        if self.heartbeat_age() >= HEARTBEAT_FRESHNESS_SECONDS:
            return "coordinator_heartbeat_stale"
        return None

    def diagnostic_fields(self) -> dict:
        """例外本文や接続情報を含めず、喪失・終了ログ用の値だけを返す。"""
        return {"error_type": self._loss_error_type,
                "last_heartbeat_age_seconds": self.heartbeat_age()}

    async def _watch(self) -> None:
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
                await self._verify()
        except Exception as exc:
            self.state = "lost"
            self._loss_error_type = type(exc).__name__
            self._close_admission()
            # driver の例外本文には DSN が含まれ得るため traceback も出さない。
            log.error("execution.lease_lost", **self.diagnostic_fields())
            try:
                await self._cleanup(LEASE_LOST_CLOSE_TIMING)
            finally:
                log.error("execution.exiting_for_restart", exit_code=LEASE_LOST_EXIT_CODE,
                          **self.diagnostic_fields())
                exit_for_restart()

    def begin_close(self) -> None:
        """通常 shutdown を記録する。worker 停止中も lease 喪失の監視は続ける。"""
        if self.state != "lost":
            self.state = "stopping"

    async def stop_watchdog(self) -> None:
        """lease 返却直前に通常監視を止める。喪失時は cleanup 待機中の監視を止めない。"""
        if self.state != "lost" and self.watchdog is not None:
            self.watchdog.cancel()
            await asyncio.gather(self.watchdog, return_exceptions=True)

    def abort(self) -> None:
        """期限切れの接続を同期的に破棄する。worker の停止確認は変更しない。"""
        if self.connection is not None:
            with suppress(Exception):
                self.connection.terminate()

    async def release(self) -> None:
        """unlock と pool 返却に各 5 秒の上限を設け、失敗時は接続を破棄する。"""
        lease = self.connection
        if lease is None:
            return
        try:
            try:
                async with asyncio.timeout(HEARTBEAT_TIMEOUT_SECONDS):
                    await lease.execute(f"SELECT pg_advisory_unlock({_LOCK_KEY})")
            except Exception as exc:
                log.warning("execution.shutdown_lease_release_failed", error_type=type(exc).__name__)
                self.abort()
            async with asyncio.timeout(HEARTBEAT_TIMEOUT_SECONDS):
                await self.pool.release(lease, timeout=HEARTBEAT_TIMEOUT_SECONDS)
        except Exception as exc:
            log.warning("execution.shutdown_lease_return_failed", error_type=type(exc).__name__)
            self.abort()
        finally:
            self.abort()
            self.connection = None
