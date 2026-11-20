"""OpenAI 互換エラーレスポンスモデルのテスト。"""

from kotoyose.models import ErrorDetail, ErrorResponse


def test_error_response_structure():
    """ErrorResponse は {"error": {..., "request_id": ...}} 形式になる。"""
    resp = ErrorResponse(
        error=ErrorDetail(
            message="Claude SDK error: connection refused",
            type="internal_error",
            request_id="req_abc123",
        )
    )
    data = resp.model_dump()
    assert data["error"]["message"] == "Claude SDK error: connection refused"
    assert data["error"]["type"] == "internal_error"
    assert data["error"]["request_id"] == "req_abc123"


def test_error_detail_defaults():
    """type のデフォルトは internal_error。"""
    detail = ErrorDetail(message="oops", request_id="req_1")
    assert detail.type == "internal_error"
