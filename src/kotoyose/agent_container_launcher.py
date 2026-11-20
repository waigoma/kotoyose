"""Agent CLI を container 内の ``docker exec`` へ置き換える provider launcher。

provider SDK の ``cli_path`` から ``python -m kotoyose.agent_container_launcher`` として
呼ばれる。標準ライブラリだけに依存する。下に複製した名前と規則は
``kotoyose.agent_container`` の同名定義と一致させること (テストで検査する)。

設定は環境変数で受け取る。
- KOTOYOSE_AGENT_CONTAINER: ``kotoyose-agent-<job_id>`` 形式の container 名 (必須)
- KOTOYOSE_AGENT_BINARY: container 内の絶対パス (必須)
- KOTOYOSE_AGENT_SECRET_ENV: 転送する環境変数名のカンマ区切り (任意、値は launcher の環境から取る)
- KOTOYOSE_AGENT_ENV: 非 secret の環境変数を表す JSON object (任意)
- KOTOYOSE_DOCKER_BINARY: docker 実行ファイル (任意、既定 docker)
- KOTOYOSE_AGENT_WORKDIR: container 内の作業ディレクトリ (任意、絶対 path)。SDK の cwd は host の path なので使えない
- KOTOYOSE_AGENT_RELAY_SOCKET: container 内の broker socket の絶対 path (任意)。指定時は
  ``python3 /opt/agent/egress_relay.py <socket> -- <binary> <args>`` を container 内で起動し、
  CLI の HTTP を 127.0.0.1:18080 から broker へ渡す
"""

from __future__ import annotations

import errno
import json
import os
import re
import sys
from collections.abc import Mapping, Sequence

# kotoyose.agent_container と同じ定義。テストで一致を検査する。
CONTAINER_PREFIX = "kotoyose-agent-"
JOB_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SECRET_NAME_RE = re.compile(
    r"TOKEN|KEY|SECRET|PASSWORD|PASSWD|AUTH|CRED|COOKIE|BEARER|DSN|PRIVATE",
    re.IGNORECASE,
)
_RESERVED_SECRET_NAMES = frozenset({"PATH"})
_RESERVED_SECRET_PREFIX = "DOCKER_"

EXIT_BAD_CONFIG = 78
EXIT_NOT_FOUND = 127
EXIT_NOT_EXECUTABLE = 126
_DEFAULT_DOCKER = "docker"
# container 内の relay の固定 path。kotoyose.container_claude の ro_files の target と一致させる。
RELAY_PROGRAM = "/opt/agent/egress_relay.py"


def is_secret_env_name(name: str) -> bool:
    return _SECRET_NAME_RE.search(name) is not None


class LauncherConfigError(Exception):
    """設定が不正。メッセージには環境変数の値を含めない。"""


