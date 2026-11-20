"""Standard-library-only print-mode CLI declarations and profile registry."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
import hashlib
import json
import math
import os
import re
import shutil
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

from .cli_install_spec import CliInstallSpec, install_path, installed_binary_version


def _freeze_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(k): _freeze_value(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze_value(item) for item in value)
    return value


def _thaw_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _thaw_value(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [_thaw_value(item) for item in value]
    return value


@dataclass(frozen=True)
class SurfaceCliSpec:
    """Per-surface CLI flags and optional permission settings payload."""

    extra_args: tuple[str, ...] = ()
    settings: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.settings is not None:
            object.__setattr__(self, "settings", _freeze_value(self.settings))

    def settings_payload(self) -> dict[str, Any] | None:
        """Return a mutable JSON-serializable copy of permission settings, or None."""
        if not self.settings:
            return None
        return _thaw_value(self.settings)

    def to_canonical_dict(self) -> dict[str, Any]:
        return {
            "extra_args": list(self.extra_args),
            "settings": self.settings_payload(),
        }


@dataclass(frozen=True)
class QuotaErrorPattern:
    """Declarative quota/rate-limit matcher for a print CLI profile."""

    name: str
    statuses: tuple[str, ...] = ()
    http_statuses: tuple[int, ...] = ()
    patterns: tuple[str, ...] = ()
    window: str = "unknown"
    default_retry_seconds: int | None = None

    def to_canonical_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "statuses": list(self.statuses),
            "http_statuses": list(self.http_statuses),
            "patterns": list(self.patterns),
            "window": self.window,
            "default_retry_seconds": self.default_retry_seconds,
        }


@dataclass(frozen=True)
class PrintCliLoginSpec:
    """Trusted PTY login declaration.

    URL candidates must match the pattern and an HTTPS host in url_hosts.
    Device code patterns capture the code in group 1. Browser and device flows
    run status_command after login and apply the success fields to its result.
    An unset success_json_field checks the exit code alone.
    """

    command: tuple[str, ...]
    url_pattern: str
    url_hosts: tuple[str, ...]
    cli_timeout_seconds: int
    code_prompt_pattern: str | None = None
    success_exit_code: int = 0
    success_json_field: str | None = "status"
    success_json_value: str | bool | int | None = "SUCCESS"
    user_code_pattern: str | None = None
    status_command: tuple[str, ...] = ()
    public_error_lines: tuple[str, ...] = ()
    # Some CLIs exit 0 from their status command even when signed out (Grok's `models`
    # prints "You are not authenticated." and still lists defaults). This regex marks that.
    signed_out_pattern: str | None = None

    def matches_status(
        self, returncode: int | None, payload: dict[str, Any] | None, output: str = "",
    ) -> bool:
        """Check the declared exit code, the signed-out marker and optional JSON status field."""
        if returncode != self.success_exit_code:
            return False
        if self.signed_out_pattern and re.search(self.signed_out_pattern, output):
            return False
        if self.success_json_field is None:
            return True
        return payload is not None and payload.get(self.success_json_field) == self.success_json_value

    def to_canonical_dict(self) -> dict[str, Any]:
        return {
            "command": list(self.command),
            "url_pattern": self.url_pattern,
            "url_hosts": list(self.url_hosts),
            "user_code_pattern": self.user_code_pattern,
            "status_command": list(self.status_command),
            "public_error_lines": list(self.public_error_lines),
            "signed_out_pattern": self.signed_out_pattern,
            "code_prompt_pattern": self.code_prompt_pattern,
            "cli_timeout_seconds": self.cli_timeout_seconds,
            "success_exit_code": self.success_exit_code,
            "success_json_field": self.success_json_field,
            "success_json_value": self.success_json_value,
        }


@dataclass(frozen=True)
class PrintCliQuotaRpc:
    """Ordered JSON-RPC requests with a named parser for the selected result."""

    command: tuple[str, ...]
    requests: tuple[Mapping[str, Any], ...]
    result_id: int | str
    parser: str
    timeout_seconds: float = 20

    def __post_init__(self) -> None:
        ids = [request.get("id") for request in self.requests]
        if (
            any(type(request_id) not in (int, str) for request_id in ids)
            or len(ids) != len(set(ids))
            or type(self.result_id) not in (int, str)
            or self.result_id not in ids
        ):
            raise ValueError("Quota RPC requires unique request IDs and a matching result ID")
        if not self.command or not self.parser or not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("Quota RPC requires a command, parser and positive timeout")
        object.__setattr__(self, "requests", tuple(_freeze_value(request) for request in self.requests))

    def to_canonical_dict(self) -> dict[str, Any]:
        return {
            "command": list(self.command),
            "requests": [_thaw_value(request) for request in self.requests],
            "result_id": self.result_id,
            "parser": self.parser,
            "timeout_seconds": self.timeout_seconds,
        }


@dataclass(frozen=True)
class PrintCliProfile:
    """Declarative specification for a print-mode (`-p`) CLI binary."""

    id: str
    provider: str
    binary: str
    min_version: tuple[int, ...]
    argv_template: tuple[str, ...]
    surfaces: Mapping[str, SurfaceCliSpec]
    settings_flag: str | None
    parser: str
    login_home_env: str
    exit_codes: Mapping[int, str]
    quota_error_patterns: tuple[QuotaErrorPattern, ...]
    auth_error_patterns: tuple[str, ...] = ()
    max_prompt_bytes: int = 120 * 1024
    max_concurrent_per_home: int = 2
    quota_command: tuple[str, ...] = ()
    models_command: tuple[str, ...] = ()
    add_dir_flag: str | None = "--add-dir"
    env_allowlist: tuple[str, ...] = ("PATH", "LANG", "TERM")
    login: PrintCliLoginSpec | None = None
    login_flow: str | None = None
    classify_stderr: str | None = None
    parse_models: str | None = None
    parse_quota: str | None = None
    models_source: str | None = None
    quota_source: str | None = None
    display_name: str | None = None
    fixed_env: Mapping[str, str] = field(default_factory=dict)
    egress_idle_seconds: float = 300.0
    egress_hosts: tuple[str, ...] = ()
    egress_ports: tuple[int, ...] = (443,)
    api_key_env: str | None = None
    host_only: bool = False
    bundle_root: Literal["realpath_parent"] | None = None
    login_home_env_paths: Mapping[str, str] = field(default_factory=dict)
    login_env: Mapping[str, str] = field(default_factory=dict)
    install: CliInstallSpec | None = None
    quota_rpc: PrintCliQuotaRpc | None = None

    def __post_init__(self) -> None:
        if self.quota_rpc is not None and self.quota_command:
            raise ValueError("quota_rpc and quota_command are mutually exclusive")
        if self.bundle_root not in (None, "realpath_parent"):
            raise ValueError("Unsupported print CLI bundle root")
        if self.api_key_env is not None and (
            not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.api_key_env)
            or self.api_key_env in {
                *self.env_allowlist, *self.fixed_env, *self.login_home_env_paths,
                *self.login_env, self.login_home_env,
            }
        ):
            raise ValueError("api_key_env must be a distinct environment variable name")
        object.__setattr__(self, "login_home_env_paths", MappingProxyType(dict(self.login_home_env_paths)))
        object.__setattr__(self, "login_env", MappingProxyType(dict(self.login_env)))
        object.__setattr__(self, "fixed_env", MappingProxyType(dict(self.fixed_env)))
        object.__setattr__(self, "surfaces", MappingProxyType(dict(self.surfaces)))
        object.__setattr__(self, "exit_codes", MappingProxyType(dict(self.exit_codes)))

    def surface_spec(self, surface: str) -> SurfaceCliSpec:
        """Return the CLI spec for the requested surface ('read' or 'write')."""
        try:
            return self.surfaces[surface]
        except KeyError as exc:
            raise ValueError(f"Unsupported surface {surface!r} for print CLI profile {self.id!r}") from exc

    def classify_exit_code(self, returncode: int | None) -> str | None:
        """Map a known non-zero exit code from the profile table, or None."""
        if returncode is None:
            return None
        return self.exit_codes.get(returncode)

    def format_timeout(self, timeout_seconds: float) -> str:
        """Format a duration string bounded strictly below tiny deadlines too."""
        clamped = max(0.1, float(timeout_seconds))
        if clamped < 1.2:
            return f"{max(100, int(clamped * 850))}ms"
        return f"{max(1, int(math.floor(clamped)))}s"

    def supports_version(self, output: str) -> bool:
        """Compare the numeric version components, ignoring a build suffix."""
        match = re.search(r"(?<![\w.])v?(\d+(?:\.\d+)+)", output)
        if match is None:
            return False
        version = tuple(int(part) for part in match[1].split("."))
        return (version + (0,) * len(self.min_version))[:len(self.min_version)] >= self.min_version

    def render_argv(
        self,
        *,
        model: str,
        prompt: str,
        timeout_seconds: float,
        surface: str,
        settings_path: Path | str | None = None,
        add_dirs: Sequence[Path | str] = (),
    ) -> list[str]:
        """Render CLI arguments safely without `str.format` interpretation of prompt text."""
        spec = self.surface_spec(surface)
        timeout_str = self.format_timeout(timeout_seconds)
        replacements = {
            "{prompt}": prompt,
            "{model}": model,
            "{timeout}": timeout_str,
        }
        extra_args = list(spec.extra_args)
        if self.add_dir_flag:
            for directory in add_dirs:
                extra_args.extend([self.add_dir_flag, str(directory)])
        if settings_path is not None and self.settings_flag:
            extra_args.extend([self.settings_flag, str(settings_path)])

        argv: list[str] = []
        for token in self.argv_template:
            if token == "{extra_args}":
                argv.extend(extra_args)
                continue
            if token in replacements:
                argv.append(replacements[token])
                continue
            argv.append(re.sub(r"\{(?:model|prompt|timeout)\}", lambda match: replacements[match[0]], token))
        if "{extra_args}" not in self.argv_template:
            argv.extend(extra_args)
        return argv

    def write_settings_file(self, surface: str, directory: Path) -> Path | None:
        """Write surface permission settings JSON into `directory`, or return None."""
        spec = self.surface_spec(surface)
        payload = spec.settings_payload()
        if payload is None:
            return None
        path = directory / f"{self.id}-{surface}-settings.json"
        path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        return path

    def to_canonical_dict(self) -> dict[str, Any]:
        """Serialize every profile field canonically for stable hashing."""
        return {
            "id": self.id,
            "provider": self.provider,
            "binary": self.binary,
            "min_version": list(self.min_version),
            "argv_template": list(self.argv_template),
            "surfaces": {
                key: self.surfaces[key].to_canonical_dict()
                for key in sorted(self.surfaces)
            },
            "settings_flag": self.settings_flag,
            "parser": self.parser,
            "login_home_env": self.login_home_env,
            "exit_codes": {str(k): self.exit_codes[k] for k in sorted(self.exit_codes)},
            "quota_error_patterns": [p.to_canonical_dict() for p in self.quota_error_patterns],
            "auth_error_patterns": list(self.auth_error_patterns),
            "max_prompt_bytes": self.max_prompt_bytes,
            "max_concurrent_per_home": self.max_concurrent_per_home,
            "quota_command": list(self.quota_command),
            **({"quota_rpc": self.quota_rpc.to_canonical_dict()} if self.quota_rpc else {}),
            "models_command": list(self.models_command),
            "add_dir_flag": self.add_dir_flag,
            "env_allowlist": list(self.env_allowlist),
            "login": self.login.to_canonical_dict() if self.login is not None else None,
            "login_flow": self.login_flow,
            "classify_stderr": self.classify_stderr,
            "parse_models": self.parse_models,
            "parse_quota": self.parse_quota,
            "models_source": self.models_source,
            "quota_source": self.quota_source,
            "display_name": self.display_name,
            "fixed_env": dict(self.fixed_env),
            "egress_idle_seconds": self.egress_idle_seconds,
            "egress_hosts": list(self.egress_hosts),
            "egress_ports": list(self.egress_ports),
            **({"install": asdict(self.install)} if self.install else {}),
            **({"host_only": True} if self.host_only else {}),
            **({"bundle_root": self.bundle_root} if self.bundle_root else {}),
            **({"login_home_env_paths": dict(self.login_home_env_paths)} if self.login_home_env_paths else {}),
            **({"login_env": dict(self.login_env)} if self.login_env else {}),
            **({"api_key_env": self.api_key_env} if self.api_key_env is not None else {}),
        }

    def isolated_bundle_root(self, resolved_binary: Path) -> Path | None:
        """Identify the versioned runtime directory for an already resolved binary."""
        return resolved_binary.parent if self.bundle_root == "realpath_parent" else None

    def launch_digest(self, resolved_binary: Path, *, placement: str) -> str:
        """Digest the executable and, for isolated bundles, the pinned version directory."""
        bundle = self.isolated_bundle_root(resolved_binary) if placement == "isolated" else None
        digest = hashlib.sha256()
        if bundle is not None:
            digest.update(b"print-cli-bundle\0" + os.fsencode(bundle) + b"\0")
        with resolved_binary.open("rb") as stream:
            return hashlib.file_digest(stream, lambda: digest).hexdigest()

    def profile_digest(self) -> str:
        """Return a stable SHA-256 digest of the canonical profile definition."""
        encoded = json.dumps(
            self.to_canonical_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


AGY_PROFILE = PrintCliProfile(
    id="agy",
    provider="antigravity",
    binary="agy",
    min_version=(1, 5, 9),
    argv_template=(
        "-p",
        "{prompt}",
        "--model",
        "{model}",
        "--output-format",
        "stream-json",
        "--print-timeout",
        "{timeout}",
    ),
    surfaces={
        "read": SurfaceCliSpec(
            extra_args=("--mode", "plan"),
            settings=None,
        ),
        "write": SurfaceCliSpec(
            extra_args=("--mode", "accept-edits"),
            settings={
                "permissions": {
                    "allow": [
                        "command(*)",
                        "write_file(*)",
                        "edit_file(*)",
                        "replace_file_content(*)",
                    ]
                }
            },
        ),
    },
    settings_flag="--settings",
    parser="agy_stream_json_v1",
    login_home_env="HOME",
    exit_codes={3: "timeout"},
    quota_error_patterns=(
        QuotaErrorPattern(
            name="rate_limit",
            statuses=("RATE_LIMITED",),
            http_statuses=(429,),
            patterns=(
                r"\brate[_\s-]?limit(?:ed| exceeded)?\b",
                r"\btoo many requests\b",
                r"\bhttp\s*429\b",
                r"\bstatus[=:\s]+429\b",
            ),
            window="rate",
        ),
        QuotaErrorPattern(
            name="quota_exhausted",
            statuses=("RESOURCE_EXHAUSTED", "MODEL_QUOTA_EXHAUSTED", "QUOTA_EXCEEDED"),
            patterns=(
                r"\bresource[_\s-]?exhausted\b",
                r"\bmodel[_\s-]?quota[_\s-]?exhausted\b",
                r"\bquota[_\s-]?(?:exceeded|exhausted|limit)\b",
                r"quota will reset",
            ),
            window="unknown",
        ),
    ),
    auth_error_patterns=(
        "authentication required",
        "visit the url to log in",
        "requires sign-in",
        "requires sign in",
        "must sign in",
        "need to sign in",
        "not logged in",
        "please log in",
        "login required",
        "requires login",
        "logged out",
        "login expired",
        "session expired",
        "re-authenticate",
        "reauthenticate",
        "authentication_failed",
        "unauthenticated",
        "invalid api key",
        "unauthorized",
    ),
    max_prompt_bytes=120 * 1024,
    max_concurrent_per_home=2,
    quota_command=("-p", "/usage", "--output-format", "json"),
    models_command=("models",),
    add_dir_flag="--add-dir",
    env_allowlist=("PATH", "LANG", "TERM"),
    login_flow="paste_code",
    classify_stderr="agy_error_v1",
    parse_models="agy_models_v1",
    parse_quota="agy_usage_v1",
    models_source="agy_cli_models",
    quota_source="agy_cli_usage",
    display_name="Antigravity",
    login=PrintCliLoginSpec(
        command=("-p", "/usage", "--output-format", "json"),
        url_pattern=r"https://accounts\.google\.com/[^\s<>\"'\x00-\x1f\x7f]+",
        url_hosts=("accounts.google.com",),
        public_error_lines=("Error: invalid authorization code",),
        code_prompt_pattern=r"(?i)paste the authorization code",
        cli_timeout_seconds=60,
        success_exit_code=0,
        success_json_field="status",
        success_json_value="SUCCESS",
    ),
    egress_hosts=(
        "daily-cloudcode-pa.googleapis.com",
        "aicode.googleapis.com",
        "www.googleapis.com",
        "play.googleapis.com",
        "lh3.googleusercontent.com",
        # agy 自身が自分の login token を更新する先 (期限前の refresh)。塞ぐと host と挙動が変わる。
        "oauth2.googleapis.com",
    ),
    egress_ports=(443,),
)


_REGISTRY: dict[str, PrintCliProfile] = {}
PRINT_CLI_PROFILES: Mapping[str, PrintCliProfile] = MappingProxyType(_REGISTRY)


def register_print_cli_profile(profile: PrintCliProfile) -> None:
    """Register a trusted print-CLI declaration by id."""
    if profile.id in _REGISTRY:
        raise ValueError(f"Print CLI profile already registered: {profile.id!r}")
    _REGISTRY[profile.id] = profile


def get_print_cli_profile(profile_id: str) -> PrintCliProfile:
    """Look up a registered print-CLI profile by id."""
    try:
        return PRINT_CLI_PROFILES[profile_id]
    except KeyError as exc:
        raise KeyError(f"Unknown print CLI profile: {profile_id!r}") from exc


def list_print_cli_profiles() -> tuple[PrintCliProfile, ...]:
    """Return all registered print-CLI profiles in registration order."""
    return tuple(PRINT_CLI_PROFILES.values())


def get_print_cli_profile_for_provider(provider: str) -> PrintCliProfile | None:
    """Find a unique profile for provider-wide model discovery."""
    target = provider.strip().lower()
    candidates = [profile for profile in PRINT_CLI_PROFILES.values() if profile.provider.lower() == target]
    return candidates[0] if len(candidates) == 1 else None


def _register_builtins() -> None:
    from .print_cli_cursor_profile import cursor_profile

    from .print_cli_grok_profile import grok_profile

    register_print_cli_profile(AGY_PROFILE)
    register_print_cli_profile(cursor_profile())
    register_print_cli_profile(grok_profile())


_register_builtins()


def build_print_cli_env(
    profile: PrintCliProfile, login_home: Path | str | None, *, minimal: bool = False,
) -> dict[str, str]:
    """Build a child environment with allowlisted parent values, fixed values, and the login home."""
    from .process_env import build_minimal_env

    env = build_minimal_env() if minimal else {
        key: os.environ[key] for key in profile.env_allowlist if key in os.environ
    }
    env.update(profile.fixed_env)
    if login_home is not None:
        env[profile.login_home_env] = str(login_home)
        env.update({key: str(Path(login_home) / path) for key, path in profile.login_home_env_paths.items()})
    return env

def resolve_print_cli_binary(profile: PrintCliProfile, settings: Any = None) -> Path | None:
    """Resolve the machine override, managed version, or PATH binary."""
    drivers = getattr(settings, "drivers", None) or {}
    install = drivers.get("cli.print")
    if install is not None:
        binaries = (
            install.get("profile_binaries", {}) if isinstance(install, Mapping)
            else getattr(install, "profile_binaries", {})
        )
        if profile.id in binaries:
            return Path(binaries[profile.id])
    if profile.install is not None:
        path = install_path(profile.id, profile.install.version, profile.binary)
        return path if installed_binary_version(path) else None
    found = shutil.which(profile.binary)
    return Path(found).resolve() if found else None


def validate_print_cli_connection(profile, connection, *, placement: str = "host") -> None:
    label = f"cli.print profile '{profile.id}' with {connection.kind} connection"
    if profile.host_only and placement != "host":
        raise ValueError(f"{label} supports host placement only")
    if connection.provider != profile.provider:
        raise ValueError(f"{label} requires provider '{profile.provider}'")
    if connection.kind == "subscription":
        if connection.config_dir is None:
            raise ValueError(f"{label} requires config_dir (login home)")
    elif connection.kind == "api_key":
        if not profile.api_key_env:
            raise ValueError(f"{label} requires a profile declaring api_key_env")
        if not connection.secret_name:
            raise ValueError(f"{label} requires a named secret-store reference")
        if placement != "host":
            raise ValueError(f"{label} supports host placement only; jail/isolated is unsupported")
    else:
        raise ValueError(f"{label} is unsupported")
