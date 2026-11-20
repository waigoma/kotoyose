"""Build a new reviewed Codex coding artifact from explicit, bounded OS inputs.

This tool never executes an input binary, downloads software, or activates a profile.
Only the separately pinned OS readelf inspection tool is executed.
"""

import argparse
from collections.abc import Mapping
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tempfile

from .codex_rootfs import rootfs_digest, validate_rootfs
from .config_paths import default_data_dir


def default_rootfs_parent(environ: Mapping[str, str] | None = None) -> Path:
    """Return `${XDG_DATA_HOME:-~/.local/share}/kotoyose/rootfs`."""
    return default_data_dir(environ) / "rootfs"


DEFAULT_PARENT = default_rootfs_parent()
PUBLIC_CA = Path("/etc/ssl/certs/ca-certificates.crt")
MAX_FILES = 9000
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_EXTERNAL_CLI_BYTES = 512 * 1024 * 1024
MAX_TOTAL_BYTES = 1024 * 1024 * 1024
SYSTEM_LIB_ROOTS = (Path("/usr/lib"), Path("/usr/local/lib"), Path("/lib"), Path("/lib64"))
SYSTEM_BIN_ROOTS = (Path("/usr/bin"), Path("/usr/local/bin"), Path("/bin"))
EXCLUDE = {"site-packages", "dist-packages", "__pycache__", "test", "tests", "idlelib", "tkinter", "ensurepip", "venv",
           "sitecustomize.py", "usercustomize.py"}
BINARY_TARGETS = {"sh": "bin/sh", "bash": "bin/bash", "git": "usr/bin/git", "env": "usr/bin/env", "rg": "usr/bin/rg"}


def _canonical(value: str, *, directory: bool = False, pinned: bool = False) -> Path:
    path = Path(value)
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise ValueError(f"source must be absolute and canonical: {path}")
    info = path.stat()
    if (info.st_mode & 0o022 and not pinned) or info.st_uid not in {0, os.getuid()}:
        raise ValueError(f"untrusted source owner/mode: {path}")
    if directory != stat.S_ISDIR(info.st_mode) or (not directory and not stat.S_ISREG(info.st_mode)):
        raise ValueError(f"unexpected source type: {path}")
    return path


def _under(path: Path, roots: tuple[Path, ...] | list[Path]) -> bool:
    return any(path.is_relative_to(root) for root in roots)


