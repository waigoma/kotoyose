"""save_config() のテスト。"""
import os

import pytest
import yaml

from kotoyose.config import (
    HarnessConfig,
    ModelPreset,
    save_config,
    get_config,
    reload_config,
)


def test_save_config_writes_new_file(tmp_path):
    """存在しないパスにも書き込める。"""
    path = str(tmp_path / "out.yml")
    cfg = HarnessConfig(
        models={"test-model": ModelPreset(model="claude-test", cwd="/tmp")}
    )
    with pytest.MonkeyPatch().context() as m:
        m.setenv("KOTOYOSE_CONFIG", path)
        save_config(cfg)

    with open(path) as f:
        data = yaml.safe_load(f)

    assert "test-model" in data["models"]
    assert data["models"]["test-model"]["model"] == "claude-test"


def test_save_config_updates_in_memory(tmp_path, monkeypatch):
    """save_config() 後に get_config() が新しい設定を返す。"""
    path = str(tmp_path / "cfg.yml")
    monkeypatch.setenv("KOTOYOSE_CONFIG", path)

    cfg = HarnessConfig(models={"new": ModelPreset(cwd="/tmp")})
    save_config(cfg)

    assert "new" in get_config().models


def test_save_config_overwrites_existing(tmp_path, monkeypatch):
    """既存ファイルを上書きする。"""
    path = tmp_path / "cfg.yml"
    path.write_text("models:\n  old-model:\n    bare: true\n")
    monkeypatch.setenv("KOTOYOSE_CONFIG", str(path))
    reload_config()

    new_cfg = HarnessConfig(models={"new-model": ModelPreset(cwd="/tmp")})
    save_config(new_cfg)

    with open(path) as f:
        data = yaml.safe_load(f)
    assert "new-model" in data["models"]
    assert "old-model" not in data.get("models", {})


def test_save_config_preserves_provider_nested_configs(tmp_path, monkeypatch):
    """save_config() は provider 固有 config を YAML に保存し reload で復元する。"""
    path = tmp_path / "cfg.yml"
    monkeypatch.setenv("KOTOYOSE_CONFIG", str(path))

    cfg = HarnessConfig(
        models={
            "codex-coding": ModelPreset(
                provider="codex",
                model="gpt-5.1-codex",
                cwd="/tmp",
                codex={"sandbox": "workspace-write"},
                cursor={"runtime": "local"},
                antigravity={"command": "agy"},
            )
        }
    )
    save_config(cfg)
    reloaded = reload_config()

    preset = reloaded.models["codex-coding"]
    assert preset.provider == "codex"
    assert preset.codex["sandbox"] == "workspace-write"
    assert preset.cursor["runtime"] == "local"
    assert preset.antigravity["command"] == "agy"
