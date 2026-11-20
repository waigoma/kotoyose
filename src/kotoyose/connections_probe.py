"""Read-only connection health probes, CLI version capture, and runtime state hooks."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import contextlib
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import tempfile
from typing import Any
import urllib.parse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import structlog

from . import process_env
from .agent_spec import driver_manifest
from .codex_subscription import read_subscription_expiry
from .config import Connection, HarnessConfig, get_config
from .host_place import kill_session
from .print_cli_events import parse_json_text
from .print_cli_hooks import ParsedQuota, classify_stderr, get_quota_parser
from .print_cli_home import use_login_home
from .print_cli_profiles import (
    PrintCliProfile,
    PrintCliQuotaRpc,
    build_print_cli_env,
    get_print_cli_profile,
    resolve_print_cli_binary,
)
from .secret_store import SecretStoreError, resolve_secret_async
from .connection_state_store import (
    ConnectionStateStore,
    _dt_or_none,
    _iso_or_none,
    default_connection_state,
    get_connection_state_store,
)

log = structlog.get_logger()

EXPIRING_THRESHOLD_SECONDS = 48 * 3600
REFRESH_THRESHOLD_SECONDS = 3 * 24 * 3600
REFRESH_COOLDOWN_SECONDS = 12 * 3600
DEFAULT_PROBE_TIMEOUT_SECONDS = 20.0
API_KEY_PROBE_TIMEOUT_SECONDS = 10.0
CLAUDE_USAGE_PROBE_TIMEOUT_SECONDS = 60.0
PRINT_CLI_QUOTA_PROBE_TIMEOUT_SECONDS = 60.0
DEFAULT_PROBE_INTERVAL_SECONDS = 30 * 60
ANTHROPIC_MODELS_URL = "https://api.anthropic.com/v1/models?limit=1"
ANTHROPIC_API_VERSION = "2023-06-01"
OPENAI_MODELS_URL = "https://api.openai.com/v1/models"
_OPENAI_API_KEY_PROVIDERS = frozenset({"codex", "openai"})

_CLI_VERSION_TIMEOUT_SECONDS = 1.5
_CLI_PROBE_REAP_TIMEOUT_SECONDS = 1.0
_SAFE_SUBPROCESS_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "USER")
_CLAUDE_USAGE_ENV_KEYS = ("PATH", "HOME", "LANG")
_CLAUDE_SUBSCRIPTION_PROVIDERS = frozenset({"claude", "anthropic"})
_CLAUDE_USAGE_LINE_RE = re.compile(
    r"^\s*(?P<label>.+?):\s*(?P<percent>\d+(?:\.\d+)?)%\s*used\s*[·•]\s*resets\s+(?P<resets>.+?)\s*$"
)
_CLAUDE_RESETS_TZ_RE = re.compile(r"^(?P<body>.+?)\s*\((?P<tz>[^()]+)\)\s*$")
_CLAUDE_LOGIN_REQUIRED_MARKERS = (
    "not logged in",
    "please run /login",
    "please log in",
    "login expired",
    "session expired",
    "re-authenticate",
    "reauthenticate",
    "oauth token has expired",
    "requires login",
    "logged out",
    "authentication_failed",
)
_ERROR_TEXT_MAX_LEN = 200
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_CLI_VERSION_CACHE: dict[tuple[str, int, int, tuple[tuple[str, str], ...]], dict[str, str | None]] = {}
_LAST_CODEX_REFRESH_AT: dict[str, datetime] = {}

_CODEX_PROBE_LOCK = asyncio.Lock()


def should_refresh_codex_token(
    conn_id: str,
    expires_at: datetime | None,
    *,
    now: datetime,
    force_refresh: bool = False,
) -> bool:
    """Return True when Codex `account/read` should request `refreshToken=True`."""
    if force_refresh:
        return True
    if expires_at is None:
        return False
    if (expires_at - now).total_seconds() >= REFRESH_THRESHOLD_SECONDS:
        return False
    last_refresh = _LAST_CODEX_REFRESH_AT.get(conn_id)
    if last_refresh is not None and (now - last_refresh).total_seconds() < REFRESH_COOLDOWN_SECONDS:
        return False
    return True


def reset_codex_refresh_history() -> None:
    """Clear per-connection Codex refresh timestamps (intended for test isolation)."""
    _LAST_CODEX_REFRESH_AT.clear()


async def _stop_probe_process(process: asyncio.subprocess.Process) -> None:
    """Stop and reap a CLI probe process."""
    with contextlib.suppress(ProcessLookupError):
        process.kill()
    with contextlib.suppress(Exception):
        async with asyncio.timeout(_CLI_PROBE_REAP_TIMEOUT_SECONDS):
            await process.wait()


async def probe_cli_version(
    cli_binary: Path | str | None, *, fixed_env: Mapping[str, str] | None = None,
) -> dict[str, str | None]:
    """Safely run `<binary> --version` with a tight timeout, minimal env, and per-process cache."""
    if cli_binary is None:
        return {"version": None, "note": None}
    raw = str(cli_binary).strip()
    if not raw:
        return {"version": None, "note": None}

    candidate = Path(raw)
    if not candidate.is_absolute():
        found = shutil.which(raw)
        if found is None:
            return {"version": None, "note": "Skipped: binary not found on PATH"}
        candidate = Path(found)

    try:
        resolved = candidate.resolve(strict=True)
        stat_result = resolved.stat()
    except OSError:
        return {"version": None, "note": "Skipped: binary not found on host"}

    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        return {"version": None, "note": "Skipped: binary is not an executable file"}

    overrides = fixed_env or {}
    cache_key = (str(resolved), stat_result.st_mtime_ns, stat_result.st_size, tuple(sorted(overrides.items())))
    cached = _CLI_VERSION_CACHE.get(cache_key)
    if cached is not None:
        return cached

    safe_env = {k: v for k, v in os.environ.items() if k in _SAFE_SUBPROCESS_ENV_KEYS}
    safe_env.update(overrides)
    try:
        process = await asyncio.create_subprocess_exec(
            str(resolved),
            "--version",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=safe_env,
        )
    except OSError:
        result = {"version": None, "note": "Skipped: could not execute binary"}
        _CLI_VERSION_CACHE[cache_key] = result
        return result

    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=_CLI_VERSION_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        await _stop_probe_process(process)
        result = {"version": None, "note": "Skipped: --version timed out"}
        _CLI_VERSION_CACHE[cache_key] = result
        return result
    except asyncio.CancelledError:
        await _stop_probe_process(process)
        raise
    except OSError:
        await _stop_probe_process(process)
        result = {"version": None, "note": "Skipped: --version failed"}
        _CLI_VERSION_CACHE[cache_key] = result
        return result

    raw_bytes = (stdout or b"")[:512] or (stderr or b"")[:512]
    text = _ANSI_ESCAPE_RE.sub("", raw_bytes.decode("utf-8", errors="replace"))
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    version_line = lines[0][:120] if lines else ""

    if process.returncode != 0:
        result = {"version": None, "note": f"Skipped: exited with status {process.returncode}"}
    elif not version_line:
        result = {"version": None, "note": "Skipped: no version output"}
    else:
        result = {"version": version_line, "note": None}

    _CLI_VERSION_CACHE[cache_key] = result
    return result


def connection_for_model(cfg: HarnessConfig | None, alias: str | None) -> str | None:
    """Resolve the connection ID backing `alias` via `preset.route`, or None."""
    if cfg is None or not alias:
        return None
    try:
        preset = cfg.preset_for(alias)
    except (KeyError, ValueError):
        return None
    if not preset.route:
        return None
    route = cfg.routes.get(preset.route)
    return route.connection if route is not None else None


def _driver_cli_binary(driver_install: Any) -> Path | None:
    """Extract the executable Path from a driver install's `cli_binary` or configured `command`."""
    cli = getattr(driver_install, "cli_binary", None)
    if cli is not None:
        return Path(cli)
    cmd = getattr(driver_install, "command", None)
    if isinstance(cmd, str) and cmd.strip():
        parts = shlex.split(cmd)
        return Path(parts[0]) if parts else None
    if isinstance(cmd, (list, tuple)) and cmd and isinstance(cmd[0], str) and cmd[0].strip():
        return Path(cmd[0].strip())
    return None


