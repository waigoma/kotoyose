"""組み込み driver の実行契約を宣言する。

判断は行の参照で行う。provider 名の分岐をここに増やさない。
Claude の read と exec は表に置くが admitted ではない。Codex の read は閲覧部屋。
R17 は Hub 側のまま。Hub が読む値は published_agent_execution が今まで通り出す。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .registry import registry
from .codex_coding_policy import (
    EXTERNAL_SANDBOX_POLICY, EXTERNAL_TOOL_POLICY_DIGEST, READ_SANDBOX_POLICY, READ_TOOL_POLICY_DIGEST,
    TOOL_POLICY_DIGEST,
)

SANDBOX_EXEC = "builtin:sandbox-exec@1"
HOST_CODING = "builtin:host-coding@1"
WORKSPACE_READ = "builtin:workspace-read@1"
JAIL_MODE = "sandbox_exec_v1"
CODING_MODE = "host_native_coding_v1"
READ_MODE = "room_read_v1"

# 場所は surface から一意。read/write は部屋、none/exec は jail。host は surface を強制しない。
_RUNTIME = {"none": "jail", "exec": "jail", "read": "room", "write": "room"}

_SANDBOX_VALUES = "codex.sandbox requires read-only, workspace-write, or danger-full-access"
_EXPLICIT_BYPASS = (
    "Explicit execution_environment does not support dangerously_bypass_approvals_and_sandbox; use codex.sandbox"
)
_NATIVE_BYPASS = "Native Codex does not support dangerously_bypass_approvals_and_sandbox; use codex.sandbox"
_HOST_APPROVAL = "Explicit host execution requires codex.approval_mode=deny_all; auto_review can bypass codex.sandbox"
_OUTPUT_SCHEMA = "output_schema is supported only by direct Claude/Codex presets"
_NATIVE_EXECUTION = "execution='native' is supported only for claude aliases"
_CLAUDE_TOOLS = "Claude coding requires the admitted container workspace tool"
_CODEX_TOOLS = "Codex mode requires its explicitly admitted workspace tool"
_ENVIRONMENT_VALUES = "execution_environment requires isolated or host"
_SAVED_SANDBOX = (
    "codex.sandbox requires read-only, workspace-write, or danger-full-access; "
    "externalSandbox is a separate app-server policy and is not supported by this runtime"
)
_JAIL_SANDBOX = (
    "sandbox_exec_v1 requires read-only provider sandbox; "
    "select an explicitly admitted host-coding profile for other codex.sandbox modes"
)
_NATIVE_CALLER = "Native caller isolation requires an explicitly configured native worker"

SurfaceName = Literal["none", "read", "exec", "write"]
PlaceName = Literal["host", "isolated"]
DriverName = Literal[
    "claude.agent-sdk", "codex.app-server", "cursor.sdk", "antigravity.sdk",
    "cli.print", "cli.acp",
]
# alias の surface は上限。要求の道具はこの順で下に収まる。
SURFACE_RANK = {"none": 0, "read": 1, "exec": 2, "write": 3}


@dataclass(frozen=True, slots=True)
class SurfaceCell:
    """isolated の 1 surface。driver が使う道具と、動かすための条件。"""

    surface: SurfaceName
    admitted: bool
    tool_refs: frozenset[str] = frozenset()
    tool_match: Literal["exact", "subset", "without"] = "exact"
    allow_mcp: bool = False
    allow_approval: bool = False
    # 受付で欠けると断る。launch だけで見る条件は requires に置く。
    admission_requires: frozenset[str] = frozenset()
    requires: frozenset[str] = frozenset()
    credential_modes: frozenset[str] = frozenset()
    execution_mode: str | None = None
    accepted_tools: str = ""
    rejection: str | None = None
    rootfs_digest: Literal["standard", "coding"] | None = None
    store_sdk_sandbox: bool = False
    sdk_sandbox_default: str | None = None
    sdk_sandbox_fixed: str | None = None
    sandbox_rejection: str | None = None
    # isolated では SDK sandbox は働かず、容器が境界になる。
    external_sandbox: bool = False
    # 部屋の /workspace。read だけ ro。home は容器側の別 mount で、ここには書かない。
    workspace_readonly: bool = False
    # coding は Hub が用意した .codex/.agents を要求する。閲覧 clone は無くてよい。
    protected_entries_required: bool = False
    sandbox_policy: str | None = None
    isolated_tool_policy_digest: str | None = None
    host_tool_policy_digest: str | None = None


@dataclass(frozen=True, slots=True)
class HostCell:
    """host は surface を強制しない。道具の上限と driver 設定だけを宣言する。"""

    tool_refs: frozenset[str]
    tool_match: Literal["subset"]
    allow_mcp: bool
    allow_approval: bool
    approval_mode: str | None
    default_sandbox: str | None
    credential_mode: str
    accepted_tools: str
    surface_enforced: bool


@dataclass(frozen=True, slots=True)
class SdkSandboxPolicy:
    """Codex の SDK sandbox。isolated では外枠が効果を持つので別名だけ残す。"""

    values: frozenset[str]
    host_approval_mode: str
    host_default: str
    inactive_alias: tuple[tuple[str, str], ...]
    bypass_forbidden: bool
    invalid_saved_message: str


@dataclass(frozen=True, slots=True)
class MappingRule:
    """審査済み profile への写像。A2 で外すまで、今の拒否をここに置く。"""

    profile_backend: str
    profile_provider: str
    model_must_match: bool


@dataclass(frozen=True, slots=True)
class IsolatedExplicit:
    """alias が execution_environment=isolated と書いたときの追加条件。"""

    requires_mapping: bool
    execution_mode: str
    credential_mode: str


@dataclass(frozen=True, slots=True)
class DriverManifest:
    """組み込み driver の実行契約。"""

    driver: str
    provider: str
    official_interfaces: tuple[str, ...]
    places: frozenset[PlaceName]
    # config の execution_environment に書いてよい値。空はフィールド自体を拒否する。
    explicit_environments: frozenset[str]
    surfaces: tuple[SurfaceCell, ...]
    host: HostCell | None
    credential_modes: frozenset[str]
    unmapped_backend: Literal["native", "print_cli"] | None
    allows_execution_native: bool
    allows_direct_output_schema: bool
    # request: 要求の道具が surface を選ぶ。profile: 審査済み mode が選ぶ。
    surface_selector: Literal["request", "profile", "none"]
    mapping: MappingRule | None = None
    isolated_explicit: IsolatedExplicit | None = None
    sdk_sandbox: SdkSandboxPolicy | None = None
    # (mode, credential)。無い driver が coding mode を名乗ることは拒否する。
    coding_profile: tuple[str, frozenset[str]] | None = None


def _closed(surface: SurfaceName) -> SurfaceCell:
    return SurfaceCell(surface=surface, admitted=False)


_CLOSED = (_closed("none"), _closed("read"), _closed("exec"), _closed("write"))

_HOST_PRINT_CLI = HostCell(
    tool_refs=frozenset({HOST_CODING}), tool_match="subset", allow_mcp=False, allow_approval=False,
    approval_mode=None, default_sandbox=None, credential_mode="cli_login_home",
    accepted_tools="none or host-coding", surface_enforced=False,
)
_CODEX_SANDBOX = SdkSandboxPolicy(
    values=frozenset({"read-only", "workspace-write", "danger-full-access"}),
    host_approval_mode="deny_all", host_default="workspace-write",
    inactive_alias=(("full-access", "danger-full-access"),), bypass_forbidden=True,
    invalid_saved_message=_SAVED_SANDBOX,
)

_CLAUDE_SURFACES = (
    SurfaceCell(
        "none", True, tool_refs=frozenset({HOST_CODING}), tool_match="without",
        allow_mcp=True, allow_approval=True, accepted_tools="none",
    ),
    _closed("read"),
    SurfaceCell(
        "exec", False, tool_refs=frozenset({SANDBOX_EXEC}), tool_match="subset",
        accepted_tools="sandbox-exec subset",
    ),
    SurfaceCell(
        "write", True, tool_refs=frozenset({HOST_CODING}), tool_match="exact",
        admission_requires=frozenset({"agent_container"}), requires=frozenset({"agent_container"}),
        credential_modes=frozenset({"runtime_managed", "environment", "secret"}), execution_mode=CODING_MODE,
        accepted_tools="exactly host-coding", rejection=_CLAUDE_TOOLS,
    ),
)
_PRINT_CLI_SURFACES = (
    _closed("none"),
    _closed("read"),
    _closed("exec"),
    SurfaceCell(
        "write",
        True,
        tool_refs=frozenset({HOST_CODING}),
        tool_match="subset",
        admission_requires=frozenset({"agent_container"}),
        requires=frozenset({"agent_container", "egress"}),
        credential_modes=frozenset({"cli_login_home"}),
        accepted_tools="none or host-coding",
        rejection="Print CLI runtime accepts only no tools or builtin:host-coding@1",
    ),
)

_CODEX_SURFACES = (
    # 道具なしの review は exec の部分集合のまま。none には分けない。
    _closed("none"),
    SurfaceCell(
        "read", True, tool_refs=frozenset({WORKSPACE_READ}), tool_match="exact",
        requires=frozenset({"rootfs", "egress", "agent_container", "subscription"}),
        credential_modes=frozenset({"chatgpt_subscription"}), execution_mode=READ_MODE,
        accepted_tools="exactly workspace-read", rejection=_CODEX_TOOLS, rootfs_digest="coding",
        store_sdk_sandbox=True, sdk_sandbox_default="read-only", sdk_sandbox_fixed="read-only",
        external_sandbox=True, workspace_readonly=True,
        sandbox_policy=READ_SANDBOX_POLICY, isolated_tool_policy_digest=READ_TOOL_POLICY_DIGEST,
    ),
    SurfaceCell(
        "exec", True, tool_refs=frozenset({SANDBOX_EXEC}), tool_match="subset",
        requires=frozenset({"rootfs"}), credential_modes=frozenset({"environment", "secret", "chatgpt_subscription"}),
        execution_mode=JAIL_MODE, accepted_tools="sandbox-exec subset", rejection=_CODEX_TOOLS,
        rootfs_digest="standard", sdk_sandbox_default="read-only", sdk_sandbox_fixed="read-only",
        sandbox_rejection=_JAIL_SANDBOX,
    ),
    SurfaceCell(
        "write", True, tool_refs=frozenset({HOST_CODING}), tool_match="exact",
        requires=frozenset({"rootfs", "egress", "agent_container", "subscription"}),
        credential_modes=frozenset({"chatgpt_subscription"}), execution_mode=CODING_MODE,
        accepted_tools="exactly host-coding", rejection=_CODEX_TOOLS, rootfs_digest="coding",
        store_sdk_sandbox=True, sdk_sandbox_default="workspace-write", external_sandbox=True,
        protected_entries_required=True, sandbox_policy=EXTERNAL_SANDBOX_POLICY,
        isolated_tool_policy_digest=EXTERNAL_TOOL_POLICY_DIGEST, host_tool_policy_digest=TOOL_POLICY_DIGEST,
    ),
)

DataPolicy = Literal["no_training", "trains", "unknown"]


DRIVERS: tuple[DriverManifest, ...] = (
    DriverManifest(
        driver="claude.agent-sdk", provider="claude",
        official_interfaces=("agent-sdk", "oauth", "api-key"),
        places=frozenset({"isolated"}), explicit_environments=frozenset(),
        surfaces=_CLAUDE_SURFACES, host=None,
        credential_modes=frozenset({"runtime_managed", "environment", "secret"}),
        unmapped_backend="native", allows_execution_native=True,
        allows_direct_output_schema=True, surface_selector="request",
    ),
    DriverManifest(
        driver="codex.app-server", provider="codex",
        official_interfaces=("cli", "sdk", "chatgpt-subscription", "api-key"),
        places=frozenset({"host", "isolated"}), explicit_environments=frozenset({"host", "isolated"}),
        surfaces=_CODEX_SURFACES, host=None,
        credential_modes=frozenset({"chatgpt_subscription", "environment", "secret", "shared_host"}),
        unmapped_backend=None, allows_execution_native=False,
        allows_direct_output_schema=True, surface_selector="profile",
        mapping=MappingRule(profile_backend="native", profile_provider="codex", model_must_match=False),
        isolated_explicit=IsolatedExplicit(
            requires_mapping=True, execution_mode=CODING_MODE, credential_mode="chatgpt_subscription",
        ),
        sdk_sandbox=_CODEX_SANDBOX,
        coding_profile=(CODING_MODE, frozenset({"chatgpt_subscription"})),
    ),
    DriverManifest(
        driver="cursor.sdk", provider="cursor", official_interfaces=("api-key",),
        places=frozenset({"host"}), explicit_environments=frozenset({"host"}),
        surfaces=_CLOSED, host=None, credential_modes=frozenset({"shared_host"}),
        unmapped_backend=None, allows_execution_native=False,
        allows_direct_output_schema=False, surface_selector="none",
    ),
    DriverManifest(
        driver="antigravity.sdk", provider="antigravity", official_interfaces=("api-key", "vertex"),
        places=frozenset({"host"}), explicit_environments=frozenset({"host"}),
        surfaces=_CLOSED, host=None, credential_modes=frozenset({"shared_host"}),
        unmapped_backend=None, allows_execution_native=False,
        allows_direct_output_schema=False, surface_selector="none",
    ),
    DriverManifest(
        driver="cli.print", provider="print_cli", official_interfaces=("print-cli",),
        places=frozenset({"host", "isolated"}), explicit_environments=frozenset({"host", "isolated"}),
        surfaces=_PRINT_CLI_SURFACES, host=_HOST_PRINT_CLI, credential_modes=frozenset({"cli_login_home"}),
        unmapped_backend="print_cli", allows_execution_native=False,
        allows_direct_output_schema=False, surface_selector="none",
    ),
    DriverManifest(
        driver="cli.acp", provider="cli_acp", official_interfaces=("acp",),
        places=frozenset({"host", "isolated"}), explicit_environments=frozenset({"host", "isolated"}),
        surfaces=_PRINT_CLI_SURFACES, host=_HOST_PRINT_CLI, credential_modes=frozenset({"cli_login_home"}),
        unmapped_backend="print_cli", allows_execution_native=False,
        allows_direct_output_schema=False, surface_selector="none",
    ),
)

_BEHAVIOR_FIELDS = tuple(
    name for name in DriverManifest.__dataclass_fields__ if name not in {"driver", "official_interfaces"}
)


def _check_table() -> None:
    """同じ provider の行は、公式 interface 以外で矛盾しない。"""
    names = [row.driver for row in DRIVERS]
    if len(names) != len(set(names)):
        raise ValueError("duplicate driver")
    by_provider: dict[str, list[DriverManifest]] = {}
    for row in DRIVERS:
        if tuple(cell.surface for cell in row.surfaces) != ("none", "read", "exec", "write"):
            raise ValueError(f"{row.driver} must declare none/read/exec/write")
        if row.provider != "codex" and any(cell.admitted and cell.surface == "read" for cell in row.surfaces):
            raise ValueError(f"{row.driver} cannot admit read yet")
        by_provider.setdefault(row.provider, []).append(row)
    for provider, rows in by_provider.items():
        first = rows[0]
        for row in rows[1:]:
            for name in _BEHAVIOR_FIELDS:
                if getattr(row, name) != getattr(first, name):
                    raise ValueError(f"{provider} drivers disagree on {name}")


_check_table()
registry.register_builtin_drivers(DRIVERS)


def driver_manifest(driver: str) -> DriverManifest:
    """driver 名で登録表を引く。未知の名前は KeyError。"""
    return registry.driver(driver)


def driver_name_for_provider(provider: str, *, interface: str | None = None) -> str:
    """provider から既定の driver 名を返す。"""
    return _behavior(provider).driver


def surface_exceeds(surface: str, ceiling: str) -> bool:
    """surface が alias または Caller の上限を超えるか。"""
    return SURFACE_RANK[surface] > SURFACE_RANK[ceiling]


def _behavior(provider: str) -> DriverManifest:
    return registry.driver_for_provider(provider)


def _cell(manifest: DriverManifest, surface: str) -> SurfaceCell | None:
    for cell in manifest.surfaces:
        if cell.surface == surface:
            return cell
    return None


def admitted_execution_cell(provider: str | None, mode: str | None) -> SurfaceCell | None:
    """provider の admitted cell のうち、execution_mode が一致するもの。"""
    if not provider:
        return None
    try:
        return _admitted_mode_cell(_behavior(provider), mode)
    except KeyError:
        return None


def mode_requests_room(mode: str | None, *, provider: str | None = None) -> bool:
    """この mode の cell が部屋か。provider が無ければ、表のどれかが部屋なら True。"""
    if not mode:
        return False
    if provider is not None:
        cell = admitted_execution_cell(provider, mode)
        return cell is not None and runtime_for(cell.surface) == "room"
    seen: set[str] = set()
    for row in registry.drivers():
        if row.provider in seen:
            continue
        seen.add(row.provider)
        cell = admitted_execution_cell(row.provider, mode)
        if cell is not None and runtime_for(cell.surface) == "room":
            return True
    return False


def room_execution_modes() -> frozenset[str]:
    """部屋の execution mode。容量は write と read を同じ集合で数える。"""
    modes = {
        cell.execution_mode
        for row in registry.drivers()
        for cell in row.surfaces
        if cell.admitted and cell.execution_mode and runtime_for(cell.surface) == "room"
    }
    return frozenset(modes)


def room_builtin_tools(*, provider: str | None, execution_mode: str | None, tool_refs) -> frozenset[str]:
    """部屋に入る要求が builtin として持つ道具。client tool の解決から外す。"""
    if not provider:
        return frozenset()
    try:
        manifest = _behavior(provider)
    except KeyError:
        return frozenset()
    if manifest.surface_selector == "profile":
        cell = _admitted_mode_cell(manifest, execution_mode)
        if cell is not None and runtime_for(cell.surface) == "room":
            return cell.tool_refs
        return frozenset()
    if manifest.surface_selector == "request":
        requested = set(tool_refs)
        for cell in manifest.surfaces:
            if cell.admitted and runtime_for(cell.surface) == "room" and set(cell.tool_refs) & requested:
                return cell.tool_refs
    return frozenset()


def _admitted_mode_cell(manifest: DriverManifest, mode: str | None) -> SurfaceCell | None:
    # execution_mode を持たない cell は要求が選ぶ。None との一致で拾わない。
    if mode is None:
        return None
    for cell in manifest.surfaces:
        if cell.admitted and cell.execution_mode == mode:
            return cell
    return None


def runtime_for(surface: str) -> str:
    """isolated の surface が jail か部屋かを返す。"""
    try:
        return _RUNTIME[surface]
    except KeyError as exc:
        raise ValueError(f"unknown surface: {surface}") from exc


def may_be_isolated(provider: str) -> bool:
    """この provider が isolated (jail または部屋) で走れるか。"""
    try:
        return "isolated" in _behavior(provider).places
    except KeyError:
        return False


def execution_environment_error(provider: str, choice: str | None) -> str | None:
    """alias に書いた execution_environment を、driver の explicit_environments で拒む。"""
    if choice is None:
        return None
    if choice not in {"isolated", "host"}:
        return _ENVIRONMENT_VALUES
    try:
        allowed = _behavior(provider).explicit_environments
    except KeyError:
        allowed = frozenset()
    if choice not in allowed:
        return f"execution_environment={choice!r} is not supported for {provider}"
    return None


def explicit_sandbox_error(provider: str, choice: str | None, *, sandbox: object, approval_mode: object,
                           bypass: object) -> str | None:
    """明示した execution_environment の Codex sandbox と approval を表から拒む。"""
    if choice is None:
        return None
    try:
        policy = _behavior(provider).sdk_sandbox
    except KeyError:
        return None
    if policy is None:
        return None
    inactive = choice == "isolated" and sandbox == "full-access"
    if sandbox is not None and not inactive and (not isinstance(sandbox, str) or sandbox not in policy.values):
        return _SANDBOX_VALUES
    if policy.bypass_forbidden and bypass is not None and bypass is not False:
        return _EXPLICIT_BYPASS
    if choice == "host" and approval_mode not in (None, policy.host_approval_mode):
        return _HOST_APPROVAL
    return None


def host_sandbox_default(provider: str, choice: str | None, sandbox: object) -> str | None:
    """明示 host で sandbox が空のとき、driver が宣言した既定を返す。保存値は書き換えない。"""
    if choice != "host" or sandbox is not None:
        return None
    try:
        policy = _behavior(provider).sdk_sandbox
    except KeyError:
        return None
    if policy is None:
        return None
    return policy.host_default


def output_schema_error(execution: str, provider: str) -> str | None:
    """direct の Claude / Codex 以外の output_schema を拒む。"""
    try:
        allowed = _behavior(provider).allows_direct_output_schema
    except KeyError:
        allowed = False
    if execution == "native" or not allowed:
        return _OUTPUT_SCHEMA
    return None


def native_execution_error(provider: str) -> str | None:
    """execution=native を宣言できない driver を拒む。"""
    try:
        allowed = _behavior(provider).allows_execution_native
    except KeyError:
        allowed = False
    if not allowed:
        return _NATIVE_EXECUTION
    return None


def native_bypass_error(provider: str, bypass: object) -> str | None:
    """mapping された Codex は bypass flag を受け取らない。"""
    try:
        policy = _behavior(provider).sdk_sandbox
    except KeyError:
        return None
    if policy is None or not policy.bypass_forbidden:
        return None
    if bypass is not None and bypass is not False:
        return _NATIVE_BYPASS
    return None


def resolve_saved_sandbox(mode: str, value: object = None) -> str:
    """保存された SDK sandbox を、その mode の cell が許す値へ解く。"""
    manifest = _behavior("codex")
    known = {cell.execution_mode for cell in manifest.surfaces if cell.admitted and cell.execution_mode}
    if mode not in known:
        raise ValueError("invalid_codex_execution_mode")
    cell = _admitted_mode_cell(manifest, mode)
    policy = manifest.sdk_sandbox
    if cell is None or policy is None or cell.sdk_sandbox_default is None:
        raise ValueError("invalid_codex_execution_mode")
    if value is None:
        return cell.sdk_sandbox_default
    if not isinstance(value, str) or value not in policy.values:
        raise ValueError(policy.invalid_saved_message)
    if cell.sdk_sandbox_fixed is not None and value != cell.sdk_sandbox_fixed:
        if cell.sandbox_rejection is None:
            raise ValueError("invalid_codex_execution_mode")
        raise ValueError(cell.sandbox_rejection)
    return value


def rewrite_inactive_sandbox(provider: str, sandbox: object) -> object:
    """SDK sandbox が働かない isolated では、保存された別名だけを残す。"""
    if sandbox is None:
        return None
    try:
        policy = _behavior(provider).sdk_sandbox
    except KeyError:
        return sandbox
    if policy is None:
        return sandbox
    return dict(policy.inactive_alias).get(sandbox, sandbox)


def alias_sandbox(provider: str, environment: str | None, mode: str, saved: object) -> object:
    """isolated は検証せず別名だけ換え、それ以外は mode の cell で解く。"""
    if environment == "isolated":
        return rewrite_inactive_sandbox(provider, saved)
    return resolve_saved_sandbox(mode, saved)


def external_execution_environment(cell: SurfaceCell, place: str) -> str | None:
    """SDK sandbox が無効な cell は、place が isolated のとき外枠を境界にする。"""
    if cell.external_sandbox and place == "isolated":
        return "isolated"
    return None


def resolved_sdk_sandbox(cell: SurfaceCell, place: str, saved: object) -> object:
    """外部 sandbox の cell は保存された別名だけ換え、それ以外は cell の規則で解く。"""
    if external_execution_environment(cell, place) == "isolated":
        if saved is not None and not isinstance(saved, str):
            policy = _behavior("codex").sdk_sandbox
            raise ValueError(policy.invalid_saved_message if policy is not None else _SANDBOX_VALUES)
        rewritten = rewrite_inactive_sandbox("codex", saved)
        if cell.sdk_sandbox_fixed is not None and rewritten is None:
            return cell.sdk_sandbox_fixed
        if rewritten is None:
            return None
        policy = _behavior("codex").sdk_sandbox
        if policy is None or not isinstance(rewritten, str) or rewritten not in policy.values:
            raise ValueError(policy.invalid_saved_message if policy is not None else _SANDBOX_VALUES)
        if cell.sdk_sandbox_fixed is not None and rewritten != cell.sdk_sandbox_fixed:
            if cell.sandbox_rejection is None:
                raise ValueError("invalid_codex_execution_mode")
            raise ValueError(cell.sandbox_rejection)
        return rewritten
    if cell.execution_mode is None:
        raise ValueError("invalid_codex_execution_mode")
    return resolve_saved_sandbox(cell.execution_mode, saved)


def stores_sdk_sandbox(provider: str, execution_mode: str | None) -> bool:
    """coding cell だけが SDK sandbox を snapshot に残す。"""
    try:
        cell = _admitted_mode_cell(_behavior(provider), execution_mode)
    except KeyError:
        return False
    return cell is not None and cell.store_sdk_sandbox


def unmapped_backend(provider: str) -> str | None:
    """mapping が無い alias の backend。Claude だけ native。"""
    try:
        return _behavior(provider).unmapped_backend
    except KeyError:
        return None


def place_for_backend(backend: str, execution_environment: str | None = None) -> str:
    """native と isolated print_cli は isolated、host print_cli は host。"""
    if backend == "print_cli" and execution_environment == "isolated":
        return "isolated"
    return "isolated" if backend == "native" else "host"


def published_agent_execution(backend: str, execution_environment: str | None = None) -> str:
    """/v1/models の agent_execution。isolated は sandbox、host は host。"""
    return "sandbox" if place_for_backend(backend, execution_environment) == "isolated" else "host"


def admission_isolation(backend: str, execution_environment: str | None = None) -> str:
    """受付が要求へ書く isolation。host の wire 名は native。"""
    return "sandbox" if place_for_backend(backend, execution_environment) == "isolated" else "native"


def native_caller_requires_worker(*, places, native_worker, publishes_host: bool) -> bool:
    """Host places no longer require a systemd-bwrap command worker (ADR-0010 Stage 7)."""
    return False


def native_caller_error(*, places, native_worker, publishes_host: bool) -> str | None:
    return None


def coding_profile_allowed(provider: str, execution_mode: str, credential_mode: str) -> bool:
    """coding mode を名乗れるのは、その mode と credential を宣言した driver だけ。"""
    try:
        manifest = _behavior(provider)
    except KeyError:
        manifest = None
    if manifest is not None:
        cell = _admitted_mode_cell(manifest, execution_mode)
        # CODING_MODE の資格は下の coding_profile が決める。それ以外の部屋は cell の宣言に従う。
        if (cell is not None and cell.credential_modes and cell.execution_mode != CODING_MODE
                and runtime_for(cell.surface) == "room"):
            return credential_mode in cell.credential_modes
    rule = None if manifest is None else manifest.coding_profile
    if rule is None:
        return execution_mode != CODING_MODE
    expected, credentials = rule
    if execution_mode != expected:
        return True
    return credential_mode in credentials


def egress_limits_allowed(provider: str | None, execution_mode: str | None) -> bool:
    """egress limits は coding cell の requires に egress があるときだけ。"""
    if not provider:
        return False
    try:
        cell = _admitted_mode_cell(_behavior(provider), execution_mode)
    except KeyError:
        return False
    return cell is not None and "egress" in cell.requires


def rootfs_digest_is_coding(provider: str | None, execution_mode: str | None) -> bool:
    """rootfs digest の coding 計算は、coding と宣言した cell だけが使う。"""
    if not provider:
        return False
    try:
        cell = _admitted_mode_cell(_behavior(provider), execution_mode)
    except KeyError:
        return False
    return cell is not None and cell.rootfs_digest == "coding"


def _run(cell: SurfaceCell) -> dict[str, str]:
    return {"accepted_tools": cell.accepted_tools, "place": runtime_for(cell.surface)}


def accepted_runs(resolved, *, agent_container: bool) -> list[dict[str, str]]:
    """解決済み profile に対し、受付が許す道具の形と場所を返す。

    Claude は要求で none と write に分かれる。Codex は profile の mode が 1 つを選ぶ。
    """
    if resolved.backend == "print_cli":
        place = "isolated" if getattr(resolved, "execution_environment", "host") == "isolated" else "host"
        if place == "isolated" and not agent_container:
            return []
        accepted_place = "room" if place == "isolated" else "host"
        ceiling = getattr(resolved, "_agent_surface", None)
        if ceiling == "none":
            return [{"accepted_tools": "none", "place": accepted_place}]
        return [{"accepted_tools": _HOST_PRINT_CLI.accepted_tools, "place": accepted_place}]
    try:
        manifest = _behavior(resolved.provider)
    except KeyError as exc:
        raise AssertionError(f"unclassified resolved backend: {resolved.backend}") from exc
    if manifest.surface_selector == "profile":
        cell = _admitted_mode_cell(manifest, getattr(resolved, "codex_execution_mode", None))
        if cell is None:
            raise AssertionError(f"unclassified resolved backend: {resolved.backend}")
        return [_run(cell)]
    if manifest.surface_selector == "request":
        # 宣言の無い解決は今まで通り両方を出す。宣言した surface は上限で、それより上は出さない。
        ceiling = getattr(resolved, "_agent_surface", None)
        runs = []
        for cell in manifest.surfaces:
            if not cell.admitted:
                continue
            if ceiling is not None and surface_exceeds(cell.surface, ceiling):
                continue
            if "agent_container" in cell.admission_requires and not agent_container:
                continue
            runs.append(_run(cell))
        if not runs:
            raise AssertionError(f"unclassified resolved backend: {resolved.backend}")
        return runs
    raise AssertionError(f"unclassified resolved backend: {resolved.backend}")


def requests_room(*, is_agent: bool, backend: str | None, provider: str | None,
                  execution_mode: str | None, tool_refs) -> bool:
    """部屋へ入る要求か。Codex は profile の mode、Claude は host-coding の有無。"""
    if not is_agent or backend != "native" or not provider:
        return False
    try:
        manifest = _behavior(provider)
    except KeyError:
        return False
    if manifest.surface_selector == "profile":
        cell = _admitted_mode_cell(manifest, execution_mode)
        return cell is not None and runtime_for(cell.surface) == "room"
    if manifest.surface_selector == "request":
        write = _cell(manifest, "write")
        return bool(write is not None and write.admitted and set(write.tool_refs) & set(tool_refs))
    return False


def room_requires_host_scope(provider: str | None, host_coding: bool) -> bool:
    """All rooms run in Docker containers via JailPlace; systemd host scopes were removed."""
    return False


def _rejects(cell: SurfaceCell, *, isolation: str, tools: set[str], has_mcp: bool, has_approval: bool,
             agent_container: bool) -> bool:
    if isolation != "sandbox":
        return True
    if cell.tool_match == "exact" and tools != set(cell.tool_refs):
        return True
    if cell.tool_match == "subset" and tools - set(cell.tool_refs):
        return True
    if not cell.allow_mcp and has_mcp:
        return True
    if not cell.allow_approval and has_approval:
        return True
    if "agent_container" in cell.admission_requires and not agent_container:
        return True
    return False


UNDECLARED_DRIVER_REJECTION = "Agent driver or mode is not declared in the driver table"


def agent_workspace_tool_error(*, backend: str | None, provider: str | None, execution_mode: str | None,
                               isolation: str, tool_refs, has_mcp: bool, has_approval: bool,
                               agent_container: bool) -> str | None:
    """native agent の道具が、選ばれた cell の宣言から外れていれば今の文言を返す。"""
    if backend != "native" or not provider:
        return None
    try:
        manifest = _behavior(provider)
    except KeyError:
        # 表に無い native provider は断る。
        return UNDECLARED_DRIVER_REJECTION
    tools = set(tool_refs)
    if manifest.surface_selector == "request":
        write = _cell(manifest, "write")
        if write is None or not write.admitted or not (set(write.tool_refs) & tools) or write.rejection is None:
            return None
        if _rejects(write, isolation=isolation, tools=tools, has_mcp=has_mcp, has_approval=has_approval,
                    agent_container=agent_container):
            return write.rejection
        return None
    if manifest.surface_selector == "profile":
        cell = _admitted_mode_cell(manifest, execution_mode)
        if cell is None:
            # 表に admitted の cell が無い mode は断る (同じ driver の既存の文言を使う)。
            known = next((c.rejection for c in manifest.surfaces if c.rejection is not None), None)
            return known or UNDECLARED_DRIVER_REJECTION
        if cell.rejection is None:
            return None
        if _rejects(cell, isolation=isolation, tools=tools, has_mcp=has_mcp, has_approval=has_approval,
                    agent_container=agent_container):
            return cell.rejection
        return None
    return None


def stamped_execution_mode(*, provider: str | None, host_coding: bool) -> str | None:
    """要求が部屋を選ぶ driver だけ、container 経路の mode を snapshot に書く。"""
    if not host_coding or not provider:
        return None
    try:
        manifest = _behavior(provider)
    except KeyError:
        return None
    if manifest.surface_selector != "request":
        return None
    write = _cell(manifest, "write")
    if write is None or not write.admitted:
        return None
    return write.execution_mode
