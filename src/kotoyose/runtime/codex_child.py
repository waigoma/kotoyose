"""既存 Codex SDK runtime の child 専用起動設定。"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from typing import Any

import structlog
from openai_codex import AsyncCodex, CodexConfig
from openai_codex.generated.v2_all import ConfigReadResponse

from ..child_codex import ChildCodexLaunch
from ..child_process_tree import TURN_NEW_SESSION
from .base import RuntimeResult, RuntimeTurnError

log = structlog.get_logger()
_START_ERROR = "Codex app-server startup failed"
_MCP_MISMATCH = "Codex MCP configuration differs from the child policy"
_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")
_MCP_FIELDS = {
    "command": None, "args": [], "env": {}, "env_vars": [], "disabled_tools": [], "enabled": True,
}

# Codex の env_vars は名前を変えずに引き渡す。MCP 起動時に戻すことで、同じ変数名を
# サーバーごとに使えて、app-server 自体の環境も上書きしない。
_MCP_ENV_LAUNCHER = """\
import json, os, subprocess, sys
names = json.loads(sys.argv[1])
env = os.environ.copy()
values = {name: env.pop(source) for name, source in names.items()}
env.update(values)
argv = sys.argv[2:]
if os.name == 'nt':
    sys.exit(subprocess.call(argv, env=env))