def resolve_connection_cli_binary(
    cfg: HarnessConfig,
    conn_id: str,
    conn: Connection,
    execution_service: Any = None,
) -> Path | None:
    """Find the machine's driver `cli_binary` matching `conn_id` or its provider."""
    exec_cfg = getattr(execution_service, "config", None) if execution_service is not None else None
    drivers = getattr(exec_cfg, "drivers", None) or {}
    profile = cfg.print_cli_profile_for_connection(conn_id)
    if profile is not None:
        return resolve_print_cli_binary(profile, exec_cfg)
    if not drivers:
        return None

    for route in cfg.routes.values():
        if route.connection == conn_id and route.driver in drivers:
            cli = _driver_cli_binary(drivers[route.driver])
            if cli is not None:
                return cli

    target_provider = "claude" if conn.provider.lower() in _CLAUDE_SUBSCRIPTION_PROVIDERS else conn.provider.lower()
    for driver_name in sorted(drivers):
        try:
            manifest_provider = driver_manifest(driver_name).provider
        except Exception:
            manifest_provider = None
        if manifest_provider == target_provider:
            cli = _driver_cli_binary(drivers[driver_name])
            if cli is not None:
                return cli
    return None


def classify_expiry_auth(
    expires_at: datetime | None,
    *,
    logged_in: bool,
    now: datetime | None = None,
) -> str:
    """Compute auth state (`ok`, `expiring`, `expired`, `logged_out`) from official state + expiry."""
    ref = now or datetime.now(timezone.utc)
    if not logged_in:
        if expires_at is not None and expires_at <= ref:
            return "expired"
        return "logged_out"
    if expires_at is None:
        return "ok"
    remaining = (expires_at - ref).total_seconds()
    if remaining <= 0:
        return "expired"
    if remaining < EXPIRING_THRESHOLD_SECONDS:
        return "expiring"
    return "ok"


def codex_home_and_auth_path(conn: Connection) -> tuple[str | None, Path | None]:
    """Return `(CODEX_HOME, auth_json_path)` matching how runtime sessions locate Codex auth."""
    if conn.auth_file is not None:
        return str(conn.auth_file.parent), conn.auth_file
    if conn.home and conn.home != "default":
        home_dir = Path(conn.home).expanduser()
        return str(home_dir), home_dir / "auth.json"
    env_home = process_env.get("CODEX_HOME")
    if env_home:
        expanded = Path(env_home).expanduser()
        return str(expanded), expanded / "auth.json"
    default_home = Path.home() / ".codex"
    return str(default_home), default_home / "auth.json"


def _minimal_probe_env(codex_home: str | None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in _SAFE_SUBPROCESS_ENV_KEYS}
    if codex_home:
        env["CODEX_HOME"] = codex_home
    return env


def _format_window_label(raw_window: str | None, duration_mins: int | None = None, fallback: str = "primary") -> str:
    if raw_window == "five_hour":
        return "5h"
    if raw_window == "seven_day":
        return "7d"
    if raw_window:
        return raw_window
    if isinstance(duration_mins, int) and duration_mins > 0:
        if duration_mins % (60 * 24) == 0:
            return f"{duration_mins // (60 * 24)}d"
        if duration_mins % 60 == 0:
            return f"{duration_mins // 60}h"
        return f"{duration_mins}m"
    return fallback


def _extract_plan_type(value: Any) -> str | None:
    if value is None:
        return None
    plan = getattr(value, "plan_type", None)
    if plan is None:
        return None
    raw = getattr(plan, "value", plan)
    return str(raw) if raw is not None else None


def _rate_window_entry(window_obj: Any, fallback: str) -> dict[str, Any] | None:
    if window_obj is None:
        return None
    used = getattr(window_obj, "used_percent", None)
    if isinstance(used, bool) or not isinstance(used, (int, float)):
        return None
    resets_at = getattr(window_obj, "resets_at", None)
    resets_iso = (
        datetime.fromtimestamp(int(resets_at), tz=timezone.utc).isoformat()
        if isinstance(resets_at, int) and not isinstance(resets_at, bool) and resets_at >= 0
        else None
    )
    mins = getattr(window_obj, "window_duration_mins", None)
    return {
        "window": _format_window_label(None, mins, fallback),
        "used_percent": float(used),
        "resets_at": resets_iso,
        "source": "official_rpc",
    }


def _extract_codex_quota(rate_limits_resp: Any) -> tuple[list[dict[str, Any]], str | None]:
    snapshot = getattr(rate_limits_resp, "rate_limits", None)
    if snapshot is None:
        return [], None
    plan = _extract_plan_type(snapshot)
    entries: list[dict[str, Any]] = []
    for attr, fallback in (("primary", "primary"), ("secondary", "secondary")):
        entry = _rate_window_entry(getattr(snapshot, attr, None), fallback)
        if entry is not None:
            entries.append(entry)
    return entries, plan


def _build_codex_probe_config(cli_binary: Path, codex_home: str | None) -> Any:
    from openai_codex import CodexConfig

    minimal_env = _minimal_probe_env(codex_home)
    work_dir = codex_home if (codex_home and Path(codex_home).is_dir()) else tempfile.gettempdir()
    resolved_bin = str(cli_binary.resolve()) if cli_binary.exists() else str(cli_binary)
    launch_args: list[str] | None = None
    env_bin = shutil.which("env")
    if env_bin and os.name == "posix":
        launch_args = [
            env_bin,
            "-i",
            *(f"{k}={v}" for k, v in minimal_env.items()),
            resolved_bin,
            "app-server",
            "--listen",
            "stdio://",
        ]
    return CodexConfig(
        codex_bin=resolved_bin,
        launch_args_override=launch_args,
        cwd=work_dir,
        env=minimal_env,
        client_name="kotoyose-connection-probe",
    )


