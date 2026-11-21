"""Standard-library-only declaration for the Cursor CLI."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .cli_install_spec import CliArtifact, CliInstallSpec

if TYPE_CHECKING:
    from .print_cli_profiles import PrintCliProfile

_AUTH_HOSTS = (
    "cursor.com",
    "authenticate.cursor.sh",
    "prod.authentication.cursor.sh",
    "authentication.cursor.sh",
)


def cursor_profile() -> PrintCliProfile:
    from .acp_spec import AcpSpec
    from .print_cli_profiles import PrintCliLoginSpec, PrintCliProfile, QuotaErrorPattern, SurfaceCliSpec

    return PrintCliProfile(
        id="cursor",
        acp=AcpSpec(("acp",), home_auth_methods=("cursor_login",), env=(("NO_OPEN_BROWSER", "1"),)),
        provider="cursor",
        display_name="Cursor",
        binary="agent",
        min_version=(2026, 11, 13),
        install=CliInstallSpec(
            version="2026.11.19-f005e9d",
            artifacts=(
                CliArtifact(
                    "linux-x64", "linux", "x64",
                    ("https://downloads.cursor.com/lab/{version}/{os}/{arch}/agent-cli-package.tar.gz",),
                    "sha256:a25986eaf0c9330aeeec599a139711cb9f267c64562d073a72b751edf5161695",
                    format="tar.gz", directory="dist-package", entrypoint="cursor-agent",
                ),
            ),
        ),
        bundle_root="realpath_parent",
        argv_template=(
            "--print", "--output-format", "stream-json", "--model", "{model}",
            "{extra_args}", "--", "{prompt}",
        ),
        surfaces={
            # Cursor's own sandbox needs AppArmor support and fails to start on hosts without it
            # ("Sandbox mode is enabled but not available on this system", measured 2026-11-19).
            # `--mode ask` alone keeps the read surface from editing files.
            "read": SurfaceCliSpec(extra_args=("--mode", "ask", "--trust")),
            "write": SurfaceCliSpec(extra_args=("--force", "--trust")),
        },
        settings_flag=None,
        parser="cursor_stream_json_v1",
        classify_stderr="cursor_error_v1",
        exit_codes={},
        quota_error_patterns=(
            QuotaErrorPattern(name="rate_limit", statuses=("RATE_LIMITED",), window="rate"),
            QuotaErrorPattern(name="quota_exhausted", statuses=("RESOURCE_EXHAUSTED",)),
        ),
        models_command=("models",),
        parse_models="cursor_models_v1",
        login_home_env="HOME",
        login_home_env_paths={"CURSOR_CONFIG_DIR": ".cursor"},
        login_env={"NO_OPEN_BROWSER": "1"},
        fixed_env={"NODE_USE_ENV_PROXY": "1"},
        login_flow="browser_wait",
        login=PrintCliLoginSpec(
            command=("login",),
            url_pattern=r"https://[^\s<>\"'\x00-\x1f\x7f]+",
            url_hosts=_AUTH_HOSTS,
            cli_timeout_seconds=300,
            status_command=("status", "--format", "json"),
            success_json_field=None,
        ),
        # Re-measure this allowlist on a real isolated run.
        # Measured on a real jail run 2026-11-19: api2 (control) and agentn.global.api5 (agent turns).
        egress_hosts=("api2.cursor.sh", "agentn.global.api5.cursor.sh", *_AUTH_HOSTS[1:]),
    )
