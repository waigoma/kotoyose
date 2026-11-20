"""Provider CLI を host の設定・filesystem から隔離する。"""

from __future__ import annotations

import os
import shutil
import stat
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import NoReturn


_BINARY_ENV = "KOTOYOSE_NATIVE_CLAUDE_BINARY"
_CREDENTIAL_KEYS = (
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
)
_FIXED_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def _trusted_binary(value: str) -> Path:
    """管理者設定の binary を canonical な regular executable に限定する。"""
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("KOTOYOSE_NATIVE_CLAUDE_BINARY must be an absolute path")
    try:
        resolved = path.resolve(strict=True)
        mode = resolved.stat().st_mode
    except OSError as exc:
        raise ValueError("KOTOYOSE_NATIVE_CLAUDE_BINARY is unavailable") from exc
    if not stat.S_ISREG(mode) or not os.access(resolved, os.X_OK):
        raise ValueError("KOTOYOSE_NATIVE_CLAUDE_BINARY must be an executable regular file")
    return resolved


def _ro_bind_if_exists(command: list[str], source: str, destination: str) -> None:
    """任意の host 依存ファイルを bind せず、指定済みの存在する入力だけを公開する。"""
    if os.path.exists(source):
        command.extend(("--ro-bind", source, destination))


def build_jail_command(binary: Path, args: Sequence[str], *, provider: str = "claude",
                       rootfs: Path | None = None, egress_socket: Path | None = None,
                       model_catalog: Path | None = None, workspace: Path | None = None,
                       protected_workspace: list[dict] | None = None,
                       session_directory: Path | None = None) -> list[str]:
    """bubblewrap の固定 mount / namespace と provider argv を組み立てる。

    ``binary`` は absolute な regular executable でなければならず、``args`` は
    shell 解釈なしで選択した `/runtime/{provider}` へ渡す。secret は引数へ入らない。
    """
    if provider not in {"claude", "codex"}:
        raise ValueError("unsupported provider jail")
    if protected_workspace is not None and (provider != "codex" or workspace is None):
        raise ValueError("invalid_codex_protected_workspace_entry")
    runtime_binary = f"/runtime/{provider}"
    resolved = _trusted_binary(str(binary))
    if session_directory is not None:
        from .native_sessions import private_directory
        private_directory(session_directory)
    forwarded = list(args)
    if any(not isinstance(value, str) or "\x00" in value for value in forwarded):
        raise ValueError("CLI arguments must be NUL-free strings")

    command = [
        "bwrap",
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        "--cap-drop",
        "ALL",
        # root を空の tmpfs に置き換え、列挙した read-only mount だけを見せる。
        "--tmpfs",
        "/",
    ]
    if provider == "claude":
        command.insert(2, "--share-net")
    else:
        if rootfs is None or egress_socket is None or not egress_socket.is_socket() or model_catalog is None:
            raise ValueError("Codex requires a reviewed rootfs and restricted egress broker")
        bridge = Path(__file__).with_name("codex_egress_bridge.py").resolve(strict=True)
        command.extend(("--ro-bind", str(rootfs), "/", "--tmpfs", "/tmp", "--tmpfs", "/home",
                        "--dir", "/home/kotoyose", "--dir", "/home/kotoyose/.codex", "--tmpfs", "/runtime",
                        "--ro-bind", str(resolved), runtime_binary,
                        "--ro-bind", str(bridge), "/runtime/egress_bridge.py",
                        "--ro-bind", str(egress_socket), "/runtime/egress.sock",
                        "--ro-bind", str(model_catalog), "/runtime/models.json"))
        if session_directory is not None:
            command.extend(("--bind", str(session_directory), "/home/kotoyose/.codex/sessions"))
        if workspace is not None:
            from .codex_coding_policy import validate_workspace
            command.extend(("--ro-bind", str(rootfs / "runtime/codex-resources/bwrap"), "/runtime/codex-resources/bwrap",
                            "--bind", str(validate_workspace(workspace)), "/workspace"))
            if protected_workspace is not None:
                from .codex_coding_policy import protected_workspace_entries
                for entry in protected_workspace_entries(workspace, protected_workspace):
                    name = entry["name"]
                    command.extend(("--ro-bind", str(workspace / name), f"/workspace/{name}"))
        command.extend(("--proc", "/proc", "--dev", "/dev",
                        "--chdir", "/workspace" if workspace is not None else "/tmp", "--",
                        "/usr/bin/python3", "/runtime/egress_bridge.py", *forwarded))
        return command
    for path in ("/usr", "/bin", "/lib", "/lib64"):
        _ro_bind_if_exists(command, path, path)
    command.extend(("--tmpfs", "/etc", "--dir", "/etc"))
    _ro_bind_if_exists(command, "/etc/ssl", "/etc/ssl")
    _ro_bind_if_exists(command, "/etc/resolv.conf", "/etc/resolv.conf")
    _ro_bind_if_exists(command, "/etc/hosts", "/etc/hosts")
    command.extend(
        (
            "--proc", "/proc",
            "--dev", "/dev",
            "--tmpfs", "/tmp",
            "--tmpfs", "/home",
            "--dir", "/home/kotoyose",
            "--dir", "/runtime",
            "--ro-bind", str(resolved), runtime_binary,
            "--chdir", "/tmp",
            "--",
            runtime_binary,
            *forwarded,
        )
    )
    if session_directory is not None:
        # /tmpと/homeのtmpfsを作った後で、SDK領域だけを同じpathへbindする。
        position = command.index('--')
        command[position:position] = ['--bind', str(session_directory), str(session_directory)]
    return command