async def probe_codex_connection(
    conn_id: str,
    conn: Connection,
    *,
    cli_binary: Path | None,
    store: ConnectionStateStore | None = None,
    timeout_seconds: float = DEFAULT_PROBE_TIMEOUT_SECONDS,
    force_refresh: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run a serialized, bounded, read-only Codex `app-server` probe and store the result."""
    target_store = store or get_connection_state_store()
    ref_now = now or datetime.now(timezone.utc)
    codex_home, auth_path = codex_home_and_auth_path(conn)
    expires_at = read_subscription_expiry(auth_path) if auth_path is not None else None
    version_info = await probe_cli_version(cli_binary)
    cli_version = version_info.get("version")

    if cli_binary is None:
        return await target_store.upsert(
            conn_id,
            auth=classify_expiry_auth(expires_at, logged_in=expires_at is not None, now=ref_now) if expires_at is not None else "unknown",
            expires_at=expires_at,
            cli_version=cli_version,
            checked_at=ref_now,
            last_error="Codex CLI binary is not configured",
        )

    from openai_codex import AsyncCodex
    from openai_codex.generated.v2_all import GetAccountRateLimitsResponse

    config = _build_codex_probe_config(cli_binary, codex_home)
    refresh_token = should_refresh_codex_token(
        conn_id,
        expires_at,
        now=ref_now,
        force_refresh=force_refresh,
    )
    if refresh_token:
        _LAST_CODEX_REFRESH_AT[conn_id] = ref_now

    logged_in = False
    plan: str | None = None
    quota: list[dict[str, Any]] | None = None
    quota_source: str | None = None
    last_error: str | None = None

    async with _CODEX_PROBE_LOCK:
        codex: AsyncCodex | None = None
        try:
            async with asyncio.timeout(timeout_seconds):
                codex = AsyncCodex(config)
                account_resp = await codex.account(refresh_token=refresh_token)
                if refresh_token and auth_path is not None:
                    # Official app-server writes refreshed auth.json; re-read exp claim after refresh.
                    expires_at = read_subscription_expiry(auth_path)
                account = getattr(account_resp, "account", None)
                logged_in = account is not None
                plan = _extract_plan_type(account)
                if logged_in:
                    try:
                        rate_resp = await codex._client.request(
                            "account/rateLimits/read",
                            None,
                            response_model=GetAccountRateLimitsResponse,
                        )
                        quota, rate_plan = _extract_codex_quota(rate_resp)
                        quota_source = "official_rpc"
                        if plan is None:
                            plan = rate_plan
                    except Exception as exc:
                        last_error = f"rate_limits_unavailable: {type(exc).__name__}"
                    try:
                        await codex.models(include_hidden=False)
                    except Exception as exc:
                        if last_error is None:
                            last_error = f"model_list_unavailable: {type(exc).__name__}"
        except TimeoutError:
            last_error = f"Probe timed out after {int(timeout_seconds)}s"
        except Exception as exc:
            last_error = f"Probe failed: {type(exc).__name__}"
        finally:
            if codex is not None:
                with contextlib.suppress(BaseException):
                    await asyncio.wait_for(codex.close(), timeout=2.0)

    if last_error and not logged_in and plan is None and quota is None:
        auth = classify_expiry_auth(expires_at, logged_in=False, now=ref_now) if expires_at and expires_at <= ref_now else (
            "expiring" if expires_at and (expires_at - ref_now).total_seconds() < EXPIRING_THRESHOLD_SECONDS else "unknown"
        )
        return await target_store.upsert(
            conn_id,
            auth=auth,
            expires_at=expires_at,
            cli_version=cli_version,
            checked_at=ref_now,
            last_error=last_error,
        )

    auth = classify_expiry_auth(expires_at, logged_in=logged_in, now=ref_now)
    kwargs: dict[str, Any] = {
        "auth": auth,
        "expires_at": expires_at,
        "plan": plan,
        "cli_version": cli_version,
        "checked_at": ref_now,
        "last_error": last_error,
    }
    if quota is not None:
        kwargs["quota"] = quota
        kwargs["quota_source"] = quota_source
    return await target_store.upsert(conn_id, **kwargs)


def _cap_error(text: str | None) -> str | None:
    if text is None:
        return None
    cleaned = " ".join(text.split())
    if not cleaned:
        return None
    return cleaned[:_ERROR_TEXT_MAX_LEN]


def parse_claude_usage_resets_at(resets_text: str, *, now: datetime | None = None) -> str | None:
    """Parse `<date/time> (<IANA Zone>)` from Claude `/usage` output into an ISO UTC timestamp."""
    match = _CLAUDE_RESETS_TZ_RE.match(resets_text.strip())
    if match is None:
        return None
    body = " ".join(match.group("body").split())
    tz_name = match.group("tz").strip()
    try:
        tzinfo = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, KeyError, ValueError):
        return None

    # Normalize e.g. "3 am" -> "3AM" / "3:30pm" -> "3:30PM" for strptime %p.
    normalized = re.sub(
        r"(\d)\s*(am|pm)\b",
        lambda m: f"{m.group(1)}{m.group(2).upper()}",
        body,
        flags=re.IGNORECASE,
    )
    ref_now = (now or datetime.now(timezone.utc)).astimezone(tzinfo)

    for fmt in (
        "%b %d, %Y, %I:%M%p",
        "%b %d, %Y, %I%p",
        "%b %d, %Y %I:%M%p",
        "%b %d, %Y %I%p",
        "%b %d, %Y, %H:%M",
        "%b %d, %Y %H:%M",
    ):
        try:
            naive = datetime.strptime(normalized, fmt)
            return naive.replace(tzinfo=tzinfo).astimezone(timezone.utc).isoformat()
        except ValueError:
            continue

    for fmt in (
        "%b %d, %I:%M%p",
        "%b %d, %I%p",
        "%b %d %I:%M%p",
        "%b %d %I%p",
        "%b %d, %H:%M",
        "%b %d %H:%M",
    ):
        parsed_any = False
        for year in (ref_now.year, ref_now.year + 1):
            try:
                naive = datetime.strptime(f"{year} {normalized}", f"%Y {fmt}")
                parsed_any = True
            except ValueError:
                continue
            candidate = naive.replace(tzinfo=tzinfo)
            if year == ref_now.year and candidate < ref_now - timedelta(hours=24):
                continue
            return candidate.astimezone(timezone.utc).isoformat()
        if parsed_any:
            return None

    for fmt in ("%I:%M%p", "%I%p", "%H:%M"):
        try:
            parsed_t = datetime.strptime(normalized, fmt)
        except ValueError:
            continue
        candidate = ref_now.replace(
            hour=parsed_t.hour,
            minute=parsed_t.minute,
            second=0,
            microsecond=0,
        )
        if candidate < ref_now - timedelta(hours=1):
            candidate = candidate + timedelta(days=1)
        return candidate.astimezone(timezone.utc).isoformat()

    return None


def _extract_claude_usage_text(raw_stdout: bytes | str) -> tuple[str | None, bool]:
    """Decode stdout from `claude -p '/usage' --output-format json` without storing raw output."""
    text = raw_stdout.decode("utf-8", errors="replace") if isinstance(raw_stdout, bytes) else str(raw_stdout)
    cleaned = _ANSI_ESCAPE_RE.sub("", text).strip()
    if not cleaned:
        return None, False
    try:
        payload = json.loads(cleaned)
    except ValueError:
        return None, False
    if not isinstance(payload, dict):
        return None, False
    is_error = bool(payload.get("is_error"))
    for key in ("result", "message", "error"):
        val = payload.get(key)
        if isinstance(val, str):
            return _ANSI_ESCAPE_RE.sub("", val), is_error
    return "", is_error


def parse_claude_usage_output(
    raw_stdout: bytes | str,
    *,
    stderr: bytes | str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Parse official Claude Code CLI `/usage` JSON output into connection health fields."""
    result_text, is_error = _extract_claude_usage_text(raw_stdout)
    stderr_text = (
        stderr.decode("utf-8", errors="replace") if isinstance(stderr, bytes) else str(stderr)
    )
    stderr_clean = _ANSI_ESCAPE_RE.sub("", stderr_text)

    windows: list[dict[str, Any]] = []
    if result_text:
        for line in result_text.splitlines():
            match = _CLAUDE_USAGE_LINE_RE.match(line)
            if match is None:
                continue
            label = match.group("label").strip()
            used_percent = round(max(0.0, min(100.0, float(match.group("percent")))), 2)
            resets_text = " ".join(match.group("resets").split())
            resets_at = parse_claude_usage_resets_at(resets_text, now=now)
            windows.append(
                {
                    "window": label,
                    "label": label,
                    "used_percent": used_percent,
                    "resets_text": resets_text,
                    "resets_at": resets_at,
                    "source": "claude_cli_usage",
                }
            )

    if windows and not is_error:
        return {
            "auth": "ok",
            "quota": windows,
            "quota_source": "claude_cli_usage",
            "last_error": None,
        }

    combined_lower = f"{result_text or ''}\n{stderr_clean}".lower()
    if any(marker in combined_lower for marker in _CLAUDE_LOGIN_REQUIRED_MARKERS):
        return {
            "auth": "login_required",
            "quota": [],
            "quota_source": "claude_cli_usage",
            "last_error": _cap_error("Claude CLI login required or expired"),
        }

    return {
        "auth": "unknown",
        "quota": [],
        "quota_source": "claude_cli_usage",
        "last_error": "quota unavailable from this login",
    }


def _parse_claude_expiry(value: Any) -> datetime | None:
    """Parse `loginExpiresAt` (ISO string, seconds, or epoch ms) into an aware UTC datetime."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        num = float(value)
        if abs(num) > 1e11:
            num /= 1000.0
        try:
            return _dt_or_none(num)
        except (ValueError, OverflowError, OSError):
            return None
    if isinstance(value, str):
        try:
            return _dt_or_none(value)
        except ValueError:
            return None
    return None


def parse_claude_auth_status(raw_stdout: bytes | str) -> dict[str, Any] | None:
    """Parse `claude auth status --json` output without touching config_dir files."""
    text = raw_stdout.decode("utf-8", errors="replace") if isinstance(raw_stdout, bytes) else str(raw_stdout)
    cleaned = _ANSI_ESCAPE_RE.sub("", text).strip()
    if not cleaned:
        return None
    try:
        payload = json.loads(cleaned)
    except ValueError:
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("loggedIn"), bool):
        return None

    logged_in = bool(payload["loggedIn"])
    expires_dt = _parse_claude_expiry(payload.get("loginExpiresAt"))
    plan: str | None = None
    for key in ("subscriptionType", "planTier"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            plan = val.strip()
            break
    raw_email = payload.get("email")
    email = raw_email.strip() if isinstance(raw_email, str) and raw_email.strip() else None
    return {
        "logged_in": logged_in,
        "expires_dt": expires_dt,
        "login_expires_at": _iso_or_none(expires_dt),
        "plan": plan,
        "email": email,
    }


async def run_claude_auth_status(
    cli_binary: Path | str,
    config_dir: Path | str,
    *,
    timeout_seconds: float = DEFAULT_PROBE_TIMEOUT_SECONDS,
    extra_env: Mapping[str, str] | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Run read-only `claude auth status --json` with minimal env + `CLAUDE_CONFIG_DIR`."""
    status_env = {k: os.environ[k] for k in _CLAUDE_USAGE_ENV_KEYS if k in os.environ}
    if extra_env:
        status_env.update(extra_env)
    status_env["CLAUDE_CONFIG_DIR"] = str(config_dir)

    with tempfile.TemporaryDirectory(prefix="kotoyose-claude-status-") as tmpdir:
        try:
            process = await asyncio.create_subprocess_exec(
                str(cli_binary),
                "auth",
                "status",
                "--json",
                cwd=tmpdir,
                env=status_env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError:
            return None, "launch_failed"
        try:
            stdout, _stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout_seconds,
            )
        except TimeoutError:
            await _stop_probe_process(process)
            return None, "timed_out"
        except asyncio.CancelledError:
            await _stop_probe_process(process)
            raise
        except OSError:
            await _stop_probe_process(process)
            return None, "failed"

    # ログインしていないとき CLI は終了コード 1 で正しい JSON (loggedIn: false) を出す。
    # 終了コードではなく出力で判断する。
    parsed = parse_claude_auth_status(stdout or b"")
    if parsed is not None:
        return parsed, None
    return None, "failed" if process.returncode != 0 else "invalid"


async def probe_claude_connection(
    conn_id: str,
    conn: Connection,
    *,
    cli_binary: Path | str | None,
    store: ConnectionStateStore | None = None,
    timeout_seconds: float = CLAUDE_USAGE_PROBE_TIMEOUT_SECONDS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run read-only `claude auth status --json` and `/usage` for a `config_dir` connection."""
    target_store = store or get_connection_state_store()
    ref_now = now or datetime.now(timezone.utc)

    resolved_cli: Path | str | None = cli_binary
    if resolved_cli is None:
        found = shutil.which("claude")
        resolved_cli = Path(found) if found else None

    version_info = await probe_cli_version(resolved_cli)
    cli_version = version_info.get("version")

    if conn.config_dir is None:
        if cli_version is not None:
            return await target_store.upsert(conn_id, cli_version=cli_version, checked_at=ref_now)
        existing = await target_store.get(conn_id)
        return existing if existing is not None else default_connection_state(conn_id)

    if resolved_cli is None:
        return await target_store.upsert(
            conn_id,
            auth="unknown",
            quota=[],
            quota_source="claude_cli_usage",
            cli_version=cli_version,
            checked_at=ref_now,
            last_error=_cap_error("Claude CLI binary not found"),
        )

    if not Path(conn.config_dir).is_dir():
        # 未ログインの dir を CLI に作らせない (作られると 0755 になる)。作るのはログインの流れ (0700)。
        return await target_store.upsert(
            conn_id,
            auth="logged_out",
            quota=[],
            quota_source="claude_cli_usage",
            cli_version=cli_version,
            checked_at=ref_now,
            last_error="Claude CLI is not logged in",
        )

    status_info, status_err = await run_claude_auth_status(
        resolved_cli,
        conn.config_dir,
        timeout_seconds=timeout_seconds,
    )
    if status_err == "timed_out":
        return await target_store.upsert(
            conn_id,
            auth="unknown",
            quota=[],
            quota_source="claude_cli_usage",
            cli_version=cli_version,
            checked_at=ref_now,
            last_error=_cap_error(f"Claude auth status probe timed out after {int(timeout_seconds)}s"),
        )

    if status_info is not None and not status_info["logged_in"]:
        status_auth = classify_expiry_auth(
            status_info["expires_dt"],
            logged_in=False,
            now=ref_now,
        )
        return await target_store.upsert(
            conn_id,
            auth=status_auth,
            expires_at=status_info["expires_dt"],
            plan=status_info["plan"],
            quota=[],
            quota_source="claude_cli_usage",
            cli_version=cli_version,
            checked_at=ref_now,
            last_error=_cap_error(
                "Claude CLI login expired" if status_auth == "expired" else "Claude CLI is not logged in"
            ),
        )

    probe_env = {k: os.environ[k] for k in _CLAUDE_USAGE_ENV_KEYS if k in os.environ}
    probe_env["CLAUDE_CONFIG_DIR"] = str(conn.config_dir)

    parsed: dict[str, Any]
    with tempfile.TemporaryDirectory(prefix="kotoyose-claude-usage-") as tmpdir:
        try:
            process = await asyncio.create_subprocess_exec(
                str(resolved_cli),
                "-p",
                "/usage",
                "--output-format",
                "json",
                cwd=tmpdir,
                env=probe_env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            parsed = {
                "auth": "unknown",
                "quota": [],
                "quota_source": "claude_cli_usage",
                "last_error": _cap_error(f"Could not launch Claude CLI: {type(exc).__name__}"),
            }
        else:
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=timeout_seconds,
                )
            except TimeoutError:
                await _stop_probe_process(process)
                parsed = {
                    "auth": "unknown",
                    "quota": [],
                    "quota_source": "claude_cli_usage",
                    "last_error": _cap_error(f"Claude usage probe timed out after {int(timeout_seconds)}s"),
                }
            except asyncio.CancelledError:
                await _stop_probe_process(process)
                raise
            except OSError as exc:
                await _stop_probe_process(process)
                parsed = {
                    "auth": "unknown",
                    "quota": [],
                    "quota_source": "claude_cli_usage",
                    "last_error": _cap_error(f"Claude usage probe failed: {type(exc).__name__}"),
                }
            else:
                parsed = parse_claude_usage_output(stdout or b"", stderr=stderr or b"", now=ref_now)

    if status_info is not None:
        status_auth = classify_expiry_auth(
            status_info["expires_dt"],
            logged_in=True,
            now=ref_now,
        )
        final_auth = "login_required" if parsed["auth"] == "login_required" else status_auth
        return await target_store.upsert(
            conn_id,
            auth=final_auth,
            expires_at=status_info["expires_dt"],
            plan=status_info["plan"],
            quota=parsed["quota"],
            quota_source=parsed["quota_source"],
            cli_version=cli_version,
            checked_at=ref_now,
            last_error=_cap_error(parsed["last_error"]),
        )

    return await target_store.upsert(
        conn_id,
        auth=parsed["auth"],
        quota=parsed["quota"],
        quota_source=parsed["quota_source"],
        cli_version=cli_version,
        checked_at=ref_now,
        last_error=_cap_error(parsed["last_error"]),
    )


_QUOTA_RPC_LINE_LIMIT = 1024 * 1024
_QUOTA_RPC_OUTPUT_LIMIT = 4 * _QUOTA_RPC_LINE_LIMIT
_QUOTA_RPC_STDERR_LIMIT = 16 * 1024
_QUOTA_RPC_SIGNED_OUT = re.compile(
    r"\b(?:unauthenticated|not authenticated|not logged in|not signed in|authentication required)\b",
    re.IGNORECASE,
)


async def _drain_probe_output(
    stream: asyncio.StreamReader, capture: bytearray, limit: int,
) -> None:
    while chunk := await stream.read(16 * 1024):
        capture.extend(chunk[:limit - len(capture)])


async def _exchange_quota_rpc(
    process: asyncio.subprocess.Process, rpc: PrintCliQuotaRpc, timeout_seconds: float,
) -> ParsedQuota:
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    stdin, stdout = process.stdin, process.stdout
    stderr = bytearray()
    stderr_task = asyncio.create_task(_drain_probe_output(process.stderr, stderr, _QUOTA_RPC_STDERR_LIMIT))
    failure: ParsedQuota = {"auth": "unknown", "quota": [], "last_error": None}
    total_bytes = 0
    result = None
    try:
        async with asyncio.timeout(timeout_seconds):
            for request in rpc.to_canonical_dict()["requests"]:
                request_id = request["id"]
                stdin.write(json.dumps(request).encode() + b"\n")
                await stdin.drain()
                while True:
                    line = await stdout.readline()
                    if not line or not line.endswith(b"\n"):
                        return {**failure, "last_error": "Quota RPC closed stdout before its response"}
                    total_bytes += len(line)
                    if total_bytes > _QUOTA_RPC_OUTPUT_LIMIT:
                        return {**failure, "last_error": "Quota RPC output exceeded its limit"}
                    try:
                        reply = json.loads(line)
                    except (ValueError, RecursionError):
                        continue
                    if not isinstance(reply, dict) or reply.get("jsonrpc") != "2.0" or "method" in reply:
                        continue
                    reply_id = reply.get("id")
                    if type(reply_id) is not type(request_id) or reply_id != request_id:
                        continue
                    if "error" in reply:
                        error = reply["error"]
                        message = error.get("message") if isinstance(error, dict) else None
                        if isinstance(message, str) and _QUOTA_RPC_SIGNED_OUT.search(message):
                            return {**failure, "auth": "logged_out", "last_error": "Quota RPC requires authentication"}
                        return {**failure, "last_error": "Quota RPC returned an error"}
                    if "result" not in reply:
                        return {**failure, "last_error": "Quota RPC response omitted its result"}
                    if request_id == rpc.result_id:
                        result = reply["result"]
                    break
    except TimeoutError:
        return {**failure, "last_error": f"Quota RPC timed out after {timeout_seconds:g}s"}
    except ValueError:
        return {**failure, "last_error": "Quota RPC output line exceeded its limit"}
    except OSError:
        return {**failure, "last_error": "Quota RPC communication failed"}
    finally:
        stdin.close()
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        kill_session(process.pid)
        stdout_task = asyncio.create_task(_drain_probe_output(stdout, bytearray(), 0))
        try:
            await _stop_probe_process(process)
            async with asyncio.timeout(_CLI_PROBE_REAP_TIMEOUT_SECONDS):
                await asyncio.gather(stdout_task, stderr_task)
        except (TimeoutError, OSError):
            pass
        finally:
            stdout_task.cancel()
            stderr_task.cancel()
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)

    return get_quota_parser(rpc.parser)(json.dumps(result).encode(), stderr=bytes(stderr))


async def probe_print_cli_connection(
    conn_id: str,
    conn: Connection,
    *,
    cli_binary: Path | str | None,
    profile: PrintCliProfile | None = None,
    store: ConnectionStateStore | None = None,
    timeout_seconds: float | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run the declared quota or auth status command with minimal env in a temp cwd."""
    if profile is None:
        if conn.profile is None:
            raise ValueError(f"Set connections.{conn_id}.profile to a registered print CLI profile")
        profile = get_print_cli_profile(conn.profile)
    label = profile.display_name or profile.id
    rpc = profile.quota_rpc if conn.show_quota else None
    if timeout_seconds is None:
        timeout_seconds = rpc.timeout_seconds if rpc else PRINT_CLI_QUOTA_PROBE_TIMEOUT_SECONDS
    parse_quota = get_quota_parser(profile.parse_quota) if profile.quota_command and profile.parse_quota else None
    status_spec = profile.login
    status_command = status_spec.status_command if status_spec is not None else ()
    command = rpc.command if rpc else profile.quota_command if parse_quota else status_command
    quota_source = (profile.quota_source or "print_cli_usage") if rpc or parse_quota else None
    probe_label = "usage probe" if parse_quota else "status probe"
    target_store = store or get_connection_state_store()
    ref_now = now or datetime.now(timezone.utc)

    resolved_cli = cli_binary or resolve_print_cli_binary(profile)

    with use_login_home(conn.config_dir or Path.home()):
        version_info = await probe_cli_version(resolved_cli, fixed_env=profile.fixed_env)
        cli_version = version_info.get("version")

        if not command:
            return await target_store.upsert(
                conn_id, auth="unknown", quota=[], quota_source=None,
                cli_version=cli_version, checked_at=ref_now, last_error=None,
            )

        if resolved_cli is None:
            return await target_store.upsert(
                conn_id,
                auth="unknown",
                quota=[],
                quota_source=quota_source,
                cli_version=cli_version,
                checked_at=ref_now,
                last_error=_cap_error(f"{label} CLI binary not found"),
            )

        if not parse_quota and (not cli_version or not profile.supports_version(cli_version)):
            return await target_store.upsert(
                conn_id, auth="unknown", quota=[], quota_source=None,
                cli_version=cli_version, checked_at=ref_now,
                last_error=f"{label} CLI version is missing or below the supported minimum",
            )

        if conn.config_dir is not None and not Path(conn.config_dir).is_dir():
            # Do not let the CLI auto-create an unlogged-in home (login flow creates it 0700).
            return await target_store.upsert(
                conn_id,
                auth="logged_out",
                quota=[],
                quota_source=quota_source,
                cli_version=cli_version,
                checked_at=ref_now,
                last_error=None,
            )

        login_home = conn.config_dir or Path.home()
        probe_env = build_print_cli_env(profile, login_home)
        if conn.config_dir is None and "TERM" not in profile.fixed_env:
            probe_env.pop("TERM", None)
        parsed: ParsedQuota
        with tempfile.TemporaryDirectory(prefix=f"kotoyose-{profile.id}-usage-") as tmpdir:
            try:
                process = await asyncio.create_subprocess_exec(
                    str(resolved_cli),
                    *command,
                    cwd=tmpdir,
                    env=probe_env,
                    stdin=asyncio.subprocess.PIPE if rpc else asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    **({"start_new_session": True, "limit": _QUOTA_RPC_LINE_LIMIT} if rpc else {}),
                )
            except OSError as exc:
                parsed = {
                    "auth": "unknown",
                    "quota": [],
                    "last_error": _cap_error(f"Could not launch {label} CLI: {type(exc).__name__}"),
                }
            else:
                if rpc:
                    parsed = await _exchange_quota_rpc(process, rpc, timeout_seconds)
                else:
                    try:
                        stdout, stderr = await asyncio.wait_for(
                            process.communicate(),
                            timeout=timeout_seconds,
                        )
                    except TimeoutError:
                        await _stop_probe_process(process)
                        parsed = {
                            "auth": "unknown",
                            "quota": [],
                            "last_error": _cap_error(
                                f"{label} {probe_label} timed out after {timeout_seconds:g}s"
                            ),
                        }
                    except asyncio.CancelledError:
                        await _stop_probe_process(process)
                        raise
                    except OSError as exc:
                        await _stop_probe_process(process)
                        parsed = {
                            "auth": "unknown",
                            "quota": [],
                            "last_error": _cap_error(f"{label} {probe_label} failed: {type(exc).__name__}"),
                        }
                    else:
                        if parse_quota is None:
                            assert status_spec is not None
                            output = stdout.decode("utf-8", errors="replace")
                            payload = parse_json_text(_ANSI_ESCAPE_RE.sub("", output).strip())
                            signed_in = status_spec.matches_status(process.returncode, payload, output)
                            diagnostics = classify_stderr(
                                profile.classify_stderr, stderr.decode("utf-8", errors="replace"),
                            )
                            if signed_in:
                                auth = "ok"
                            elif (process.returncode == status_spec.success_exit_code
                                  or diagnostics.get("status") == "UNAUTHENTICATED"):
                                auth = "logged_out"
                            else:
                                auth = "unknown"
                            parsed = {
                                "auth": auth, "quota": [],
                                "last_error": f"{label} CLI status check failed" if auth == "unknown" else None,
                            }
                        else:
                            parsed = parse_quota(stdout or b"", stderr=stderr or b"")
                        if parse_quota and process.returncode != 0 and parsed["auth"] == "ok":
                            parsed = {
                                "auth": "unknown",
                                "quota": [],
                                "last_error": _cap_error(
                                    f"{label} CLI exited with status {process.returncode}"
                                ),
                            }

        return await target_store.upsert(
            conn_id,
            auth=parsed["auth"],
            quota=parsed["quota"],
            quota_source=quota_source,
            cli_version=cli_version,
            checked_at=ref_now,
            last_error=_cap_error(parsed["last_error"]),
            **({"plan": parsed["plan"]} if "plan" in parsed else {}),
        )


def _sanitize_probe_error(text: str | None, *secrets: str | None) -> str | None:
    """Scrub any resolved secret values from `text` and cap to `_ERROR_TEXT_MAX_LEN` chars."""
    if text is None:
        return None
    cleaned = text
    for secret in secrets:
        if secret:
            cleaned = cleaned.replace(secret, "[REDACTED]")
    return _cap_error(cleaned)


def _format_model_count_plan(count: int | None) -> str | None:
    """Format observed model count (e.g. '12 models') for `state['plan']` when known."""
    if count is None or count < 0:
        return None
    return f"{count} {'model' if count == 1 else 'models'}"


def _require_https_url(url: str) -> str:
    """Ensure fixed vendor probe URLs always use HTTPS."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("Vendor probe URL must use HTTPS")
    return url


async def _resolve_connection_secret_for_probe(conn: Connection) -> tuple[str | None, str | None]:
    """Resolve `conn.secret` (`env:` or secret store) without logging or exposing the value.

    Returns `(token, missing_error)`. When `conn.secret` is unset, returns `(None, None)`.
    """
    if not conn.secret:
        return None, None
    if conn.env_name:
        raw = process_env.get(conn.env_name)
        token = raw.strip() if isinstance(raw, str) else ""
        if not token:
            return None, f"secret '{conn.env_name}' is not set"
        return token, None
    secret_name = conn.secret_name or conn.secret
    try:
        raw = await resolve_secret_async(secret_name)
    except (SecretStoreError, ValueError):
        return None, f"secret '{secret_name}' is not set"
    token = raw.strip() if isinstance(raw, str) else ""
    if not token:
        return None, f"secret '{secret_name}' is not set"
    return token, None


def _count_models_from_response(response: httpx.Response) -> int | None:
    """Extract unique model count from an OpenAI/Anthropic `/models` JSON response, or None."""
    from .model_discovery import _model_ids_from_values

    try:
        payload = response.json()
    except ValueError:
        return None
    if isinstance(payload, Mapping):
        if isinstance(payload.get("models"), list):
            raw_list = payload["models"]
        elif isinstance(payload.get("data"), list):
            raw_list = payload["data"]
        else:
            return None
    elif isinstance(payload, list):
        raw_list = payload
    else:
        return None
    return len(_model_ids_from_values(list(raw_list)))


def _classify_http_probe_response(response: httpx.Response) -> tuple[str, str | None, str | None]:
    """Map an HTTP `/models` response to `(auth, plan, last_error)`."""
    status = response.status_code
    if status == 200:
        return "ok", _format_model_count_plan(_count_models_from_response(response)), None
    if status in (401, 403):
        return "logged_out", None, f"API key rejected (HTTP {status})"
    if status == 429:
        return "ok", None, "rate limited"
    if 300 <= status < 400:
        return "unknown", None, f"redirect not followed (HTTP {status})"
    return "unknown", None, f"HTTP {status}"


async def _probe_http_get(
    url: str,
    *,
    headers: Mapping[str, str],
    timeout_seconds: float,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.Response:
    """Perform a bounded read-only HTTP GET without following redirects."""
    client_kwargs: dict[str, Any] = {
        "follow_redirects": False,
        "timeout": httpx.Timeout(timeout_seconds),
        "trust_env": False,
    }
    if transport is not None:
        client_kwargs["transport"] = transport
    async with httpx.AsyncClient(**client_kwargs) as client:
        return await client.get(url, headers=dict(headers))


async def _record_key_probe_state(
    store: ConnectionStateStore,
    conn_id: str,
    *,
    auth: str,
    plan: str | None,
    checked_at: datetime,
    last_error: str | None,
    secret_value: str | None = None,
) -> dict[str, Any]:
    """Persist `api_key` / `endpoint` probe state with empty quota and scrubbed, capped error."""
    return await store.upsert(
        conn_id,
        auth=auth,
        expires_at=None,
        plan=plan,
        quota=[],
        quota_source=None,
        checked_at=checked_at,
        last_error=_sanitize_probe_error(last_error, secret_value),
    )


async def _probe_cursor_api_key_connection(
    conn_id: str,
    conn: Connection,
    *,
    store: ConnectionStateStore,
    timeout_seconds: float,
    now: datetime,
    secret_value: str,
) -> dict[str, Any]:
    """Probe a Cursor `api_key` connection via the existing Cursor SDK model listing."""
    from . import model_discovery

    try:
        async with asyncio.timeout(timeout_seconds):
            result = await model_discovery._discover_cursor_models(conn)
    except TimeoutError:
        return await _record_key_probe_state(
            store,
            conn_id,
            auth="unknown",
            plan=None,
            checked_at=now,
            last_error=f"probe timed out after {timeout_seconds:g}s",
            secret_value=secret_value,
        )
    except Exception as exc:
        auth = "logged_out" if model_discovery._is_cursor_auth_error(exc) else "unknown"
        err = "API key rejected" if auth == "logged_out" else f"Cursor SDK probe failed ({type(exc).__name__})"
        return await _record_key_probe_state(
            store,
            conn_id,
            auth=auth,
            plan=None,
            checked_at=now,
            last_error=err,
            secret_value=secret_value,
        )

    error_code = result.get("error_code")
    if error_code in (None, "empty_result"):
        models = result.get("models") or []
        return await _record_key_probe_state(
            store,
            conn_id,
            auth="ok",
            plan=_format_model_count_plan(len(models)),
            checked_at=now,
            last_error=None,
            secret_value=secret_value,
        )
    if error_code in ("authentication_failed", "authentication_missing"):
        secret_label = conn.secret_name or conn.env_name or "cursor"
        err = f"secret '{secret_label}' is not set" if error_code == "authentication_missing" else "API key rejected"
        return await _record_key_probe_state(
            store,
            conn_id,
            auth="logged_out",
            plan=None,
            checked_at=now,
            last_error=err,
            secret_value=secret_value,
        )
    if error_code == "rate_limited":
        return await _record_key_probe_state(
            store,
            conn_id,
            auth="ok",
            plan=None,
            checked_at=now,
            last_error="rate limited",
            secret_value=secret_value,
        )
    if error_code == "timeout":
        return await _record_key_probe_state(
            store,
            conn_id,
            auth="unknown",
            plan=None,
            checked_at=now,
            last_error=f"probe timed out after {timeout_seconds:g}s",
            secret_value=secret_value,
        )
    return await _record_key_probe_state(
        store,
        conn_id,
        auth="unknown",
        plan=None,
        checked_at=now,
        last_error="Cursor SDK probe failed",
        secret_value=secret_value,
    )


async def probe_api_key_connection(
    conn_id: str,
    conn: Connection,
    *,
    store: ConnectionStateStore | None = None,
    timeout_seconds: float = API_KEY_PROBE_TIMEOUT_SECONDS,
    now: datetime | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Probe an `api_key` connection using read-only model listing endpoints or Cursor SDK."""
    target_store = store or get_connection_state_store()
    ref_now = now or datetime.now(timezone.utc)
    token, missing_err = await _resolve_connection_secret_for_probe(conn)
    if missing_err is not None or not token:
        return await _record_key_probe_state(
            target_store,
            conn_id,
            auth="logged_out",
            plan=None,
            checked_at=ref_now,
            last_error=missing_err or "secret is not set",
        )

    provider = conn.provider.lower()
    if provider == "cursor":
        return await _probe_cursor_api_key_connection(
            conn_id,
            conn,
            store=target_store,
            timeout_seconds=timeout_seconds,
            now=ref_now,
            secret_value=token,
        )

    if provider in _CLAUDE_SUBSCRIPTION_PROVIDERS:
        url = _require_https_url(ANTHROPIC_MODELS_URL)
        headers = {
            "x-api-key": token,
            "anthropic-version": ANTHROPIC_API_VERSION,
        }
    elif provider in _OPENAI_API_KEY_PROVIDERS:
        url = _require_https_url(OPENAI_MODELS_URL)
        headers = {"Authorization": f"Bearer {token}"}
    else:
        existing = await target_store.get(conn_id)
        return existing if existing is not None else default_connection_state(conn_id)

    try:
        response = await _probe_http_get(
            url,
            headers=headers,
            timeout_seconds=timeout_seconds,
            transport=transport,
        )
    except (httpx.TimeoutException, TimeoutError):
        auth, plan, last_error = "unknown", None, f"probe timed out after {timeout_seconds:g}s"
    except (httpx.RequestError, OSError) as exc:
        auth, plan, last_error = "unknown", None, f"network error ({type(exc).__name__})"
    else:
        auth, plan, last_error = _classify_http_probe_response(response)

    return await _record_key_probe_state(
        target_store,
        conn_id,
        auth=auth,
        plan=plan,
        checked_at=ref_now,
        last_error=last_error,
        secret_value=token,
    )


async def probe_endpoint_connection(
    conn_id: str,
    conn: Connection,
    *,
    store: ConnectionStateStore | None = None,
    timeout_seconds: float = API_KEY_PROBE_TIMEOUT_SECONDS,
    now: datetime | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Probe an `endpoint` connection via read-only `GET {endpoint}/models`."""
    from .model_discovery import _models_url_candidates

    target_store = store or get_connection_state_store()
    ref_now = now or datetime.now(timezone.utc)
    if not conn.endpoint:
        return await _record_key_probe_state(
            target_store,
            conn_id,
            auth="unknown",
            plan=None,
            checked_at=ref_now,
            last_error="endpoint unreachable",
        )

    token, missing_err = await _resolve_connection_secret_for_probe(conn)
    if missing_err is not None:
        return await _record_key_probe_state(
            target_store,
            conn_id,
            auth="logged_out",
            plan=None,
            checked_at=ref_now,
            last_error=missing_err,
        )

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    candidates = _models_url_candidates(conn.endpoint)
    auth = "unknown"
    plan: str | None = None
    last_error: str | None = "endpoint unreachable"

    for idx, url in enumerate(candidates):
        try:
            response = await _probe_http_get(
                url,
                headers=headers,
                timeout_seconds=timeout_seconds,
                transport=transport,
            )
        except (httpx.TimeoutException, TimeoutError, httpx.RequestError, OSError):
            auth, plan, last_error = "unknown", None, "endpoint unreachable"
            break

        if response.status_code == 404 and idx + 1 < len(candidates):
            continue
        auth, plan, last_error = _classify_http_probe_response(response)
        break

    return await _record_key_probe_state(
        target_store,
        conn_id,
        auth=auth,
        plan=plan,
        checked_at=ref_now,
        last_error=last_error,
        secret_value=token,
    )


async def probe_connection(
    conn_id: str,
    *,
    cfg: HarnessConfig | None = None,
    execution_service: Any = None,
    store: ConnectionStateStore | None = None,
    timeout_seconds: float | None = None,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Probe one connection safely; supports subscription CLIs, API keys, and endpoints."""
    active_cfg = cfg or get_config()
    conn = active_cfg.connections.get(conn_id)
    if conn is None:
        raise KeyError(conn_id)
    target_store = store or get_connection_state_store()

    if conn.kind == "api_key":
        return await probe_api_key_connection(
            conn_id,
            conn,
            store=target_store,
            timeout_seconds=timeout_seconds if timeout_seconds is not None else API_KEY_PROBE_TIMEOUT_SECONDS,
        )

    if conn.kind == "endpoint":
        return await probe_endpoint_connection(
            conn_id,
            conn,
            store=target_store,
            timeout_seconds=timeout_seconds if timeout_seconds is not None else API_KEY_PROBE_TIMEOUT_SECONDS,
        )

    cli_binary = resolve_connection_cli_binary(active_cfg, conn_id, conn, execution_service)

    profile = active_cfg.print_cli_profile_for_connection(conn_id)
    if profile is not None:
        return await probe_print_cli_connection(
            conn_id,
            conn,
            cli_binary=cli_binary,
            profile=profile,
            store=target_store,
            timeout_seconds=timeout_seconds,
        )

    if conn.kind == "subscription" and conn.provider == "codex":
        return await probe_codex_connection(
            conn_id,
            conn,
            cli_binary=cli_binary,
            store=target_store,
            timeout_seconds=timeout_seconds if timeout_seconds is not None else DEFAULT_PROBE_TIMEOUT_SECONDS,
            force_refresh=force_refresh,
        )

    if (
        conn.kind == "subscription"
        and conn.provider.lower() in _CLAUDE_SUBSCRIPTION_PROVIDERS
        and conn.config_dir is not None
    ):
        return await probe_claude_connection(
            conn_id,
            conn,
            cli_binary=cli_binary,
            store=target_store,
            timeout_seconds=timeout_seconds if timeout_seconds is not None else CLAUDE_USAGE_PROBE_TIMEOUT_SECONDS,
        )

    version_info = await probe_cli_version(cli_binary)
    now = datetime.now(timezone.utc)
    if version_info.get("version") is not None:
        return await target_store.upsert(
            conn_id,
            cli_version=version_info["version"],
            checked_at=now,
        )
    existing = await target_store.get(conn_id)
    return existing if existing is not None else default_connection_state(conn_id)


async def probe_all_connections(
    *,
    cfg: HarnessConfig | None = None,
    execution_service: Any = None,
    store: ConnectionStateStore | None = None,
    timeout_seconds: float | None = None,
) -> dict[str, dict[str, Any]]:
    """Run probes sequentially across configured connections without raising."""
    active_cfg = cfg or get_config()
    target_store = store or get_connection_state_store()
    results: dict[str, dict[str, Any]] = {}
    for conn_id, conn in sorted(active_cfg.connections.items()):
        if conn.kind not in {"subscription", "api_key", "endpoint"}:
            continue
        try:
            results[conn_id] = await probe_connection(
                conn_id,
                cfg=active_cfg,
                execution_service=execution_service,
                store=target_store,
                timeout_seconds=timeout_seconds,
            )
        except Exception as exc:
            log.warning(
                "connections_probe.connection_failed",
                connection=conn_id,
                error_type=type(exc).__name__,
            )
    return results


async def connection_probe_loop(
    *,
    app_state: Any = None,
    interval_seconds: float = DEFAULT_PROBE_INTERVAL_SECONDS,
    probe_fn: Any = None,
) -> None:
    """Periodic background task: probe subscriptions at startup and then every `interval_seconds`."""
    runner = probe_fn or probe_all_connections
    while True:
        try:
            service = getattr(app_state, "execution_service", None) if app_state is not None else None
            store = getattr(app_state, "connection_state_store", None) if app_state is not None else None
            await runner(execution_service=service, store=store)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("connections_probe.loop_failed", error_type=type(exc).__name__)
        await asyncio.sleep(interval_seconds)


def is_auth_failure_error(exc: BaseException) -> bool:
    """Return True if `exc` represents a normalized provider/runtime authentication failure."""
    if getattr(exc, "code", None) in {"authentication_failed", "unauthorized", "invalid_api_key"}:
        return True
    if getattr(exc, "http_status_code", None) == 401:
        return True
    diagnostics = getattr(exc, "diagnostics", None)
    if isinstance(diagnostics, Mapping):
        if diagnostics.get("assistant_error") == "authentication_failed":
            return True
        if diagnostics.get("terminal_reason") == "authentication_failed":
            return True
    return False


async def record_connection_auth_failure(
    connection_id: str | None,
    *,
    error: str | None = None,
    store: ConnectionStateStore | None = None,
) -> None:
    """Record a runtime or admission authentication failure (`auth='expired'`) safely."""
    if not connection_id:
        return
    target_store = store or get_connection_state_store()
    try:
        await target_store.mark_auth_expired(connection_id, error=error)
    except Exception as exc:
        log.warning("connections_probe.auth_mark_failed", connection=connection_id, error_type=type(exc).__name__)


async def record_claude_rate_limit(
    connection_id: str | None,
    payload: Mapping[str, Any] | None,
    *,
    store: ConnectionStateStore | None = None,
) -> None:
    """Persist official Claude SDK `RateLimitEvent` quota info for `connection_id`."""
    if not connection_id or not isinstance(payload, Mapping):
        return
    target_store = store or get_connection_state_store()
    raw_window = payload.get("rate_limit_type")
    status = payload.get("status")
    utilization = payload.get("utilization")
    used_percent: float | None = None
    if isinstance(utilization, (int, float)) and not isinstance(utilization, bool):
        used_percent = float(utilization) * 100.0
    elif status == "rejected":
        used_percent = 100.0
    if used_percent is None:
        return

    resets_at = payload.get("resets_at")
    window_entry = {
        "window": _format_window_label(raw_window if isinstance(raw_window, str) else None, fallback="default"),
        "used_percent": used_percent,
        "resets_at": resets_at,
        "source": "official_stream",
    }
    try:
        await target_store.merge_quota_window(
            connection_id,
            window_entry,
            quota_source="official_stream",
            clear_auth_failure=(status in {"allowed", "allowed_warning"}),
        )
    except Exception as exc:
        log.warning("connections_probe.claude_quota_failed", connection=connection_id, error_type=type(exc).__name__)


async def record_provider_usage_limit(
    connection_id: str | None,
    limit: Any,
    *,
    store: ConnectionStateStore | None = None,
) -> None:
    """Record an observed `ProviderUsageLimit` (B2) on the connection's quota state."""
    if not connection_id or limit is None:
        return
    target_store = store or get_connection_state_store()
    provider = getattr(limit, "provider", None)
    source = "official_stream" if provider == "claude" else "official_rpc"
    limit_window = getattr(limit, "limit_window", None)
    reset_at = getattr(limit, "reset_at", None)
    detected_at = getattr(limit, "detected_at", None) or datetime.now(timezone.utc)
    window_entry = {
        "window": _format_window_label(limit_window if isinstance(limit_window, str) else None, fallback="limit"),
        "used_percent": 100.0,
        "resets_at": reset_at,
        "source": source,
    }
    try:
        await target_store.merge_quota_window(
            connection_id,
            window_entry,
            quota_source=source,
            checked_at=detected_at,
        )
    except Exception as exc:
        log.warning("connections_probe.limit_record_failed", connection=connection_id, error_type=type(exc).__name__)
