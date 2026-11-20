"""管理者が固定した Codex binary を空の HOME と filesystem で起動する。"""

import os
import shutil
import sys
import json
from pathlib import Path

from .codex_rootfs import validate_rootfs
from .codex_model_policy import model_catalog
from .codex_coding_policy import DEFAULT_CODEX_MODE, validate_mode, validate_workspace, protected_workspace_entries

from .provider_jail import _trusted_binary, build_jail_command, provider_environment


def main() -> None:
    """host 設定や環境を継承せず exec する。bwrap 不在では起動しない。"""
    binary = _trusted_binary(os.environ.get("KOTOYOSE_NATIVE_CODEX_BINARY", ""))
    bwrap = shutil.which("bwrap")
    if not bwrap:
        raise RuntimeError("bubblewrap (bwrap) is required")
    coding = validate_mode(os.environ.get("KOTOYOSE_NATIVE_CODEX_MODE", DEFAULT_CODEX_MODE))
    workspace_value = os.environ.get("KOTOYOSE_NATIVE_CODEX_WORKSPACE")
    if coding != bool(workspace_value):
        raise ValueError("invalid_codex_workspace")
    workspace = validate_workspace(Path(workspace_value)) if coding else None
    environment_mode = os.environ.get("KOTOYOSE_NATIVE_CODEX_ENVIRONMENT")
    protection = os.environ.get("KOTOYOSE_NATIVE_CODEX_PROTECTED_WORKSPACE")
    if environment_mode is not None:
        if environment_mode != "isolated" or not coding or not protection or len(protection) > 1024:
            raise ValueError("invalid_codex_coding_policy")
        protection = protected_workspace_entries(workspace, json.loads(protection))
    elif protection is not None:
        raise ValueError("invalid_codex_coding_policy")
    rootfs_value = os.environ.get("KOTOYOSE_NATIVE_CODEX_ROOTFS")
    rootfs = validate_rootfs(Path(rootfs_value) if rootfs_value else None,
                            os.environ.get("KOTOYOSE_NATIVE_CODEX_ROOTFS_DIGEST"), coding=coding)
    socket_value = os.environ.get("KOTOYOSE_NATIVE_CODEX_EGRESS_SOCKET")
    if not socket_value:
        raise RuntimeError("restricted egress broker is required")
    catalog_path = Path(os.environ.get("KOTOYOSE_NATIVE_CODEX_MODEL_CATALOG", ""))
    if (not catalog_path.is_absolute() or catalog_path.resolve(strict=True) != catalog_path
            or not catalog_path.is_file() or catalog_path.stat().st_size > 8192):
        raise ValueError("invalid_codex_model_catalog")
    value = json.loads(catalog_path.read_text())
    slug = value["models"][0]["slug"]
    if value not in (model_catalog(slug, coding=coding), model_catalog(slug, images=True, coding=coding)):
        effort = value["models"][0].get("default_reasoning_level")
        if effort is None or value not in (model_catalog(slug, reasoning_effort=effort, coding=coding),
                                          model_catalog(slug, images=True, reasoning_effort=effort, coding=coding)):
            raise ValueError("invalid_codex_model_catalog")
    command = build_jail_command(binary, sys.argv[1:], provider="codex", rootfs=rootfs,
                                 egress_socket=Path(socket_value), model_catalog=catalog_path, workspace=workspace,
                                 **({"protected_workspace": protection} if protection is not None else {}),
                                 session_directory=(Path(os.environ['KOTOYOSE_NATIVE_CODEX_SESSION_DIR'])
                                                    if os.environ.get('KOTOYOSE_NATIVE_CODEX_SESSION_DIR') else None))
    environment = provider_environment(os.environ, provider="codex")
    if coding:
        environment["SHELL"] = "/bin/bash"
    os.execve(bwrap, command, environment)


if __name__ == "__main__":
    main()
