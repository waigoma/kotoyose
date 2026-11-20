"""
structlog 設定モジュール。

JSONL 形式でログを標準出力に流す。
request_id は contextvars 経由でスレッド/タスクをまたいで自動挿入される。
"""

import logging

import structlog

from .config import LOG_LEVEL


def configure_logging() -> None:
    """structlog を JSONL モードで設定する。アプリ起動時に1回だけ呼ぶ。"""
    level = getattr(logging, LOG_LEVEL, logging.INFO)

    logging.basicConfig(
        format="%(message)s",
        level=level,
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


log = structlog.get_logger()
