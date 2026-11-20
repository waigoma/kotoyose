"""管理者固定設定から Remote Worker server を起動する CLI。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import signal
import threading

from .local_worker import DockerLocalWorker
from .remote_worker_contract import RemoteWorkerRef
from .remote_worker_journal import RemoteWorkerJournal
from .remote_worker_server import RemoteWorkerServer


def _private_file(path: Path, label: str) -> Path:
    """Caller が渡す設定ではなく管理者専用ファイルとして検証する。"""
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} must be an existing regular file")
    if path.stat().st_mode & 0o077:
        raise ValueError(f"{label} must not be group/world accessible")
    return path


def load_admin_config(path: Path) -> dict:
    """Remote Worker の enrollment と binding を JSON から厳密に読み込む。"""
    _private_file(path, "admin config")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or set(payload) != {
        "image_id", "workspace_root", "journal_path", "ca_file", "certificate_file", "key_file",
        "worker", "workspaces",
    }:
        raise ValueError("admin config has unknown or missing fields")
    if not isinstance(payload["image_id"], str) or not payload["image_id"].startswith("sha256:"):
        raise ValueError("image_id must be an immutable digest")
    for key in ("workspace_root", "journal_path", "ca_file", "certificate_file", "key_file"):
        if not Path(payload[key]).is_absolute():
            raise ValueError(f"{key} must be absolute")
    if not isinstance(payload["workspaces"], list) or not payload["workspaces"]:
        raise ValueError("at least one workspace binding is required")
    return payload


def build_server_from_admin_config(path: Path) -> RemoteWorkerServer:
    """管理者 config から enrollment 済み server を組み立てる。"""
    payload = load_admin_config(path)
    root = Path(payload["workspace_root"]).resolve(strict=True)
    journal = RemoteWorkerJournal(Path(payload["journal_path"]))
    server = RemoteWorkerServer(
        journal=journal, worker=DockerLocalWorker(payload["image_id"]), workspace_root=root,
        ca_file=_private_file(Path(payload["ca_file"]), "ca_file"),
        certificate_file=_private_file(Path(payload["certificate_file"]), "certificate_file"),
        key_file=_private_file(Path(payload["key_file"]), "key_file"),
    )
    worker = RemoteWorkerRef.model_validate(payload["worker"])
    server.enroll_worker(worker)
    for workspace in payload["workspaces"]:
        if not isinstance(workspace, dict) or set(workspace) != {"ref", "path", "generation", "challenge_relative"}:
            server.close()
            journal.close()
            raise ValueError("workspace binding has unknown or missing fields")
        server.register_workspace(
            workspace["ref"], Path(workspace["path"]), int(workspace["generation"]), workspace["challenge_relative"],
        )
    return server


def main() -> None:
    """管理者 config だけで Remote Worker server を起動する。"""
    parser = argparse.ArgumentParser(prog="kotoyose-remote-worker")
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    server = build_server_from_admin_config(args.config)
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    server.start()
    try:
        stopped.wait()
    finally:
        server.close()
        server.journal.close()


if __name__ == "__main__":
    main()