os.execvpe(argv[0], argv, env)
"""


def _mcp_config(servers: dict[str, dict]) -> tuple[dict[str, dict], dict[str, str]]:
    config: dict[str, dict] = {}
    env: dict[str, str] = {}
    for index, (name, server) in enumerate(servers.items()):
        if server.get("type") == "http":
            from ..child_mcp_proxy import AUTH_ENV, authorization_header, relay_process_env

            headers = dict(server.get("headers") or {})
            entry = {
                "url": server["url"], "enabled": True,
                "disabled_tools": server.get("disabled_tools", []),
                # The child owner approved this server (host publication or child policy). Codex runs
                # with approval_policy never, so without this every MCP tool call is declined.
                "default_tools_approval_mode": "approve",
            }
            # The loopback bearer is an env var name on the command line. Codex adds "Bearer ".
            if headers.get("Authorization") == authorization_header() and AUTH_ENV in relay_process_env():
                headers.pop("Authorization")
                entry["bearer_token_env_var"] = AUTH_ENV
                env[AUTH_ENV] = relay_process_env()[AUTH_ENV]
            if headers:
                entry["http_headers"] = headers
            config[name] = entry
            continue
        command, args = server["command"], server.get("args", [])
        names = {}
        for offset, (key, value) in enumerate((server.get("env") or {}).items()):
            source = f"KOTOYOSE_CHILD_MCP_{index}_{offset}"
            names[key] = source
            env[source] = value
        if names:
            args = ["-I", "-c", _MCP_ENV_LAUNCHER, json.dumps(names), command, *args]
            command = sys.executable
        config[name] = {
            "command": command, "args": args, "env": {}, "env_vars": list(names.values()),
            "disabled_tools": server.get("disabled_tools", []), "enabled": True,
            "default_tools_approval_mode": "approve",
        }
    return config, env


def _inline_toml(value: Any) -> str:
    if isinstance(value, dict):
        return "{" + ", ".join(f"{_inline_toml(key)}={_inline_toml(item)}" for key, item in value.items()) + "}"
    return json.dumps(value, ensure_ascii=False)


class LocalMcpToDisable(Exception):
    """child のローカルの Codex 設定に、方針に無い MCP がある。その turn では無効にして起動し直す。"""

    def __init__(self, names: tuple[str, ...]) -> None:
        super().__init__("local Codex MCP servers outside the child policy")
        self.names = names


def child_sdk_config(
    launch: ChildCodexLaunch, cwd: str, overrides: tuple[str, ...], servers: dict[str, dict],
    disabled: tuple[str, ...] = (),
) -> CodexConfig:
    """ローカルのログイン環境を継承し、方針の MCP と公式 provider で app-server を起動する。

    `disabled` は、ローカルの config.toml にあって方針に無い MCP の名前。その turn では起動させない。
    """
    mcp, env = _mcp_config(servers)
    # 方針の表 (mcp_servers={...}) の後に置く。前に置くと表の上書きで無効化が消える。
    # codex の -c はキーの引用符を受け付けない (app-server が応答しなくなる)。素のキーだけを使う。
    overrides += (
        f"mcp_servers={_inline_toml(mcp)}",
        *(f"mcp_servers.{name}.enabled=false" for name in disabled),
        "features.plugins=false", "features.apps=false", 'model_provider="openai"',
    )
    if launch.bundled:
        return CodexConfig(cwd=cwd, config_overrides=overrides, env=env or None)
    argv = list(launch.argv)
    for override in overrides:
        argv.extend(("--config", override))
    argv.extend(("app-server", "--listen", "stdio://"))
    return CodexConfig(cwd=cwd, launch_args_override=tuple(argv), env=env or None)


async def _read_effective_mcp(client: AsyncCodex, cwd: str) -> Any:
    # openai-codex 0.170.0 の公開 API には AsyncCodex の接続で config/read を呼ぶ手段がない。
    transport = getattr(client, "_client", None)
    if not callable(getattr(transport, "start", None)) or not callable(getattr(transport, "request", None)):
        log.warning("child.codex.sdk_transport_changed", expected="AsyncCodex transport for config/read")
        raise RuntimeError("openai-codex changed AsyncCodex transport for config/read")

    # SDK の worker thread が Popen を終えるまで、取消時にもプロセス追跡を残す。
    token = TURN_NEW_SESSION.set(True)
    try:
        start = asyncio.create_task(transport.start())
        try:
            await asyncio.shield(start)
        except asyncio.CancelledError:
            await asyncio.gather(start, return_exceptions=True)
            raise
    finally:
        TURN_NEW_SESSION.reset(token)

    await client.__aenter__()
    response = await transport.request("config/read", {"cwd": cwd}, response_model=ConfigReadResponse)
    return response.config.model_dump().get("mcp_servers", {})


def _local_extras(effective: Any, expected: dict[str, dict]) -> tuple[str, ...]:
    """方針に無く、まだ有効なローカルの MCP の名前。"""
    if not isinstance(effective, dict):
        return ()
    return tuple(sorted(
        name for name, server in effective.items()
        if name not in expected and not (isinstance(server, dict) and server.get("enabled") is False)
    ))


def _matches_mcp_policy(effective: Any, expected: dict[str, dict]) -> bool:
    if effective is None:
        effective = {}
    if not isinstance(effective, dict):
        return False
    # 方針に無いローカルの MCP は、無効になっていれば起動しないので照合から外す。
    effective = {
        name: server for name, server in effective.items()
        if name in expected or not (isinstance(server, dict) and server.get("enabled") is False)
    }
    if effective.keys() != expected.keys():
        return False
    for name, server in effective.items():
        if not isinstance(server, dict):
            return False
        wanted = expected[name]
        if "url" in wanted:
            # These change which process or credential the server uses. Allow them only when the
            # policy itself names that exact value (bearer_token_env_var / env_http_headers).
            for key in ("command", "bearer_token_env_var", "env_http_headers", "enabled_tools", "http_headers"):
                if key not in wanted and server.get(key):
                    return False
            defaults = {"enabled": True, "disabled_tools": []}
        else:
            if server.get("url") is not None:
                return False
            defaults = _MCP_FIELDS
        actual = {key: server.get(key) if server.get(key) is not None else defaults.get(key) for key in wanted}
        if actual != wanted:
            return False
    return True


async def start_child_sdk(
    client: AsyncCodex, cwd: str, servers: dict[str, dict], *, disabled: tuple[str, ...] = (),
) -> None:
    """thread の起動前に、cwd に適用される MCP 設定を照合する。

    ローカルの config.toml に方針に無い MCP があれば LocalMcpToDisable を投げ、呼び出し側が
    それを無効にして 1 度だけ起動し直す。無効にした後も方針と違えば turn を断る。
    """
    try:
        effective = await _read_effective_mcp(client, cwd)
    except Exception as exc:
        # argv、stderr、config/read の応答を含む例外があるため、例外型と OS のエラー番号だけ記録する。
        log.warning("child.codex.startup_failed", error_type=type(exc).__name__, errno=exc.errno if isinstance(exc, OSError) else None)
        raise RuntimeTurnError(_START_ERROR, partial_result=RuntimeResult(text="", provider="codex")) from None
    expected, _ = _mcp_config(servers)
    extras = _local_extras(effective, expected)
    if extras and not disabled:
        unsupported = [name for name in extras if not _BARE_KEY.fullmatch(name)]
        if unsupported:
            # 素のキーで書けない名前は -c で無効にできない。
            raise RuntimeTurnError(
                f"{_MCP_MISMATCH}: local MCP servers that cannot be disabled: {', '.join(unsupported)}",
                partial_result=RuntimeResult(text="", provider="codex"),
            )
        raise LocalMcpToDisable(extras)
    if not _matches_mcp_policy(effective, expected):
        raise RuntimeTurnError(_MCP_MISMATCH, partial_result=RuntimeResult(text="", provider="codex"))
