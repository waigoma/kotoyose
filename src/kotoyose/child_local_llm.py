"""Owner-approved OpenAI-compatible endpoints on the child's loopback interface."""

from dataclasses import dataclass
import re

LOCAL_LLM_CONNECTION_FIELDS = frozenset({"provider", "kind", "data_policy", "on", "local_llm"})
LOCAL_LLM_UI_PORT_FILE = "settings.port"
_MAX_CONNECTIONS = 16
_MAX_MODELS = 64
_MAX_MODEL_ID_BYTES = 256
_NAME = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_URL = re.compile(r"https?://(?:127\.0\.0\.1|\[::1\]):([0-9]{1,5})/v1")
_CONTROL_CHAR = re.compile(r"[\x00-\x1f\x7f]")
_URL_ERROR = "Local LLM URL must use http(s), 127.0.0.1 or [::1], an explicit port, and /v1 (port 1024–65535)"


def valid_local_llm_name(name: object) -> bool:
    return isinstance(name, str) and _NAME.fullmatch(name) is not None


def _models(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= _MAX_MODELS:
        raise ValueError(f"Local LLM connections require 1 to {_MAX_MODELS} models")
    if not all(isinstance(model, str) for model in value):
        raise ValueError("Local LLM models must be a non-empty list of unique model IDs")
    length_error = f"Local LLM model IDs must be valid UTF-8 and at most {_MAX_MODEL_ID_BYTES} bytes"
    try:
        if any(len(model) > _MAX_MODEL_ID_BYTES or len(model.encode("utf-8")) > _MAX_MODEL_ID_BYTES for model in value):
            raise ValueError(length_error)
    except UnicodeEncodeError:
        raise ValueError(length_error) from None
    if len(set(value)) != len(value) or any(not model.strip() or _CONTROL_CHAR.search(model) for model in value):
        raise ValueError("Local LLM models must be a non-empty list of unique model IDs")
    return tuple(value)


def completion_url(url: object, *, reserved_ports: tuple[int, ...] = ()) -> str:
    match = _URL.fullmatch(url) if isinstance(url, str) else None
    if match is None or not 1024 <= (port := int(match[1])) <= 65535:
        raise ValueError(_URL_ERROR)
    if port in reserved_ports:
        raise ValueError("Local LLM URL must not use the child settings UI or control-plane port")
    return f"{url}/chat/completions"


@dataclass(frozen=True, slots=True)
class LocalLLM:
    url: str
    models: tuple[str, ...]


def parse_local_llm_policy(value: object, *, reserved_ports: tuple[int, ...] = ()) -> dict[str, LocalLLM]:
    if not isinstance(value, dict):
        raise ValueError("policy.json local_llm must be an object")
    if len(value) > _MAX_CONNECTIONS:
        raise ValueError(f"Local LLM policy allows at most {_MAX_CONNECTIONS} connections")
    connections = {}
    for name, entry in value.items():
        if not valid_local_llm_name(name):
            raise ValueError("Local LLM connection IDs must match [A-Za-z0-9_.-]{1,64}")
        if not isinstance(entry, dict) or entry.keys() != {"url", "models"}:
            raise ValueError("Local LLM connections require only url and models")
        completion_url(entry["url"], reserved_ports=reserved_ports)
        connections[name] = LocalLLM(entry["url"], _models(entry["models"]))
    return connections


def parse_local_llm_catalog(value: object) -> dict[str, tuple[str, ...]]:
    if not isinstance(value, dict) or len(value) > _MAX_CONNECTIONS:
        return {}
    try:
        return {name: _models(models) for name, models in value.items() if valid_local_llm_name(name)}
    except ValueError:
        return {}
