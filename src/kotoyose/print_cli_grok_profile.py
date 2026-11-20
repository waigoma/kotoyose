"""Pinned Grok Build CLI declaration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .cli_install_spec import CliInstallSpec, DirectPlatformBinary

if TYPE_CHECKING:
    from .print_cli_profiles import PrintCliProfile


_GROK_BINARY_URLS = (
    "https://x.ai/cli/grok-{version}-{os}-{arch}",
    "https://storage.googleapis.com/grok-build-public-artifacts/cli/grok-{version}-{os}-{arch}",
)


def grok_profile() -> PrintCliProfile:
    from .print_cli_profiles import PrintCliLoginSpec, PrintCliProfile, PrintCliQuotaRpc, QuotaErrorPattern, SurfaceCliSpec

    return PrintCliProfile(
        id="grok",
        provider="grok",
        display_name="Grok Build",
        binary="grok",
        min_version=(1, 0, 89),
        install=CliInstallSpec(
            version="1.0.89",
            method="direct_binary",
            direct_binaries=(
                DirectPlatformBinary(
                    "linux-x64", "linux", "x86_64", _GROK_BINARY_URLS,
                    "a3dface51d9bdc71fd121e0481dd02d9b46a721f7a6b892f756480e9666eba1f",
                ),
                DirectPlatformBinary("linux-arm64", "linux", "aarch64", _GROK_BINARY_URLS, "unset"),
            ),
        ),
        argv_template=(
            "--output-format", "streaming-json", "-m", "{model}", "{extra_args}",
            "--single={prompt}",
        ),
        surfaces={
            "read": SurfaceCliSpec(extra_args=(
                "--permission-mode", "dontAsk", "--no-subagents", "--disable-web-search",
                "--tools", "Read,Grep,Glob",
                "--allow", "Read", "--allow", "Grep", "--allow", "Glob",
                "--deny", "Bash", "--deny", "Edit", "--deny", "Write", "--deny", "MCPTool",
            )),
            # Grok calls this always-approve mode. It avoids tool approval prompts.
            "write": SurfaceCliSpec(extra_args=("--permission-mode", "bypassPermissions")),
        },
        settings_flag=None,
        parser="grok_streaming_json_v1",
        classify_stderr="grok_error_v1",
        exit_codes={},
        quota_error_patterns=(
            QuotaErrorPattern(name="rate_limit", statuses=("RATE_LIMITED",), window="rate"),
            QuotaErrorPattern(name="quota_exhausted", statuses=("RESOURCE_EXHAUSTED",)),
        ),
        models_command=("models",),
        parse_models="grok_models_v1",
        models_source="grok_cli_models",
        quota_rpc=PrintCliQuotaRpc(
            command=("agent", "stdio"),
            requests=(
                {
                    "jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {
                        "protocolVersion": 1,
                        "clientCapabilities": {
                            "fs": {"readTextFile": False, "writeTextFile": False},
                            "terminal": False,
                        },
                    },
                },
                {"jsonrpc": "2.0", "id": 2, "method": "_x.ai/billing", "params": {}},
            ),
            result_id=2,
            parser="grok_billing_v1",
        ),
        quota_source="grok_acp_billing",
        login_home_env="GROK_HOME",
        login_home_env_paths={"HOME": "."},
        max_concurrent_per_home=1,
        login_flow="device_code",
        login=PrintCliLoginSpec(
            command=("login", "--device-auth"),
            url_pattern=r"https://[^\s<>\"'\x00-\x1f\x7f]+",
            url_hosts=("accounts.x.ai", "auth.x.ai", "x.ai", "grok.com"),
            user_code_pattern=r"(?i)\b([a-z0-9]{4}-[a-z0-9]{4})\b",
            cli_timeout_seconds=300,
            status_command=("models",),
            signed_out_pattern=r"(?i)\bnot authenticated\b",
            success_json_field=None,
        ),
        egress_hosts=("cli-chat-proxy.grok.com", "auth.x.ai"),
        egress_idle_seconds=900,
    )
