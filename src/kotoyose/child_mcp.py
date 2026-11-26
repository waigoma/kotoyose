"""Wire limits and owner approvals for host HTTP MCPs."""

from __future__ import annotations

import json
import math
import os
import stat
import re
from pathlib import Path

from .alias_mcp import valid_public_name

FEATURE = "host-http-mcp/1"
MAX_BODY = 128 * 1024
MAX_CONCURRENT = 8
MAX_PER_LINK = 32
MAX_CALLS = 2048
UPSTREAM_TIMEOUT = 30
CALL_TIMEOUT = 35
DEFAULT_RELAY_TURN_TIMEOUT = 600.0
DELEGATION_REPLY_MARGIN = 5.0
RELAY_WRITE_TIMEOUT = 2.0
METHODS = frozenset({"initialize", "notifications/initialized", "ping", "tools/list", "tools/call"})
_DIGEST = re.compile(r"[0-9a-f]{64}")


def relay_turn_timeout(value: object) -> float:
    """Resolve the relay deadline from the preset timeout shared by the host and child."""
    if type(value) not in (float, int):
        return DEFAULT_RELAY_TURN_TIMEOUT
    try:
        timeout = float(value)
    except OverflowError:
        return DEFAULT_RELAY_TURN_TIMEOUT
    return timeout if math.isfinite(timeout) and timeout > 0 else DEFAULT_RELAY_TURN_TIMEOUT


def valid_version(value: object) -> bool:
    return isinstance(value, str) and _DIGEST.fullmatch(value) is not None


def versions(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    selected = {
        name: version for name, version in value.items()
        if valid_public_name(name) and len(name) <= 128 and valid_version(version)
    }
    if len(selected) > 64:
        return {}
    return selected


def approved_versions(home: Path, fingerprint: str) -> dict[str, str]:
    try:
        path = home / "host-mcp-approvals.json"
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        with os.fdopen(os.open(path, flags), "rb") as source:
            mode = os.fstat(source.fileno()).st_mode
            if not stat.S_ISREG(mode) or (os.name == "posix" and mode & 0o077):
                return {}
            data = json.loads(source.read(65536))
    except (OSError, ValueError, RecursionError):
        return {}
    if not isinstance(data, dict) or data.get("fingerprint") != fingerprint:
        return {}
    return versions(data.get("versions"))


def rpc_body(value: object) -> bytes:
    raw = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode()
    if len(raw) > MAX_BODY:
        raise ValueError("MCP message exceeds size limit")
    return raw


def validate_request(value: object) -> dict:
    if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
        raise ValueError("Invalid MCP JSON-RPC request")
    method = value.get("method")
    if not isinstance(method, str) or method not in METHODS:
        raise ValueError("MCP method is unavailable through the child relay")
    if method == "notifications/initialized":
        if "id" in value:
            raise ValueError("Invalid MCP notification")
    elif not valid_rpc_id(value.get("id")):
        raise ValueError("MCP request needs a JSON-RPC ID")
    if "params" in value and not isinstance(value["params"], dict):
        raise ValueError("Invalid MCP request parameters")
    rpc_body(value)
    return value


def valid_rpc_id(value: object) -> bool:
    return (type(value) is int and value.bit_length() <= 63) or (isinstance(value, str) and len(value) <= 256)


def rpc_error(request: object, message: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request.get("id") if isinstance(request, dict) and valid_rpc_id(request.get("id")) else None,
        "error": {"code": -32000, "message": message},
    }


def lost_result(request: dict) -> str:
    if request.get("method") == "tools/call":
        return "MCP tool execution result is unknown"
    return "MCP relay did not receive a response"
