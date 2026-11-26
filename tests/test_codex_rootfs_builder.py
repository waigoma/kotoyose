"""Builder tests are synthetic artifact tests, never a live host/runtime rollout."""

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from kotoyose import codex_rootfs_builder as builder
from kotoyose.codex_rootfs import rootfs_digest, validate_rootfs


def pin(path):
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    system = tmp_path / "system"
    system.mkdir(mode=0o755)
    lib = tmp_path / "lib"
    lib.mkdir(mode=0o755)
    stdlib = lib / "python3.12"
    for name in ("encodings/__init__.py", "json/__init__.py", "os.py", "socket.py"):
        path = stdlib / name
        path.parent.mkdir(parents=True, exist_ok=True)
        stdlib.chmod(0o755)
        path.parent.chmod(0o755)
        path.write_text("# synthetic stdlib\n")
        path.chmod(0o644)
    result = {"schema_version": 1, "stdlib": str(stdlib), "library_dirs": [str(lib)], "binaries": {}}
    for name in ("python", "codex", "bwrap", "readelf", "sh", "bash", "git", "env", "rg"):
        path = system / name
        path.write_bytes(b"\x7fELFsynthetic-" + name.encode())
        path.chmod(0o755)
        if name in builder.BINARY_TARGETS:
            result["binaries"][name] = pin(path)
        else:
            result[name] = pin(path)
    ca = tmp_path / "ca-certificates.crt"
    ca.write_bytes(b"-----\x42EGIN CERTIFICATE-----\nZml4dHVyZQ==\n-----END CERTIFICATE-----\n")
    ca.chmod(0o644)
    result["ca"] = pin(ca)
    monkeypatch.setattr(builder, "PUBLIC_CA", ca)
    monkeypatch.setattr(builder, "SYSTEM_BIN_ROOTS", (system,))
    monkeypatch.setattr(builder, "SYSTEM_LIB_ROOTS", (lib,))
    monkeypatch.setattr(builder, "_elf_info", lambda *args: {"needed": [], "interpreter": None})
    return result


def test_build_publishes_valid_tree_and_external_manifest(tmp_path, inputs):
    before = deepcopy(inputs)
    release = builder.build(inputs, tmp_path, "20261015-test-v1")
    root = release / "rootfs"
    manifest = json.loads((release / "manifest.json").read_text())
    assert validate_rootfs(root, manifest["rootfs_digest"], coding=True) == root
    assert manifest["rootfs_digest"] == rootfs_digest(root, coding=True)
    assert manifest["rootfs_path"] == str(root)
    assert inputs == before
    assert not (root / "manifest.json").exists()
    assert not (root / "runtime/codex").exists()
    assert manifest["external_executables"][0]["sha256"] == inputs["codex"]["sha256"]
    assert (root / "runtime/codex-resources/bwrap").stat().st_mode & 0o777 == 0o555
    assert all(not p.is_symlink() and not p.stat().st_mode & 0o022 for p in root.rglob("*"))
    for item in manifest["files"]:
        content = (root / item["destination"]).read_bytes()
        assert hashlib.sha256(content).hexdigest() == item["sha256"]
        assert (root / item["destination"]).stat().st_mode & 0o777 == int(item["mode"], 8)
    assert not list(tmp_path.glob(".rootfs-build-*"))


def test_new_versions_have_same_content_digest(tmp_path, inputs):
    a = builder.build(inputs, tmp_path, "v1")
    b = builder.build(inputs, tmp_path, "v2")
    assert rootfs_digest(a / "rootfs", coding=True) == rootfs_digest(b / "rootfs", coding=True)


@pytest.mark.parametrize("kind", ["directory", "file", "symlink"])
def test_existing_destination_is_never_replaced(tmp_path, inputs, kind):
    destination = tmp_path / "v1"
    if kind == "directory":
        destination.mkdir()
    elif kind == "file":
        destination.write_text("keep")
    else:
        destination.symlink_to(tmp_path / "missing")
    with pytest.raises(FileExistsError):
        builder.build(inputs, tmp_path, "v1")
    assert destination.is_symlink() if kind == "symlink" else destination.exists()


def test_atomic_race_cannot_overwrite_empty_directory(tmp_path, inputs, monkeypatch):
    publish = builder._publish_noreplace

    def raced(source, destination):
        destination.mkdir()
        return publish(source, destination)

    monkeypatch.setattr(builder, "_publish_noreplace", raced)
    with pytest.raises(FileExistsError):
        builder.build(inputs, tmp_path, "race")
    assert list((tmp_path / "race").iterdir()) == []
    assert not list(tmp_path.glob(".rootfs-build-*"))


