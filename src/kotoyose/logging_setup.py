"""
structlog 設定モジュール。

JSONL 形式でログを標準出力に流す。
request_id は contextvars 経由でスレッド/タスクをまたいで自動挿入される。
"""

import logging

import structlog

from .config import LOG_LEVEL
from .delegation import redact_token


class _DelegationSafeFormatter(logging.Formatter):
    def __init__(self, formatter: logging.Formatter | None):
        super().__init__("%(message)s")
        self.formatter = formatter

    def format(self, record: logging.LogRecord) -> str:
        line = self.formatter.format(record) if self.formatter else super().format(record)
        return redact_token(line)


def _redact_structlog_output(_logger, _method, log_line: str) -> str:
    return redact_token(log_line)


def configure_logging() -> None:
    """structlog を JSONL モードで設定する。アプリ起動時に1回だけ呼ぶ。"""
    level = getattr(logging, LOG_LEVEL, logging.INFO)

    logging.basicConfig(
        format="%(message)s",
        level=level,
    )

    # HTTP client のログには上流の reason phrase、応答ヘッダ、生の例外が含まれ得る。
    for logger_name in ("httpx", "httpcore"):
        logging.getLogger(logger_name).setLevel(max(level, logging.WARNING))

    for handler in logging.getLogger().handlers:
        if not isinstance(handler.formatter, _DelegationSafeFormatter):
            handler.setFormatter(_DelegationSafeFormatter(handler.formatter))

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
            structlog.processors.JSONRenderer(),
            _redact_structlog_output,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


log = structlog.get_logger()
