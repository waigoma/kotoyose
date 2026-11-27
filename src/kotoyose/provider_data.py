"""Validate provider data declarations before they join the built-in catalog.

The schema id is ``kotoyose.provider-data/1``. ``kotoyose.provider/1`` stays reserved
for the external package contract and is not accepted here.
"""

from dataclasses import dataclass
import hashlib
import json
import re
import unicodedata
from urllib.parse import urlsplit

from .acp_spec import AcpSpec
from .cli_install_spec import CliArtifact, CliInstallSpec
from .print_cli_grok_profile import (
    DEVICE_LOGIN_URL_PATTERN, GROK_ACP_READ_ARGV, GROK_ACP_UNRESTRICTED_ARGV, GROK_ACP_WRITE_ARGV, GROK_DEVICE_CODE_PATTERN,
    GROK_INTEGRATION_ENV, GROK_LINUX_SKILL_DIGEST, GROK_LINUX_SKILL_VERSION, GROK_NATIVE_SKILL_LAYOUT,
    GROK_SIGNED_OUT_PATTERN,
)
from .print_cli_profiles import PrintCliProfile, builtin_print_cli_profiles
from .provider_login import LoginFlowKind, PrintCliLoginSpec
from .provider_spec import BUILTIN_PROVIDERS, PROMPT_PREFIX_SKILL_FORMAT, ChildTurnSpec

