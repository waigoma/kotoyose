"""TaskHub の JSON 再パース後も client tool 引数の同一性を保つ。"""

import hashlib
import math

from .execution_resources import canonical_json


_MAX_INTEGER_ABS = 2**53


def normalize_client_tool_arguments(value: object) -> object:
    """JSON 型を再帰的に正規化し、有限数と ±2**53 以内の int に制限する。

    TaskHub が区別できない範囲内の整数値 float は int にそろえる。
    list / str key の dict は新しく作り、型や数値の違反は ValueError にする。
    """
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        if abs(value) > _MAX_INTEGER_ABS:
            raise ValueError("Client tool argument integers must satisfy |n| <= 2**53")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Client tool argument numbers must be finite")
        if value.is_integer() and abs(value) <= _MAX_INTEGER_ABS:
            return int(value)
        return value
    if isinstance(value, list):
        return [normalize_client_tool_arguments(item) for item in value]
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("Client tool argument object keys must be strings")
        return {key: normalize_client_tool_arguments(item) for key, item in value.items()}
    raise ValueError("Client tool arguments must contain only JSON types")


def client_tool_arguments_canonical(value: object) -> str:
    """正規化後の JSON を返し、不正な引数や lone surrogate は ValueError にする。"""
    text = canonical_json(normalize_client_tool_arguments(value))
    try:
        # ensure_ascii=False だけでは lone surrogate が残り、言語間で bytes が一致しない。
        text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("Client tool arguments must contain valid Unicode") from exc
    return text


def client_tool_arguments_digest(value: object) -> str:
    """正準 JSON の UTF-8 bytes を SHA-256 hex にし、不正な引数は ValueError にする。"""
    return hashlib.sha256(client_tool_arguments_canonical(value).encode("utf-8")).hexdigest()
