"""実行保証を満たせない要求の HTTP 応答を両 API で共有する。"""

from fastapi.responses import JSONResponse

from .capabilities import CapabilityName


def unsupported_response(required: list[CapabilityName], request_id: str) -> JSONResponse:
    """未 dispatch の拒否を返す。再試行可能性と再送の安全性を分離する。"""
    return JSONResponse(status_code=501, content={"error": {
        "type": "unsupported_capability",
        "code": "unsupported_capability",
        "message": "Required execution capabilities are not supported.",
        "request_id": request_id,
        "capabilities": required,
        "retryable": False,
        "safe_to_retry": True,
    }})
