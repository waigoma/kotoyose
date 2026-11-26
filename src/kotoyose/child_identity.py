"""child と host の鍵、自己署名証明書、方針ファイル。

方針ファイルは child のディレクトリにだけ作る。host の台帳はこのモジュールでは書かない。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import ipaddress
import json
import os
import re
from pathlib import Path
import ssl
import sys

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from .config_paths import default_data_dir
from .child_local_llm import LOCAL_LLM_UI_PORT_FILE, LocalLLM, parse_local_llm_policy
from .provider_spec import CODEX_SANDBOX_CAP
from .remote_worker_identity import certificate_file_fingerprint, certificate_fingerprint

CHILD_HOME_ENV = "KOTOYOSE_CHILD_HOME"
HOST_HOME_ENV = "KOTOYOSE_CHILD_HOST_HOME"
_OPTIONAL_POLICY_KEYS = {"logins", "local_llm", CODEX_SANDBOX_CAP.policy_key}
_POLICY_KEYS = {"workspace_roots", "adapters", "mcp"} | _OPTIONAL_POLICY_KEYS
_DATA_MCP_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*")


def valid_data_mcp_name(name: object) -> bool:
    """データ宣言の MCP 名から、ACP ツール名の区切り文字を除外する。"""
    return isinstance(name, str) and _DATA_MCP_NAME.fullmatch(name) is not None


def default_child_home(environ: Mapping[str, str] | None = None) -> Path:
    """Linux は `${KOTOYOSE_CHILD_HOME:-~/.local/share/kotoyose-child}`。

    Windows は `%LOCALAPPDATA%\\kotoyose-child`。LOCALAPPDATA が空なら
    `%USERPROFILE%\\AppData\\Local\\kotoyose-child`。
    """
    env = os.environ if environ is None else environ
    explicit = (env.get(CHILD_HOME_ENV) or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    if sys.platform == "win32":
        local = (env.get("LOCALAPPDATA") or "").strip()
        if local:
            return Path(local) / "kotoyose-child"
        profile = (env.get("USERPROFILE") or "").strip()
        base = Path(profile) if profile else Path.home()
        return base / "AppData" / "Local" / "kotoyose-child"
    home = (env.get("HOME") or "").strip()
    base = Path(home).expanduser() if home else Path.home()
    return base / ".local" / "share" / "kotoyose-child"


def default_host_child_home(environ: Mapping[str, str] | None = None) -> Path:
    """host の証明書と child 台帳。`KOTOYOSE_CHILD_HOST_HOME` が無ければ data dir の `child`。"""
    env = os.environ if environ is None else environ
    explicit = (env.get(HOST_HOME_ENV) or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    return default_data_dir(env) / "child"


def parse_host_address(address: str) -> tuple[str, int]:
    """`host:port` または `[ipv6]:port`。"""
    text = address.strip()
    if text.startswith("["):
        host, separator, port_text = text[1:].partition("]:")
        if separator != "]:" or not host:
            raise ValueError("address must be host:port")
    else:
        host, separator, port_text = text.rpartition(":")
        if separator != ":" or not host:
            raise ValueError("address must be host:port")
    if not port_text.isdigit():
        raise ValueError("address must be host:port")
    port = int(port_text)
    if not 1 <= port <= 65535:
        raise ValueError("address must be host:port")
    return host, port


def _chmod_private(path: Path, mode: int) -> None:
    """Unix の mode。Windows では st_mode が ACL を表さないので、user profile を境界にして失敗させない。"""
    if sys.platform == "win32":
        return
    path.chmod(mode)


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _chmod_private(path, 0o700)


def _write_private(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    _chmod_private(path, 0o600)


def _generate_certificate(common_name: str) -> tuple[bytes, bytes]:
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=825))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName("localhost"),
                x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
            ]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    cert_pem = certificate.public_bytes(serialization.Encoding.PEM)
    return key_pem, cert_pem


def _material(directory: Path, common_name: str, key_name: str, cert_name: str) -> tuple[Path, Path, str, str]:
    _ensure_dir(directory)
    key_file = directory / key_name
    cert_file = directory / cert_name
    if not key_file.is_file() or not cert_file.is_file():
        key_pem, cert_pem = _generate_certificate(common_name)
        _write_private(key_file, key_pem)
        cert_file.write_bytes(cert_pem)
        _chmod_private(cert_file, 0o644)
    cert_pem_text = cert_file.read_text(encoding="ascii")
    fingerprint = certificate_file_fingerprint(cert_file)
    return key_file, cert_file, fingerprint, cert_pem_text


@dataclass(frozen=True, slots=True)
class ChildIdentity:
    home: Path
    key_file: Path
    certificate_file: Path
    fingerprint: str
    cert_pem: str


@dataclass(frozen=True, slots=True)
class HostIdentity:
    home: Path
    key_file: Path
    certificate_file: Path
    secret: bytes
    fingerprint: str
    cert_pem: str


def ensure_child_identity(home: Path) -> ChildIdentity:
    # OpenSSL の trust store は subject が同じ証明書を 1 枚しか置かない。
    # 再登録のたびに新しい証明書を足すので、CN は発行ごとに違う。
    key_file, cert_file, fingerprint, cert_pem = _material(
        home, f"kotoyose-child-{os.urandom(6).hex()}", "key.pem", "cert.pem",
    )
    return ChildIdentity(home, key_file, cert_file, fingerprint, cert_pem)


def ensure_host_identity(home: Path) -> HostIdentity:
    key_file, cert_file, fingerprint, cert_pem = _material(home, "kotoyose-host", "host-key.pem", "host-cert.pem")
    secret_file = home / "host.secret"
    if secret_file.is_file():
        secret = secret_file.read_bytes()
        if len(secret) != 32:
            raise ValueError("host secret has an unexpected length")
    else:
        secret = os.urandom(32)
        _write_private(secret_file, secret)
    return HostIdentity(home, key_file, cert_file, secret, fingerprint, cert_pem)


def pem_fingerprint(pem: str) -> str:
    der = ssl.PEM_cert_to_DER_cert(pem)
    return certificate_fingerprint(der)


def der_to_pem(der: bytes) -> str:
    return ssl.DER_cert_to_PEM_cert(der)


@dataclass(frozen=True, slots=True)
class ChildPolicy:
    workspace_roots: tuple[str, ...]
    adapters: tuple[str, ...]
    mcp: dict
    logins: tuple[str, ...]
    codex_sandbox_max: str = CODEX_SANDBOX_CAP.policy_default
    local_llm: dict[str, LocalLLM] = field(default_factory=dict)


def default_policy_text() -> str:
    return json.dumps(
        {"adapters": [], "logins": [], "mcp": {}, "workspace_roots": []},
        indent=2,
        sort_keys=True,
    ) + "\n"


def ensure_default_policy(home: Path) -> None:
    """未作成のときだけ空の方針ファイルを置く。既存のバイトは変えない。"""
    path = home / "policy.json"
    if path.exists():
        return
    _ensure_dir(home)
    path.write_text(default_policy_text(), encoding="utf-8")


def _backslashes_as_text(text: str) -> str:
    """`\\"` 以外のバックスラッシュを、すべて文字そのものにする (エクスプローラーから貼った path 用)。

    厳密に読めなかった文書は、エスケープを書いていないものとみなす。`\\\\server\\share` の UNC も
    そのまま残る。一部だけエスケープした path は区切りが二重になるが、Windows は二重の区切りを 1 つとして扱う。
    """
    out: list[str] = []
    for index, char in enumerate(text):
        if char == "\\" and not (index + 1 < len(text) and text[index + 1] == '"'):
            out.append("\\\\")
        else:
            out.append(char)
    return "".join(out)


def _has_control_chars(value: object) -> bool:
    if isinstance(value, str):
        return any(ord(char) < 0x20 for char in value)
    if isinstance(value, list):
        return any(_has_control_chars(item) for item in value)
    if isinstance(value, dict):
        return any(_has_control_chars(key) or _has_control_chars(item) for key, item in value.items())
    return False


def parse_policy_text(text: str) -> object:
    """policy.json を読む。Windows の path をエスケープせずに貼ったものも受ける。

    `D:\\work` のような 1 本の `\\` は JSON として読めない。`D:\\tmp` や `C:\\new` は `\\t` `\\n` がタブや改行に
    化けて黙って別の path になる。厳密に読めないとき、または読めても文字列に制御文字が入ったときだけ、
    `\\` を文字そのものとして読み直す。
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return json.loads(_backslashes_as_text(text))
    if _has_control_chars(data):
        try:
            return json.loads(_backslashes_as_text(text))
        except json.JSONDecodeError:
            return data
    return data


