"""Process environment secret scrubbing and controlled credential lookup.

Startup calls :func:`scrub` once after ``.env`` and configuration files are
loaded so third-party agent SDKs and child subprocesses that copy ``os.environ``
cannot inherit Kotoyose secrets or config-referenced credentials.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import json
import os
from pathlib import Path
import re
import threading
from typing import Any

import structlog

log = structlog.get_logger()

# Single authoritative pattern for secret-looking environment variable names.
_SECRET_NAME_RE = re.compile(
    r"(TOKEN|SECRET|PASSWORD|PASSWD|PASSPHRASE|CREDENTIAL|PRIVATE|"
    r"API_?KEY|ACCESS_?KEY|AUTH|COOKIE|SESSION|DSN|DATABASE_URL|_KEY$)",
    re.IGNORECASE,
)

# Operational names that must stay in os.environ even if a substring matches.
# Keep SSH_AUTH_SOCK out of this allowlist so it is always scrubbed.
_ALLOWLIST_EXACT: frozenset[str] = frozenset({
    "PATH",
    "HOME",
    "LANG",
    "TERM",
    "USER",
    "LOGNAME",
    "SHELL",
    "TZ",
    "DBUS_SESSION_BUS_ADDRESS",
    "KOTOYOSE_CONFIG",
    "KOTOYOSE_EXECUTION_CONFIG",
    "KOTOYOSE_COMPATIBILITY_CONFIG",
    "KOTOYOSE_CONVERSATION_CONFIG",
    "KOTOYOSE_BROWSE_ROOTS",
    "KOTOYOSE_PROMPT_ROOT",
    "KOTOYOSE_BASIC_USER",
    "LOG_LEVEL",
    "SESSION_TTL_SECONDS",
    "VIRTUAL_ENV",
    "INVOCATION_ID",
    "JOURNAL_STREAM",
})

_ALLOWLIST_PREFIXES: tuple[str, ...] = (
    "LC_",
    "XDG_",
    "PYTHON",
)

# Key-location variables that do not end with _KEY but still point at key material.
_BUILTIN_SENSITIVE_NAMES: frozenset[str] = frozenset({
    "KOTOYOSE_MASTER_KEY_FILE",
})

_DEFAULT_SUBPROCESS_ENV_KEYS: tuple[str, ...] = (
    "PATH",
    "HOME",
    "LANG",
    "TERM",
    "USER",
    "LOGNAME",
    "SHELL",
    "TZ",
    "VIRTUAL_ENV",
    "TMPDIR",
    "TEMP",
    "TMP",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
)

_LOCK = threading.RLock()
_SCRUBBED: dict[str, str] = {}


def is_allowlisted(name: str) -> bool:
    """Return True if *name* is an operational variable that stays in os.environ."""
    if name in _ALLOWLIST_EXACT:
        return True
    return any(name.startswith(prefix) for prefix in _ALLOWLIST_PREFIXES)


def is_secret_name(name: str) -> bool:
    """Return True if *name* looks like a secret variable by name."""
    return name in _BUILTIN_SENSITIVE_NAMES or bool(_SECRET_NAME_RE.search(name))


def collect_config_env_names(
    harness_cfg: Any = None,
    execution_cfg: Any = None,
) -> list[str]:
    """Collect env variable names referenced by loaded Kotoyose/Execution configs."""
    found: set[str] = set()

    def _add(name: Any) -> None:
        if isinstance(name, str):
            stripped = name.strip()
            if stripped:
                found.add(stripped)

    if harness_cfg is not None:
        connections = getattr(harness_cfg, "connections", None)
        if isinstance(connections, Mapping):
            for conn in connections.values():
                _add(getattr(conn, "env_name", None))

        presets: list[Any] = []
        models = getattr(harness_cfg, "models", None)
        if isinstance(models, Mapping):
            presets.extend(models.values())
        for preset in presets:
            _add(getattr(preset, "anthropic_auth_token_env", None))
            antigravity = getattr(preset, "antigravity", None)
            if isinstance(antigravity, Mapping):
                _add(antigravity.get("api_key_env"))
    if execution_cfg is not None:
        mcp_servers = getattr(execution_cfg, "mcp_servers", None)
        if isinstance(mcp_servers, Mapping):
            for server in mcp_servers.values():
                _add(getattr(server, "credential_env", None))

        artifact_storage = getattr(execution_cfg, "artifact_storage", None)
        backends = getattr(artifact_storage, "backends", None) if artifact_storage is not None else None
        if isinstance(backends, Mapping):
            for backend in backends.values():
                _add(getattr(backend, "access_key_env", None))
                _add(getattr(backend, "secret_key_env", None))

        aliases = getattr(execution_cfg, "aliases", None)
        if isinstance(aliases, Mapping):
            for alias in aliases.values():
                _add(getattr(alias, "credential_env", None))

    return sorted(found)


def collect_execution_config_file_env_names(config_path: str | None) -> list[str]:
    """Read env var names from a NativeExecutionConfig JSON file without loading plugins."""
    if not config_path:
        return []
    try:
        raw = json.loads(Path(config_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(raw, Mapping):
        return []

    found: set[str] = set()

    def _add(name: Any) -> None:
        if isinstance(name, str):
            stripped = name.strip()
            if stripped:
                found.add(stripped)

    mcp_servers = raw.get("mcp_servers")
    if isinstance(mcp_servers, Mapping):
        for server in mcp_servers.values():
            if isinstance(server, Mapping):
                _add(server.get("credential_env"))

    artifact_storage = raw.get("artifact_storage")
    backends = artifact_storage.get("backends") if isinstance(artifact_storage, Mapping) else None
    if isinstance(backends, Mapping):
        for backend in backends.values():
            if isinstance(backend, Mapping):
                _add(backend.get("access_key_env"))
                _add(backend.get("secret_key_env"))

    aliases = raw.get("aliases")
    if isinstance(aliases, Mapping):
        for alias in aliases.values():
            if isinstance(alias, Mapping):
                _add(alias.get("credential_env"))

    return sorted(found)


def scrub(extra_names: Iterable[str] = ()) -> None:
    """Move secret-looking and config-referenced vars out of ``os.environ``.

    Never logs variable values; logs only ``process_env.scrubbed`` with the count
    and sorted names of removed variables.
    """
    extra_set = {
        str(name).strip()
        for name in extra_names
        if isinstance(name, str) and name.strip()
    }
    moved: list[str] = []
    with _LOCK:
        for key in list(os.environ.keys()):
            if is_allowlisted(key):
                continue
            if key in extra_set or is_secret_name(key):
                _SCRUBBED[key] = os.environ.pop(key)
                moved.append(key)
    moved.sort()
    log.info("process_env.scrubbed", count=len(moved), names=moved)


def get(name: str, default: str | None = None) -> str | None:
    """Look up *name* in scrubbed process env first, then ``os.environ``."""
    with _LOCK:
        if name in _SCRUBBED:
            return _SCRUBBED[name]
    return os.environ.get(name, default)


def require(name: str) -> str:
    """Return *name* from scrubbed process env or ``os.environ``, or raise KeyError."""
    value = get(name)
    if value is None:
        raise KeyError(name)
    return value


def names() -> list[str]:
    """Return the sorted names of variables currently held in the scrubbed store."""
    with _LOCK:
        return sorted(_SCRUBBED.keys())


def build_minimal_env(
    extra_keys: Iterable[str] = (),
    *,
    overrides: Mapping[str, str] | None = None,
    include_lc: bool = True,
    include_python: bool = False,
) -> dict[str, str]:
    """Build a minimal child env from allowlisted ``os.environ`` keys only.

    Never reads from the scrubbed secret store unless a caller explicitly passes
    an override in *overrides*.
    """
    allowed = set(_DEFAULT_SUBPROCESS_ENV_KEYS)
    allowed.update(extra_keys)
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        if key in allowed:
            env[key] = value
        elif include_lc and key.startswith("LC_"):
            env[key] = value
        elif include_python and key.startswith("PYTHON"):
            env[key] = value
    if overrides:
        for key, value in overrides.items():
            if value is not None:
                env[key] = str(value)
    return env


def _reset_for_tests(*, restore_environ: bool = False) -> None:
    """Clear scrubbed state for unit test isolation."""
    with _LOCK:
        if restore_environ:
            for key, value in _SCRUBBED.items():
                os.environ.setdefault(key, value)
        _SCRUBBED.clear()