@pytest.mark.parametrize("version", ["../escape", ".", "..", "/abs", "v/a", "", ".hidden", "a" * 81])
def test_version_name_is_bounded(tmp_path, inputs, version):
    with pytest.raises(ValueError):
        builder.build(inputs, tmp_path, version)


def test_source_hash_drift_fails_without_publication(tmp_path, inputs):
    Path(inputs["codex"]["path"]).write_bytes(b"\x7fELFchanged")
    with pytest.raises(ValueError, match="hash mismatch"):
        builder.build(inputs, tmp_path, "v1")
    assert not (tmp_path / "v1").exists()


def test_pinned_group_writable_helper_is_normalized(tmp_path, inputs):
    Path(inputs["bwrap"]["path"]).chmod(0o775)
    release = builder.build(inputs, tmp_path, "v1")
    assert (release / "rootfs/runtime/codex-resources/bwrap").stat().st_mode & 0o777 == 0o555


def test_group_writable_inspector_is_not_executed(inputs):
    Path(inputs["readelf"]["path"]).chmod(0o775)
    with pytest.raises(ValueError, match="owner/mode"):
        builder.collect(inputs)


def test_private_stdlib_and_caches_are_excluded(inputs):
    stdlib = Path(inputs["stdlib"])
    for excluded in [*builder.EXCLUDE, ".private", "config-build"]:
        path = stdlib / excluded / "secret"
        path.parent.mkdir()
        path.write_text("never copy")
    (stdlib / "compiled.pyc").write_bytes(b"cache")
    manifest, files = builder.collect(inputs)
    assert all(b"never copy" not in content and content != b"cache" for content in files.values())
    assert "site-packages" in manifest["excluded_stdlib_names"]


@pytest.mark.parametrize("target", ["file", "directory"])
def test_stdlib_symlinks_are_rejected(tmp_path, inputs, target):
    outside = tmp_path / "outside"
    if target == "file":
        outside.write_text("private")
    else:
        outside.mkdir()
    (Path(inputs["stdlib"]) / "escape").symlink_to(outside)
    with pytest.raises(ValueError, match="canonical"):
        builder.collect(inputs)


def test_writable_stdlib_source_is_rejected(inputs):
    (Path(inputs["stdlib"]) / "socket.py").chmod(0o666)
    with pytest.raises(ValueError, match="owner/mode"):
        builder.collect(inputs)


def test_distro_sysconfig_alias_is_dereferenced_within_same_stdlib_root(inputs):
    stdlib = Path(inputs["stdlib"])
    actual = stdlib / "_sysconfigdata__x86_64-linux-gnu.py"
    actual.write_text("# trusted distro sysconfig\n")
    actual.chmod(0o644)
    alias = stdlib / "_sysconfigdata__linux_x86_64-linux-gnu.py"
    alias.symlink_to(actual.name)
    manifest, files = builder.collect(inputs)
    target = f"usr/lib/{stdlib.name}/{alias.name}"
    assert files[target] == actual.read_bytes()
    entry = next(item for item in manifest["files"] if item["destination"] == target)
    assert entry["source"] == str(actual)


def test_sysconfig_alias_cannot_escape_stdlib_root(tmp_path, inputs):
    outside = tmp_path / "_sysconfigdata__x86_64-linux-gnu.py"
    outside.write_text("# outside\n")
    (Path(inputs["stdlib"]) / "_sysconfigdata__linux_x86_64-linux-gnu.py").symlink_to(outside)
    with pytest.raises(ValueError, match="stdlib symlink"):
        builder.collect(inputs)


def test_config_build_subtree_is_not_followed(inputs):
    stdlib = Path(inputs["stdlib"])
    config = stdlib / "config-3.12-x86_64-linux-gnu"
    config.mkdir()
    (config / "libpython3.12.so").symlink_to("/not-read/external-libpython3.12.so.1.0")
    _, files = builder.collect(inputs)
    assert not any("config-3.12" in target for target in files)


def test_missing_stdlib_essentials_fail(inputs):
    (Path(inputs["stdlib"]) / "os.py").unlink()
    with pytest.raises(FileNotFoundError):
        builder.collect(inputs)


