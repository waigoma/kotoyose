"""Remote Worker の管理者 config と CLI 境界を検証する。"""

import json
from pathlib import Path

import pytest

from kotoyose.remote_worker_cli import load_admin_config


def _write(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)
    return path


def test_admin_config_rejects_unknown_fields_and_non_private_file(tmp_path: Path):
    config = _write(tmp_path / "config.json", {"worker": {}, "unexpected": True})
    with pytest.raises(ValueError, match="unknown or missing"):
        load_admin_config(config)
    config.chmod(0o644)
    with pytest.raises(ValueError, match="group/world"):
        load_admin_config(config)


def test_admin_config_requires_absolute_paths_and_shared_workspace(tmp_path: Path):
    payload = {
        "image_id": "sha256:" + "a" * 64,
        "workspace_root": "relative",
        "journal_path": str(tmp_path / "journal.sqlite"),
        "ca_file": str(tmp_path / "ca.crt"),
        "certificate_file": str(tmp_path / "server.crt"),
        "key_file": str(tmp_path / "server.key"),
        "worker": {}, "workspaces": [],
    }
    config = _write(tmp_path / "config.json", payload)
    with pytest.raises(ValueError, match="workspace_root must be absolute"):
        load_admin_config(config)
    payload["workspace_root"] = str(tmp_path)
    config = _write(tmp_path / "config.json", payload)
    with pytest.raises(ValueError, match="at least one workspace"):
        load_admin_config(config)