def provider_environment(source: Mapping[str, str], *, provider: str = "claude") -> dict[str, str]:
    """provider process 用の固定環境を作り、credential と明示 endpoint だけを引き継ぐ。

    ``source`` は通常 ``os.environ`` だが、返却値には allowlist の値だけを含める。
    空文字は SDK による host credential の解除として扱い、非空の値が一つだけ必要。
    """
    if provider not in {"claude", "codex"}:
        raise ValueError("unsupported provider environment")
    credential_keys = _CREDENTIAL_KEYS if provider == "claude" else ("OPENAI_API_KEY", "KOTOYOSE_CODEX_BROKER_CAPABILITY")
    present = [key for key in credential_keys if source.get(key)]
    if len(present) != 1:
        raise ValueError("exactly one provider credential must be configured")
    credential_key = present[0]
    credential = source[credential_key]
    if "\x00" in credential:
        raise ValueError("provider credential contains an invalid character")
    environment = {
        "PATH": _FIXED_PATH,
        "HOME": "/home/kotoyose",
        "LANG": "C.UTF-8",
        credential_key: credential,
    }
    if provider == "codex":
        environment["CODEX_HOME"] = "/home/kotoyose/.codex"
        environment["OPENAI_BASE_URL"] = "http://127.0.0.1:18080/v1"
        return environment
    environment["CLAUDE_CODE_ENTRYPOINT"] = "sdk-py"
    base_url = source.get("ANTHROPIC_BASE_URL")
    if base_url:
        if "\x00" in base_url:
            raise ValueError("provider base URL contains an invalid character")
        environment["ANTHROPIC_BASE_URL"] = base_url
    return environment


def main() -> NoReturn:
    """設定済み provider CLI を jail 内で exec し、launcher を process に置き換える。"""
    binary_value = os.environ.get(_BINARY_ENV)
    if not binary_value:
        raise ValueError("KOTOYOSE_NATIVE_CLAUDE_BINARY is required")
    binary = _trusted_binary(binary_value)
    command = build_jail_command(binary, sys.argv[1:])
    environment = provider_environment(os.environ)
    if os.environ.get('KOTOYOSE_NATIVE_CLAUDE_SESSION_ENABLED') == '1':
        state = Path(os.environ.get('CLAUDE_CONFIG_DIR', ''))
        if (not state.is_absolute() or not (state.name.startswith('claude-resume-')
                or state.name == 'session-config' and state.parent.name.startswith('kotoyose-native-claude-'))):
            raise ValueError('invalid_native_session_directory')
        command = build_jail_command(binary, sys.argv[1:], session_directory=state)
        environment['CLAUDE_CONFIG_DIR'] = str(state)
    bwrap = shutil.which("bwrap")
    if not bwrap:
        raise RuntimeError("bubblewrap (bwrap) is required")
    os.execve(bwrap, command, environment)
    raise RuntimeError("bubblewrap exec returned")
