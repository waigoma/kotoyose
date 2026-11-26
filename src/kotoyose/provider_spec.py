"""実行、導入、ログインを記述する組み込み SDK プロバイダー宣言。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from typing import Literal

from .provider_login import LoginFlowKind, PrintCliLoginSpec

_Scalar = str | bool | int | float
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass(frozen=True, slots=True)
class ChildLoginHome:
    """OS ユーザーの CLI ログイン先。CLI が自ら読む場合は config_dir を設定しない。"""

    env: str
    default_subdir: str
    inject_config_dir: bool = False

    def __post_init__(self) -> None:
        if not _ENV_NAME.fullmatch(self.env) or (
            self.default_subdir in ("", ".", "..") or any(char in self.default_subdir for char in "/\\:\x00")
        ):
            raise ValueError("invalid child login home declaration")


class ChildLaunch(Enum):
    """既存 builder に渡す、子機ローカルの起動方式。"""

    CLAUDE_AGENT_SDK_PATH = "cli_path"
    CODEX_EFFECTIVE_CONFIG_CHECK = "child_launch"


LoginTransport = Literal["pty", "console"]


@dataclass(frozen=True, slots=True)
class LocalLogin:
    """ローカルログインを検証済みの OS と起動方式に限定する。"""

    transports: tuple[tuple[str, LoginTransport], ...] = ()

    def __post_init__(self) -> None:
        supported = {("linux", "pty"), ("win32", "console")}
        if not set(self.transports) <= supported or len(dict(self.transports)) != len(self.transports):
            raise ValueError("unsupported local login transport")


@dataclass(frozen=True, slots=True)
class SdkInstall:
    """子機で所有する配布物、import 名、同梱 CLI、PATH の探索候補。"""

    packages: tuple[str, ...]
    module: str
    cli: str
    cli_name: str
    windows_cli: tuple[str, ...]
    bundled_cli: str
    cli_package: str
    windows_profile_cli: str | None = None


@dataclass(frozen=True, slots=True)
class SdkLogin:
    """SDK 製品に付属する CLI のログイン、状態確認、残量取得の契約。"""

    spec: PrintCliLoginSpec
    flow: LoginFlowKind
    local: LocalLogin
    quota_command: tuple[str, ...]
    quota_probe: Literal["claude_cli_usage", "codex_app_server"]
    status_parser: Literal["strict_json_bool", "codex_exit_code"]


@dataclass(frozen=True, slots=True)
class ChildSandboxCap:
    """sandbox の権限順、要求の同義語、子機所有者が指定する上限。"""

    order: tuple[str, ...]
    aliases: tuple[tuple[str, str], ...]
    request_default: str
    policy_key: str
    policy_default: str

    def __post_init__(self) -> None:
        if len(self.order) != len(set(self.order)) or any(
            value not in self.order for value in (self.request_default, self.policy_default, *(mode for _, mode in self.aliases))
        ):
            raise ValueError("invalid child sandbox declaration")


@dataclass(frozen=True, slots=True)
class ChildOptions:
    """wire に残す preset のオプション。sandbox と強制値は受信時だけ適用する。"""

    field: str
    allowed: tuple[tuple[str, tuple[type, ...]], ...]
    sandbox: ChildSandboxCap | None = None
    force: tuple[tuple[str, _Scalar], ...] = ()

    def wire(self, value: object) -> dict[str, _Scalar]:
        """指定した型そのものに一致する値だけをコピーする。"""
        if not isinstance(value, dict):
            return {}
        allowed = dict(self.allowed)
        return {key: item for key, item in value.items() if type(item) in allowed.get(key, ())}

    def local(self, options: dict[str, _Scalar], *, sandbox_max: str | None, display_name: str) -> dict[str, _Scalar]:
        """wire の投影に子機の sandbox 上限と強制値を適用する。"""
        values = dict(options)
        if cap := self.sandbox:
            requested = options.get("sandbox", cap.request_default)
            requested = dict(cap.aliases).get(requested, requested)
            if requested not in cap.order:
                raise ValueError(f"invalid {display_name} sandbox mode")
            ceiling = cap.policy_default if sandbox_max is None else sandbox_max
            values["sandbox"] = min(requested, ceiling, key=cap.order.index)
        values.update(self.force)
        return values


SDK_SKILL_FORMATS = ("claude_plugin", "codex_skill_input")
PROMPT_PREFIX_SKILL_FORMAT = "prompt_prefix"


@dataclass(frozen=True, slots=True)
class ChildTurnSpec:
    """子機で許可する接続と host の値を投影する規則。MCP の定義は常に子機方針から得る。"""

    connection_kind: Literal["subscription"] = "subscription"
    login_home: ChildLoginHome | None = None
    launch: ChildLaunch | None = None
    strip_route: tuple[str, ...] = ()
    strip_preset: tuple[str, ...] = ()
    options: ChildOptions | None = None
    skill_format: Literal["claude_plugin", "codex_skill_input", "prompt_prefix", "none"] = "claude_plugin"
    hello_feature: str | None = None
    output_guard_env: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.connection_kind != "subscription" or self.skill_format not in (*SDK_SKILL_FORMATS, PROMPT_PREFIX_SKILL_FORMAT, "none"):
            raise ValueError("unsupported child turn declaration")
        if self.launch is not None and not isinstance(self.launch, ChildLaunch):
            raise ValueError("unsupported child launch operation")
        if any(not _ENV_NAME.fullmatch(name) for name in self.output_guard_env):
            raise ValueError("invalid child output guard environment name")

    def project(self, route: dict, preset: dict) -> None:
        """送信時と受信時の両方で、許可した route/preset の値だけを残す。"""
        for key in self.strip_route:
            route.pop(key, None)
        for key in self.strip_preset:
            preset.pop(key, None)
        if options := self.options:
            preset[options.field] = options.wire(preset.get(options.field))


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    """SDK 製品の識別情報と子機 turn。実行時は driver の provider で builder を引く。"""

    id: str
    display_name: str
    drivers: tuple[str, ...]
    child_turn: ChildTurnSpec
    install: SdkInstall
    login: SdkLogin
    initial: str
    tone: str
    aliases: tuple[str, ...] = ()


CLAUDE_PRESET_OVERRIDES = ("anthropic_base_url", "anthropic_auth_token_env", "anthropic_auth_token_secret")
CODEX_PRESET_OVERRIDES = ("codex_home",)
CODEX_CHILD_DRIVER = "codex.app-server"
CODEX_TURNS_FEATURE = "codex-turns"

CODEX_SANDBOX_CAP = ChildSandboxCap(
    order=("read-only", "workspace-write", "danger-full-access"),
    aliases=(("full-access", "danger-full-access"),),
    request_default="read-only",
    policy_key="codex_sandbox_max",
    policy_default="workspace-write",
)
_WIRE_SCALARS = (str, bool, int, float)
CODEX_CHILD_OPTIONS = ChildOptions(
    field="codex",
    allowed=(("timeout", _WIRE_SCALARS), ("sandbox", _WIRE_SCALARS)),
    sandbox=CODEX_SANDBOX_CAP,
    force=(("approval_mode", "deny_all"),),
)

_SDK_LOCAL_LOGIN = LocalLogin((("linux", "pty"), ("win32", "console")))

BUILTIN_PROVIDERS = (
    ProviderSpec(
        id="claude", display_name="Claude", drivers=("claude.agent-sdk",), aliases=("anthropic",),
        initial="A", tone="claude",
        install=SdkInstall(
            packages=("claude-agent-sdk",), module="claude_agent_sdk", cli="claude", cli_name="Claude Code",
            windows_cli=("claude.exe",), windows_profile_cli=".local/bin/claude.exe",
            bundled_cli="claude_agent_sdk/_bundled/claude", cli_package="claude-agent-sdk",
        ),
        login=SdkLogin(
            spec=PrintCliLoginSpec(
                cli_timeout_seconds=600, command=("auth", "login", "--claudeai"),
                url_pattern=r"https://[^\s<>\"']+",
                url_hosts=("claude.com", "claude.ai", "platform.claude.com"),
                status_command=("auth", "status", "--json"),
                success_json_field="loggedIn", success_json_value=True,
            ),
            flow="paste_code", local=_SDK_LOCAL_LOGIN,
            quota_command=("-p", "/usage", "--output-format", "json"),
            quota_probe="claude_cli_usage", status_parser="strict_json_bool",
        ),
        child_turn=ChildTurnSpec(
            login_home=ChildLoginHome("CLAUDE_CONFIG_DIR", ".claude", inject_config_dir=True),
            launch=ChildLaunch.CLAUDE_AGENT_SDK_PATH,
            strip_preset=CLAUDE_PRESET_OVERRIDES,
            output_guard_env=("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"),
        ),
    ),
    ProviderSpec(
        id="codex", display_name="Codex", drivers=(CODEX_CHILD_DRIVER,),
        initial="O", tone="openai",
        install=SdkInstall(
            packages=("openai-codex", "openai-codex-cli-bin"), module="openai_codex", cli="codex", cli_name="Codex",
            windows_cli=("codex.exe", "codex.cmd"), bundled_cli="codex_cli_bin/bin/codex", cli_package="openai-codex-cli-bin",
        ),
        login=SdkLogin(
            spec=PrintCliLoginSpec(
                cli_timeout_seconds=600, command=("login", "--device-auth"),
                url_pattern=r"https://auth\.openai\.com/[^\s<>\"']+", url_hosts=("auth.openai.com",),
                user_code_pattern=r"(?i)\b([a-z0-9]{4}-[a-z0-9]{4,5})\b",
                status_command=("login", "status"), success_json_field=None,
            ),
            flow="device_code", local=_SDK_LOCAL_LOGIN,
            quota_command=("app-server", "--listen", "stdio://"),
            quota_probe="codex_app_server", status_parser="codex_exit_code",
        ),
        child_turn=ChildTurnSpec(
            login_home=ChildLoginHome("CODEX_HOME", ".codex"),
            launch=ChildLaunch.CODEX_EFFECTIVE_CONFIG_CHECK,
            strip_route=("model_provider",),
            strip_preset=CODEX_PRESET_OVERRIDES,
            options=CODEX_CHILD_OPTIONS,
            skill_format="codex_skill_input",
            hello_feature=CODEX_TURNS_FEATURE,
            output_guard_env=("OPENAI_API_KEY",),
        ),
    ),
)


def sdk_provider(name: str) -> ProviderSpec | None:
    """正規 ID と既存の別名から組み込み SDK 宣言を引く。"""
    key = name.casefold()
    return next((row for row in BUILTIN_PROVIDERS if key == row.id or key in row.aliases), None)
