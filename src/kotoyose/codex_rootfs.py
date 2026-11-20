"""Codex 専用のレビュー済み runtime artifact を検査する。host directory は mount しない。"""

import hashlib
import os
from pathlib import Path
import re
import stat


def rootfs_digest(root: Path, *, coding: bool = False) -> str:
    """canonical な専用 tree の inventory/content/mode を固定する。秘密領域を拒否する。"""
    if not root.is_absolute() or root.resolve(strict=True) != root or not root.is_dir():
        raise ValueError("invalid_codex_rootfs")
    if root.stat().st_mode & 0o022:
        raise ValueError("invalid_codex_rootfs")
    digest = hashlib.sha256(f"root\0{stat.S_IMODE(root.stat().st_mode)}\n".encode())
    count = 0
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        info = path.lstat()
        count += 1
        if count > 10000 or info.st_mode & 0o022 or path.is_symlink():
            raise ValueError("invalid_codex_rootfs")
        # Artifact は public CA、interpreter とその library に限定する。
        allowed = (relative in {"tmp", "home", "runtime", "proc", "dev",
                                "usr", "usr/bin", "usr/bin/python3", "usr/lib", "lib", "lib64",
                                "etc", "etc/ssl", "etc/ssl/certs", "etc/ssl/certs/ca-certificates.crt"}
                   or re.fullmatch(r"(?:usr/)?lib(?:64)?/[^\n]+", relative))
        if coding and relative in {"bin", "bin/sh", "bin/bash", "usr/bin/bash", "usr/bin/git", "usr/bin/env", "usr/bin/rg", "workspace",
                                  "runtime/codex-resources", "runtime/codex-resources/bwrap"}:
            allowed = True
        if (not allowed or any(part in {"site-packages", "dist-packages", "kotoyose", "private"}
                               for part in path.relative_to(root).parts)):
            raise ValueError("invalid_codex_rootfs")
        if stat.S_ISREG(info.st_mode):
            with path.open("rb") as stream:
                content = hashlib.file_digest(stream, "sha256").hexdigest()
        elif stat.S_ISDIR(info.st_mode):
            content = "directory"
        else:
            raise ValueError("invalid_codex_rootfs")
        digest.update(f"{relative}\0{stat.S_IMODE(info.st_mode)}\0{content}\n".encode())
    return digest.hexdigest()


def validate_rootfs(root: Path | None, approved_digest: str | None, *, coding: bool = False) -> Path:
    """未設定、内容変更、controller tree と重なる rootfs では起動しない。"""
    if root is None or approved_digest is None or not re.fullmatch(r"[a-f0-9]{64}", approved_digest):
        raise ValueError("invalid_codex_rootfs")
    controller = Path(__file__).resolve()
    if controller.is_relative_to(root) or rootfs_digest(root, coding=coding) != approved_digest:
        raise ValueError("invalid_codex_rootfs")
    interpreter = root / "usr/bin/python3"
    if not interpreter.is_file() or not os.access(interpreter, os.X_OK):
        raise ValueError("invalid_codex_rootfs")
    if any(not (root / name).is_dir() for name in ("tmp", "home", "runtime", "proc", "dev")):
        raise ValueError("invalid_codex_rootfs")
    if coding:
        for executable in ("bin/sh", "bin/bash", "usr/bin/git", "runtime/codex-resources/bwrap"):
            if not (root / executable).is_file() or not os.access(root / executable, os.X_OK):
                raise ValueError("invalid_codex_rootfs")
    return root
