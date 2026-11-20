"""受付閉鎖時も受理済み key を優先し、照合不能を未受理と区別する。"""

import asyncpg

from .coordinator_lease import CoordinatorLease
from .execution_resources import ExecutionError
from .execution_store import ExecutionStore


def admission_closed_error(coordinator: CoordinatorLease | None) -> ExecutionError:
    """受付前の拒否を返す。lease 喪失が確認済みの場合だけ専用理由を付ける。"""
    return ExecutionError(
        "provider_failure", "Execution coordinator is not accepting work", 503,
        safe_to_retry=True,
        reason_code="coordinator_lease_lost" if coordinator is not None and coordinator.state == "lost" else None,
    )


async def replay_execution(store: ExecutionStore, principal: str, key: str, digest: str, *,
                           coordinator: CoordinatorLease | None = None, **options: str) -> dict | None:
    """既存 Attempt または未受理の None を返す。喪失中の照合不能は安全な再送を保証しない。"""
    try:
        return await store.replay(principal, key, digest, **options)
    except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError):
        if coordinator is None or coordinator.state != "lost":
            raise
        # replay の照合不能は「未受理」の証拠ではない。
        error = admission_closed_error(coordinator)
        error.safe_to_retry = False
        raise error from None