def _read(path: Path, *, pinned: bool = False) -> bytes:
    # Open the canonical regular file, not a last-component symlink or device.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or (info.st_mode & 0o022 and not pinned) or info.st_size > MAX_FILE_BYTES:
            raise ValueError(f"unsafe or oversized source: {path}")
        data = stream.read(MAX_FILE_BYTES + 1)
        after = os.fstat(stream.fileno())
        if (len(data) != info.st_size or len(data) > MAX_FILE_BYTES
                or (info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            raise ValueError(f"source changed while reading: {path}")
        return data


def _pin(value: dict, *, system: bool = False) -> tuple[Path, bytes]:
    if set(value) != {"path", "sha256"} or not re.fullmatch(r"[a-f0-9]{64}", value["sha256"]):
        raise ValueError("every explicit file requires exactly path and SHA-256")
    path = _canonical(value["path"], pinned=True)
    if system and not _under(path, SYSTEM_BIN_ROOTS):
        raise ValueError(f"not a system executable: {path}")
    data = _read(path, pinned=True)
    if hashlib.sha256(data).hexdigest() != value["sha256"]:
        raise ValueError(f"source hash mismatch: {path}")
    return path, data


def _external_cli_digest(path: Path) -> tuple[str, int]:
    """Hash the separately mounted CLI in bounded chunks; never load/copy its body."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_EXTERNAL_CLI_BYTES:
            raise ValueError(f"unsafe or oversized external Codex CLI: {path}")
        header = stream.read(4)
        if header != b"\x7fELF" or not os.access(path, os.X_OK):
            raise ValueError(f"input executable must be a trusted ELF: {path}")
        digest, size = hashlib.sha256(header), len(header)
        while chunk := stream.read(min(1024 * 1024, MAX_EXTERNAL_CLI_BYTES + 1 - size)):
            size += len(chunk)
            if size > MAX_EXTERNAL_CLI_BYTES:
                raise ValueError(f"oversized external Codex CLI: {path}")
            digest.update(chunk)
        after = os.fstat(stream.fileno())
        if (size != info.st_size or (info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            raise ValueError(f"external Codex CLI changed while reading: {path}")
        return digest.hexdigest(), size


def _elf_info(path: Path, tool: tuple[Path, str]) -> dict:
    """Inspect ELF metadata without ldd or execution of the inspected program."""
    readelf, expected = tool
    fd = os.open(readelf, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        with os.fdopen(os.dup(fd), "rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                raise ValueError("readelf changed after pin validation")
        with tempfile.TemporaryFile() as output:
            result = subprocess.run(
                [f"/proc/self/fd/{fd}", "--wide", "--dynamic", "--program-headers", str(path)],
                stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.DEVNULL,
                env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"}, pass_fds=(fd,), timeout=10, check=False,
            )
            output.seek(0)
            raw = output.read(1024 * 1024 + 1)
        if result.returncode or len(raw) > 1024 * 1024:
            raise ValueError(f"ELF inspection failed: {path}")
    finally:
        os.close(fd)
    text = raw.decode("utf-8", errors="strict")
    if re.search(r"\((?:RPATH|RUNPATH)\)", text):
        raise ValueError(f"RPATH/RUNPATH requires a separately reviewed recipe: {path}")
    needed = re.findall(r"\(NEEDED\).*?\[([^\]]+)\]", text)
    if any(not re.fullmatch(r"[A-Za-z0-9_+.-]+", name) or name in {".", ".."} for name in needed):
        raise ValueError(f"invalid ELF dependency name: {path}")
    interpreters = re.findall(r"Requesting program interpreter: ([^\]]+)\]", text)
    if len(interpreters) > 1:
        raise ValueError(f"ambiguous ELF interpreter: {path}")
    return {"needed": sorted(set(needed)), "interpreter": interpreters[0] if interpreters else None}


def collect(inputs: dict) -> tuple[dict, dict[str, bytes]]:
    required = {"schema_version", "python", "stdlib", "binaries", "codex", "bwrap", "ca", "readelf", "library_dirs"}
    if set(inputs) != required or type(inputs["schema_version"]) is not int or inputs["schema_version"] != 1:
        raise ValueError("unsupported input schema")
    binaries = inputs["binaries"]
    if not {"sh", "bash", "git"} <= binaries.keys() or binaries.keys() - BINARY_TARGETS.keys():
        raise ValueError("binaries must contain sh, bash, git; only env and rg are optional")
    tool_path, tool_data = _pin(inputs["readelf"], system=True)
    _canonical(str(tool_path))
    if not tool_data.startswith(b"\x7fELF") or not os.access(tool_path, os.X_OK):
        raise ValueError("readelf must be an executable OS ELF binary")
    tool = (tool_path, inputs["readelf"]["sha256"])
    libdirs = [_canonical(value, directory=True) for value in inputs["library_dirs"]]
    if not libdirs or len(libdirs) > 16 or len(set(libdirs)) != len(libdirs) or any(not _under(p, SYSTEM_LIB_ROOTS) for p in libdirs):
        raise ValueError("provide 1..16 unique canonical system library directories")
    stdlib = _canonical(inputs["stdlib"], directory=True)
    if not _under(stdlib, SYSTEM_LIB_ROOTS) or not re.fullmatch(r"python3\.\d+", stdlib.name):
        raise ValueError("stdlib must be an explicitly reviewed system python3.X directory")
    for name in ("encodings/__init__.py", "json/__init__.py", "os.py"):
        _canonical(str(stdlib / name))
    files, entries, pending, external = {}, {}, [], []
    cli = inputs["codex"]
    if set(cli) != {"path", "sha256"} or not re.fullmatch(r"[a-f0-9]{64}", cli["sha256"]):
        raise ValueError("every explicit file requires exactly path and SHA-256")
    cli_path = _canonical(cli["path"], pinned=True)
    cli_digest, total = _external_cli_digest(cli_path)
    if cli_digest != cli["sha256"]:
        raise ValueError(f"source hash mismatch: {cli_path}")
    cli_item = {"source": str(cli_path), "destination": None, "sha256": cli_digest, "size": total, "mode": "0555"}
    external.append(cli_item)
    pending.append(cli_item)

    def add(source: Path, target: str | None, data: bytes, mode: int, *, expected: str | None = None) -> None:
        nonlocal total
        sha = hashlib.sha256(data).hexdigest()
        if expected is not None and sha != expected:
            raise ValueError(f"source hash mismatch: {source}")
        item = {"source": str(source), "destination": target, "sha256": sha, "size": len(data), "mode": f"{mode:04o}"}
        if target in files:
            if files[target] != data or entries[target]["mode"] != item["mode"]:
                raise ValueError(f"conflicting destination: {target}")
            return
        total += len(data)
        if len(files) >= MAX_FILES or total > MAX_TOTAL_BYTES:
            raise ValueError("rootfs input inventory exceeds bounded limits")
        if target is None:
            external.append(item)
        else:
            files[target], entries[target] = data, item
        if data.startswith(b"\x7fELF"):
            pending.append(item)

    specs = [(inputs["python"], "usr/bin/python3", True),
             (inputs["bwrap"], "runtime/codex-resources/bwrap", False)]
    specs += [(value, BINARY_TARGETS[name], name != "rg") for name, value in sorted(binaries.items())]
    for value, target, system in specs:
        source, data = _pin(value, system=system)
        if not data.startswith(b"\x7fELF") or not os.access(source, os.X_OK):
            raise ValueError(f"input executable must be a trusted ELF: {source}")
        add(source, target, data, 0o555, expected=value["sha256"])

    # Only this explicitly supplied stdlib root is traversed; no HOME or host-wide scan.
    def walk_error(error: OSError) -> None:
        raise error

    visited = 0
    for current, dirs, names in os.walk(stdlib, followlinks=False, onerror=walk_error):
        visited += 1 + len(dirs) + len(names)
        if visited > 20000:
            raise ValueError("stdlib traversal exceeds bounded limits")
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDE and not d.startswith((".", "config-")) and not d.endswith(".pth"))
        for name in dirs:
            _canonical(str(Path(current) / name), directory=True)
        for name in sorted(names):
            if name in EXCLUDE or name.startswith(".") or name.endswith((".pyc", ".pyo", ".a", ".pth")):
                continue
            selected = Path(current) / name
            source = selected
            if selected.is_symlink():
                # Distro sysconfig aliases are regular Python modules in this same root.
                source = selected.resolve(strict=True)
                if (selected.parent != stdlib or source.parent != stdlib
                        or not re.fullmatch(r"_sysconfigdata__[A-Za-z0-9_-]+\.py", selected.name)
                        or not re.fullmatch(r"_sysconfigdata__[A-Za-z0-9_-]+\.py", source.name)):
                    raise ValueError(f"non-canonical stdlib symlink: {selected}")
            source = _canonical(str(source))
            add(source, f"usr/lib/{stdlib.name}/{selected.relative_to(stdlib).as_posix()}", _read(source), 0o444)

    ca, data = _pin(inputs["ca"])
    if ca != PUBLIC_CA or not re.fullmatch(
        rb"\s*(?:-----BEGIN" rb" CERTIFICATE-----\s+[A-Za-z0-9+/=\s]+-----END CERTIFICATE-----\s*)+", data
    ):
        raise ValueError("CA input must be the reviewed public system certificate bundle")
    add(ca, "etc/ssl/certs/ca-certificates.crt", data, 0o444)

    while pending:
        item = pending.pop()
        source = Path(item["source"])
        def source_digest() -> str:
            if item["destination"] is None:
                return _external_cli_digest(source)[0]
            return hashlib.sha256(_read(source, pinned=True)).hexdigest()

        # Pin the source before and after readelf, so a changing source cannot produce a manifest.
        if source_digest() != item["sha256"]:
            raise ValueError(f"ELF changed before inspection: {source}")
        info = _elf_info(source, tool)
        if source_digest() != item["sha256"]:
            raise ValueError(f"ELF changed during inspection: {source}")
        item["elf"] = info
        dependencies = []
        for name in info["needed"]:
            matches = [directory / name for directory in libdirs if (directory / name).exists()]
            if len(matches) != 1:
                raise ValueError(f"dependency must resolve uniquely in approved library_dirs: {name}")
            dependencies.append(matches[0])
        if info["interpreter"]:
            interpreter = Path(info["interpreter"])
            if not interpreter.is_absolute() or ".." in interpreter.parts or not _under(interpreter, SYSTEM_LIB_ROOTS):
                raise ValueError("ELF interpreter is outside system library paths")
            dependencies.append(interpreter)
        item["dependencies"] = []
        for dependency in dependencies:
            actual = _canonical(str(dependency.resolve(strict=True)))
            if not _under(actual, libdirs):
                raise ValueError(f"dependency escapes approved library_dirs: {dependency}")
            target = dependency.as_posix().lstrip("/")
            item["dependencies"].append(target)
            dependency_bytes = _read(actual)
            if not dependency_bytes.startswith(b"\x7fELF"):
                raise ValueError(f"dependency is not ELF: {actual}")
            add(actual, target, dependency_bytes, 0o555)
    manifest = {"schema_version": 1, "kind": "reviewed-codex-coding-rootfs", "inputs": inputs,
                "excluded_stdlib_names": sorted(EXCLUDE), "files": [entries[k] for k in sorted(entries)],
                "external_executables": external, "total_source_bytes": total}
    return manifest, files


def _publish_noreplace(source: Path, destination: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, "renameat2", None)
    if rename is None:
        raise RuntimeError("Linux renameat2 RENAME_NOREPLACE is required; no unsafe fallback")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1):
        code = ctypes.get_errno()
        if code == errno.EEXIST:
            raise FileExistsError(destination)
        raise OSError(code, os.strerror(code), str(destination))


def build(inputs: dict, parent: Path, version: str) -> Path:
    parent = _canonical(str(parent), directory=True)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", version):
        raise ValueError("version must be a new simple directory name")
    destination = parent / version
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(destination)
    manifest, files = collect(inputs)
    stage = Path(tempfile.mkdtemp(prefix=".rootfs-build-", dir=parent))
    try:
        root = stage / "rootfs"
        root.mkdir(mode=0o755)
        for name in ("tmp", "home", "runtime", "proc", "dev", "workspace"):
            (root / name).mkdir(mode=0o755)
        modes = {entry["destination"]: int(entry["mode"], 8) for entry in manifest["files"]}
        for name, data in files.items():
            # Inventory still receives the unchanged authoritative validator below.
            relative = PurePosixPath(name)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("invalid inventory destination")
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fchmod(stream.fileno(), modes[name])
                os.fsync(stream.fileno())
        for directory in sorted((p for p in root.rglob("*") if p.is_dir()), reverse=True):
            directory.chmod(0o555)
        root.chmod(0o555)
        digest = rootfs_digest(root, coding=True)
        validate_rootfs(root, digest, coding=True)
        manifest["rootfs_digest"] = digest
        manifest["rootfs_path"] = str(destination / "rootfs")
        manifest["directories"] = [{"path": p.relative_to(root).as_posix(), "mode": "0555"}
                                   for p in [root, *sorted(p for p in root.rglob("*") if p.is_dir())]]
        with (stage / "manifest.json").open("x") as stream:
            json.dump(manifest, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fchmod(stream.fileno(), 0o444)
            os.fsync(stream.fileno())
        stage.chmod(0o555)
        for directory in [*sorted((p for p in root.rglob("*") if p.is_dir()), reverse=True), root, stage]:
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        _publish_noreplace(stage, destination)
        fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return destination
    finally:
        if stage.exists():
            # Only remove this invocation's unpublished staging envelope.
            for current, dirs, _ in os.walk(stage):
                Path(current).chmod(0o700)
            shutil.rmtree(stage)


def load_inputs(path: Path) -> dict:
    def unique_keys(pairs: list) -> dict:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate input field: {key}")
            result[key] = value
        return result

    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("input specification must be a regular file")
        data = stream.read(65537)
        if info.st_size > 65536 or len(data) > 65536:
            raise ValueError("input specification exceeds 64 KiB")
    value = json.loads(data, object_pairs_hook=unique_keys)
    if not isinstance(value, dict):
        raise ValueError("input specification must be a JSON object")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--parent", type=Path, default=DEFAULT_PARENT)
    args = parser.parse_args()
    if _under(args.parent, (Path("/tmp"), Path("/var/tmp"), Path("/run"))):
        parser.error("use a persistent parent, not a temporary/runtime directory")
    try:
        inputs = load_inputs(args.inputs)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    destination = build(inputs, args.parent, args.version)
    print(json.dumps({"release": str(destination), "manifest": str(destination / "manifest.json")}))


if __name__ == "__main__":
    main()
