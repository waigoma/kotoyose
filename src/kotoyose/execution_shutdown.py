"""通常終了と lease 喪失で使う cleanup の待機時間。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class CloseTiming:
    """worker 待機・取消後の停止待機・呼出元の全体期限 (None は無制限) を指定する。

    共有 cleanup task の worker 待機には最初の呼出元の値を使い、全体期限は呼出元ごとに適用する。
    """

    worker_wait_seconds: float
    settle_wait_seconds: float
    deadline_seconds: float | None


NORMAL_CLOSE_TIMING = CloseTiming(90, 30, None)
LEASE_LOST_CLOSE_TIMING = CloseTiming(10, 5, 20)
