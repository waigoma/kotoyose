"""
設定モジュール。

環境変数と YAML から HarnessConfig を読み込み、互換 facade を提供する。
"""

from __future__ import annotations

import os
import re
import stat
from contextvars import ContextVar
from datetime import date, datetime
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal
from urllib.parse import urlsplit

from dotenv import load_dotenv
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_serializer, model_validator
import structlog
import yaml

from . import process_env
from .agent_spec import (
    DataPolicy, PlaceName, SurfaceName, driver_manifest, execution_environment_error, explicit_sandbox_error,
    host_sandbox_default, native_execution_error, output_schema_error,
)
from .data_policy import TrainingCapability, alias_data_policy, configured_capabilities
from .compatibility_config import CompatibilityConfig, NativeAliasSettings, load_compatibility_config
from .registry import registry
from .pool_strategies import KNOWN_REQUIRES, get_strategy
from .print_cli_profiles import (
    PrintCliProfile,
    get_print_cli_profile,
    list_print_cli_profiles,
    validate_print_cli_connection,
)
from .secret_store import (
    SecretStore,
    SecretStoreError,
    get_secret_store,
    resolve_secret_sync,
    validate_secret_name,
)

if TYPE_CHECKING:
    from claude_agent_sdk import ClaudeAgentOptions

load_dotenv()

log = structlog.get_logger()

# 管理用 HTTP Basic 認証のパスワードとユーザー名。
API_KEY: str | None = process_env.get("KOTOYOSE_TOKEN") or None
BASIC_AUTH_USER: str = os.environ.get("KOTOYOSE_BASIC_USER") or "kotoyose-service"

def _build_database_url() -> str | None:
    """個別の POSTGRES_* 変数から asyncpg 用 DSN を組み立てる。
    いずれかが未設定なら None を返す (インメモリモード)。
    """
    host = process_env.get("POSTGRES_URL")
    port = process_env.get("POSTGRES_PORT", "5432")
    user = process_env.get("POSTGRES_USER")
    password = process_env.get("POSTGRES_PASSWORD")
    db = process_env.get("POSTGRES_DB")

    if not all([host, user, password, db]):
        return None
    return f"postgresql://{user}:{password}@{host}:{port}/{db}"


DATABASE_URL: str | None = _build_database_url()

LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO").upper()

SESSION_TTL_SECONDS: int = int(os.environ.get("SESSION_TTL_SECONDS", "1800"))


SECRET_SETTING_FIELDS: frozenset[str] = frozenset({
    "anthropic_auth_token",
})


# _inject_mcp_request_headers() と同じ構文だけを起動時に検証する。
_MCP_REQUEST_HEADER_TEMPLATE = re.compile(r"\{\{header:([A-Za-z0-9-]+)\}\}")


def parse_secret_reference(value: object) -> str | None:
    """Return the secret name when `value` is a valid `secret:NAME` reference."""
    if not isinstance(value, str) or not value.startswith("secret:"):
        return None
    return validate_secret_name(value.removeprefix("secret:"))


def parse_mcp_secret_value(value: object) -> str | None:
    """Return the secret name when `value` is `{'secret': 'NAME'}`."""
    if not isinstance(value, dict):
        return None
    if set(value.keys()) != {"secret"} or not isinstance(value["secret"], str):
        raise ValueError("MCP dict values must have exact form {'secret': 'NAME'}")
    return validate_secret_name(value["secret"])


def _validate_mcp_mapping(values: object, *, scope: str, kind: str) -> None:
    """Validate an MCP `env` or `headers` mapping allowing strings or `{'secret': 'NAME'}`."""
    if values is None:
        return
    if not isinstance(values, dict):
        raise ValueError(f"{scope}.{kind} must be a mapping")
    for key, val in values.items():
        if not isinstance(key, str) or not key:
            raise ValueError(f"{scope}.{kind} keys must be non-empty strings")
        if isinstance(val, dict):
            parse_mcp_secret_value(val)
        elif not isinstance(val, str):
            raise ValueError(
                f"{scope}.{kind}['{key}'] must be a string or {{'secret': 'NAME'}}"
            )


def collect_mcp_secret_names(mcp_servers: Mapping[str, object]) -> set[str]:
    """Collect all secret names referenced by MCP `env` or `headers` dicts."""
    names: set[str] = set()
    for server in mcp_servers.values():
        if not isinstance(server, Mapping):
            continue
        for section in ("env", "headers"):
            mapping = server.get(section)
            if not isinstance(mapping, Mapping):
                continue
            for val in mapping.values():
                secret_name = parse_mcp_secret_value(val)
                if secret_name:
                    names.add(secret_name)
    return names


class AliasAgent(BaseModel):
    """alias が宣言する場所と surface の上限。model は route だけが持つ。"""

    model_config = ConfigDict(extra="forbid")
    place: PlaceName
    surface: SurfaceName


def _validate_http_endpoint(value: str | None, *, label: str = "endpoint") -> str | None:
    """空の endpoint は公式の既定。書くなら userinfo の無い絶対 HTTP(S) URL。"""
    if value is None:
        return None
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or any(character.isspace() or character == "\x00" for character in value)
    ):
        raise ValueError(f"{label} は userinfo の無い絶対 HTTP(S) URL です")
    return value


def _validate_connection_secret_ref(value: str | None) -> str | None:
    """Connection.secret は env:変数名 または secret ストア名。生値は置かない。"""
    if value is None:
        return None
    if value.startswith("env:"):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value.removeprefix("env:")):
            raise ValueError("env: の後は環境変数名です")
        return value
    return validate_secret_name(value)


_CLAUDE_SUBSCRIPTION_PROVIDERS = frozenset({"claude", "anthropic"})


def _validate_no_whitespace(value: str, *, label: str) -> str:
    """Reject empty or whitespace-containing identifier strings cleanly."""
    if not value.strip() or any(ch.isspace() for ch in value):
        raise ValueError(f"{label} は空白を含められません")
    return value


def missing_data_policy_message(connection_id: str) -> str:
    return f"connection '{connection_id}': set data_policy (no_training, trains or unknown)"


class Connection(BaseModel):
    """1 つのアカウント login または接続先 endpoint。秘密の値そのものは持たない。"""

    model_config = ConfigDict(extra="forbid")
    provider: str = Field(min_length=1)
    kind: Literal["subscription", "api_key", "endpoint"]
    data_policy: DataPolicy
    profile: str | None = Field(default=None, exclude_if=lambda value: value is None)
    home: str | None = Field(default=None, exclude_if=lambda value: value is None or value == "default")
    auth_file: Path | None = Field(default=None, exclude_if=lambda value: value is None)
    config_dir: Path | None = Field(default=None, exclude_if=lambda value: value is None)
    quota_from: str | None = Field(default=None, exclude_if=lambda value: value is None)
    endpoint: str | None = Field(default=None, exclude_if=lambda value: value is None)
    secret: str | None = Field(default=None, exclude_if=lambda value: value is None)
    show_quota: bool = Field(default=True, exclude_if=lambda value: value is True)
    on: str = Field(default="local", min_length=1, exclude_if=lambda value: value == "local")

    @property
    def env_name(self) -> str | None:
        if self.secret and self.secret.startswith("env:"):
            return self.secret.removeprefix("env:")
        return None

    @property
    def secret_name(self) -> str | None:
        if self.secret and not self.secret.startswith("env:"):
            return self.secret
        return None

    @field_validator("provider")
    @classmethod
    def _validate_provider(cls, value: str) -> str:
        return _validate_no_whitespace(value, label="provider")

    @field_validator("on")
    @classmethod
    def _validate_on(cls, value: str) -> str:
        return _validate_no_whitespace(value, label="on")

    @field_validator("endpoint")
    @classmethod
    def _validate_endpoint(cls, value: str | None) -> str | None:
        return _validate_http_endpoint(value, label="connection endpoint")

    @field_validator("secret")
    @classmethod
    def _validate_secret(cls, value: str | None) -> str | None:
        return _validate_connection_secret_ref(value)

    @field_validator("config_dir")
    @classmethod
    def _validate_config_dir(cls, value: Path | None) -> Path | None:
        if value is None:
            return None
        raw = str(value)
        cleaned = raw.strip()
        if not cleaned or "\x00" in raw:
            raise ValueError("config_dir は有効なパスである必要があります")
        expanded = Path(cleaned).expanduser() if (cleaned == "~" or cleaned.startswith("~/")) else value
        if not expanded.is_absolute():
            raise ValueError("config_dir must be an absolute path")
        return expanded

    @field_validator("quota_from")
    @classmethod
    def _validate_quota_from(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned or any(ch.isspace() for ch in cleaned):
            raise ValueError("quota_from は空白を含まない接続 ID を指定してください")
        return cleaned

    @model_validator(mode="after")
    def _validate_kind_fields(self) -> "Connection":
        if self.profile is not None:
            try:
                profile = get_print_cli_profile(self.profile)
            except KeyError as exc:
                raise ValueError(str(exc)) from exc
            if self.kind != "subscription":
                validate_print_cli_connection(profile, self)
            elif self.provider != profile.provider:
                raise ValueError(f"cli.print profile '{profile.id}' requires provider '{profile.provider}'")
        if self.kind == "subscription":
            if self.endpoint is not None:
                raise ValueError("subscription connection cannot specify endpoint")
            if self.provider.lower() in _CLAUDE_SUBSCRIPTION_PROVIDERS:
                if self.auth_file is not None:
                    raise ValueError("claude subscription connection cannot specify auth_file")
                if self.secret and self.config_dir is not None:
                    raise ValueError("claude subscription connection cannot specify both secret and config_dir")
                if not self.secret and self.config_dir is None:
                    raise ValueError("claude subscription connection requires secret or config_dir")
            elif self.provider.lower() in {p.provider.lower() for p in list_print_cli_profiles()}:
                if self.auth_file is not None:
                    raise ValueError("subscription connection cannot specify auth_file")
                if self.secret is not None:
                    raise ValueError("subscription connection cannot specify endpoint or secret")
            else:
                if self.config_dir is not None:
                    raise ValueError("subscription connection cannot specify config_dir")
                if self.secret is not None:
                    raise ValueError("subscription connection cannot specify endpoint or secret")
            if self.home is None:
                self.home = "default"
            elif not self.home.strip():
                raise ValueError("home は空白だけにできません")
            if self.auth_file is not None and not self.auth_file.is_absolute():
                raise ValueError("auth_file must be an absolute path")
        elif self.kind == "api_key":
            if (
                self.endpoint is not None
                or self.auth_file is not None
                or self.home is not None
                or self.config_dir is not None
            ):
                raise ValueError("api_key connection cannot specify endpoint, home, auth_file, or config_dir")
            if not self.secret:
                raise ValueError("api_key connection requires secret")
        elif self.kind == "endpoint":
            if self.auth_file is not None or self.home is not None or self.config_dir is not None:
                raise ValueError("endpoint connection cannot specify home, auth_file, or config_dir")
            if not self.endpoint:
                raise ValueError("endpoint connection requires endpoint")
            if not self.secret:
                raise ValueError("endpoint connection requires secret")
        return self


class AgentRoute(BaseModel):
    """名前の意味。接続先と model はここだけに書き、alias は route を指す。"""

    model_config = ConfigDict(extra="forbid")
    # 組み込み driver 名。registry が唯一の根拠。
    driver: str
    model: str = Field(min_length=1)
    connection: str = Field(min_length=1)
    # Codex chat だけ。alias の codex.model_provider が無いとき、preset_for がここを使う。
    model_provider: str | dict | None = Field(default=None, exclude_if=lambda value: value is None)
    # この connection の組についての owner の断言。model 名や参照名からは推測しない。
    subscription_only: bool = Field(default=False, strict=True, exclude_if=lambda value: not value)
    # cli.print 用の profile ID (e.g. "agy")。
    profile: str | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="before")
    @classmethod
    def _reject_replaced_route_keys(cls, value: object) -> object:
        if isinstance(value, dict):
            for old_key, replacement in (
                ("credential", "connection"),
                ("endpoint", "connection"),
                ("pool", "connection"),
            ):
                if old_key in value:
                    raise ValueError(f"{old_key} was replaced by {replacement}")
        return value

    @field_validator("driver")
    @classmethod
    def _validate_driver(cls, value: str) -> str:
        if not registry.has_driver(value):
            raise ValueError(f"unknown driver: {value}")
        return value

    @model_validator(mode="after")
    def _validate_chat_source(self) -> "AgentRoute":
        """model_provider と subscription_only は、driver から決まる provider に縛る。"""
        manifest = driver_manifest(self.driver)
        provider = manifest.provider
        if self.model_provider is not None and provider != "codex":
            raise ValueError("model_provider は provider='codex' の場合だけ指定できます")
        if self.subscription_only and provider != "claude":
            raise ValueError("subscription_only requires a Claude driver and endpoint connection")
        if manifest.unmapped_backend == "print_cli":
            if not self.profile or not self.profile.strip():
                raise ValueError("profile is required for cli.print routes")
            try:
                get_print_cli_profile(self.profile)
            except KeyError as exc:
                raise ValueError(f"unknown print CLI profile: {self.profile}") from exc
        elif self.profile is not None:
            raise ValueError("profile is supported only for cli.print routes")
        return self

    @field_validator("model", "connection")
    @classmethod
    def _validate_non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("空白だけにできません")
        return value