def _local_llm_reserved_ports(home: Path) -> tuple[int, ...]:
    ports = []
    try:
        ports.append(parse_host_address(load_host_pin(home).address)[1])
    except ValueError:
        pass
    try:
        raw = (home / LOCAL_LLM_UI_PORT_FILE).read_text(encoding="ascii")
        if re.fullmatch(r"[0-9]{1,5}", raw) is None or not 1 <= (port := int(raw)) <= 65535:
            raise ValueError("invalid settings port")
        ports.append(port)
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        raise ValueError("Child settings UI port is unavailable") from exc
    return tuple(ports)


def load_policy(path: Path) -> ChildPolicy:
    try:
        data = parse_policy_text(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("policy.json is not readable json") from exc
    if not isinstance(data, dict) or set(data) | _OPTIONAL_POLICY_KEYS != _POLICY_KEYS:
        raise ValueError("policy.json must contain workspace_roots, adapters, and mcp (logins, codex_sandbox_max, and local_llm are optional)")
    roots = data["workspace_roots"]
    adapters = data["adapters"]
    mcp = data["mcp"]
    logins = data.get("logins", [])
    codex_sandbox_max = data.get(CODEX_SANDBOX_CAP.policy_key, CODEX_SANDBOX_CAP.policy_default)
    if codex_sandbox_max not in CODEX_SANDBOX_CAP.order:
        raise ValueError("policy.json codex_sandbox_max must be read-only, workspace-write, or danger-full-access")
    if not isinstance(roots, list) or not all(isinstance(item, str) and Path(item).is_absolute() for item in roots):
        raise ValueError("policy.json workspace_roots must be absolute paths")
    if not isinstance(adapters, list) or not all(isinstance(item, str) and item for item in adapters):
        raise ValueError("policy.json adapters must be names")
    if not isinstance(mcp, dict):
        raise ValueError("policy.json mcp must be an object")
    for name, spec in mcp.items():
        if not isinstance(name, str) or not name or not isinstance(spec, dict):
            raise ValueError("policy.json mcp entries must be objects")
        command = spec.get("command")
        args = spec.get("args")
        if not isinstance(command, str) or not command:
            raise ValueError("policy.json mcp command must be a string")
        if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
            raise ValueError("policy.json mcp args must be strings")
        description = spec.get("description")
        if description is not None and not isinstance(description, str):
            raise ValueError("policy.json mcp description must be a string")
        env = spec.get("env")
        if env is not None and (
            not isinstance(env, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in env.items())
        ):
            raise ValueError("policy.json mcp env must be strings")
    if (
        not isinstance(logins, list)
        or not all(isinstance(item, str) and 0 < len(item) <= 64 and not any(char.isspace() or char == "\x00" for char in item) for item in logins)
    ):
        raise ValueError("policy.json logins must be provider names")
    if len({item.casefold() for item in logins}) != len(logins):
        raise ValueError("policy.json logins must be unique")
    entries = data.get("local_llm", {})
    local_llm = parse_local_llm_policy(entries, reserved_ports=_local_llm_reserved_ports(path.parent) if entries else ())
    return ChildPolicy(tuple(roots), tuple(adapters), dict(mcp), tuple(logins), codex_sandbox_max, local_llm)


@dataclass(frozen=True, slots=True)
class HostPin:
    address: str
    fingerprint: str
    name: str
    host_cert_pem: str


def write_host_pin(home: Path, pin: HostPin) -> None:
    _ensure_dir(home)
    _write_private(
        home / "host.json",
        json.dumps(
            {
                "address": pin.address,
                "fingerprint": pin.fingerprint,
                "host_cert_pem": pin.host_cert_pem,
                "name": pin.name,
            },
            indent=2,
            sort_keys=True,
        ).encode(),
    )


def load_host_pin(home: Path) -> HostPin:
    path = home / "host.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ChildRejectedNotEnrolled() from exc
    if not isinstance(data, dict):
        raise ChildRejectedNotEnrolled()
    address = data.get("address")
    fingerprint = data.get("fingerprint")
    name = data.get("name")
    pem = data.get("host_cert_pem")
    if not all(isinstance(item, str) and item for item in (address, fingerprint, name, pem)):
        raise ChildRejectedNotEnrolled()
    return HostPin(address, fingerprint, name, pem)


class ChildRejectedNotEnrolled(ValueError):
    def __init__(self):
        super().__init__("child is not enrolled")
