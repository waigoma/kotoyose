"""画像 request の HTTP 応答完了と Responses SSE terminal を別々に検証する。"""

from .codex_request_policy import parse_json


def validate_image_response(body: bytes) -> None:
    """空の HTTP 200 や中断した SSE を画像 delivery の完了証拠にしない。"""
    response_id = None
    completed = False
    for record in body.replace(b"\r\n", b"\n").split(b"\n\n"):
        event = None
        data = []
        for line in record.split(b"\n"):
            if not line or line.startswith(b":"):
                continue
            field, separator, value = line.partition(b":")
            if not separator or field not in {b"event", b"data", b"id", b"retry"}:
                raise ValueError("provider_error")
            if value.startswith(b" "):
                value = value[1:]
            if field == b"event":
                if event is not None:
                    raise ValueError("provider_error")
                event = value
            elif field == b"data":
                data.append(value)
        if not data:
            continue
        value = b"\n".join(data)
        if value == b"[DONE]" and completed:
            continue
        value = parse_json(value)
        if (not isinstance(value, dict) or not isinstance(value.get("type"), str)
                or event is not None and event.decode("ascii") != value["type"]
                or value["type"] in {"error", "response.failed", "response.incomplete"} or completed):
            raise ValueError("provider_error")
        if value["type"] in {"response.created", "response.completed"}:
            response = value.get("response")
            if not isinstance(response, dict) or not isinstance(response.get("id"), str) or not response["id"]:
                raise ValueError("provider_error")
            if value["type"] == "response.created":
                if response_id is not None:
                    raise ValueError("provider_error")
                response_id = response["id"]
            else:
                if response["id"] != response_id or response.get("status") != "completed":
                    raise ValueError("provider_error")
                completed = True
    if not completed:
        raise ValueError("provider_error")