def test_inventory_bound_is_enforced(inputs, monkeypatch):
    monkeypatch.setattr(builder, "MAX_FILES", 2)
    with pytest.raises(ValueError, match="bounded limits"):
        builder.collect(inputs)


def test_stdlib_walk_errors_are_not_silently_ignored(inputs, monkeypatch):
    def failed_walk(path, **kwargs):
        kwargs["onerror"](PermissionError("fixture unreadable subtree"))
        return iter(())

    monkeypatch.setattr(builder.os, "walk", failed_walk)
    with pytest.raises(PermissionError, match="unreadable subtree"):
        builder.collect(inputs)


def test_cli_and_python_extensions_participate_in_dependency_closure(inputs, monkeypatch):
    library = Path(inputs["library_dirs"][0]) / "libfixture.so.1"
    library.write_bytes(b"\x7fELFlibrary")
    library.chmod(0o644)
    extension = Path(inputs["stdlib"]) / "extension.so"
    extension.write_bytes(b"\x7fELFextension")
    extension.chmod(0o644)
    inspected = []

    def inspect(path, tool):
        inspected.append(path)
        needs = ["libfixture.so.1"] if path in {extension, Path(inputs["codex"]["path"])} else []
        return {"needed": needs, "interpreter": None}

    monkeypatch.setattr(builder, "_elf_info", inspect)
    manifest, files = builder.collect(inputs)
    assert extension in inspected and library in inspected and Path(inputs["codex"]["path"]) in inspected
    assert library.as_posix().lstrip("/") in files
    assert manifest["external_executables"][0]["dependencies"] == [library.as_posix().lstrip("/")]


def test_missing_dependency_fails_closed(inputs, monkeypatch):
    monkeypatch.setattr(builder, "_elf_info", lambda *args: {"needed": ["missing.so"], "interpreter": None})
    with pytest.raises(ValueError, match="resolve uniquely"):
        builder.collect(inputs)


def test_dependency_symlink_cannot_escape_approved_library_root(tmp_path, inputs, monkeypatch):
    outside = tmp_path / "private.so"
    outside.write_bytes(b"\x7fELFprivate")
    outside.chmod(0o644)
    (Path(inputs["library_dirs"][0]) / "escape.so").symlink_to(outside)
    monkeypatch.setattr(builder, "_elf_info", lambda *args: {"needed": ["escape.so"], "interpreter": None})
    with pytest.raises(ValueError, match="escapes approved"):
        builder.collect(inputs)


def test_source_change_during_inspection_fails(inputs, monkeypatch):
    def change(path, tool):
        path.write_bytes(b"\x7fELFchanged")
        return {"needed": [], "interpreter": None}

    monkeypatch.setattr(builder, "_elf_info", change)
    with pytest.raises(ValueError, match="changed during"):
        builder.collect(inputs)


def test_validator_still_rejects_disallowed_tree_before_publish(tmp_path, inputs, monkeypatch):
    collect = builder.collect

    def extra(inputs):
        manifest, files = collect(inputs)
        files["private/data"] = b"forbidden"
        manifest["files"].append({"destination": "private/data", "mode": "0444"})
        return manifest, files

    monkeypatch.setattr(builder, "collect", extra)
    with pytest.raises(ValueError, match="invalid_codex_rootfs"):
        builder.build(inputs, tmp_path, "v1")
    assert not (tmp_path / "v1").exists() and not list(tmp_path.glob(".rootfs-build-*"))


def test_real_os_readelf_inspects_without_running_target():
    tool = shutil.which("readelf")
    target = shutil.which("true")
    if not tool or not target:
        pytest.skip("OS readelf and true are required for this local parser check")
    tool, target = Path(tool).resolve(), Path(target).resolve()
    info = builder._elf_info(target, (tool, pin(tool)["sha256"]))
    assert isinstance(info["needed"], list)
    assert info["interpreter"] is None or info["interpreter"].startswith("/")


@pytest.mark.parametrize("output,match", [
    (b"0x0 (RUNPATH) Library runpath: [$ORIGIN]\n", "RPATH/RUNPATH"),
    (b"0x0 (RPATH) Library rpath: [/private]\n", "RPATH/RUNPATH"),
    (b"0x0 (NEEDED) Shared library: [../private.so]\n", "invalid ELF dependency"),
])
def test_elf_unsafe_paths_fail_without_executing_source(tmp_path, monkeypatch, output, match):
    tool = tmp_path / "readelf"
    tool.write_bytes(b"fake pinned inspection tool")
    target = tmp_path / "never-execute"

    def inspect(command, **kwargs):
        assert command[0].startswith("/proc/self/fd/")
        assert command[-1] == str(target)
        assert kwargs["env"] == {"LC_ALL": "C", "PATH": "/usr/bin:/bin"}
        kwargs["stdout"].write(output)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(builder.subprocess, "run", inspect)
    with pytest.raises(ValueError, match=match):
        builder._elf_info(target, (tool, pin(tool)["sha256"]))


