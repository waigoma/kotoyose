"""Public release boundaries exercised without a real repository or secrets."""

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
EXPORT = ROOT / "scripts/export-public.py"
CHECK = ROOT / "scripts/check-public.py"


@pytest.fixture
def source(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("public_export", EXPORT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    repo = tmp_path / "source"
    (repo / "release").mkdir(parents=True)
    (repo / "release/public-paths.txt").write_text("src/\n!src/private/\n/docs/**/*.md\n")
    tracked = ("src/tool", "src/private/note", "docs/index.md", "docs/guide/note.md")
    for name in (*tracked, "src/untracked"):
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name)
    (repo / "src/tool").chmod(0o751)

    def ls_files(command):
        assert command == ["git", "-C", str(repo), "ls-files", "-z", "--cached"]
        return b"\0".join(os.fsencode(name) for name in (*tracked, ""))

    monkeypatch.setattr(module.subprocess, "check_output", ls_files)
    return module, repo


def test_export_matches_tracked_paths_and_preserves_mode(source, tmp_path, capsys):
    module, repo = source
    out = tmp_path / "out"
    module.export(repo, out)
    assert {p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()} == {
        "src/tool", "docs/index.md", "docs/guide/note.md",
    }
    assert (out / "src/tool").stat().st_mode & 0o777 == 0o751
    assert "Exported 3 files, excluded 1" in capsys.readouterr().out
    with pytest.raises(ValueError, match="empty directory"):
        module.export(repo, out)


@pytest.mark.parametrize("safe", [False, True])
def test_export_only_preserves_links_to_exported_files(source, tmp_path, safe):
    module, repo = source
    tool = repo / "src/tool"
    tool.unlink()
    external = tmp_path / "external"
    external.write_text("private")
    tool.symlink_to("../docs/index.md" if safe else "../../external")
    out = tmp_path / "out"
    if safe:
        module.export(repo, out)
        assert (out / "src/tool").is_symlink()
        assert (out / "src/tool").read_text() == "docs/index.md"
    else:
        with pytest.raises(ValueError, match="symlink"):
            module.export(repo, out)
        assert not out.exists()


def test_checker_reports_only_locations_and_kinds(tmp_path, monkeypatch):
    monkeypatch.setenv("PUBLIC_DENYLIST", "  SAMPLE-INTERNAL-TERM  \n\n")
    cases = {
        "denylist": "sample-internal-term",
        "absolute-home": "/home/" + "operator/project",
        "private-ipv4": ".".join(("100", "127", "0", "2")),
        "email": "private-user@" + "company.invalid",
        "secret-pattern": "sk" + "-" + "SyntheticFixture123456789",
    }
    path = tmp_path / "input.txt"
    path.write_text("\n".join(cases.values()))
    result = subprocess.run([sys.executable, CHECK, tmp_path], capture_output=True, text=True)
    assert result.returncode == 1
    for number, (kind, value) in enumerate(cases.items(), 1):
        assert f"input.txt:{number}: {kind}" in result.stdout
        assert value not in result.stdout + result.stderr


def test_checker_accepts_scoped_fake_ip_and_public_email(tmp_path, monkeypatch):
    monkeypatch.delenv("PUBLIC_DENYLIST", raising=False)
    tools = tmp_path / "tools"
    (tools / "scripts").mkdir(parents=True)
    (tools / "release").mkdir()
    script = shutil.copyfile(CHECK, tools / "scripts/check-public.py")
    ip = ".".join(("172", "16", "0", "2"))
    (tools / "release/public-fake-ips.txt").write_text(f"fixture.txt {ip}\n")
    export = tmp_path / "export"
    export.mkdir()
    (export / "fixture.txt").write_text(f"{ip}\nuser@example.com\n")
    result = subprocess.run([sys.executable, script, export], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    (export / "other.txt").write_text(ip)
    result = subprocess.run([sys.executable, script, export], capture_output=True, text=True)
    assert result.returncode == 1
    assert "other.txt:1: private-ipv4" in result.stdout