SCHEMA = "kotoyose.provider-data/1"
FEATURE = "provider-data/1"
SANDBOX_CAPABILITIES = ("read", "write", "mcp")
UNRESTRICTED = "unrestricted"
UNRESTRICTED_PLATFORM = "windows-x64"
CAPABILITIES = (*SANDBOX_CAPABILITIES, UNRESTRICTED)
GROK_MCP_GUARD = "grok_servers_updated_v1"
DATA_DRIVERS = {"cli.acp": 1, "cli.print": 1}
MAX_DOCUMENT = 16 * 1024
MAX_OFFERS = 16
MAX_CATALOG = 96 * 1024
_ID = re.compile(r"[a-z][a-z0-9-]{0,47}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_PATH_SEGMENT = re.compile(r"[a-zA-Z0-9_-][a-zA-Z0-9._-]{0,127}")
_ENV = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
_PLATFORMS = {"linux-x64", "linux-arm64", "windows-x64", "windows-arm64", "darwin-x64", "darwin-arm64"}
_SHELL = re.compile(
    r"^(?:sh|bash|dash|zsh|ksh|cmd(?:\.exe)?|powershell(?:\.exe)?|pwsh(?:\.exe)?"
    r"|python\d*(?:\.exe)?|node(?:\.exe)?|perl|ruby|busybox)$",
    re.I,
)
_CREDENTIAL = re.compile(r"(?i)(api[_-]?key|token|secret|password|credential)")
_ACP_ARGV = {("acp",), ("acp", "--model", "{model}"), GROK_ACP_READ_ARGV}
_ACP_WRITE_ARGV = {GROK_ACP_WRITE_ARGV}
_LOGIN_ARGV = {("-p", "/usage", "--output-format", "json"), ("login",), ("login", "--device-auth")}
_STATUS_ARGV = {("status", "--format", "json"), ("-p", "/usage", "--output-format", "json"), ("status",), ("login", "status"), ("models",)}
_PRINT_ARGV = {("--", "{prompt}"), ("--prompt={prompt}",)}
_PROMPT_FLAG = re.compile(r"--[a-z0-9][a-z0-9-]*=\{prompt\}")


def supports_capability(maximum: str, requested: str) -> bool:
    if UNRESTRICTED in (maximum, requested):
        return maximum == requested
    return (maximum in SANDBOX_CAPABILITIES and requested in SANDBOX_CAPABILITIES
            and SANDBOX_CAPABILITIES.index(requested) <= SANDBOX_CAPABILITIES.index(maximum))


def require_unrestricted_alias(on: str, surface: str, full: bool, driver: str, *, tools: object, deny: object) -> None:
    from .child_registry import child_on_name

    if child_on_name(on) is None or surface != "write" or full is not True or driver != "cli.acp":
        raise ValueError("unrestricted requires on: child:<name>, agent.surface: write, allow.full: true, and cli.acp")
    if tools is not None or deny:
        raise ValueError("unrestricted aliases cannot set tools or deny because these restrictions cannot be enforced")


def child_capability(surface: str, *, has_mcp: bool) -> str:
    return "mcp" if surface == "write" and has_mcp else surface


def _object(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= value.keys() <= set(required) | set(optional):
        raise ValueError("declaration has missing or unknown fields")
    return value


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _no_constant(_):
    raise ValueError("non-finite JSON number")


def _depth(value, depth=0):
    if depth > 10:
        raise ValueError("declaration exceeds depth limit")
    if isinstance(value, (dict, list)):
        for item in value.values() if isinstance(value, dict) else value:
            _depth(item, depth + 1)


def _text(value, limit=120):
    if (
        not isinstance(value, str) or not 0 < len(value) <= limit or not value.strip()
        or any(ord(char) < 32 or 0x7F <= ord(char) <= 0x9F or 0xD800 <= ord(char) <= 0xDFFF for char in value)
    ):
        raise ValueError("invalid declaration text")
    return value


def _display_text(value, limit=120):
    """Names shown on the approval screen. Format and control characters can spoof that screen."""
    text = _text(value, limit)
    for char in text:
        category = unicodedata.category(char)
        if category.startswith("C") or category in {"Zl", "Zp"}:
            raise ValueError("display name contains unsupported characters")
    return text


def identifier(value):
    if not _ID.fullmatch(_text(value, 48)):
        raise ValueError("invalid declaration identifier")
    return value


def digest(value):
    if not _SHA256.fullmatch(_text(value, 64)):
        raise ValueError("expected a lowercase SHA-256")
    return value


def hostname(value):
    name = _text(value, 253)
    labels = name.split(".")
    if (
        len(labels) < 2 or not all(_LABEL.fullmatch(label) for label in labels)
        or not labels[-1].isalpha()
        or labels[-1] in {"local", "localhost", "localdomain", "internal", "lan", "home", "onion"}
    ):
        raise ValueError("expected a public, exact DNS hostname")
    return name


def check_url(value, hosts):
    """Accept a literal HTTPS URL on an exact declared host."""
    url = _text(value, 2048)
    try:
        parsed = urlsplit(url)
    except ValueError:
        raise ValueError("URL must be literal HTTPS on a declared host") from None
    if (
        not url.startswith("https://") or parsed.netloc not in hosts or not parsed.path.startswith("/")
        or parsed.username or parsed.password or parsed.query or parsed.fragment
        or any(char.isspace() or char in "\\{}#?" or ord(char) > 126 for char in url)
        or re.search(r"%(?:0[0-9a-f]|1[0-9a-f]|7f|7b|7d|5c)", url, re.I)
        or re.search(r"%(?:25)*2[ef]", url, re.I)
    ):
        raise ValueError("URL must be literal HTTPS on a declared host")
    return url


def _array(value, parse, limit, minimum=0):
    if not isinstance(value, list) or not minimum <= len(value) <= limit:
        raise ValueError("invalid declaration list size")
    result = tuple(parse(item) for item in value)
    if len(result) != len(set(result)):
        raise ValueError("duplicate declaration list item")
    return result


def _path(value):
    path = _text(value, 240)
    if not all(_PATH_SEGMENT.fullmatch(part) and not part.endswith(".") for part in path.split("/")):
        raise ValueError("invalid artifact member")
    return path


def reject_option_value(value: str, label: str) -> str:
    """Reject a substituted value that the child could pass as a new CLI option."""
    if not isinstance(value, str) or not value or value.startswith("-") or any(ord(char) < 32 for char in value):
        raise ValueError(f"{label} cannot be an option")
    return value


def _argv_token(token, placeholders):
    if token in placeholders:
        return token
    if "{prompt}" in placeholders and _PROMPT_FLAG.fullmatch(token):
        return token
    if (
        any(char in token for char in "|;&<>`$(){}") or token.startswith("-c") or "/" in token or "\\" in token
        or _SHELL.fullmatch(token) or token.lower().endswith((".sh", ".py", ".js", ".ps1", ".bat", ".cmd"))
        or _CREDENTIAL.search(token) or token in {"HOME", "USERPROFILE", "env"}
    ):
        raise ValueError("declaration arguments cannot launch a shell, script, or credential read")
    return token


def _print_arguments(value, *, stdin: bool) -> tuple[str, ...]:
    """Keep ``{prompt}`` from becoming its own option.

    A prompt is the argv element after ``--``, the value of one ``--flag={prompt}``
    element, or the process stdin. A separate ``{prompt}`` element can be ``--help``.
    """
    if not isinstance(value, list):
        raise ValueError("unsupported declaration arguments")
    if stdin:
        if value:
            raise ValueError("stdin prompts cannot also be argv")
        return ()
    tokens = tuple(_argv_token(_text(item), {"{prompt}"}) for item in value)
    saw = False
    for index, token in enumerate(tokens):
        if token == "{prompt}":
            if index == 0 or tokens[index - 1] != "--":
                raise ValueError("prompt can become an option")
            saw = True
        elif "{prompt}" in token:
            if _PROMPT_FLAG.fullmatch(token) is None:
                raise ValueError("prompt can become an option")
            saw = True
    if not saw:
        raise ValueError("prompt can become an option")
    if tokens not in _PRINT_ARGV:
        raise ValueError("unsupported declaration arguments")
    return tokens


def _argv(value, allowed, placeholders):
    if not isinstance(value, list):
        raise ValueError("unsupported declaration arguments")
    tokens = tuple(_text(item) for item in value)
    if tokens in allowed:
        return tokens
    for token in tokens:
        _argv_token(token, placeholders)
    raise ValueError("unsupported declaration arguments")


def _acp_commands(value) -> tuple[AcpSpec | None, AcpSpec | None, AcpSpec | None, str | None]:
    obj = _object(value, ("placeholders",), ("argv", "write_argv", "unrestricted_argv", "mcp_guard"))
    read = AcpSpec(_argv(obj["argv"], _ACP_ARGV, {"{model}"})) if "argv" in obj else None
    write = AcpSpec(_argv(obj["write_argv"], _ACP_WRITE_ARGV, {"{model}"})) if "write_argv" in obj else None
    unrestricted = (AcpSpec(_argv(obj["unrestricted_argv"], {GROK_ACP_UNRESTRICTED_ARGV, ("acp",), ("acp", "--model", "{model}")}, {"{model}"}))
                    if "unrestricted_argv" in obj else None)
    model = any(spec is not None and "{model}" in spec.argv for spec in (read, write, unrestricted))
    if not ((model and obj["placeholders"] == ["model"]) or (
        any(spec is not None for spec in (read, unrestricted))
        and all(spec is None or spec.argv == ("acp",) for spec in (read, unrestricted))
        and obj["placeholders"] == [] and "write_argv" not in obj
    )):
        raise ValueError("acp placeholders do not match the driver contract")
    guard = obj.get("mcp_guard")
    if "mcp_guard" in obj and guard != GROK_MCP_GUARD:
        raise ValueError("unsupported ACP mcp_guard")
    return read, write, unrestricted, guard


def _timeout(operation, value, maximum):
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"invalid {operation} timeout")
    return value


def _login_operations(operations, hosts):
    login = _object(operations["login"], ("argv", "timeout_seconds"), ("flow",))
    status = _object(operations["status"], ("argv", "timeout_seconds"), ("signed_out_pattern",))
    login_argv = _argv(login["argv"], _LOGIN_ARGV, ())
    status_argv = _argv(status["argv"], _STATUS_ARGV, ())
    flow = login.get("flow", "browser_wait")
    paste = flow == "paste_code" and login_argv == ("-p", "/usage", "--output-format", "json") and bool(hosts)
    if flow != "browser_wait" and not paste and (flow != "device_code" or login_argv != ("login", "--device-auth") or not hosts):
        raise ValueError("unsupported declaration login flow")
    signed_out = status.get("signed_out_pattern")
    if signed_out is not None and (status_argv != ("models",) or signed_out != GROK_SIGNED_OUT_PATTERN):
        raise ValueError("unsupported declaration signed-out pattern")
    login_limit = 300 if flow == "device_code" or (
        flow == "browser_wait" and login_argv == ("login",) and status_argv == ("status", "--format", "json")
    ) else 120
    return PrintCliLoginSpec(
        command=login_argv, status_command=status_argv, url_pattern=DEVICE_LOGIN_URL_PATTERN, url_hosts=hosts,
        user_code_pattern=GROK_DEVICE_CODE_PATTERN if flow == "device_code" else None,
        code_prompt_pattern=r"(?i)paste the authorization code" if paste else None,
        success_json_field="status" if paste else None, signed_out_pattern=signed_out,
        cli_timeout_seconds=_timeout("login", login["timeout_seconds"], login_limit),
    ), flow, _timeout("status", status["timeout_seconds"], 120)


@dataclass(frozen=True)
class Platform:
    artifact: CliArtifact
    binary: str
    capability: str

    def supports(self, capability: str) -> bool:
        return supports_capability(self.capability, capability)


@dataclass(frozen=True)
class Isolation:
    keep_home_env: tuple[str, ...]
    env: tuple[tuple[str, str], ...]


def _isolation(value, acp: AcpSpec | None, unrestricted: AcpSpec | None, platforms: list[Platform]) -> Isolation:
    obj = _object(value, ("private_home", "keep_home_env", "env"))
    if (
        (acp is not None and acp.argv != GROK_ACP_READ_ARGV)
        or (acp is None and unrestricted is None)
        or any(row.artifact.os != "linux" and not row.supports(UNRESTRICTED) for row in platforms)
        or obj["private_home"] is not True or obj["keep_home_env"] != ["GROK_HOME"]
        or obj["env"] != dict(GROK_INTEGRATION_ENV)
    ):
        raise ValueError("unsupported declaration isolation")
    return Isolation(("GROK_HOME",), GROK_INTEGRATION_ENV)


@dataclass(frozen=True)
class Declaration:
    document: str
    sha256: str
    id: str
    profile_id: str
    aliases: tuple[str, ...]
    name: str
    publisher: str
    driver: str
    contract_version: int
    install: CliInstallSpec
    platforms: tuple[Platform, ...]
    download_hosts: tuple[str, ...]
    login_hosts: tuple[str, ...]
    runtime_hosts: tuple[str, ...]
    acp: AcpSpec | None
    acp_write: AcpSpec | None
    acp_unrestricted: AcpSpec | None
    mcp_guard: str | None
    isolation: Isolation | None
    print_argv: tuple[str, ...]
    prompt_via: str
    login: PrintCliLoginSpec
    login_flow: LoginFlowKind
    status_timeout: int
    output_guard_env: tuple[str, ...]
    native_skill_layout: str | None = None

    @property
    def names(self) -> set[str]:
        return {self.id, self.profile_id, *self.aliases}

    def platform(self, key):
        return next((row for row in self.platforms if row.artifact.platform == key), None)

    def supports(self, capability: str) -> bool:
        return any(row.supports(capability) for row in self.platforms)

    def offer(self):
        return {"id": self.id, "sha256": self.sha256, "document": self.document}

    def binary_name(self, key):
        row = self.platform(key)
        return row.binary if row is not None else self.platforms[0].binary

    def commands(self, key):
        """Every command an approval of this OS would run, including the version check."""
        row = self.platform(key)
        if row is None:
            return []
        binary = row.binary
        prefix = [row.artifact.entrypoint or binary, *row.artifact.entrypoint_args] if row.artifact.entrypoint_args else [binary]
        rows = [
            {"operation": "version", "argv": [*prefix, "--version"], "placeholders": {}},
            {"operation": "login", "argv": [*prefix, *self.login.command], "placeholders": {}},
            {"operation": "status", "argv": [*prefix, *self.login.status_command], "placeholders": {}},
        ]
        acp = self.acp_unrestricted if row.supports(UNRESTRICTED) else self.acp
        if acp is not None:
            rows.append({
                "operation": "acp", "argv": [*prefix, *acp.argv],
                "placeholders": {"model": "child-validated model id"} if "{model}" in acp.argv else {},
            })
        if self.acp_write is not None and row.supports("write"):
            rows.append({
                "operation": "acp (write)", "argv": [*prefix, *self.acp_write.argv],
                "placeholders": {"model": "child-validated model id"},
            })
        if self.driver == "cli.print":
            rows.append({
                "operation": "print", "argv": [*prefix, *self.print_argv],
                "placeholders": {"prompt": "stdin" if self.prompt_via == "stdin" else "turn text"},
            })
        return rows

    def profile(self):
        from .cli_install_spec import platform_key

        target = self.platform(platform_key())
        unrestricted = target is not None and target.supports(UNRESTRICTED)
        surfaces = {}
        if self.driver == "cli.print":
            from .print_cli_profiles import SurfaceCliSpec

            surfaces = {"read": SurfaceCliSpec()}
        return PrintCliProfile(
            id=self.profile_id, provider=self.id, binary=self.binary_name(platform_key()), min_version=(),
            argv_template=self.print_argv, surfaces=surfaces, settings_flag=None,
            parser="text" if self.driver == "cli.print" else "",
            login_home_env="HOME", exit_codes={}, quota_error_patterns=(), env_allowlist=(),
            install=self.install, acp=self.acp_unrestricted if unrestricted else self.acp or self.acp_unrestricted,
            login=self.login, login_flow=self.login_flow,
            child_turn=ChildTurnSpec(skill_format=PROMPT_PREFIX_SKILL_FORMAT, output_guard_env=self.output_guard_env),
            display_name=self.name, terminal_login="hidden",
            data_declaration=self, add_dir_flag=None, native_skill_layout=self.native_skill_layout,
        )


def _env_name(value):
    name = _text(value, 64)
    if not _ENV.fullmatch(name) or name in {"PATH", "HOME", "USERPROFILE", "SHELL"} or _CREDENTIAL.search(name):
        raise ValueError("invalid output guard environment name")
    return name


def _decode_declaration(raw: bytes) -> tuple[str, object]:
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_DOCUMENT:
        raise ValueError("invalid declaration size")
    try:
        document = raw.decode("utf-8")
        body = json.loads(document, object_pairs_hook=_unique, parse_constant=_no_constant)
        _depth(body)
    except (RecursionError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid declaration JSON") from exc
    return document, body


def stored_declaration_identity(raw: bytes) -> tuple[str | None, str | None]:
    """Recover only well-formed identifiers for diagnostics from a rejected file."""
    try:
        _, body = _decode_declaration(raw)
    except ValueError:
        return None, None
    identity = body.get("identity") if isinstance(body, dict) else None
    if not isinstance(identity, dict):
        return None, None
    declaration_id, profile_id = identity.get("id"), identity.get("profile_id")
    return (
        declaration_id if isinstance(declaration_id, str) and _ID.fullmatch(declaration_id) else None,
        profile_id if isinstance(profile_id, str) and _ID.fullmatch(profile_id) else None,
    )


def parse_declaration(raw: bytes) -> Declaration:
    document, body = _decode_declaration(raw)
    driver_body = body.get("driver") if isinstance(body, dict) else None
    driver_id = driver_body.get("id") if isinstance(driver_body, dict) else None
    if not isinstance(driver_id, str) or driver_id not in DATA_DRIVERS:
        raise ValueError("unsupported data driver contract")
    block = "acp" if driver_id == "cli.acp" else "print"
    _object(body, ("schema", "identity", "driver", "platforms", "connection_kinds", "network", "operations", block),
            ("output_guard_env", "child_turn", "isolation", "native_skill_layout"))
    if body["schema"] != SCHEMA:
        raise ValueError("unsupported declaration schema")
    if body.get("child_turn", {"skill_format": "none"}) != {"skill_format": "none"}:
        raise ValueError("data declarations cannot loosen the child turn contract")
    identity = _object(body["identity"], ("id", "profile_id", "display_name", "publisher"), ("aliases",))
    driver = _object(body["driver"], ("id", "contract_version"))
    if type(driver["contract_version"]) is not int or driver["contract_version"] != DATA_DRIVERS[driver_id]:
        raise ValueError("unsupported data driver contract")
    if body["connection_kinds"] != ["subscription"]:
        raise ValueError("unsupported data connection kind")
    network = _object(body["network"], ("download_hosts", "login_hosts", "runtime_hosts"))
    download, login_hosts, runtime = (
        _array(network[key], hostname, 16) for key in ("download_hosts", "login_hosts", "runtime_hosts")
    )
    rows = body["platforms"]
    if not isinstance(rows, dict) or not rows or not rows.keys() <= _PLATFORMS:
        raise ValueError("unsupported artifact platform")
    platforms = []
    versions = set()
    for key, row in rows.items():
        _object(row, ("install", "max_capability"))
        capability = row["max_capability"]
        if key == UNRESTRICTED_PLATFORM:
            if capability != UNRESTRICTED:
                raise ValueError("Windows x64 requires unrestricted capability")
        elif key.startswith("windows-") or capability not in SANDBOX_CAPABILITIES:
            raise ValueError("unsupported capability for this platform")
        pin = _object(row["install"], ("version", "urls", "sha256", "format", "binary"), ("member", "directory", "entrypoint", "entrypoint_args"))
        kind = pin["format"]
        layout = set(pin) & {"member", "directory", "entrypoint", "entrypoint_args"}
        if not ((kind == "raw" and not layout) or (
            kind in ("tar.gz", "zip")
            and layout in ({"member"}, {"directory", "entrypoint"}, {"directory", "entrypoint", "entrypoint_args"})
        )):
            raise ValueError("unsupported artifact layout")
        os_name, arch = key.split("-", 1)
        binary = identifier(pin["binary"]) if os_name != "windows" else _text(pin["binary"], 52)
        if os_name == "windows":
            executable = pin.get("entrypoint", "") if kind == "zip" else binary
            if (kind not in ("raw", "zip") or (kind == "zip" and "directory" not in pin)
                    or not re.fullmatch(r"[a-z][a-z0-9-]{0,47}\.exe", executable) or re.fullmatch(
                r"(?:aux|con|nul|prn|com[0-9]|lpt[0-9])\.exe", executable,
            )):
                raise ValueError("Windows artifacts require a raw or zip EXE with an ordinary filename")
        artifact = CliArtifact(
            key, os_name, arch, _array(pin["urls"], lambda url: check_url(url, download), 4, 1),
            f"sha256:{digest(pin['sha256'])}", kind, _path(pin["member"]) if "member" in pin else None,
            _path(pin["directory"]) if "directory" in pin else None,
            _path(pin["entrypoint"]) if "entrypoint" in pin else None,
            _array(pin["entrypoint_args"], _path, 4) if "entrypoint_args" in pin else (),
        )
        platforms.append(Platform(artifact, binary, capability))
        versions.add(_text(pin["version"], 64))
    if len(versions) != 1:
        raise ValueError("platforms must share an exact release version")
    operations = _object(body["operations"], ("login", "status"))
    login, login_flow, status_timeout = _login_operations(operations, login_hosts)
    acp = acp_write = acp_unrestricted = None
    mcp_guard = None
    print_argv = ()
    prompt_via = "argv"
    if driver_id == "cli.acp":
        acp, acp_write, acp_unrestricted, mcp_guard = _acp_commands(body["acp"])
        if any(row.supports("read") for row in platforms) != (acp is not None):
            raise ValueError("ACP argv must be declared for read-capable platforms")
    else:
        printed = _object(body["print"], ("argv", "placeholders"), ("parser", "mode", "prompt"))
        if printed.get("parser", "text") != "text" or printed.get("mode", "read") != "read":
            raise ValueError("print data declarations are read-only text in this stage")
        if printed.get("placeholders") != ["prompt"]:
            raise ValueError("print placeholders do not match the driver contract")
        prompt_via = printed.get("prompt", "argv")
        if prompt_via not in ("argv", "stdin"):
            raise ValueError("unsupported print prompt transport")
        print_argv = _print_arguments(printed["argv"], stdin=prompt_via == "stdin")
    isolation = _isolation(body["isolation"], acp, acp_unrestricted, platforms) if "isolation" in body else None
    native_skill_layout = body.get("native_skill_layout")
    if "native_skill_layout" in body and (
        native_skill_layout != GROK_NATIVE_SKILL_LAYOUT or isolation is None
        or acp is None or acp.argv != GROK_ACP_READ_ARGV or versions != {GROK_LINUX_SKILL_VERSION}
        or not any(row.artifact.platform == "linux-x64" and row.artifact.digest == GROK_LINUX_SKILL_DIGEST
                   and row.artifact.format == "raw" for row in platforms)
    ):
        raise ValueError("unsupported native_skill_layout for this driver and artifact")
    if any(row.supports("write") for row in platforms) and acp_write is None:
        raise ValueError("write capability requires a sandboxed ACP write_argv")
    if acp_write is not None and isolation is None:
        raise ValueError("ACP write_argv requires Grok isolation on Linux")
    if any(row.supports("mcp") for row in platforms) and mcp_guard is None:
        raise ValueError("MCP capability requires a validated ACP mcp_guard")
    if any(row.supports(UNRESTRICTED) for row in platforms):
        if acp_unrestricted is None:
            raise ValueError("unrestricted requires validated ACP argv")
        if acp_unrestricted.argv == GROK_ACP_UNRESTRICTED_ARGV and (isolation is None or mcp_guard is None):
            raise ValueError("unrestricted requires validated ACP argv, Grok isolation, and mcp_guard")
        if acp_unrestricted.argv == GROK_ACP_UNRESTRICTED_ARGV and (
            login.command != ("login", "--device-auth") or login_flow != "device_code"
            or login.signed_out_pattern != GROK_SIGNED_OUT_PATTERN
        ):
            raise ValueError("unrestricted requires Grok device login and models status")
    elif acp_unrestricted is not None:
        raise ValueError("ACP unrestricted_argv requires a Windows x64 artifact")
    if mcp_guard is not None and acp_write is None and acp_unrestricted is None:
        raise ValueError("ACP mcp_guard requires a sandboxed ACP write_argv")
    return Declaration(
        document, hashlib.sha256(raw).hexdigest(), identifier(identity["id"]), identifier(identity["profile_id"]),
        _array(identity.get("aliases", []), identifier, 8), _display_text(identity["display_name"]),
        _display_text(identity["publisher"]), driver_id, driver["contract_version"],
        CliInstallSpec(versions.pop(), tuple(row.artifact for row in platforms)), tuple(platforms),
        download, login_hosts, runtime, acp, acp_write, acp_unrestricted, mcp_guard, isolation, print_argv, prompt_via,
        login, login_flow, status_timeout,
        _array(body.get("output_guard_env", []), _env_name, 16),
        native_skill_layout,
    )


def reserved_declaration_names() -> set[str]:
    from .agent_spec import DRIVERS

    reserved = {key for provider in BUILTIN_PROVIDERS for key in (provider.id, *provider.aliases)}
    reserved.update(key for profile in builtin_print_cli_profiles() for key in (profile.id, profile.provider))
    reserved.update(key for driver in DRIVERS for key in (driver.driver, driver.provider))
    reserved.update({"openai", "openai_compat", "google", "gemini", "antigravity", "cli_acp", "print_cli"})
    return reserved


def validate_catalog(declarations):
    rows = tuple(declarations)
    if len(rows) > MAX_OFFERS or sum(len(row.document.encode("utf-8")) for row in rows) > MAX_CATALOG:
        raise ValueError("provider catalog exceeds its limits")
    reserved = reserved_declaration_names()
    for row in rows:
        names = row.names
        if reserved.intersection(names) or set(row.aliases).intersection({row.id, row.profile_id}):
            raise ValueError(f"provider declaration identifier collision: {row.id}")
        reserved.update(names)
    return rows


def parse_offers(offers):
    if not isinstance(offers, list) or len(offers) > MAX_OFFERS:
        raise ValueError("invalid provider catalog")
    rows = []
    for item in offers:
        _object(item, ("id", "sha256", "document"))
        document = item["document"]
        if not isinstance(document, str) or len(document.encode("utf-8")) > MAX_DOCUMENT:
            raise ValueError("invalid catalog document")
        try:
            raw = document.encode("utf-8")
            row = parse_declaration(raw)
        except UnicodeError as exc:
            raise ValueError("invalid catalog document") from exc
        if item["id"] != row.id:
            raise ValueError("offer id does not match identity.id")
        if item["sha256"] != row.sha256 or document != row.document:
            raise ValueError("catalog declaration pin does not match")
        rows.append(row)
    return validate_catalog(rows)
