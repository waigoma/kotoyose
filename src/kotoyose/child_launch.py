"""子機宣言が選べる起動操作と、SDK を必要時だけ読み込む builder。"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from .agent_spec import DRIVERS as _DRIVERS  # 組み込み driver とプロバイダーを先に登録する。
from .provider_spec import ChildLaunch
from .registry import registry

if TYPE_CHECKING:
    from .config import ModelPreset


def _claude_sdk_path(home: Path) -> Path:
    from .claude_cli import child_claude_cli

    return child_claude_cli(home)


def _codex_effective_config_launch(home: Path):
    from .child_codex import resolve_child_codex

    return resolve_child_codex(home)


_LAUNCH_RESOLVERS = {
    ChildLaunch.CLAUDE_AGENT_SDK_PATH: _claude_sdk_path,
    ChildLaunch.CODEX_EFFECTIVE_CONFIG_CHECK: _codex_effective_config_launch,
}


def child_builder_options(home: Path, launch: ChildLaunch | None) -> dict:
    """選ばれたローカル CLI を解決し、既存 builder が受け取る引数にする。"""
    if launch is None:
        return {}
    return {launch.value: _LAUNCH_RESOLVERS[launch](home)}


def sdk_cli_prefix(home: Path, launch: ChildLaunch) -> tuple[str, ...]:
    """子機 turn と同じローカル CLI を、ログインと状態確認の argv に使う。"""
    resolved = _LAUNCH_RESOLVERS[launch](home)
    return (str(resolved),) if isinstance(resolved, Path) else resolved.argv


async def _build_claude(alias: str, preset: ModelPreset, cwd: str, *, cli_path: Path | None = None, resume_id: str | None = None):
    from .claude_cli import use_claude_cli
    from .config import get_config
    from .session import _build_claude_runtime_session

    config = get_config()
    with use_claude_cli(cli_path):
        return await _build_claude_runtime_session(
            alias, preset, None, config, config, cwd, None, {}, resume_id, alias, None,
        )


def _build_codex(_alias: str, preset: ModelPreset, cwd: str, *, resume_id: str | None = None, **options):
    from .runtime.codex import build_codex_session

    return build_codex_session(preset, cwd, resume_id, None, skill_folders=(), **options)


registry.register_builtin_child_builder("claude", _build_claude)
registry.register_builtin_child_builder("codex", _build_codex)
