from dataclasses import replace
from contextlib import contextmanager
import hashlib
import io
import shutil
from pathlib import Path
import stat
import subprocess
import sys
import tarfile
from types import SimpleNamespace

import httpx
import pytest

from kotoyose import cli_installer
from kotoyose.cli_install_spec import CliArtifact, CliInstallSpec, install_path
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


def _fake_http(monkeypatch, request):
    @contextmanager
    def stream(method, url, **kwargs):
        assert method == "GET"
        assert kwargs["trust_env"] is False
        body = request(url)
        response = httpx.Response(
            200, request=httpx.Request(method, url), stream=httpx.ByteStream(body),
        )
        yield response

    monkeypatch.setattr(cli_installer.httpx, "stream", stream)


@pytest.fixture
def distribution(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr(cli_installer.install_spec, "platform_key", lambda: "linux-x64")

    def prepare(*, version="1.2.3", layout="member", member="package/bin/fake", extra=()):
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
        if layout == "directory":
            binary = binary.replace(b'print("fixture', b'assert (Path(__file__).parent.parent / "package.json").is_file()\nprint("fixture')
        data = _tar([
            (member, binary),
            ("package/package.json", b'{"scripts":{"postinstall":"touch script-ran"}}'),
            *extra,
        ])
        url = "https://vendor.invalid/{version}/{os}/{arch}.tar.gz"
        artifact = CliArtifact(
            "linux-x64", "linux", "x64", (url,), "sha512:" + hashlib.sha512(data).hexdigest(),
            format="tar.gz",
            **({"directory": "package", "entrypoint": "bin/fake"} if layout == "directory"
               else {"member": "package/bin/fake"}),
        )
        spec = CliInstallSpec("1.2.3", (artifact,))
        profile = replace(get_print_cli_profile("grok"), id="fixture", binary="fake", install=spec)

        def download(url):
            assert url == "https://vendor.invalid/1.2.3/linux/x64.tar.gz"
            return data

        _fake_http(monkeypatch, download)
        return profile

    return prepare


@pytest.mark.parametrize("layout", ["member", "directory"])
def test_publish_version_manifest_and_override(distribution, monkeypatch, tmp_path, layout):
    profile = distribution(layout=layout)
    assert resolve_print_cli_binary(profile) is None
    run = subprocess.run
    commands = []

    def record_command(argv, **kwargs):
        assert not kwargs.get("shell")
        commands.append(argv)
        return run(argv, **kwargs)

    monkeypatch.setattr(cli_installer.subprocess, "run", record_command)
    status = cli_installer.install_tool(profile)
    assert len(commands) == 1
    assert Path(commands[0][0]).name == "fake"
    assert commands[0][1:] == ["--version"]
    path = Path(status.path)
    assert status.installed
    assert status.binary_version == "fixture 1.2.3 (build)"
    assert cli_installer.tool_status(profile) == status
    assert stat.S_IMODE(path.stat().st_mode) == 0o755
    release = install_path(profile.id, profile.install.version, profile.binary).parent
    if layout == "directory":
        assert path == release / "bin/fake"
        assert {item.name for item in release.iterdir()} == {"bin", "package.json", "manifest.json"}
    else:
        assert {item.name for item in release.iterdir()} == {"fake", "manifest.json"}
    assert resolve_print_cli_binary(profile) == path
    assert not list(tmp_path.rglob("script-ran"))
    settings = SimpleNamespace(drivers={"cli.print": {"profile_binaries": {"fixture": "/override"}}})
    assert resolve_print_cli_binary(profile, settings) == Path("/override")

    def unexpected_download(_):
        pytest.fail("The installed version must be reused")

    _fake_http(monkeypatch, unexpected_download)
    assert cli_installer.install_tool(profile) == status


@pytest.mark.parametrize(("kwargs", "message"), [
    ({"extra": (("package/../../escape", b"x"),)}, "Unsafe"),
    ({"extra": (("/escape", b"x"),)}, "Unsafe"),
    ({"member": "package/other", "extra": (("package/bin/fake", "../other"),)}, "Unsafe"),
    ({"extra": (("package/escape", "../../outside"),)}, "Unsafe"),
    ({"version": "11.2.3"}, "unexpected version"),
    ({"version": "1.2.3-rc"}, "unexpected version"),
])
@pytest.mark.parametrize("layout", ["member", "directory"])
def test_invalid_distribution_leaves_no_release(distribution, kwargs, message, layout):
    profile = distribution(layout=layout, **kwargs)
    with pytest.raises(cli_installer.CliInstallError, match=message):
        cli_installer.install_tool(profile)
    assert not install_path(profile.id, profile.install.version, profile.binary).parent.exists()


@pytest.mark.parametrize("layout", ["member", "directory"])
def test_digest_mismatch_prevents_extraction(distribution, monkeypatch, layout):
    profile = distribution(layout=layout)

    def unexpected_extract(*args, **kwargs):
        pytest.fail("An unverified artifact must never be extracted")

    _fake_http(monkeypatch, lambda _: b"bad")
    monkeypatch.setattr(cli_installer, "_extract", unexpected_extract)
    with pytest.raises(cli_installer.CliInstallError, match="integrity mismatch"):
        cli_installer.install_tool(profile)
    assert resolve_print_cli_binary(profile) is None
    assert not install_path(profile.id, profile.install.version, profile.binary).parent.exists()


@pytest.fixture
def direct_distribution(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr(cli_installer.install_spec, "platform_key", lambda: "linux-x64")
    profile = get_print_cli_profile("grok")
    binary = f"#!{sys.executable}\nprint('grok 1.0.89')\n".encode()
    entry = replace(profile.install.artifacts[0], digest="sha256:" + hashlib.sha256(binary).hexdigest())
    return replace(profile, install=replace(profile.install, artifacts=(entry,))), binary


@pytest.mark.parametrize("primary_fails", [False, True])
def test_direct_binary_install_without_node(direct_distribution, monkeypatch, primary_fails):
    profile, binary = direct_distribution
    primary = "https://x.ai/cli/grok-1.0.89-linux-x86_64"
    fallback = "https://storage.googleapis.com/grok-build-public-artifacts/cli/grok-1.0.89-linux-x86_64"
    requests = []

    def download(url):
        requests.append(url)
        assert 207850592 <= cli_installer._MAX_DOWNLOAD <= 256 * 1024**2
        if url == primary and primary_fails:
            raise httpx.ConnectError("primary unavailable")
        return binary

    _fake_http(monkeypatch, download)
    monkeypatch.setattr(cli_installer.shutil, "which", lambda _: None)
    status = cli_installer.install_tool(profile)

    assert requests == ([primary, fallback] if primary_fails else [primary])
    assert status == cli_installer.tool_status(profile)
    assert status.installed
    assert status.binary_version == "grok 1.0.89"
    assert Path(status.path).read_bytes() == binary


def test_direct_binary_hash_mismatch_prevents_execution(direct_distribution, monkeypatch):
    profile, binary = direct_distribution

    def unexpected_run(*args, **kwargs):
        pytest.fail("An unverified binary must never run")

    _fake_http(monkeypatch, lambda _: binary + b"# corrupted\n")
    monkeypatch.setattr(cli_installer.subprocess, "run", unexpected_run)
    with pytest.raises(cli_installer.CliInstallError, match="integrity mismatch"):
        cli_installer.install_tool(profile)
    assert not install_path(profile.id, profile.install.version, profile.binary).parent.exists()


def test_raw_artifact_reuses_existing_member_install(distribution, monkeypatch):
    profile = distribution()
    status = cli_installer.install_tool(profile)

    def unexpected_download(*args, **kwargs):
        pytest.fail("The installed version must be reused across distribution methods")

    _fake_http(monkeypatch, unexpected_download)
    direct = replace(get_print_cli_profile("grok").install, version=profile.install.version)
    assert cli_installer.install_tool(replace(profile, install=direct)) == status


@pytest.mark.parametrize("platform", ["linux-arm64", "darwin-arm64"])
def test_unpinned_platform_does_not_download(tmp_path, monkeypatch, platform):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))

    def unexpected_download(*args, **kwargs):
        pytest.fail("Unpinned distributions must never be downloaded")

    _fake_http(monkeypatch, unexpected_download)
    monkeypatch.setattr(cli_installer.install_spec, "platform_key", lambda: platform)
    with pytest.raises(cli_installer.CliInstallError, match="not available for this platform"):
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
