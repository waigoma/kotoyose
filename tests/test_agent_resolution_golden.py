"""本番設定の写しに対する agent 解決を黄金値で固定する。

挙動は変えない。各 models alias について、今の解決器が返す
backend・codex_execution_mode・execution_environment・codex_sandbox・機械の材料と、
agent_spec の driver 表が返す道具・場所を golden.json と比較する。材料は旧 profile 写像が渡していた rootfs・digest・egress・
CLI・認証と同じ値である。この写しの profile は rootfs を持たない。

再生成 (同じ関数が golden.json を上書きする):

    KOTOYOSE_REGENERATE_AGENT_RESOLUTION_GOLDEN=1 \\
      uv run --extra dev pytest -q -p no:cacheprovider tests/test_agent_resolution_golden.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import yaml

from kotoyose.agent_spec import accepted_runs, rootfs_digest_is_coding
from kotoyose.config import HarnessConfig
from kotoyose.execution_resources import (
    NativeExecutionConfig,
    resolve_agent_model,
)
from kotoyose.provider_jail import _trusted_binary as _real_trusted_binary

FIXTURE = Path(__file__).parent / "fixtures" / "agent_resolution"
GOLDEN = FIXTURE / "golden.json"
REGEN = "KOTOYOSE_REGENERATE_AGENT_RESOLUTION_GOLDEN"

def _offline_claude(value: str) -> Path:
    """CLI の absolute 条件は残し、stat と実行ビットだけを外す。"""
    path = Path(value)
    if not path.is_absolute():
        return _real_trusted_binary(value)
    return path


def _plugin_dir_present(self: Path) -> bool:
    """skill 定義の plugin ディレクトリは解決の入力ではない。欠落で読込を落とさない。"""
    if self.name == "kotoyose-skills" or ".claude-plugin" in self.parts:
        return True
    return _real_is_dir(self)


def _plugin_manifest_present(self: Path) -> bool:
    if self.name == "plugin.json":
        return True
    return _real_is_file(self)


_real_is_dir = Path.is_dir
_real_is_file = Path.is_file


def load_config() -> HarnessConfig:
    """写しの path は書き換えない。plugin の存在確認だけを読込中に外す。"""
    with FIXTURE.joinpath("config.yml").open(encoding="utf-8") as stream:
        data = yaml.safe_load(stream)
    with patch.object(Path, "is_dir", _plugin_dir_present), patch.object(Path, "is_file", _plugin_manifest_present):
        return HarnessConfig.model_validate(data)


def load_settings() -> NativeExecutionConfig:
    data = json.loads(FIXTURE.joinpath("execution.json").read_text(encoding="utf-8"))
    return NativeExecutionConfig.model_validate(data)


def _runs(resolved, settings) -> list[dict[str, str]]:
    """道具と場所は agent_spec の driver 表から導く。黄金値の形は変えない。"""
    return accepted_runs(resolved, agent_container=settings.agent_container is not None)


def _fact(runs: list[dict[str, str]], key: str):
    """1 通りなら文字列、要求で分かれるなら同じ添字の配列にする。"""
    values = [item[key] for item in runs]
    if len(values) == 1:
        return values[0]
    return values


def _blank_materials() -> dict:
    return {
        "auth_file": None,
        "cli_binary": None,
        "codex_egress_limits": None,
        "credential_mode": None,
        "rootfs": None,
        "rootfs_coding": None,
        "rootfs_digest": None,
    }


def _materials(resolved) -> dict:
    """解決された attempt が使う機械の材料。path は fixture に書いたまま出す。"""
    materials = _blank_materials()
    if getattr(resolved, "backend", None) != "native":
        return materials
    materials["cli_binary"] = str(resolved.cli_binary)
    materials["credential_mode"] = resolved.credential_mode
    provider = getattr(resolved, "provider", None)
    if provider != "codex":
        return materials
    rootfs = getattr(resolved, "codex_rootfs", None)
    digest = getattr(resolved, "codex_rootfs_digest", None)
    egress = getattr(resolved, "codex_egress_limits", None)
    auth = getattr(resolved, "codex_auth_file", None)
    materials["rootfs"] = None if rootfs is None else str(rootfs)
    materials["rootfs_digest"] = digest
    materials["rootfs_coding"] = rootfs_digest_is_coding(provider, resolved.codex_execution_mode)
    materials["codex_egress_limits"] = None if egress is None else egress.model_dump(mode="json")
    materials["auth_file"] = None if auth is None else str(auth)
    return materials


def _row(alias: str, config, settings) -> dict:
    """/v1/models と同じく prompt 本文は読まない。失敗は例外クラスだけを残す。"""
    try:
        resolved = resolve_agent_model(alias, settings, config=config, resolve_prompts=False)
    except (OSError, ValueError) as exc:
        return {
            "accepted_tools": None,
            "backend": None,
            "codex_execution_mode": None,
            "codex_sandbox": None,
            "error": type(exc).__name__,
            "execution_environment": None,
            **_blank_materials(),
            "place": None,
        }
    provider = getattr(resolved, "provider", None)
    runs = _runs(resolved, settings)
    return {
        "accepted_tools": _fact(runs, "accepted_tools"),
        "backend": resolved.backend,
        "codex_execution_mode": resolved.codex_execution_mode if provider == "codex" else None,
        "codex_sandbox": getattr(resolved, "codex_sandbox", None),
        "error": None,
        "execution_environment": getattr(resolved, "execution_environment", None),
        **_materials(resolved),
        "place": _fact(runs, "place"),
    }


def resolution_table(config=None, settings=None) -> dict:
    """models の全 alias を名前順で解く。材料は解決結果の path をそのまま残す。"""
    config = load_config() if config is None else config
    settings = load_settings() if settings is None else settings
    with patch("kotoyose.provider_jail._trusted_binary", _offline_claude):
        table = {alias: _row(alias, config, settings) for alias in sorted(config.models)}
    if set(table) != set(config.models):
        raise AssertionError("resolution table dropped a models alias")
    return table


def _dump(table: dict) -> str:
    return json.dumps(table, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


# origin/main の agent_sandbox_profiles が指す profile。rootfs も egress も無い。
# coding かどうかだけ execution mode が違う。
_OLD_CODEX_CLI = (
    "/opt/fixture/programming/kotoyose/.venv/lib/python3.13/site-packages/codex_cli_bin/bin/codex"
)
_OLD_CODEX_AUTH = "/opt/fixture/.codex/auth.json"
_OLD_CODEX_CODING = {
    "orchestrator-codex-subscription": False,
    "orchestrator-coding": True,
    "orchestrator-coding-sol": True,
    "orchestrator-review-terra": False,
}


def test_isolated_codex_materials_match_old_profiles():
    """旧 profile 写像の材料を、alias ごとにそのまま出す。"""
    table = resolution_table()
    for alias, coding in _OLD_CODEX_CODING.items():
        row = table[alias]
        assert row["error"] is None
        assert row["cli_binary"] == _OLD_CODEX_CLI
        assert row["auth_file"] == _OLD_CODEX_AUTH
        assert row["credential_mode"] == "chatgpt_subscription"
        assert row["rootfs"] is None and row["rootfs_digest"] is None
        assert row["codex_egress_limits"] is None
        assert row["rootfs_coding"] is coding


def test_agent_resolution_matches_golden():
    """黄金値と今の解決が 1 行もずれないこと。"""
    text = _dump(resolution_table())
    if os.environ.get(REGEN) == "1":
        GOLDEN.write_text(text, encoding="utf-8")
    assert GOLDEN.is_file(), (
        "golden.json が無い。再生成: "
        "KOTOYOSE_REGENERATE_AGENT_RESOLUTION_GOLDEN=1 "
        "uv run --extra dev pytest -q -p no:cacheprovider tests/test_agent_resolution_golden.py"
    )
    assert GOLDEN.read_text(encoding="utf-8") == text


def test_chat_resolution_matches_origin_main():
    """The route-based chat presets retain the original fixture's effective values."""
    config = load_config()
    expected = json.loads((Path(__file__).parent / "agent_resolution_chat_golden.json").read_text(encoding="utf-8"))
    actual = {}
    for alias in config.models:
        preset = config.preset_for(alias)
        actual[alias] = {
            "provider": preset.provider,
            "model": preset.model,
            "anthropic_base_url": preset.anthropic_base_url,
            "anthropic_auth_token_env": preset.anthropic_auth_token_env,
            "model_provider": preset.codex.get("model_provider"),
            "subscription_only": preset.subscription_only,
            "execution": preset.execution,
        }
    assert actual == expected