def test_changed_inspector_is_not_executed(tmp_path, monkeypatch):
    tool = tmp_path / "readelf"
    tool.write_bytes(b"original")
    expected = pin(tool)["sha256"]
    tool.write_bytes(b"changed")
    monkeypatch.setattr(builder.subprocess, "run", lambda *a, **kw: pytest.fail("must not execute"))
    with pytest.raises(ValueError, match="readelf changed"):
        builder._elf_info(tmp_path / "target", (tool, expected))


def test_no_unsafe_publication_fallback(tmp_path, monkeypatch):
    source, destination = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    monkeypatch.setattr(builder.ctypes, "CDLL", lambda *args, **kwargs: SimpleNamespace())
    with pytest.raises(RuntimeError, match="no unsafe fallback"):
        builder._publish_noreplace(source, destination)
    assert source.is_dir() and not destination.exists()


def test_ambiguous_dependency_fails(inputs, monkeypatch):
    first = Path(inputs["library_dirs"][0])
    second = first / "second"
    second.mkdir(mode=0o755)
    for directory in (first, second):
        (directory / "same.so").write_bytes(b"\x7fELFlibrary")
        (directory / "same.so").chmod(0o644)
    inputs["library_dirs"].append(str(second))
    monkeypatch.setattr(builder, "_elf_info", lambda *args: {"needed": ["same.so"], "interpreter": None})
    with pytest.raises(ValueError, match="resolve uniquely"):
        builder.collect(inputs)


def test_non_public_ca_input_is_rejected(inputs):
    Path(inputs["ca"]["path"]).write_bytes(b"-----\x42EGIN PRIVATE KEY-----\nZmFrZQ==\n-----END PRIVATE KEY-----\n")
    inputs["ca"] = pin(Path(inputs["ca"]["path"]))
    with pytest.raises(ValueError, match="public system certificate bundle"):
        builder.collect(inputs)


def test_input_json_is_regular_bounded_and_unambiguous(tmp_path, inputs):
    path = tmp_path / "inputs.json"
    path.write_text(json.dumps(inputs))
    assert builder.load_inputs(path) == inputs
    path.write_bytes(b" " * 65537)
    with pytest.raises(ValueError, match="64 KiB"):
        builder.load_inputs(path)
    path.write_text('{"schema_version":1,"schema_version":2}')
    with pytest.raises(ValueError, match="duplicate input field"):
        builder.load_inputs(path)
    path.write_text("[]")
    with pytest.raises(ValueError, match="JSON object"):
        builder.load_inputs(path)


def test_input_fifo_cannot_block_or_be_read(tmp_path):
    path = tmp_path / "input-fifo"
    os.mkfifo(path)
    with pytest.raises(ValueError, match="regular file"):
        builder.load_inputs(path)


def test_input_spec_symlink_is_not_followed(tmp_path):
    actual = tmp_path / "actual.json"
    actual.write_text("{}")
    alias = tmp_path / "alias.json"
    alias.symlink_to(actual)
    with pytest.raises(OSError):
        builder.load_inputs(alias)


def test_growing_input_cannot_bypass_bounded_read(tmp_path, monkeypatch):
    path = tmp_path / "inputs.json"
    path.write_bytes(b" " * 65537)
    original = builder.os.fstat

    def previous_size(fd):
        info = original(fd)
        return SimpleNamespace(st_mode=info.st_mode, st_size=2)

    monkeypatch.setattr(builder.os, "fstat", previous_size)
    with pytest.raises(ValueError, match="64 KiB"):
        builder.load_inputs(path)


