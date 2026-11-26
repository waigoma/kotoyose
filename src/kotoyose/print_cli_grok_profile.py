"""Pinned Grok Build CLI declaration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .cli_install_spec import CliArtifact, CliInstallSpec

if TYPE_CHECKING:
    from .print_cli_profiles import PrintCliProfile


_GROK_BINARY_URLS = (
    "https://x.ai/cli/grok-{version}-{os}-{arch}",
    "https://storage.googleapis.com/grok-build-public-artifacts/cli/grok-{version}-{os}-{arch}",
)
GROK_ACP_ARGV = ("agent", "--no-leader", "--model", "{model}", "stdio")
GROK_READ_FLAGS = (
    "--permission-mode", "dontAsk", "--no-subagents", "--disable-web-search",
    "--tools", "Read,Grep,Glob",
    "--allow", "Read", "--allow", "Grep", "--allow", "Glob",
    "--deny", "Bash", "--deny", "Edit", "--deny", "Write", "--deny", "MCPTool",
)
# child には OS の閉じ込めが無い。Grok Build は ACP では --tools / --deny / --permission-mode を効かせず、
# 編集を許可の問い合わせなしに行う (1.0.89 で実測)。読み取りは Grok 自身の read-only sandbox
# (Linux は Landlock、書き込みは ~/.grok と /tmp だけ、子プロセスはネットワーク不可) で守る。
# ネットワーク制約には Linux カーネル 6.7 以降 (Landlock ABI 4 以上) が必要。子機は起動前に確認する。
GROK_ACP_READ_SANDBOX = ("--sandbox", "read-only")
GROK_ACP_READ_ARGV = GROK_ACP_READ_SANDBOX + GROK_READ_FLAGS + GROK_ACP_ARGV
GROK_ACP_UNRESTRICTED_ARGV = ("--no-subagents", "--disable-web-search") + GROK_ACP_ARGV
GROK_ACP_WRITE_ARGV = ("--sandbox", "strict") + GROK_ACP_UNRESTRICTED_ARGV
GROK_INTEGRATION_ENV = tuple(
    (f"GROK_{source}_{feature}_ENABLED", "0")
    for source in ("CLAUDE", "CURSOR", "CODEX")
    for feature in ("HOOKS", "MCPS", "SKILLS", "RULES", "AGENTS")
)
GROK_NATIVE_SKILL_LAYOUT = "grok_agents_home_v1"
GROK_LINUX_SKILL_VERSION = "1.0.89"
GROK_LINUX_SKILL_DIGEST = "sha256:a3dface51d9bdc71fd121e0481dd02d9b46a721f7a6b892f756480e9666eba1f"
DEVICE_LOGIN_URL_PATTERN = r"https://[^\s<>\"'\x00-\x1f\x7f]+"
GROK_DEVICE_CODE_PATTERN = r"(?i)\b([a-z0-9]{4}-[a-z0-9]{4})\b"
GROK_SIGNED_OUT_PATTERN = r"(?i)\bnot authenticated\b"


def grok_profile() -> PrintCliProfile:
    from .acp_spec import AcpSpec
    from .print_cli_profiles import PrintCliLoginSpec, PrintCliProfile, PrintCliQuotaRpc, QuotaErrorPattern, SurfaceCliSpec

    return PrintCliProfile(
        id="grok",
        acp=AcpSpec(GROK_ACP_ARGV, usage_parser="grok_acp_meta_v1"),
        provider="grok",
        display_name="Grok Build",
        binary="grok",
        min_version=(1, 0, 89),
        native_skill_layout=GROK_NATIVE_SKILL_LAYOUT,
        install=CliInstallSpec(
            version=GROK_LINUX_SKILL_VERSION,
            artifacts=(
                CliArtifact(
                    "linux-x64", "linux", "x86_64", _GROK_BINARY_URLS,
                    GROK_LINUX_SKILL_DIGEST,
                ),
                CliArtifact("linux-arm64", "linux", "aarch64", _GROK_BINARY_URLS, "unset"),
            ),
        ),
        argv_template=(
            "--output-format", "streaming-json", "-m", "{model}", "{extra_args}",
            "--single={prompt}",
        ),
        surfaces={
            "read": SurfaceCliSpec(extra_args=GROK_READ_FLAGS),
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
        terminal_login="hidden",
        login_home_env_paths={"HOME": "."},
        login_flow="device_code",
        login=PrintCliLoginSpec(
            command=("login", "--device-auth"),
            url_pattern=DEVICE_LOGIN_URL_PATTERN,
            url_hosts=("accounts.x.ai", "auth.x.ai", "x.ai", "grok.com"),
            user_code_pattern=GROK_DEVICE_CODE_PATTERN,
            cli_timeout_seconds=300,
            status_command=("models",),
            signed_out_pattern=GROK_SIGNED_OUT_PATTERN,
            success_json_field=None,
        ),
        egress_hosts=("cli-chat-proxy.grok.com", "auth.x.ai"),
        egress_idle_seconds=900,
    )
