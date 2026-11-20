"""
リクエスト ID ミドルウェア。

各リクエストに UUID ベースの request_id を発行し、
structlog の contextvars に束縛する。
以降のログ出力には自動的に request_id が付く。
"""

import uuid

import structlog
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware


class RequestIDMiddleware(BaseHTTPMiddleware):
    """request_id を発行して structlog contextvars に設定する。"""

    async def dispatch(self, request: Request, call_next):
        request_id = f"req_{uuid.uuid4().hex[:8]}"
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        # downstream のコードで request.state.request_id を参照できる
        request.state.request_id = request_id

        try:
            response = await call_next(request)
        finally:
            structlog.contextvars.clear_contextvars()
        return response
