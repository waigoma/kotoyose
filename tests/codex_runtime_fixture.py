"""専用 fake-runtime artifact。production rootfs の作成・登録には使わない。"""

from pathlib import Path
import re
import os
import shutil
import subprocess

import pytest

from kotoyose.codex_rootfs import rootfs_digest


@pytest.fixture(scope="session")
def codex_rootfs(tmp_path_factory):
    root = tmp_path_factory.mktemp("reviewed-fake-codex-runtime").resolve()
    for name in ("tmp", "home", "runtime", "proc", "dev"):
        (root / name).mkdir(mode=0o755)
    interpreter = Path("/usr/bin/python3")
    library = Path(subprocess.check_output([str(interpreter), "-I", "-S", "-c",
        "import sysconfig; print(sysconfig.get_path('stdlib'))"], text=True).strip())
    destination = root / library.relative_to("/")
    shutil.copytree(library, destination, ignore=shutil.ignore_patterns(
        "__pycache__", "site-packages", "dist-packages", "test", "tests", "idlelib", "tkinter",
        "sitecustomize.py", "usercustomize.py", "*.pth"))
    binaries = [interpreter, *destination.rglob("*.so")]
    if os.environ.get("KOTOYOSE_CODEX_CONFORMANCE_BINARY"):
        binaries.append(Path(os.environ["KOTOYOSE_CODEX_CONFORMANCE_BINARY"]))
    dependencies = set()
    for binary in binaries:
        output = subprocess.run(["ldd", str(binary)], capture_output=True, text=True).stdout
        dependencies.update(re.findall(r"(/[^\s()]+)", output))
    for source in [str(interpreter), *sorted(dependencies)]:
        file = Path(source)
        target = root / file.relative_to("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(file, target)
        target.chmod(0o755)
    for path in root.rglob("*"):
        path.chmod(0o755 if path.is_dir() or path.stat().st_mode & 0o111 else 0o644)
    return root, rootfs_digest(root)
