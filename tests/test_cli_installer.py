import base64
from dataclasses import replace
import hashlib
import io
import shutil
from pathlib import Path
import stat
import subprocess
import sys
import tarfile
from types import SimpleNamespace

import pytest

from kotoyose import cli_installer
from kotoyose.cli_install_spec import CliInstallSpec, NpmPlatformBinary, install_path
from kotoyose.print_cli_profiles import get_print_cli_profile, resolve_print_cli_binary


def _tar(entries):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, value in entries:
            member = tarfile.TarInfo(name)
            if isinstance(value, bytes):
                member.size = len(value)
                archive.addfile(member, io.BytesIO(value))
            else:
                member.type = tarfile.SYMTYPE
                member.linkname = value
                archive.addfile(member)
    return output.getvalue()


@pytest.fixture
def distribution(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr(cli_installer, "_platform_key", lambda: "linux-x64")

    def prepare(*, version="1.2.3", compression=None, member="package/bin/fake", extra=()):
        binary = f"""#!{sys.executable}
import os
from pathlib import Path
import sys
assert sys.argv[1:] == ["--version"]
assert set(os.environ) <= {{"HOME", "LC_CTYPE"}}
assert list(Path(os.environ["HOME"]).iterdir()) == []
assert not Path({str(tmp_path / "kotoyose/tools/fixture/1.2.3")!r}).exists()
print("fixture {version} (build)")
""".encode()
        if compression == "brotli":
            binary = subprocess.run(
                ["node", "-e", "process.stdout.write(require('node:zlib').brotliCompressSync(require('node:fs').readFileSync(0)))"],
                input=binary, capture_output=True, check=True, timeout=5,
            ).stdout
        data = _tar([
            (member, binary),
            ("package/package.json", b'{"scripts":{"postinstall":"touch script-ran"}}'),
            *extra,
        ])
        integrity = "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()
        spec = CliInstallSpec("1.2.3", (NpmPlatformBinary("linux-x64", "@sample/binary-linux-x64", integrity),),
                              "package/bin/fake", compression)
        profile = replace(get_print_cli_profile("grok"), id="fixture", binary="fake", install=spec)

        def download(url):
            assert url == "https://registry.npmjs.org/@sample/binary-linux-x64/-/binary-linux-x64-1.2.3.tgz"
            return data

        monkeypatch.setattr(cli_installer, "_download", download)
        return profile

    return prepare


@pytest.mark.skipif(shutil.which("node") is None, reason="brotli decompression runs the system node")
def test_brotli_publish_version_manifest_and_override(distribution, monkeypatch, tmp_path):
    profile = distribution(compression="brotli")
    assert resolve_print_cli_binary(profile) is None
    run = subprocess.run
    commands = []

    def record_command(argv, **kwargs):
        assert not kwargs.get("shell")
        commands.append(argv)
        return run(argv, **kwargs)

    monkeypatch.setattr(cli_installer.subprocess, "run", record_command)
    status = cli_installer.install_tool(profile)
    assert len(commands) == 2
    assert commands[0][0] == shutil.which("node") and commands[0][1] == "-e"
    assert Path(commands[1][0]).name == "fake"
    assert commands[1][1:] == ["--version"]
    path = Path(status.path)
    assert status.installed
    assert status.binary_version == "fixture 1.2.3 (build)"
    assert cli_installer.tool_status(profile) == status
    assert stat.S_IMODE(path.stat().st_mode) == 0o755
    assert {item.name for item in path.parent.iterdir()} == {"fake", "manifest.json"}
    assert resolve_print_cli_binary(profile) == path
    assert not list(tmp_path.rglob("script-ran"))
    settings = SimpleNamespace(drivers={"cli.print": {"profile_binaries": {"fixture": "/override"}}})
    assert resolve_print_cli_binary(profile, settings) == Path("/override")

    def unexpected_download(_):
        pytest.fail("The installed version must be reused")

    monkeypatch.setattr(cli_installer, "_download", unexpected_download)
    assert cli_installer.install_tool(profile) == status


@pytest.mark.parametrize(("kwargs", "message"), [
    ({"extra": (("package/../../escape", b"x"),)}, "Unsafe"),
    ({"extra": (("/escape", b"x"),)}, "Unsafe"),
    ({"member": "package/other", "extra": (("package/bin/fake", "../other"),)}, "Unsafe"),
    ({"version": "11.2.3"}, "unexpected version"),
])
def test_invalid_distribution_leaves_no_release(distribution, kwargs, message):
    profile = distribution(**kwargs)
    with pytest.raises(cli_installer.CliInstallError, match=message):
        cli_installer.install_tool(profile)
    assert not install_path(profile.id, profile.install.version, profile.binary).parent.exists()


def test_sri_mismatch_prevents_extraction(distribution, monkeypatch):
    profile = distribution()
    monkeypatch.setattr(cli_installer, "_download", lambda _: b"bad")
    with pytest.raises(cli_installer.CliInstallError, match="integrity mismatch"):
        cli_installer.install_tool(profile)
    assert resolve_print_cli_binary(profile) is None


def test_unpinned_platform_does_not_download(distribution, monkeypatch):
    distribution()

    def unexpected_download(_):
        pytest.fail("Unpinned distributions must never be downloaded")

    monkeypatch.setattr(cli_installer, "_download", unexpected_download)
    monkeypatch.setattr(cli_installer, "_platform_key", lambda: "linux-arm64")
    with pytest.raises(cli_installer.CliInstallError, match="integrity is unset"):
        cli_installer.install_tool(get_print_cli_profile("grok"))


def test_old_managed_version_in_use(distribution):
    profile = distribution()
    old = install_path(profile.id, "1.2.2", profile.binary)
    old.parent.mkdir(parents=True)
    shutil.copy2(shutil.which("sleep"), old)
    with subprocess.Popen([str(old), "20"], env={}) as process:
        try:
            with pytest.raises(cli_installer.CliInstallConflict, match="different managed CLI version"):
                cli_installer.install_tool(profile)
        finally:
            process.terminate()
    assert old.is_file()
    assert resolve_print_cli_binary(profile) is None



@pytest.mark.skipif(shutil.which("node") is None, reason="brotli decompression runs the system node")
def test_brotli_finds_node_outside_system_dirs(distribution, monkeypatch, tmp_path):
    """node outside /bin:/usr/bin (nvm, user installs, CI tool caches) is still used (CI failure 2026-11-21)."""
    profile = distribution(compression="brotli")
    bindir = tmp_path / "elsewhere"
    bindir.mkdir()
    (bindir / "node").symlink_to(shutil.which("node"))
    monkeypatch.setenv("PATH", str(bindir))
    run = subprocess.run
    commands = []

    def record_command(argv, **kwargs):
        commands.append(argv)
        return run(argv, **kwargs)

    monkeypatch.setattr(cli_installer.subprocess, "run", record_command)
    status = cli_installer.install_tool(profile)
    assert status.installed
    assert commands[0][0] == str(bindir / "node")


@pytest.mark.skipif(shutil.which("node") is None, reason="brotli decompression runs the system node")
def test_brotli_without_node_reports_a_clear_error(distribution, monkeypatch):
    profile = distribution(compression="brotli")
    monkeypatch.setattr(cli_installer.shutil, "which", lambda name: None)
    with pytest.raises(cli_installer.CliInstallError, match="needs Node.js"):
        cli_installer.install_tool(profile)
    assert not install_path(profile.id, profile.install.version, profile.binary).parent.exists()