def build_exec_argv(environ: Mapping[str, str], agent_args: Sequence[str]) -> list[str]:
    """``docker exec`` の argv を組み立てる。secret の値は含めない。"""
    container = environ.get("KOTOYOSE_AGENT_CONTAINER", "")
    if not container.startswith(CONTAINER_PREFIX) or JOB_ID_RE.fullmatch(container[len(CONTAINER_PREFIX):]) is None:
        raise LauncherConfigError("KOTOYOSE_AGENT_CONTAINER is missing or invalid")

    binary = environ.get("KOTOYOSE_AGENT_BINARY", "")
    if not binary.startswith("/") or any(c in binary for c in ("\x00", "\n", "\r")):
        raise LauncherConfigError("KOTOYOSE_AGENT_BINARY must be an absolute path inside the container")

    docker = environ.get("KOTOYOSE_DOCKER_BINARY") or _DEFAULT_DOCKER
    if docker.startswith("-") or any(c in docker for c in ("\x00", "\n", "\r")):
        raise LauncherConfigError("KOTOYOSE_DOCKER_BINARY is invalid")

    secret_names = _parse_secret_names(environ, environ.get("KOTOYOSE_AGENT_SECRET_ENV", ""))
    plain_env = _parse_plain_env(environ.get("KOTOYOSE_AGENT_ENV", ""))
    if set(secret_names) & set(plain_env):
        raise LauncherConfigError("a name cannot be both plain env and secret env")
    workdir = _parse_absolute_path(environ.get("KOTOYOSE_AGENT_WORKDIR", ""), "KOTOYOSE_AGENT_WORKDIR")
    relay_socket = _parse_absolute_path(environ.get("KOTOYOSE_AGENT_RELAY_SOCKET", ""), "KOTOYOSE_AGENT_RELAY_SOCKET")

    argv = [docker, "exec", "-i"]
    if workdir is not None:
        argv.extend(("--workdir", workdir))
    for key in sorted(plain_env):
        argv.extend(("-e", f"{key}={plain_env[key]}"))
    for name in secret_names:
        # docker exec は -e NAME を client 側の環境変数から読む。値は argv に出ない。
        argv.extend(("-e", name))
    argv.append(container)
    if relay_socket is not None:
        argv.extend(("python3", RELAY_PROGRAM, relay_socket, "--"))
    argv.extend((binary, *agent_args))
    return argv


def _parse_absolute_path(value: str, name: str) -> str | None:
    """空なら未指定。指定時は container 内の絶対 path だけを受け付ける。"""
    if not value:
        return None
    if not value.startswith("/") or any(c in value for c in ("\x00", "\n", "\r")):
        raise LauncherConfigError(f"{name} must be an absolute path inside the container")
    return value


def _parse_secret_names(environ: Mapping[str, str], raw: str) -> list[str]:
    names: list[str] = []
    for item in raw.split(","):
        name = item.strip()
        if not name:
            continue
        if ENV_NAME_RE.fullmatch(name) is None:
            raise LauncherConfigError("KOTOYOSE_AGENT_SECRET_ENV contains an invalid name")
        if name in _RESERVED_SECRET_NAMES or name.startswith(_RESERVED_SECRET_PREFIX):
            raise LauncherConfigError("KOTOYOSE_AGENT_SECRET_ENV contains a name reserved for the docker client")
        if name not in environ:
            raise LauncherConfigError("a secret env name is not set in the launcher environment")
        if name not in names:
            names.append(name)
    return names


def _parse_plain_env(raw: str) -> dict[str, str]:
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise LauncherConfigError("KOTOYOSE_AGENT_ENV must be a JSON object") from exc
    if not isinstance(decoded, dict):
        raise LauncherConfigError("KOTOYOSE_AGENT_ENV must be a JSON object")
    for key, value in decoded.items():
        if not isinstance(key, str) or ENV_NAME_RE.fullmatch(key) is None:
            raise LauncherConfigError("KOTOYOSE_AGENT_ENV contains an invalid name")
        if is_secret_env_name(key):
            raise LauncherConfigError("KOTOYOSE_AGENT_ENV must not contain secret-looking names; use KOTOYOSE_AGENT_SECRET_ENV")
        if not isinstance(value, str) or "\x00" in value:
            raise LauncherConfigError("KOTOYOSE_AGENT_ENV values must be strings")
    return decoded


def main(argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    """成功時は execvp で置き換わり戻らない。設定不正は 78、起動失敗は 127 / 126 を返す。"""
    env = os.environ if environ is None else environ
    agent_args = list(sys.argv[1:] if argv is None else argv)
    try:
        exec_argv = build_exec_argv(env, agent_args)
    except LauncherConfigError as exc:
        print(f"agent launcher: {exc}", file=sys.stderr)
        return EXIT_BAD_CONFIG
    try:
        os.execvp(exec_argv[0], exec_argv)
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            print("agent launcher: docker not found", file=sys.stderr)
            return EXIT_NOT_FOUND
        print("agent launcher: docker could not be executed", file=sys.stderr)
        return EXIT_NOT_EXECUTABLE
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
