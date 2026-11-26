"""組み込み宣言の、子機 UI とインストーラー向けの投影。"""

from dataclasses import asdict
import hashlib
import json

from .print_cli_profiles import builtin_print_cli_profiles, list_print_cli_profiles
from .provider_login import PrintCliLoginSpec
from .provider_spec import BUILTIN_PROVIDERS, LocalLogin


def _login(binary: str, name: str, spec: PrintCliLoginSpec | None, flow: str | None, local: LocalLogin) -> dict | None:
    if spec is None:
        return None
    return {
        "command": [binary, *spec.command],
        "cli_name": name,
        "flow": flow,
        "transports": dict(local.transports),
        "status": spec.status_command,
    }


def component_rows() -> list[dict]:
    """SDK と CLI の導入・表示・ログイン情報を登録順で返す。"""
    rows = []
    for provider in BUILTIN_PROVIDERS:
        install = {key: value for key, value in asdict(provider.install).items() if value is not None}
        rows.append({
            "id": provider.id, "name": provider.display_name, "initial": provider.initial, "tone": provider.tone,
            "aliases": provider.aliases, "driver": provider.drivers[0],
            **install,
            "login": _login(
                provider.install.cli, provider.install.cli_name, provider.login.spec, provider.login.flow, provider.login.local,
            ),
            "quota": bool(provider.login.quota_command),
        })
    for profile in builtin_print_cli_profiles():
        # CLI の操作では製品 ID と profile ID の組を使う。
        install = profile.install
        managed = {
            "version": install.version,
            "platforms": [artifact.platform for artifact in install.artifacts
                          if artifact.os == "linux" and artifact.digest != "unset"],
        } if install else None
        name = profile.display_name or profile.provider
        rows.append({
            "id": profile.id, "provider": profile.provider, "name": name, "initial": name[0].upper(), "tone": "cli",
            "aliases": (), "driver": "cli.acp" if profile.acp is not None else "cli.print", "profile": profile.id,
            "packages": (), "cli": profile.binary, "windows_cli": (f"{profile.binary}.exe",),
            "managed": managed,
            "login": _login(profile.binary, name, profile.login, profile.login_flow, profile.child_local_login),
            "quota": bool(profile.quota_rpc or (profile.quota_command and profile.parse_quota)),
        })
    return rows


def catalog_document(rows: list[dict]) -> dict:
    """Runner と Rust が照合する schema 版と、行の内容に対応する識別子を付ける。"""
    encoded = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
    return {"version": f"1:{hashlib.sha256(encoded).hexdigest()}", "providers": rows}


def host_connections() -> dict:
    """ホストの接続フォームとログイン案内に必要な公開情報を返す。"""
    connections = {}
    for provider in BUILTIN_PROVIDERS:
        row = {"child_login": bool(provider.login.spec.command)}
        # 設定ディレクトリーを使う SDK には、公式 CLI の対話ログインも案内する。
        login_home = provider.child_turn.login_home
        if login_home and login_home.inject_config_dir:
            row["terminal"] = {
                "env_paths": {login_home.env: "."}, "env": {},
                "argv": [provider.install.cli], "follow_up": "/login",
            }
        connections.update({name: row for name in (provider.id, *provider.aliases)})
    for profile in list_print_cli_profiles():
        if profile.data_declaration is not None:
            connections[profile.provider] = {"profile": profile.id, "child_login": False, "data_declaration": True}
            continue
        row = {
            "profile": profile.id,
            "login_home": f"~/.local/share/kotoyose/{profile.id}-home",
            "child_login": bool(profile.login and profile.login_flow),
        }
        if profile.login and profile.terminal_login != "hidden":
            interactive = profile.terminal_login == "interactive"
            row["terminal"] = {
                "env_paths": {profile.login_home_env: ".", **profile.login_home_env_paths},
                "env": {} if interactive else dict(profile.login_env),
                "argv": [profile.binary, *(() if interactive else profile.login.command)],
                "follow_up": "/usage" if interactive else None,
            }
        connections[profile.provider] = row
    return connections


if __name__ == "__main__":
    print(json.dumps(catalog_document(component_rows()), indent=2))