class AgentPreset(BaseModel):
    """Claude SDK の AgentDefinition へ変換する設定上の subagent 定義。"""

    description: str
    prompt: str
    tools: list[str] | None = None
    model: Literal["haiku", "sonnet", "opus", "inherit"] = "inherit"


AllowCapability = Literal["web", "browser", "desktop"]
AllowWorkspace = Literal["none", "read", "write"]


class AliasAllow(BaseModel):
    """alias が許す作業場・追加能力・egress・owner 全権の上限。"""

    model_config = ConfigDict(extra="forbid")
    workspace: AllowWorkspace = Field(default="none", exclude_if=lambda value: value == "none")
    capabilities: list[AllowCapability] = Field(default_factory=list, exclude_if=lambda value: not value)
    egress: list[str] = Field(default_factory=list, exclude_if=lambda value: not value)
    full: bool = Field(default=False, exclude_if=lambda value: not value)

    @field_validator("capabilities")
    @classmethod
    def _validate_capabilities(cls, value: list[AllowCapability]) -> list[AllowCapability]:
        return list(dict.fromkeys(value))

    @field_validator("egress")
    @classmethod
    def _validate_egress(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in value:
            stripped = item.strip()
            if not stripped or stripped != item or any(ch.isspace() for ch in item):
                raise ValueError("allow.egress entries must be non-empty host/pattern strings without whitespace")
            if stripped not in cleaned:
                cleaned.append(stripped)
        return cleaned


class ModelPreset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    execution: Literal["direct", "native"] = "direct"
    # Placement is independent of the chat execution mode. An absent choice preserves
    # existing routing, SDK defaults, and serialized configuration digests.
    execution_environment: Literal["isolated", "host"] | None = Field(
        default=None, exclude_if=lambda value: value is None)
    # route だけなら chat。agent には route が要る。agent が無い alias は agent として動かさない。
    route: str | None = Field(default=None, exclude_if=lambda value: value is None)
    agent: AliasAgent | None = Field(default=None, exclude_if=lambda value: value is None)
    on: str = Field(default="local", min_length=1, exclude_if=lambda value: value == "local")
    place: Literal["host", "jail"] | None = Field(default=None, exclude_if=lambda value: value is None)
    allow: AliasAllow | None = Field(default=None, exclude_if=lambda value: value is None)
    # direct でも検証・保存し、Native への切替時に設定値を復元できるようにする。
    native: NativeAliasSettings | None = None
    provider: Literal["claude", "codex", "cursor", "antigravity"] = "claude"
    model: str | None = None  # SDK に渡す Claude モデル ID (例: "claude-sonnet-4-5-20241022")
    anthropic_base_url: str | None = None
    anthropic_auth_token_env: str | None = None
    anthropic_auth_token_secret: str | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    # route がある alias では route の値が勝つ。route が無い alias の既定は false。
    subscription_only: bool = Field(default=False, strict=True, exclude_if=lambda value: not value)
    bare: bool = True  # True = --bare: CLAUDE.md/MCP/skills/agents/commands の自動検出を無効化
    # codex 専用の隔離弁。bare (= project_doc_max_bytes: 0) が塞ぐのは *project* の
    # AGENTS.md だけで、$CODEX_HOME/AGENTS.md と $CODEX_HOME/plugins は塞げない
    # (Claude の --bare と守備範囲が違う)。判定者のように個人の作法を継がせたくない
    # preset には、専用の CODEX_HOME を与える。auth.json も配下なので、認証を
    # 引き継ぐには symlink を張った最小構成の home を用意すること。
    codex_home: str | None = None
    # validate_default: Pydantic は既定値にバリデータを走らせないため、これが無いと
    # cwd を省いた preset だけ expand_cwd が動かず '~/kotoyose-workspace' が未展開のまま
    # SDK へ渡り、"Working directory does not exist: ~/..." で実行時に失敗する。
    cwd: str = Field(default="~/kotoyose-workspace", validate_default=True)
    # Claude SDK に渡す built-in tools の絞り込み (preset 単位の allowlist)。
    #   None (既定) → 従来通り ToolsPreset(type="preset", preset="claude_code") (全 built-in tool)。
    #     既存 preset の挙動は一切変えない。
    #   リスト指定 (空リスト含む) → 列挙した built-in tool 名だけを SDK へ渡す。
    #     空リスト [] は「built-in tool なし」を意味する有効値。SDK の `tools` は
    #     built-in tool 専用のスコープであり、MCP サーバー由来のツールは
    #     mcp_servers/mcp_refs の設定に従い別途利用可能 (影響を受けない)。
    #   deny (disallowed_tools) とは独立に併用可能 (defense in depth)。
    tools: list[str] | None = None
    deny: list[str] = Field(default_factory=list)
    prompt_file_refs: list[str] = Field(default_factory=list)  # HarnessConfig.prompt_files の参照名
    mcp_servers: dict[str, dict] = Field(default_factory=dict)  # MCP サーバー設定
    mcp_refs: list[str] = Field(default_factory=list)  # HarnessConfig.mcps の参照名
    # MCP header の `{{header:...}}` テンプレートへ渡すため、HTTP request に必須な header 名。
    # 認証値は config に保存せず、リクエストごとに build_options() へ受け渡す。
    required_request_headers: list[str] = Field(default_factory=list)
    # 旧 memory-service hook の後方互換フィールド。hook は commit d1e51c5 で撤去済み。
    # 既存 YAML の round-trip だけを維持し、新 Memory Gateway の制御には使用しない。
    memory_enabled: bool = False
    memory_force: bool = False
    fixed_conversation_key: str | None = None  # ヘッダなしでも v1 パスに固定する key (example-client など)
    # 以下 3 項目も旧 hook の後方互換値。新規設定では使用しない。
    # memory_force=True かつ X-Memory-Source ヘッダなし時の fallback source 値。
    # 任意文字列を許容 (中継地点で型を絞らず、判定は memory-service 側に委ねる)。
    # 推奨値は memory-service が現在受け付けている 'discord' / 'openwebui' / 'n8n' / 'manual'。
    # example-client のような OpenWebUI 互換クライアントは 'openwebui' が自然。
    # None の場合は 'manual' (汎用) で送信される。
    memory_source: str | None = None
    # X-Memory-Channel ヘッダ未指定時の fallback channel 値。
    # None の場合は f"preset:{model}" (例: "preset:example-client-test") で送信される。
    # example-client のような HEADER 付与困難なクライアントから "example-client:default" 等を指定可能にするための設定。
    memory_channel: str | None = None
    # X-Memory-Sender ヘッダ未指定時の fallback sender 値。
    # None の場合は "default" で送信される。
    # 同様にユーザー名や bot 名を preset 単位で固定したい時に使う。
    memory_sender: str | None = None

    # Thinking / effort 設定。
    #   thinking:
    #     - None     -> SDK のデフォルト挙動 (Opus 4.6+ では adaptive)
    #     - "adaptive" -> {"type": "adaptive"} を明示送信
    #     - "enabled"  -> {"type": "enabled", "budget_tokens": N} (旧形式)
    #     - "disabled" -> {"type": "disabled"} (Haiku など adaptive 非対応モデル用)
    #   effort: low | medium | high | xhigh | max | None
    #     - xhigh は Opus 4.7 で追加された段階 (high と max の間)。
    #     - Haiku 4.5 / Sonnet 4.5 は effort 非対応。
    #     - SDK 0.1.72 の Literal には xhigh が無いが、SDK は値をそのまま
    #       CLI 引数 (--effort) に転送するため文字列として通る。
    thinking: Literal["adaptive", "enabled", "disabled"] | None = None
    thinking_budget_tokens: int | None = None
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    # Claude の idle timeout。CLI provider は各 provider 設定の timeout を優先する。
    timeout_seconds: float | None = Field(default=None, gt=0)
    # Claude SDK の権限ポリシー。既定値は既存互換の bypassPermissions。
    permission_mode: Literal["default", "acceptEdits", "bypassPermissions", "plan", "dontAsk"] = "bypassPermissions"

    # per-request cwd 上書きの opt-in ゲート。
    # False (既定) の preset に cwd を含むリクエストが来ると build_options で ValueError、
    # chat.py で 400 を返す。TaskHub の coding_run worker など worktree を指定する
    # 呼び出し元のみ True を設定し、他 preset への誤送信を loud に失敗させる。
    allow_cwd_override: bool = False

    # allow_cwd_override=True の場合、per-request cwd の上書き先を制限するルートディレクトリ群。
    # None (既定) または空リストの場合、allow_cwd_override=True は ValidationError で拒否される。
    # リストの各要素は ~ 展開可能。chat.py で resolve() + is_relative_to() で検証される。
    # シンボリックリンクと .. を使った迂回を拒否する目的 (resolve の使用意図をコメントに残す)。
    cwd_allowed_roots: list[str] | None = None

    # Claude SDK に渡す --setting-sources の値。
    #   None (既定) → build_options で ["project"] にフォールバック (既存 preset 動作を維持)。
    #   明示指定    → 空リスト含めそのまま SDK へ渡す。
    #   推奨例:
    #     ["user", "project"] — user 設定 (plugins/skills) + worktree の CLAUDE.md を両方読む
    #     ["project"]         — プロジェクト設定のみ (デフォルト相当)
    #     []                  — 設定ファイルを一切読まない (隔離モード)
    setting_sources: list[Literal["user", "project", "local"]] | None = None

    # True は preset が明示した MCP だけを使い、user/project の自動 MCP 読込を拒否する。
    strict_mcp_config: bool = False

    # None は SDK の既定値を維持。False は resume 後も prompt を毎回再構成する。
    system_prompt_snapshot: bool | None = None

    # require_isolation=True の preset は「設定ソースを一切読まない」ことを不変条件として要求する。
    # 未信頼 cwd で Agent tool を露出する (bare:false) Task Hub 実行系のような preset に付与し、
    # setting_sources:null/省略への手戻りを ModelPreset レベルで拒否する。ModelPreset バリデータ
    # ゆえ admin API 経由 (update_model 等) の更新でも発火する — HarnessConfig 階層のガードは
    # model_copy + dict 代入経路を素通りするため、この階層に置くのが要。
    require_isolation: bool = False

    # プラグイン束のパス (--plugin-dir に転送)。skills/agents/commands 等を同梱した
    # ディレクトリを指す。絶対パス or ~/ 展開 (system_prompt_files と同じ流儀)。
    # _validate_plugins で存在と最小マニフェストを起動時に検証する。
    plugins: list[str] = Field(default_factory=list)

    skill_refs: list[str] = Field(default_factory=list)  # HarnessConfig.skill_definitions の参照名

    # 有効化する skill の選別 (context filter)。
    #   None=SDK 自動構成なし / "all"=発見した全 skill 有効 /
    #   [names]=列挙のみ有効 (残りは隠す) / []=全 skill 消灯。
    # plugins で読み込んだ束の中から名前で絞る。
    skills: list[str] | Literal["all"] | None = None

    agent_refs: list[str] = Field(default_factory=list)  # HarnessConfig.agent_definitions の参照名

    # Claude SDK にプログラマティック定義として渡す subagent 群。
    # bare:true でもファイル自動検出に頼らず明示的に注入できるため、
    # 信頼境界を保ったまま preset ごとの補助 agent を構成できる。
    # agent_refs との合成は HarnessConfig.resolve_agents が担い、同名は inline を優先する。
    agents: dict[str, AgentPreset] = Field(default_factory=dict)

    # Opt-in, locally validated final JSON. Unsupported execution paths fail closed.
    output_schema: dict[str, object] | None = None

    @model_validator(mode="after")
    def _validate_output_schema(self) -> "ModelPreset":
        if self.output_schema is not None:
            message = output_schema_error(self.execution, self.provider)
            if message:
                raise ValueError(message)
            from .structured_output import validate_output_schema
            validate_output_schema(self.output_schema)
        return self

    # provider 固有設定。codex / cursor / antigravity の CLI adapter が参照する。
    codex: dict[str, object] = Field(default_factory=dict)
    cursor: dict[str, object] = Field(default_factory=dict)
    antigravity: dict[str, object] = Field(default_factory=dict)

    def validate_execution_environment(self, provider: str) -> None:
        """Validate the new choice against the resolved provider, never its label."""
        message = execution_environment_error(provider, self.execution_environment)
        if message:
            raise ValueError(message)
        message = explicit_sandbox_error(
            provider, self.execution_environment, sandbox=self.codex.get("sandbox"),
            approval_mode=self.codex.get("approval_mode"),
            bypass=self.codex.get("dangerously_bypass_approvals_and_sandbox"),
        )
        if message:
            raise ValueError(message)

    @model_serializer(mode="wrap")
    def _serialize_route_preset(self, handler):
        result = handler(self)
        if self.route is not None:
            for key in (
                "provider",
                "model",
                "anthropic_base_url",
                "anthropic_auth_token_env",
                "anthropic_auth_token_secret",
                "subscription_only",
            ):
                result.pop(key, None)
        return result

    @model_validator(mode="before")
    @classmethod
    def _reject_runtime_ref(cls, value: object) -> object:
        """runtime_ref は route に置き換えた。黙って捨てると、接続先が消えたように見える。"""
        if isinstance(value, dict):
            if "runtime_ref" in value:
                raise ValueError("runtime_ref comes from route")
            if value.get("route") is not None:
                for key in (
                    "provider",
                    "model",
                    "anthropic_base_url",
                    "anthropic_auth_token_env",
                    "anthropic_auth_token_secret",
                ):
                    if key in value:
                        raise ValueError(f"{key} comes from route")
        return value

    @field_validator("on")
    @classmethod
    def _validate_on(cls, value: str) -> str:
        return _validate_no_whitespace(value, label="on")

    @model_validator(mode="after")
    def _validate_execution_axes(self) -> "ModelPreset":
        """place='host' では allow.full=True 以外で作業場アクセスや egress を許さない。"""
        if self.place == "host" and self.allow is not None and not self.allow.full:
            if self.allow.workspace != "none":
                raise ValueError("place='host' cannot set allow.workspace other than 'none' unless allow.full=True")
            if self.allow.egress:
                raise ValueError("place='host' cannot set allow.egress unless allow.full=True")
        return self

    @model_validator(mode="after")
    def _validate_execution_environment(self) -> "ModelPreset":
        # route があるとき、実効 provider は driver から HarnessConfig が決める。
        if self.route is None:
            self.validate_execution_environment(self.provider)
        return self

    def with_execution_environment(self) -> "ModelPreset":
        """Apply host-only defaults to a copy, without rewriting saved presets."""
        self.validate_execution_environment(self.provider)
        default = host_sandbox_default(self.provider, self.execution_environment, self.codex.get("sandbox"))
        if default is None:
            return self
        return self.model_copy(update={"codex": {**self.codex, "sandbox": default}})

    @model_validator(mode="after")
    def _validate_native_execution(self) -> "ModelPreset":
        """Native alias の明示設定を必須とし、direct chat 設定で代用しない。"""
        if self.execution == "native":
            message = native_execution_error(self.provider)
            if message:
                raise ValueError(message)
        if self.execution == "native" and self.native is None:
            raise ValueError("native settings are required when execution='native'")
        return self

    @field_validator("cwd")
    @classmethod
    def expand_cwd(cls, v: str) -> str:
        return os.path.expanduser(v)

    @field_validator("skill_refs", "mcp_refs", "prompt_file_refs", "agent_refs")
    @classmethod
    def _reject_duplicate_profiles(cls, value: list[str]) -> list[str]:
        """同じ参照の二重指定による plugin / skill の重複注入を拒否する。"""
        seen: set[str] = set()
        duplicates: list[str] = []
        for name in value:
            if name in seen and name not in duplicates:
                duplicates.append(name)
            seen.add(name)
        if duplicates:
            raise ValueError(f"参照を重複指定できません: {', '.join(duplicates)}")
        return value

    @field_validator("mcp_servers")
    @classmethod
    def _validate_mcp_servers(cls, v: dict[str, dict]) -> dict[str, dict]:
        """各 MCP server エントリが Claude SDK の McpServerConfig スキーマを満たすことを検証する。

        - type: 'http' / 'sse' なら url が必須
        - type: 'stdio' (または省略) なら command が必須
        - url がある場合は type が 'http' または 'sse' のいずれかでなければならない
          (`url` だけ書いて `type` を忘れる事故が一番多い)
        """
        for name, srv in v.items():
            if not isinstance(srv, dict):
                raise ValueError(f"mcp_servers['{name}'] は dict でなければなりません")
            stype = srv.get("type")
            has_url = "url" in srv
            has_cmd = "command" in srv

            if stype is not None and stype not in ("http", "sse", "stdio"):
                raise ValueError(
                    f"mcp_servers['{name}'].type は 'http' / 'sse' / 'stdio' のいずれかでなければなりません (受領: {stype!r})"
                )
            if has_url and stype not in ("http", "sse"):
                raise ValueError(
                    f"mcp_servers['{name}']: url を指定する場合 type は 'http' または 'sse' を明示してください"
                )
            if stype in ("http", "sse") and not has_url:
                raise ValueError(
                    f"mcp_servers['{name}']: type='{stype}' のときは url が必須です"
                )
            if stype == "stdio" and not has_cmd:
                raise ValueError(
                    f"mcp_servers['{name}']: type='stdio' のときは command が必須です"
                )
            if stype is None and not has_cmd and not has_url:
                # 空の dict (type も command も url も無い) は無効
                raise ValueError(
                    f"mcp_servers['{name}']: command (stdio) または url+type (http/sse) のいずれかが必要です"
                )
            _validate_mcp_mapping(srv.get("env"), scope=f"mcp_servers['{name}']", kind="env")
            _validate_mcp_mapping(srv.get("headers"), scope=f"mcp_servers['{name}']", kind="headers")
        return v

    @field_validator("plugins", mode="before")
    @classmethod
    def _validate_plugins(cls, v: object) -> list[str]:
        """各 plugin パスを expanduser し、存在と最小マニフェストを検証する。

        plugins は明示指定の道具立てなので、欠落は silent ignore せず config
        ロード時に ValueError で即座に気付かせる (system_prompt_files の任意
        ファイルとは扱いを変え、loud に失敗させる)。
        """
        if v is None:
            return []
        if not isinstance(v, list):
            raise ValueError(
                f"plugins は文字列リストで指定してください (受け取った型: {type(v).__name__})"
            )
        resolved: list[str] = []
        for raw in v:
            path = os.path.expanduser(raw)
            p = Path(path)
            if not p.is_dir():
                raise ValueError(
                    f"plugins: ディレクトリが存在しません: {raw} (展開後: {path})"
                )
            if not (p / ".claude-plugin" / "plugin.json").is_file():
                raise ValueError(
                    f"plugins['{raw}']: .claude-plugin/plugin.json が見つかりません "
                    f"(プラグイン束の最小構成が必要)"
                )
            resolved.append(path)
        return resolved

    @model_validator(mode="after")
    def _validate_thinking(self) -> "ModelPreset":
        if self.thinking == "enabled" and self.thinking_budget_tokens is None:
            raise ValueError(
                "thinking='enabled' のときは thinking_budget_tokens を指定してください"
            )
        return self

    @model_validator(mode="after")
    def _validate_cwd_allowed_roots(self) -> "ModelPreset":
        """allow_cwd_override=True のときは cwd_allowed_roots が必須かつ非空。
        各要素も非空（空文字列・空白のみは不可）。

        silent ignore を禁止し、配置ミスを loud に検知する (coding-preset の前例に倣う)。
        理由: worktree 指定予定の preset に roots 指定忘れ時に、任意ディレクトリ指定可能になる
        セキュリティ問題を防ぐため。また、空文字列要素は Path("").resolve() = cwd になり
        許可範囲が黙って広がるため、各要素も必ずチェック。
        """
        if self.allow_cwd_override:
            # リスト全体の非空チェック
            if self.cwd_allowed_roots is None or len(self.cwd_allowed_roots) == 0:
                raise ValueError(
                    "allow_cwd_override=True のときは cwd_allowed_roots に 1 つ以上のパスを指定してください。\n"
                    "例: cwd_allowed_roots: [~/kotoyose-workspace, /tmp/worktrees]"
                )
            # 各要素の非空チェック (空文字列・空白のみはNG)
            for i, root in enumerate(self.cwd_allowed_roots):
                if not root or not root.strip():
                    raise ValueError(
                        f"cwd_allowed_roots[{i}] が空または空白のみです。"
                        f" 各要素は有効なパスでなければなりません。"
                    )
        return self

    @model_validator(mode="after")
    def _validate_isolation(self) -> "ModelPreset":
        """require_isolation preset は bare:false 時に setting_sources: [] を必須とする。

        setting_sources の None/省略は build_options で ['project'] にフォールバックし、
        未信頼 cwd の project スコープ設定 (CLAUDE.md / .claude/agents / project MCP) を
        読み込むため禁止。bare:true 下では --bare が自動検出を無効化するため対象外とする。
        """
        if self.require_isolation and not self.bare and self.setting_sources != []:
            raise ValueError(
                "require_isolation=True の preset は bare=false のとき setting_sources: [] を"
                " 明示してください。None/省略は project スコープ設定を読み込むため禁止です"
            )
        return self

    @model_validator(mode="after")
    def _validate_individual_skill_filter(self) -> "ModelPreset":
        """個別 Skill 選択と全 Skill 無効化の矛盾を保存前に拒否する。"""
        if self.skill_refs and self.skills == []:
            raise ValueError(
                "skill_refs と skills: [] は同時に指定できません。"
                "個別 Skill を使う場合は skills を null または有効な Skill 名リストにしてください"
            )
        return self


class SkillDefinition(BaseModel):
    """Model から個別に選択できる Claude Skill の定義。"""

    description: str = ""
    plugin: str
    skill: str
    # skills 非対応 provider (cursor) へ前置する凝縮版の path。
    # 未指定なら SKILL.md 本文が使われる。SKILL.md は「条件が合ったら読む」体裁で
    # 書かれており frontmatter に trigger 条件を持つため、常時前置に向く短い版を
    # 別に用意したい skill だけがここを設定する。
    fallback_prompt: str | None = None

    @field_validator("fallback_prompt")
    @classmethod
    def _expand_fallback_prompt(cls, value: str | None) -> str | None:
        """PromptFileDefinition.path と同じ扱いに揃える (空白だけは弾く)。"""
        if value is None:
            return None
        if not value.strip():
            raise ValueError("fallback_prompt は空白だけにできません")
        return os.path.expanduser(value)

    def fallback_path(self) -> Path:
        """非対応 provider へ前置する本文の path を返す。"""
        if self.fallback_prompt:
            return Path(self.fallback_prompt).expanduser()
        return Path(self.plugin).expanduser() / "skills" / self.skill / "SKILL.md"

    @field_validator("plugin")
    @classmethod
    def _validate_plugin(cls, value: str) -> str:
        """Skill を含む plugin directory の存在と manifest を検証する。"""
        return ModelPreset._validate_plugins([value])[0]

    @field_validator("skill")
    @classmethod
    def _validate_skill(cls, value: str) -> str:
        """空白だけの Skill 名は実行時に意味を持たないため拒否する。"""
        if not value.strip():
            raise ValueError("skill は空白だけにできません")
        return value


class McpDefinition(BaseModel):
    """Model から個別に選択できる MCP server の定義。"""

    description: str = ""
    server: dict[str, object]

    @field_validator("server")
    @classmethod
    def _validate_server(cls, value: dict[str, object]) -> dict[str, object]:
        """inline MCP と同一のスキーマを適用する。"""
        return ModelPreset(mcp_servers={"resource": value}).mcp_servers["resource"]


class PromptFileDefinition(BaseModel):
    """説明付きで再利用する system prompt file の定義。"""

    description: str = ""
    path: str

    @field_validator("path")
    @classmethod
    def _expand_path(cls, value: str) -> str:
        """保存時に home shorthand を実パスへ正規化する。"""
        if not value.strip():
            raise ValueError("path は空白だけにできません")
        return os.path.expanduser(value)


SANCTION_REASONS = frozenset({
    "unofficial_endpoint", "cross_provider_model", "custom_model_provider",
})


class OwnerAcknowledgement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    reasons: list[str] = Field(min_length=1)
    acknowledged_by: str = Field(min_length=1)
    at: str = Field(min_length=1)
    note: str | None = Field(default=None, exclude_if=lambda value: value is None)

    @field_validator("at", mode="before")
    @classmethod
    def _normalize_at(cls, value: object) -> object:
        """YAML は ISO 時刻を datetime にするので、文字列へ戻して検証する。"""
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        if isinstance(value, str):
            try:
                datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("at は ISO 8601 の日時にしてください") from exc
        return value


class RiskAck(OwnerAcknowledgement):
    route: str = Field(min_length=1)

    @field_validator("reasons")
    @classmethod
    def _known_reasons(cls, value: list[str]) -> list[str]:
        unknown = sorted(set(value) - SANCTION_REASONS)
        if unknown:
            raise ValueError(f"不明な reason: {', '.join(unknown)}")
        return value


class TrainingAck(OwnerAcknowledgement):
    alias: str = Field(min_length=1)
    capabilities: list[TrainingCapability] = Field(min_length=1)


def _unique_provider_ids(values: list[str]) -> list[str]:
    if len(values) != len(set(values)):
        raise ValueError("provider IDs must be unique")
    return values


ProviderOrder = Annotated[
    list[Annotated[str, Field(min_length=1)]],
    Field(max_length=128),
    AfterValidator(_unique_provider_ids),
]


class HarnessSettings(BaseModel):
    """管理 UI から更新できる provider 認証・接続設定。"""

    anthropic_auth_token: str | None = None
    anthropic_base_url: str | None = None

    provider_order: ProviderOrder = Field(default_factory=list, exclude_if=lambda value: not value)

    @model_validator(mode="before")
    @classmethod
    def _reject_replaced_settings_keys(cls, value: object) -> object:
        if isinstance(value, dict) and "cursor_api_key" in value:
            raise ValueError("cursor_api_key was replaced by connections")
        return value

    @field_validator("anthropic_auth_token")
    @classmethod
    def _validate_secret_setting(cls, value: str | None) -> str | None:
        if isinstance(value, str) and value.startswith("secret:"):
            parse_secret_reference(value)
        return value


class PoolAlias(BaseModel):
    """`models` に書ける、候補 alias から 1 つを admission で選ぶ名前。route も agent も持たない。"""

    model_config = ConfigDict(extra="forbid")
    candidates: list[str] = Field(min_length=1)
    policy: str = Field(min_length=1)
    requires: list[str] = Field(default_factory=list, exclude_if=lambda value: not value)

    @field_validator("policy")
    @classmethod
    def _known_policy(cls, value: str) -> str:
        get_strategy(value)
        return value

    @field_validator("requires")
    @classmethod
    def _known_requires(cls, value: list[str]) -> list[str]:
        unknown = sorted(set(value) - KNOWN_REQUIRES)
        if unknown:
            raise ValueError(f"不明な requires: {', '.join(unknown)}")
        return value


CLIENT_NAME_PATTERN = r"^[a-z0-9][a-z0-9-]{0,63}$"
_CLIENT_NAME_RE = re.compile(CLIENT_NAME_PATTERN)
_CLIENT_REF_RE = re.compile(r"^[A-Za-z0-9._:/-]+$")
ClientProvenance = Literal["trusted", "verified_fact", "project", "agent_generated", "external_untrusted", "mixed"]
_DEFAULT_CLIENT_PROVENANCE: tuple[ClientProvenance, ...] = (
    "project", "agent_generated", "external_untrusted", "mixed",
)


class Client(BaseModel):
    """Client policy configured under `clients:` in config.yml."""

    model_config = ConfigDict(extra="forbid")

    models: list[str] = Field(default_factory=list)
    places: list[PlaceName] = Field(default_factory=lambda: ["isolated"])
    max_surface: SurfaceName | None = Field(default=None, exclude_if=lambda value: value is None)
    allow_full: bool = Field(default=False, exclude_if=lambda value: not value)
    workspace_roots: list[Path] = Field(default_factory=list)

    external_mcps: list[str] = Field(default_factory=list, max_length=32, exclude_if=lambda value: not value)
    allowed_env_vars: list[str] = Field(default_factory=list, exclude_if=lambda value: not value)
    allowed_provenance: list[ClientProvenance] = Field(default_factory=lambda: list(_DEFAULT_CLIENT_PROVENANCE))
    max_run_seconds: float = Field(default=300.0, gt=0, allow_inf_nan=False)
    max_memory_bytes: int = Field(default=512 * 1024 * 1024, ge=16 * 1024 * 1024)
    max_processes: int = Field(default=128, ge=1)
    max_cpus: float = Field(default=2.0, gt=0, allow_inf_nan=False)
    max_concurrent_runs: int = Field(default=4, ge=1)

    @field_validator("models")
    @classmethod
    def _validate_models(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in value:
            stripped = item.strip()
            if not stripped:
                raise ValueError("models entries cannot be blank")
            if stripped not in cleaned:
                cleaned.append(stripped)
        return cleaned

    @field_validator("places", mode="before")
    @classmethod
    def _normalize_jail_place(cls, value: object) -> object:
        if isinstance(value, (list, tuple)):
            return ["isolated" if item == "jail" else item for item in value]
        return value

    @field_validator("places")
    @classmethod
    def _validate_places(cls, value: list[PlaceName]) -> list[PlaceName]:
        return list(dict.fromkeys(value))

    @field_validator("workspace_roots")
    @classmethod
    def _validate_workspace_roots(cls, value: list[Path]) -> list[Path]:
        for path in value:
            if not path.is_absolute():
                raise ValueError("workspace_roots entries must be absolute paths")
        return value

    @field_validator("external_mcps")
    @classmethod
    def _validate_external_mcps(cls, value: list[str]) -> list[str]:
        for item in value:
            if not (1 <= len(item) <= 128) or not _CLIENT_REF_RE.fullmatch(item):
                raise ValueError(f"Invalid external MCP reference: {item!r}")
        return list(dict.fromkeys(value))

    @field_validator("allowed_env_vars")
    @classmethod
    def _validate_allowed_env_vars(cls, value: list[str]) -> list[str]:
        for item in value:
            if not item.strip() or any(ch.isspace() for ch in item):
                raise ValueError(f"Invalid environment variable name: {item!r}")
        return list(dict.fromkeys(value))

    @field_validator("allowed_provenance")
    @classmethod
    def _validate_allowed_provenance(cls, value: list[ClientProvenance]) -> list[ClientProvenance]:
        return list(dict.fromkeys(value))

    @property
    def agent_models(self) -> list[str]:
        return self.models

    @property
    def mcp_servers(self) -> list[str]:
        return self.external_mcps

    @property
    def env_keys(self) -> list[str]:
        return self.allowed_env_vars

    @property
    def provenance(self) -> list[ClientProvenance]:
        return self.allowed_provenance

    @property
    def max_seconds(self) -> float:
        return self.max_run_seconds

    @property
    def max_pids(self) -> int:
        return self.max_processes

    @property
    def max_concurrent(self) -> int:
        return self.max_concurrent_runs


class HarnessConfig(BaseModel):
    """Model preset と再利用可能な個別リソースを保持する。"""

    model_config = ConfigDict(extra="forbid")

    credential_leak_guard: bool = Field(default=True, exclude_if=lambda value: value)
    models: dict[str, ModelPreset] = {}
    # models に書いた pool alias。読込時に models から分け、保存時に models へ戻す。
    pools: dict[str, PoolAlias] = Field(default_factory=dict, exclude=True)
    # アカウントと接続先。空なら保存しない。
    connections: dict[str, Connection] = Field(default_factory=dict, exclude_if=lambda value: not value)
    # 空なら保存しない。古い config.yml の dump に routes を足さない。
    routes: dict[str, AgentRoute] = Field(default_factory=dict, exclude_if=lambda value: not value)
    # route を公式の口の外で使う承諾。空なら保存しない。
    risk_acks: list[RiskAck] = Field(default_factory=list, exclude_if=lambda value: not value)
    training_acks: list[TrainingAck] = Field(default_factory=list, exclude_if=lambda value: not value)
    # API clients and their authorization / execution boundaries. Empty is omitted on save.
    clients: dict[str, Client] = Field(default_factory=dict, exclude_if=lambda value: not value)
    skill_definitions: dict[str, SkillDefinition] = Field(default_factory=dict)
    agent_definitions: dict[str, AgentPreset] = Field(default_factory=dict)
    mcps: dict[str, McpDefinition] = Field(default_factory=dict)
    prompt_files: dict[str, PromptFileDefinition] = Field(default_factory=dict)
    settings: HarnessSettings = Field(default_factory=HarnessSettings)
    _compatibility_config: CompatibilityConfig | None = PrivateAttr(default=None)

    @field_validator("clients")
    @classmethod
    def _validate_client_names(cls, value: dict[str, Client]) -> dict[str, Client]:
        for name in value:
            if not _CLIENT_NAME_RE.fullmatch(name):
                raise ValueError(f"Invalid client name '{name}': must match {CLIENT_NAME_PATTERN}")
        return value

    @field_validator("connections", mode="before")
    @classmethod
    def _require_connection_data_policies(cls, connections: object) -> object:
        if isinstance(connections, Mapping):
            for conn_id, connection in connections.items():
                if isinstance(connection, Mapping) and "data_policy" not in connection:
                    raise ValueError(missing_data_policy_message(conn_id))
        return connections

    @model_validator(mode="before")
    @classmethod
    def _reject_replaced_models(cls, value: object) -> object:
        if isinstance(value, dict) and "runtime_models" in value:
            raise ValueError("runtime_models has been replaced by routes")
        models = value.get("models") if isinstance(value, dict) else None
        if isinstance(models, dict):
            pools = {name: item for name, item in models.items()
                     if isinstance(item, PoolAlias) or (isinstance(item, dict) and "candidates" in item)}
            if pools:
                value = {**value, "models": {name: item for name, item in models.items() if name not in pools},
                         "pools": {**(value.get("pools") or {}), **pools}}
        return value

    @model_serializer(mode="wrap")
    def _serialize_pools(self, handler, info):
        """pool alias は YAML では models の中に居る。"""
        data = handler(self)
        if self.pools:
            data["models"] = {**data.get("models", {}),
                              **{name: pool.model_dump(mode=info.mode) for name, pool in self.pools.items()}}
        return data

    @property
    def compatibility_config(self) -> CompatibilityConfig:
        """この設定世代の Native alias を保持する。移行用 JSON は YAML に複製しない。"""
        if self._compatibility_config is None:
            self._compatibility_config = load_compatibility_config(self.models)
        return self._compatibility_config

    def resolve_setting(
        self,
        field: str,
        env_var: str,
        *,
        resolved_secrets: Mapping[str, str] | None = None,
    ) -> str | None:
        """Settings を優先し、未設定時だけ対応する環境変数へフォールバックする。"""
        value = getattr(self.settings, field)
        secret_name = parse_secret_reference(value)
        if secret_name is not None:
            if resolved_secrets is not None and secret_name in resolved_secrets:
                return resolved_secrets[secret_name]
            return resolve_secret_sync(secret_name)
        return value if value else process_env.get(env_var) or None

    def _secret_users_map(self, *, include_claude_subscription: bool = True) -> dict[str, list[str]]:
        """Map each referenced secret name to the config entries referencing it."""
        users: dict[str, set[str]] = {}

        def add_user(secret_name: str, label: str) -> None:
            users.setdefault(secret_name, set()).add(label)

        for conn_name, conn in self.connections.items():
            if not conn.secret_name:
                continue
            if (
                not include_claude_subscription
                and conn.kind == "subscription"
                and conn.provider.lower() in _CLAUDE_SUBSCRIPTION_PROVIDERS
            ):
                continue
            add_user(conn.secret_name, f"connection:{conn_name}")

        for mcp_name, mcp_def in self.mcps.items():
            for secret_name in collect_mcp_secret_names({"mcp": mcp_def.server}):
                add_user(secret_name, f"mcp:{mcp_name}")

        for alias_name, preset in self.models.items():
            for srv_name, srv in preset.mcp_servers.items():
                for secret_name in collect_mcp_secret_names({srv_name: srv}):
                    add_user(secret_name, f"alias:{alias_name}.mcp:{srv_name}")

        for setting_field in SECRET_SETTING_FIELDS:
            secret_name = parse_secret_reference(getattr(self.settings, setting_field, None))
            if secret_name:
                add_user(secret_name, f"settings.{setting_field}")

        return {name: sorted(labels) for name, labels in sorted(users.items())}

    def secret_users(self) -> dict[str, list[str]]:
        """Map each referenced secret name to the config entries referencing it."""
        return self._secret_users_map(include_claude_subscription=True)

    def referenced_secret_names(self) -> set[str]:
        """Return the set of all secret names referenced in this config."""
        return set(self.secret_users())

    def validate_secret_references(
        self,
        known_secrets: Iterable[str] | None = None,
        *,
        store: SecretStore | None = None,
    ) -> None:
        """Ensure non-subscription secret references resolve to an existing secret in `known_secrets`/`store`."""
        users = self._secret_users_map(include_claude_subscription=False)
        if not users:
            return
        if known_secrets is not None:
            known = set(known_secrets)
        elif store is not None:
            if not store.is_configured:
                raise ValueError("secret store not configured")
            known = {name for name in users if store.exists_sync_memory(name)}
        else:
            return
        missing = sorted(name for name in users if name not in known)
        if missing:
            details = ", ".join(f"'{name}' (used by {', '.join(users[name])})" for name in missing)
            raise ValueError(f"Unknown secret reference(s): {details}")

    async def validate_secret_references_async(self, store: SecretStore | None = None) -> None:
        """Async check that non-subscription secret references exist in Postgres or in-memory store."""
        users = self._secret_users_map(include_claude_subscription=False)
        if not users:
            return
        target = store or get_secret_store()
        if not target.is_configured:
            raise ValueError("secret store not configured")
        existing = {row["name"] for row in await target.list()}
        self.validate_secret_references(known_secrets=existing)

    def cursor_connection(self) -> Connection | None:
        """Cursor SDK が使う api_key connection を返す (`cursor` 優先)。"""
        direct = self.connections.get("cursor")
        if direct is not None and direct.provider == "cursor" and direct.kind != "subscription":
            return direct
        for conn in self.connections.values():
            if conn.provider == "cursor" and conn.kind != "subscription":
                return conn
        return None

    def print_cli_profile_for_connection(self, connection_id: str) -> PrintCliProfile | None:
        """Resolve the configured profile or the unique profile of an API-key route."""
        conn = self.connections[connection_id]
        if conn.profile is not None:
            return get_print_cli_profile(conn.profile)
        if conn.kind != "api_key":
            return None
        route_profiles = {
            route.profile for route in self.routes.values()
            if route.connection == connection_id and route.profile is not None
        }
        if len(route_profiles) > 1:
            raise ValueError(f"Connection {connection_id!r} is shared by different print CLI profiles")
        if route_profiles:
            return get_print_cli_profile(route_profiles.pop())
        return None

    @model_validator(mode="after")
    def _validate_profile_references(self) -> "HarnessConfig":
        """全 preset の参照と request header template 契約を起動時に検証する。"""
        for route_name, route in self.routes.items():
            conn = self.connections.get(route.connection)
            if conn is None:
                raise ValueError(f"routes.{route_name}.connection: 不明な connection '{route.connection}' です")
            manifest = driver_manifest(route.driver)
            driver_provider = manifest.provider
            if manifest.unmapped_backend == "print_cli":
                assert route.profile is not None
                cli_profile = get_print_cli_profile(route.profile)
                try:
                    validate_print_cli_connection(cli_profile, conn)
                    if self.print_cli_profile_for_connection(route.connection) != cli_profile:
                        raise ValueError(f"set connections.{route.connection}.profile to '{cli_profile.id}'")
                except ValueError as exc:
                    raise ValueError(f"routes.{route_name}: connection '{route.connection}': {exc}") from exc
            elif conn.provider != driver_provider:
                raise ValueError(
                    f"routes.{route_name}: driver '{route.driver}' と connection '{route.connection}' "
                    f"(provider={conn.provider}) が一致しません"
                )
            if route.subscription_only:
                endpoint = urlsplit(conn.endpoint or "")
                valid_endpoint = (
                    conn.kind == "endpoint"
                    and endpoint.scheme in {"http", "https"}
                    and bool(endpoint.hostname)
                    and endpoint.username is None
                    and endpoint.password is None
                    and not endpoint.query
                    and not endpoint.fragment
                    and bool(conn.secret)
                )
                valid_oauth_subscription = (
                    conn.kind == "subscription"
                    and conn.config_dir is None
                    and bool(conn.secret)
                )
                if driver_provider != "claude" or not (valid_endpoint or valid_oauth_subscription):
                    raise ValueError(
                        "subscription_only requires an explicit Claude endpoint or OAuth subscription connection and credential source"
                    )
        for ack in self.risk_acks:
            if ack.route not in self.routes:
                raise ValueError(f"risk_acks: 不明な route '{ack.route}' です")
        ack_aliases = [ack.alias for ack in self.training_acks]
        if len(set(ack_aliases)) != len(ack_aliases):
            raise ValueError("training_acks must contain at most one acknowledgement per alias")
        for pool_name, pool in self.pools.items():
            self._validate_pool(pool_name, pool)
        for conn_id, conn in self.connections.items():
            if conn.quota_from is not None:
                if conn.quota_from == conn_id:
                    raise ValueError(f"connections.{conn_id}.quota_from cannot reference itself")
                target = self.connections.get(conn.quota_from)
                if target is None:
                    raise ValueError(
                        f"connections.{conn_id}.quota_from references unknown connection '{conn.quota_from}'"
                    )
                if target.quota_from is not None:
                    raise ValueError(
                        f"connections.{conn_id}.quota_from cannot chain through '{conn.quota_from}'"
                    )

        for preset_name, preset in self.models.items():
            if preset.route is not None and preset.route not in self.routes:
                raise ValueError(f"{preset_name}.route: 不明な route '{preset.route}' です")
            if preset.agent is not None and preset.route is None:
                raise ValueError(f"{preset_name}: agent には route が必要です")
            route_provider = None
            if preset.route is not None:
                manifest = driver_manifest(self.routes[preset.route].driver)
                route_provider = manifest.provider
                route = self.routes[preset.route]
                conn = self.connections[route.connection]
                if manifest.unmapped_backend == "print_cli":
                    placement = ("jail" if preset.place == "jail" or preset.execution_environment == "isolated"
                                 else preset.agent.place if preset.agent is not None else "host")
                    try:
                        validate_print_cli_connection(get_print_cli_profile(route.profile), conn, placement=placement)
                    except ValueError as exc:
                        raise ValueError(f"{preset_name}: {exc}") from exc
                if preset.agent is not None and preset.agent.place not in manifest.places:
                    raise ValueError(
                        f"{preset_name}.agent.place={preset.agent.place!r} は {manifest.driver} では使えません"
                    )
            provider = route_provider if route_provider is not None else preset.provider
            if preset.execution == "native" and provider != "claude":
                raise ValueError("execution='native' is supported only for claude aliases")
            preset.validate_execution_environment(provider)
            sub_pair = _find_claude_subscription_connection(self, preset) if provider == "claude" else None
            if sub_pair is not None and sub_pair[1].config_dir is not None:
                if (
                    preset.execution == "native"
                    or preset.execution_environment == "isolated"
                    or (preset.agent is not None and preset.agent.place == "isolated")
                    or preset.place == "jail"
                ):
                    raise ValueError(
                        f"{preset_name}: config_dir connection '{sub_pair[0]}' supports host placement only; "
                        "isolated/jail routes are not supported"
                    )
            for resource_name in preset.skill_refs:
                if resource_name not in self.skill_definitions:
                    raise ValueError(f"{preset_name}.skill_refs: 不明な Skill '{resource_name}' です")
            for resource_name in preset.agent_refs:
                if resource_name not in self.agent_definitions:
                    raise ValueError(f"{preset_name}.agent_refs: 不明な agent '{resource_name}' です")
            for resource_name in preset.mcp_refs:
                if resource_name not in self.mcps:
                    raise ValueError(f"{preset_name}.mcp_refs: 不明な MCP '{resource_name}' です")
            for resource_name in preset.prompt_file_refs:
                if resource_name not in self.prompt_files:
                    raise ValueError(f"{preset_name}.prompt_file_refs: 不明な prompt file '{resource_name}' です")

            # 個別リソース / inline の優先順位を反映した後の MCP 設定を検査する。
            # 実行時にはここで見つかる template だけが request header を参照する。
            _, _, mcp_servers = self.resolve_resources(preset)
            decision = alias_data_policy(self, preset_name)
            if message := decision.refusal(configured_capabilities(preset)):
                raise ValueError(message)
            declared_headers = {
                header_name.lower() for header_name in preset.required_request_headers
            }
            for server in mcp_servers.values():
                headers = server.get("headers")
                if not isinstance(headers, dict):
                    continue
                for value in headers.values():
                    if not isinstance(value, str):
                        continue
                    for header_name in _MCP_REQUEST_HEADER_TEMPLATE.findall(value):
                        if header_name.lower() not in declared_headers:
                            raise ValueError(
                                f"{preset_name}: MCP request header template "
                                f"'{{{{header:{header_name}}}}}' requires "
                                f"required_request_headers entry '{header_name}'"
                            )
        return self

    def _validate_pool(self, pool_name: str, pool: PoolAlias) -> None:
        """候補は入れ子にならない通常の alias で、同じ place と surface を宣言する。"""
        shapes = set()
        for candidate in pool.candidates:
            if candidate in self.pools:
                raise ValueError(f"{pool_name}: 候補 '{candidate}' は pool alias です (入れ子にできません)")
            preset = self.models.get(candidate)
            if preset is None:
                raise ValueError(f"{pool_name}: 不明な候補 '{candidate}' です")
            if preset.agent is None:
                raise ValueError(f"{pool_name}: 候補 '{candidate}' に agent がありません")
            shapes.add((preset.agent.place, preset.agent.surface))
        if len(shapes) > 1:
            raise ValueError(f"{pool_name}: 候補の agent.place と agent.surface は同じでなければなりません")

    def _apply_route(self, preset: ModelPreset) -> ModelPreset:
        """route があるとき、provider と model と Claude の口は route が勝つ。"""
        if preset.route is None:
            return preset
        route = self.routes.get(preset.route)
        if route is None:
            raise ValueError(f"不明な route '{preset.route}' です")
        conn = self.connections.get(route.connection)
        if conn is None:
            raise ValueError(f"不明な connection '{route.connection}' です")
        provider = driver_manifest(route.driver).provider
        codex = dict(preset.codex)
        if route.model_provider is not None and "model_provider" not in codex:
            codex["model_provider"] = route.model_provider
        update: dict[str, object] = {
            "provider": provider, "model": route.model, "codex": codex,
            "subscription_only": route.subscription_only,
        }
        if provider == "claude":
            update["anthropic_base_url"] = conn.endpoint if conn.kind == "endpoint" else None
            if conn.kind == "subscription":
                update["anthropic_auth_token_env"] = None
                update["anthropic_auth_token_secret"] = None
            else:
                update["anthropic_auth_token_env"] = conn.env_name
                update["anthropic_auth_token_secret"] = conn.secret_name
        return preset.model_copy(update=update)

    def preset_for(self, model: str, *, resolve_execution_environment: bool = True) -> ModelPreset:
        """モデル名に対応する preset と route を解決する。"""
        resolved = self._apply_route(self.models[model])
        resolved._validate_output_schema()
        decision = alias_data_policy(self, model)
        if decision.restricted and resolved.provider == "claude":
            update = {"strict_mcp_config": True}
            explicit_tools = resolved.tools is not None or (resolved.allow is not None and resolved.allow.full)
            if "tools" not in decision.unlocked or not explicit_tools:
                update["tools"] = []
            if "workspace" not in decision.unlocked or "tools" not in decision.unlocked:
                update["bare"] = True
            if "workspace" not in decision.unlocked:
                update["cwd"] = os.path.abspath(os.sep)
            if resolved.setting_sources is None or "workspace" not in decision.unlocked:
                update["setting_sources"] = []
            resolved = resolved.model_copy(update=update)
        return resolved.with_execution_environment() if resolve_execution_environment else resolved

    def resolve_resources(
        self, preset: ModelPreset
    ) -> tuple[list[str], list[str] | Literal["all"] | None, dict[str, dict]]:
        """個別リソース参照 (skill_refs/mcp_refs) を記載順で合成し、最後に inline 設定を適用する。"""
        plugins: list[str] = []
        skills: list[str] | Literal["all"] | None = None
        mcp_servers: dict[str, dict] = {}

        for resource_name in preset.skill_refs:
            resource = self.skill_definitions[resource_name]
            plugins.append(resource.plugin)
            if skills != "all":
                skills = [*(skills or []), resource.skill]
        for resource_name in preset.mcp_refs:
            mcp_servers[resource_name] = dict(self.mcps[resource_name].server)

        plugins.extend(preset.plugins)
        if preset.skills == "all" or preset.skills == []:
            skills = preset.skills
        elif preset.skills is not None:
            skills = [*(skills if isinstance(skills, list) else []), *preset.skills]
        mcp_servers.update(preset.mcp_servers)
        return plugins, skills, mcp_servers

    def resolve_prompt_files(self, preset: ModelPreset) -> list[str]:
        """個別 prompt file を選択順で解決する。"""
        return [self.prompt_files[name].path for name in preset.prompt_file_refs]

    def resolve_agents(self, preset: ModelPreset) -> dict[str, AgentPreset]:
        """agent_refs を記載順で合成し、最後に inline agents を適用する。

        参照名がそのまま subagent 名になる (mcp_refs と同じ流儀)。同名の inline 定義が
        あれば inline を優先し、位置は最初に現れた順を保つ。
        """
        agents = {name: self.agent_definitions[name] for name in preset.agent_refs}
        agents.update(preset.agents)
        return agents


def _find_claude_subscription_connection(
    cfg: HarnessConfig,
    preset: ModelPreset | None = None,
) -> tuple[str, Connection] | None:
    """Resolve the active Claude subscription connection ID and Connection, if any."""
    if preset is not None and preset.route:
        route = cfg.routes.get(preset.route)
        if route is None:
            return None
        conn_id = route.connection
        conn = cfg.connections.get(conn_id)
        if conn is not None and conn.kind == "subscription" and conn.provider.lower() in _CLAUDE_SUBSCRIPTION_PROVIDERS:
            return conn_id, conn
        return None
    direct = cfg.connections.get("claude")
    if (
        direct is not None
        and direct.kind == "subscription"
        and direct.provider.lower() in _CLAUDE_SUBSCRIPTION_PROVIDERS
    ):
        return "claude", direct
    for conn_id, conn in cfg.connections.items():
        if conn.kind == "subscription" and conn.provider.lower() in _CLAUDE_SUBSCRIPTION_PROVIDERS:
            return conn_id, conn
    return None


def _load_yaml_config() -> HarnessConfig:
    """KOTOYOSE_CONFIG 環境変数 (デフォルト: ./config.yml または XDG config) からロード。"""
    from .config_paths import resolve_config_file_path

    path = resolve_config_file_path()
    if os.path.exists(path):
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        cfg = HarnessConfig.model_validate(data)
    else:
        cfg = HarnessConfig()
    # 起動・reload 時点で合成を検証し、名前衝突を初回リクエストまで持ち越さない。
    cfg.compatibility_config
    store = get_secret_store()
    if store.is_configured and store._pool is None:
        cfg.validate_secret_references(store=store)
    return cfg


_config = _load_yaml_config()


def get_config() -> HarnessConfig:
    """現在の HarnessConfig を返す。"""
    return _config


def get_clients(config: HarnessConfig | None = None) -> dict[str, Client]:
    """Return the configured Clients mapping from HarnessConfig (single source of truth)."""
    cfg = get_config() if config is None else config
    return dict(getattr(cfg, "clients", None) or {})


def get_client(name: str, config: HarnessConfig | None = None) -> Client | None:
    """Return a configured Client by name, or None."""
    return get_clients(config).get(name)


def reload_config() -> HarnessConfig:
    """config.yml を再読み込みして _config を更新する。"""
    global _config
    _config = _load_yaml_config()
    return _config


def save_config(cfg: HarnessConfig) -> None:
    """HarnessConfig を config.yml に書き込み、in-memory を同期する。

    admin API 経由の更新 (model_copy + dict 代入) は ModelPreset 単体しか検証せず、
    渡された cfg が不変条件を破っている可能性がある。書き込み前にここで全体を
    model_validate し直し、不正な設定の永続化 (次回リロードでの起動失敗) を防ぐ関所とする。
    """
    global _config
    validated = HarnessConfig.model_validate(cfg.model_dump())
    validated.compatibility_config
    store = get_secret_store()
    if store.is_configured and store._pool is None:
        validated.validate_secret_references(store=store)
    from .config_paths import resolve_config_file_path

    path = resolve_config_file_path()
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(validated.model_dump(mode="json"), f, allow_unicode=True, default_flow_style=False)
    _config = validated


def resolve_prompt_texts(files: list[str], *, strict_limits: tuple[int, int] | None = None) -> list[str]:
    """prompt file 群の本文を読み込む。

    provider 非依存 (Claude の system prompt append / Codex の developer_instructions /
    Cursor・Antigravity の初回ターン前置で共用する)。存在しないファイルは黙ってスキップする
    (_build_system_prompt と同じ寛容さ — system_prompt_files 時代からの既存挙動を維持)。
    UTF-8 でなければ ValueError で loud に失敗させる。
    """
    # Native admission uses bounded, fail-closed reads. The existing host loader
    # remains permissive unless this mode is explicitly requested.
    if strict_limits is not None:
        per_file, total_limit = strict_limits
        texts, total = [], 0
        try:
            for path in files:
                descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
                with os.fdopen(descriptor, "rb") as stream:
                    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                        raise ValueError("not a regular file")
                    limit = min(per_file, total_limit - total)
                    content = stream.read(limit + 1)
                if len(content) > limit or b"\x00" in content:
                    raise ValueError("invalid prompt size or content")
                texts.append(content.decode("utf-8"))
                total += len(content)
        except (OSError, ValueError, TypeError) as exc:
            # Paths and decoder excerpts are private configuration, not errors
            # suitable for a public admission response or event.
            raise ValueError("Preset prompt files are unavailable or invalid") from exc
        return texts
    texts: list[str] = []
    for path in files:
        p = Path(path)
        if p.exists():
            try:
                texts.append(p.read_text(encoding="utf-8"))
            except UnicodeDecodeError as e:
                raise ValueError(
                    f"prompt file '{p}' は UTF-8 でエンコードされていません: {e}"
                ) from e
    return texts


def strip_frontmatter(text: str) -> str:
    """先頭の YAML frontmatter を落とす。

    SKILL.md の frontmatter は description に "Triggers: ..." 等の読み込み条件を
    書く場所で、常時前置する本文としては誤解を招く (条件付き = 任意、と読まれうる)。
    区切りが閉じていなければ本文とみなしてそのまま返す。
    """
    if not text.startswith("---"):
        return text
    lines = text.splitlines(keepends=True)
    for index in range(1, len(lines)):
        if lines[index].rstrip() == "---":
            return "".join(lines[index + 1 :]).lstrip("\n")
    return text


def resolve_skill_fallback_texts(
    definitions: dict[str, SkillDefinition], skill_refs: list[str]
) -> list[tuple[str, str]]:
    """skills 非対応 provider へ前置する (skill 名, 本文) を記載順で返す。

    存在しない path は黙ってスキップする — prompt_file_refs と同じ寛容さに揃える。
    未解決ぶんは呼び出し側が warning へ出すため、不可視にはならない。
    ただし UTF-8 で読めない file は resolve_prompt_texts と同様 loud に落とす
    (path の綴り誤りと違い、encoding の誤りは黙って劣化させる価値が無い)。
    """
    resolved: list[tuple[str, str]] = []
    for name in skill_refs:
        definition = definitions.get(name)
        if definition is None:
            continue
        path = definition.fallback_path()
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as e:
            raise ValueError(
                f"skill '{name}' の fallback '{path}' は UTF-8 でエンコードされていません: {e}"
            ) from e
        except OSError:
            continue
        text = strip_frontmatter(text).strip()
        if text:
            resolved.append((name, text))
    return resolved


_ACTIVE_OAUTH_CONTEXT: ContextVar[
    tuple[HarnessConfig, ModelPreset | None, Mapping[str, str] | None] | None
] = ContextVar("_ACTIVE_OAUTH_CONTEXT", default=None)


class ClaudeSubscriptionAuthError(SecretStoreError):
    """Claude subscription の token (`claude setup-token`) が未設定または不在のときのエラー。"""


def _missing_claude_subscription_message(conn_id: str, conn: Connection | None) -> str:
    if conn is not None and conn.env_name:
        return (
            f"Claude subscription token env var '{conn.env_name}' for connection '{conn_id}' is not set. "
            f"Run `claude setup-token` in a terminal and set '{conn.env_name}' or save it in the Kotoyose secret store."
        )
    if conn is not None and conn.secret_name:
        return (
            f"Claude subscription secret '{conn.secret_name}' for connection '{conn_id}' is missing. "
            f"Run `claude setup-token` in a terminal and save it under secret name '{conn.secret_name}'."
        )
    return (
        f"Claude subscription connection '{conn_id}' has no secret configured. "
        "Run `claude setup-token` in a terminal and save the token in the Kotoyose secret store."
    )


def resolve_claude_subscription_token_sync(
    cfg: HarnessConfig,
    preset: ModelPreset | None = None,
    *,
    resolved_secrets: Mapping[str, str] | None = None,
    require_configured: bool = False,
) -> str | None:
    """Resolve `CLAUDE_CODE_OAUTH_TOKEN` from the active Claude subscription connection's secret."""
    pair = _find_claude_subscription_connection(cfg, preset)
    if pair is None:
        if require_configured:
            raise ClaudeSubscriptionAuthError(
                "No Claude subscription secret is configured. "
                "Run `claude setup-token` in a terminal and save it in the Kotoyose secret store."
            )
        return None
    conn_id, conn = pair
    if conn.config_dir is not None:
        if require_configured:
            raise ClaudeSubscriptionAuthError(
                f"Claude subscription connection '{conn_id}' uses config_dir '{conn.config_dir}', "
                "not a setup-token secret."
            )
        return None
    if conn.env_name:
        token = process_env.get(conn.env_name, "").strip()
        if not token:
            raise ClaudeSubscriptionAuthError(_missing_claude_subscription_message(conn_id, conn))
        return token
    if not conn.secret_name:
        raise ClaudeSubscriptionAuthError(_missing_claude_subscription_message(conn_id, conn))
    if resolved_secrets is not None and conn.secret_name in resolved_secrets:
        token = resolved_secrets[conn.secret_name].strip()
    else:
        try:
            token = resolve_secret_sync(conn.secret_name).strip()
        except SecretStoreError as exc:
            raise ClaudeSubscriptionAuthError(_missing_claude_subscription_message(conn_id, conn)) from exc
    if not token:
        raise ClaudeSubscriptionAuthError(_missing_claude_subscription_message(conn_id, conn))
    return token


async def resolve_claude_subscription_token_async(
    cfg: HarnessConfig,
    preset: ModelPreset | None = None,
    *,
    require_configured: bool = False,
) -> str | None:
    """Async counterpart to `resolve_claude_subscription_token_sync`."""
    from .secret_store import resolve_secret_async

    pair = _find_claude_subscription_connection(cfg, preset)
    if pair is None:
        if require_configured:
            raise ClaudeSubscriptionAuthError(
                "No Claude subscription secret is configured. "
                "Run `claude setup-token` in a terminal and save it in the Kotoyose secret store."
            )
        return None
    conn_id, conn = pair
    if conn.config_dir is not None:
        if require_configured:
            raise ClaudeSubscriptionAuthError(
                f"Claude subscription connection '{conn_id}' uses config_dir '{conn.config_dir}', "
                "not a setup-token secret."
            )
        return None
    if conn.env_name:
        token = process_env.get(conn.env_name, "").strip()
        if not token:
            raise ClaudeSubscriptionAuthError(_missing_claude_subscription_message(conn_id, conn))
        return token
    if not conn.secret_name:
        raise ClaudeSubscriptionAuthError(_missing_claude_subscription_message(conn_id, conn))
    try:
        token = (await resolve_secret_async(conn.secret_name)).strip()
    except SecretStoreError as exc:
        raise ClaudeSubscriptionAuthError(_missing_claude_subscription_message(conn_id, conn)) from exc
    if not token:
        raise ClaudeSubscriptionAuthError(_missing_claude_subscription_message(conn_id, conn))
    return token


def _default_get_oauth_token() -> str | None:
    """Resolve the active Claude subscription connection's OAuth secret synchronously."""
    ctx = _ACTIVE_OAUTH_CONTEXT.get()
    if ctx is not None:
        cfg, preset, resolved_secrets = ctx
    else:
        cfg, preset, resolved_secrets = _config, None, None
    return resolve_claude_subscription_token_sync(
        cfg,
        preset,
        resolved_secrets=resolved_secrets,
    )


_get_oauth_token = _default_get_oauth_token


def _build_auth_env(
    preset: ModelPreset | None = None,
    *,
    resolved_secrets: Mapping[str, str] | None = None,
    config: HarnessConfig | None = None,
) -> dict[str, str]:
    """Claude 認証 adapter の既存 import / patch path を維持する facade。

    `_config` と `_get_oauth_token` を呼出時に渡すため、reload と test patch が
    新 adapter module の内部状態に遮られない。
    """
    from .runtime.claude_auth import build_auth_env

    active = _config if config is None else config
    token_ctx = _ACTIVE_OAUTH_CONTEXT.set((active, preset, resolved_secrets))
    try:
        return build_auth_env(
            preset,
            active,
            _get_oauth_token,
            resolved_secrets=resolved_secrets,
        )
    finally:
        _ACTIVE_OAUTH_CONTEXT.reset(token_ctx)


def build_options(
    model: str = "kotoyose-chat",
    cwd_override: str | None = None,
    request_headers: dict[str, str] | None = None,
    resolved_secrets: Mapping[str, str] | None = None,
    *, config: HarnessConfig | None = None,
) -> ClaudeAgentOptions:
    """モデル名に対応する ClaudeAgentOptions を返す。

    Args:
        model: preset キー (例: "kotoyose-chat", "coding")。
        cwd_override: request body から渡された cwd。preset の allow_cwd_override=True の
            場合のみ有効。False の preset に渡すと ValueError (呼び出し元の検証漏れを loud
            に失敗させる belt-and-suspenders。silent ignore 禁止)。
    """
    from .runtime.claude_options import build_claude_options
    from .session_store import get_default_store

    active = _config if config is None else config
    p = active.preset_for(model)
    if p.provider != "claude":
        raise ValueError(
            f"preset '{model}' は provider='{p.provider}' のため ClaudeAgentOptions を生成できません"
        )

    return build_claude_options(
        active,
        p,
        model_name=model,
        cwd_override=cwd_override,
        request_headers=request_headers,
        env=_build_auth_env(p, resolved_secrets=resolved_secrets,
                            **({"config": active} if config is not None else {})),
        session_store=get_default_store(),
        resolved_secrets=resolved_secrets,
    )


def resolve_mcp_secrets(
    mcp_servers: dict[str, dict],
    *,
    resolved_secrets: Mapping[str, str] | None = None,
    secret_resolver: Callable[[str], str] | None = None,
) -> dict[str, dict]:
    """Resolve `{'secret': 'NAME'}` entries inside MCP `env` and `headers` at launch time."""
    import copy

    resolved = copy.deepcopy(mcp_servers)
    lookup = secret_resolver or resolve_secret_sync

    def resolve_value(value: object) -> object:
        secret_name = parse_mcp_secret_value(value)
        if secret_name is None:
            return value
        if resolved_secrets is not None and secret_name in resolved_secrets:
            return resolved_secrets[secret_name]
        return lookup(secret_name)

    for server in resolved.values():
        if not isinstance(server, dict):
            continue
        for section in ("env", "headers"):
            mapping = server.get(section)
            if isinstance(mapping, dict):
                server[section] = {key: resolve_value(val) for key, val in mapping.items()}
    return resolved


async def resolve_mcp_secrets_async(
    mcp_servers: dict[str, dict],
    store: SecretStore | None = None,
) -> dict[str, dict]:
    """Asynchronously resolve `{'secret': 'NAME'}` entries inside MCP `env` and `headers`."""
    names = collect_mcp_secret_names(mcp_servers)
    if not names:
        return resolve_mcp_secrets(mcp_servers, resolved_secrets={})
    target = store or get_secret_store()
    fetched = {name: await target.get(name) for name in sorted(names)}
    return resolve_mcp_secrets(mcp_servers, resolved_secrets=fetched)


def collect_preset_secret_names(cfg: object, preset: ModelPreset) -> set[str]:
    """Collect all secret names required to launch `preset`."""
    names: set[str] = set()
    if getattr(preset, "anthropic_auth_token_secret", None):
        names.add(preset.anthropic_auth_token_secret)
    if isinstance(cfg, HarnessConfig):
        for field in SECRET_SETTING_FIELDS:
            secret_name = parse_secret_reference(getattr(cfg.settings, field, None))
            if secret_name:
                names.add(secret_name)
        _, _, mcp_servers = cfg.resolve_resources(preset)
        names.update(collect_mcp_secret_names(mcp_servers))
        if preset.route and preset.route in cfg.routes:
            conn = cfg.connections.get(cfg.routes[preset.route].connection)
            if conn is not None and conn.secret_name:
                names.add(conn.secret_name)
        resolved_provider = preset.provider
        if preset.route and preset.route in cfg.routes:
            resolved_provider = driver_manifest(cfg.routes[preset.route].driver).provider
        if resolved_provider == "cursor" and not (preset.route and preset.route in cfg.routes):
            cursor_conn = cfg.cursor_connection()
            if cursor_conn is not None and cursor_conn.secret_name:
                names.add(cursor_conn.secret_name)
    elif isinstance(getattr(preset, "mcp_servers", None), Mapping):
        names.update(collect_mcp_secret_names(preset.mcp_servers))
    return names


async def resolve_preset_secrets_with_versions_async(
    cfg: HarnessConfig,
    preset: ModelPreset,
    store: SecretStore | None = None,
) -> tuple[dict[str, str], dict[str, int]]:
    """Asynchronously resolve all secret values and versions required to launch `preset`."""
    names = collect_preset_secret_names(cfg, preset)
    if not names:
        return {}, {}
    pair = _find_claude_subscription_connection(cfg, preset)
    claude_sub_secret = pair[1].secret_name if pair is not None else None
    env_oauth_override = bool(process_env.get("CLAUDE_CODE_OAUTH_TOKEN"))
    target = store or get_secret_store()
    values: dict[str, str] = {}
    versions: dict[str, int] = {}
    for name in sorted(names):
        try:
            value, version = await target.get_with_version(name)
        except SecretStoreError as exc:
            if pair is not None and name == claude_sub_secret:
                if env_oauth_override:
                    other_preset = preset.model_copy(update={"route": None})
                    if name not in collect_preset_secret_names(cfg, other_preset):
                        continue
                raise ClaudeSubscriptionAuthError(
                    _missing_claude_subscription_message(pair[0], pair[1])
                ) from exc
            raise
        values[name] = value
        versions[name] = version
    return values, versions


async def resolve_preset_secrets_async(
    cfg: HarnessConfig,
    preset: ModelPreset,
    store: SecretStore | None = None,
) -> dict[str, str]:
    """Asynchronously resolve all secret references required to launch `preset`."""
    values, _ = await resolve_preset_secrets_with_versions_async(cfg, preset, store=store)
    return values


def _inject_mcp_request_headers(
    mcp_servers: dict[str, dict], request_headers: dict[str, str],
) -> dict[str, dict]:
    """MCP 設定中の request header テンプレートを接続時にだけ解決する。

    `{{header:x-pm-mcp-token}}` のような完全一致テンプレートを HTTP request header
    の値で置換する。認証情報を YAML やログへ残さないため、ここでのみ扱う。
    """
    import copy
    resolved = copy.deepcopy(mcp_servers)
    normalized_headers = {key.lower(): value for key, value in request_headers.items()}

    def replace(value: object) -> object:
        if not isinstance(value, str):
            return value

        def lookup(match: re.Match[str]) -> str:
            header_name = match.group(1).lower()
            header_value = normalized_headers.get(header_name)
            if not header_value:
                raise ValueError(f"必須 request header がありません: {match.group(1)}")
            return header_value

        return _MCP_REQUEST_HEADER_TEMPLATE.sub(lookup, value)

    for server in resolved.values():
        headers = server.get("headers")
        if isinstance(headers, dict):
            server["headers"] = {key: replace(value) for key, value in headers.items()}
    return resolved
