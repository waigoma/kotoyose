"""子機 turn の送受信で共有する route/preset の投影とログイン先の解決。"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import sys

from .agent_spec import driver_manifest
from .print_cli_profiles import get_print_cli_profile
from .provider_spec import ChildLoginHome, ChildTurnSpec
from .registry import registry


@dataclass(frozen=True, slots=True)
class ChildProvider:
    """driver または CLI profile から解決した子機での製品契約。"""

    id: str
    name: str
    turn: ChildTurnSpec


def child_provider(route: dict) -> ChildProvider | None:
    """宣言がある driver/profile の組だけを返す。legacy route は通常の設定検証に委ねる。"""
    driver = str(route.get("driver") or "")
    if spec := registry.provider_for_driver(driver):
        return ChildProvider(spec.id, spec.display_name, spec.child_turn)
    if not registry.has_driver(driver) or driver_manifest(driver).unmapped_backend != "print_cli":
        return None
    profile_id = route.get("profile")
    if not isinstance(profile_id, str):
        return None
    try:
        profile = get_print_cli_profile(profile_id)
    except KeyError:
        return None
    if profile.child_turn is None or (driver_manifest(driver).driver == "cli.acp" and profile.acp is None):
        return None
    return ChildProvider(profile.provider, profile.display_name or profile.provider, profile.child_turn)


def local_login_home(spec: ChildLoginHome) -> str:
    """CLI が使う OS ユーザーの通常のパスを環境だけから求める。"""
    explicit = os.environ.get(spec.env) or ""
    if spec.inject_config_dir:
        explicit = explicit.strip()
    if explicit:
        return explicit
    if sys.platform == "win32":
        profile = (os.environ.get("USERPROFILE") or "").strip() or str(Path.home())
        return profile.rstrip("\\/") + "\\" + spec.default_subdir
    return str(Path.home() / spec.default_subdir)


def bind_login_home(connection: dict, spec: ChildLoginHome | None) -> str | None:
    """必要な場合だけ接続に設定先を補い、温存の照合に使うパスを返す。"""
    if spec is None:
        return None
    home = local_login_home(spec)
    if spec.inject_config_dir:
        connection["config_dir"] = home
    return home


def use_local_cli_login(connection: dict) -> dict:
    """単独の SDK 接続へ、宣言されたローカルログイン先を補う。"""
    spec = registry.provider(connection.get("provider"))
    if spec is not None and connection.get("kind") == spec.child_turn.connection_kind:
        bind_login_home(connection, spec.child_turn.login_home)
    return connection