def test_external_cli_above_256mib_is_streamed_and_not_copied(inputs, monkeypatch):
    cli = Path(inputs["codex"]["path"])
    size = 298309560  # 0.170.0 Linux x86_64 wheel の CLI サイズ。sparse file で再現する。
    with cli.open("wb") as stream:
        stream.write(b"\x7fELF")
        stream.truncate(size)
    with cli.open("rb") as stream:
        inputs["codex"]["sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
    original = builder._read

    def copied_file_only(path, **kwargs):
        assert path != cli, "external CLI must never use the whole-file copy reader"
        return original(path, **kwargs)

    monkeypatch.setattr(builder, "_read", copied_file_only)
    manifest, files = builder.collect(inputs)
    external = manifest["external_executables"][0]
    assert external["size"] == size and external["sha256"] == inputs["codex"]["sha256"]
    assert external["destination"] is None and "elf" in external
    assert not any(item["source"] == str(cli) for item in manifest["files"])
    assert all(len(data) <= builder.MAX_FILE_BYTES for data in files.values())


def test_external_cli_has_separate_finite_cap(inputs, monkeypatch):
    monkeypatch.setattr(builder, "MAX_EXTERNAL_CLI_BYTES", 16)
    with pytest.raises(ValueError, match="oversized external Codex CLI"):
        builder.collect(inputs)


def test_copied_files_retain_256mib_cap(inputs):
    source = Path(inputs["python"]["path"])
    with source.open("wb") as stream:
        stream.write(b"\x7fELF")
        stream.truncate(builder.MAX_FILE_BYTES + 1)
    with pytest.raises(ValueError, match="oversized source"):
        builder.collect(inputs)


def test_external_cli_drift_during_inspection_is_rejected(inputs, monkeypatch):
    cli = Path(inputs["codex"]["path"])

    def inspect(path, tool):
        if path == cli:
            path.write_bytes(b"\x7fELFchanged-cli")
        return {"needed": [], "interpreter": None}

    monkeypatch.setattr(builder, "_elf_info", inspect)
    with pytest.raises(ValueError, match="ELF changed during inspection"):
        builder.collect(inputs)


@pytest.mark.parametrize("name", ["sitecustomize.py", "usercustomize.py", "startup.pth"])
@pytest.mark.parametrize("kind", ["regular", "external_symlink"])
@pytest.mark.parametrize("nested", [False, True])
def test_startup_hooks_are_excluded_before_resolve_or_read(tmp_path, inputs, monkeypatch, name, kind, nested):
    stdlib = Path(inputs["stdlib"])
    parent = stdlib / "pkg" if nested else stdlib
    parent.mkdir(mode=0o755, exist_ok=True)
    hook = parent / name
    outside = tmp_path / "external-customization"
    outside.write_text("must never be read\n")
    outside.chmod(0o644)
    if kind == "external_symlink":
        hook.symlink_to(outside)
    else:
        hook.write_text("must never be read\n")
        hook.chmod(0o644)
    original_resolve, original_read = Path.resolve, builder._read

    def guarded_resolve(path, *args, **kwargs):
        assert path not in {hook, outside}, "excluded hook must not be resolved"
        return original_resolve(path, *args, **kwargs)

    def guarded_read(path, **kwargs):
        assert path not in {hook, outside}, "excluded hook must not be opened/read"
        return original_read(path, **kwargs)

    monkeypatch.setattr(Path, "resolve", guarded_resolve)
    monkeypatch.setattr(builder, "_read", guarded_read)
    manifest, files = builder.collect(inputs)
    assert not any(Path(path).name == name for path in files)
    assert not any(item["source"] in {str(hook), str(outside)} for item in manifest["files"])


def test_pth_named_directory_is_not_traversed(inputs):
    hook = Path(inputs["stdlib"]) / "startup.pth"
    hook.mkdir(mode=0o755)
    (hook / "external.py").symlink_to("/must-not-follow/external.py")
    _, files = builder.collect(inputs)
    assert not any("startup.pth" in target for target in files)


def test_default_rootfs_parent_uses_xdg_or_home_without_personal_paths():
    from_xdg = builder.default_rootfs_parent({"XDG_DATA_HOME": "/var/lib/kotoyose", "HOME": "/srv/sample-user"})
    assert from_xdg == Path("/var/lib/kotoyose/kotoyose/rootfs")

    from_home = builder.default_rootfs_parent({"HOME": "/tmp/example"})
    assert from_home == Path("/tmp/example/.local/share/kotoyose/rootfs")
    assert "sample-user" not in str(from_home)
    assert ".task-hub" not in str(from_home)
    # DEFAULT_PARENT is resolved at import time; tests point XDG_DATA_HOME elsewhere, so compare its shape.
    assert builder.DEFAULT_PARENT.parts[-2:] == ("kotoyose", "rootfs")
    assert ".task-hub" not in str(builder.DEFAULT_PARENT)
