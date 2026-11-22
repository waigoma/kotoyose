"""標準 Responses の history と job 固定画像だけを受け入れる。"""

import json


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("egress_denied")
        value[key] = item
    return value


def parse_json(body: bytes):
    """duplicate keys と非標準数値を拒否し parser 間の曖昧さをなくす。"""
    def invalid(_):
        raise ValueError("egress_denied")
    return json.loads(body, object_pairs_hook=_object, parse_constant=invalid)


def _text_content(content, types):
    if not isinstance(content, list):
        raise ValueError("egress_denied")
    for item in content:
        if (not isinstance(item, dict) or item.get("type") not in types
                or not isinstance(item.get("text"), str)
                or set(item) - {"type", "text", "annotations"}
                or item.get("annotations", []) != []):
            raise ValueError("egress_denied")


def validate_input(items, has_tool, image_uris: tuple[str, ...] = (), *, coding: bool = False, function_names=None):
    """remote media/files、AdditionalTools、未知の API input を推論前に拒否する。"""
    if not isinstance(items, list):
        raise ValueError("egress_denied")
    names = function_names if function_names is not None else ({"exec_command", "write_stdin"} if coding else {"sandbox_exec"})
    observed = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("egress_denied")
        kind = item.get("type")
        if kind == "message":
            if (set(item) - {"type", "id", "role", "content", "phase", "status"}
                    or item.get("role") not in {"system", "developer", "user", "assistant"}):
                raise ValueError("egress_denied")
            content = item.get("content")
            if not isinstance(content, list):
                raise ValueError("egress_denied")
            for part in content:
                if isinstance(part, dict) and part.get("type") == "input_image":
                    if (item["role"] != "user" or set(part) - {"type", "image_url", "detail"}
                            or not isinstance(part.get("image_url"), str)
                            or part.get("detail") != "high"):
                        raise ValueError("egress_denied")
                    observed.append(part["image_url"])
                else:
                    _text_content([part], {"input_text", "output_text"})
        elif kind == "function_call":
            if (not has_tool or not isinstance(item.get("name"), str) or item["name"] not in names
                    or not isinstance(item.get("arguments"), str)
                    or set(item) - {"type", "id", "call_id", "name", "arguments", "status"}):
                raise ValueError("egress_denied")
        elif kind == "function_call_output":
            if (not has_tool or not isinstance(item.get("call_id"), str)
                    or set(item) - {"type", "id", "call_id", "output", "status"}):
                raise ValueError("egress_denied")
            if not isinstance(item.get("output"), str):
                _text_content(item.get("output"), {"input_text"})
        elif kind == "custom_tool_call" and coding:
            if (item.get("name") != "apply_patch" or not isinstance(item.get("call_id"), str)
                    or not isinstance(item.get("input"), str)
                    or set(item) - {"type", "id", "call_id", "name", "input", "status"}):
                raise ValueError("egress_denied")
        elif kind == "custom_tool_call_output" and coding:
            if (not isinstance(item.get("call_id"), str) or not isinstance(item.get("output"), str)
                    or set(item) - {"type", "id", "call_id", "output", "status"}):
                raise ValueError("egress_denied")
        elif kind == "reasoning":
            if set(item) - {"type", "id", "summary", "content", "encrypted_content", "status"}:
                raise ValueError("egress_denied")
            _text_content(item.get("summary", []), {"summary_text"})
            _text_content(item.get("content", []), {"reasoning_text"})
            if item.get("encrypted_content") is not None and not isinstance(item["encrypted_content"], str):
                raise ValueError("egress_denied")
        else:
            raise ValueError("egress_denied")
    if tuple(observed) != image_uris:
        raise ValueError("egress_denied")
