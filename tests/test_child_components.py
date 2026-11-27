"""Provider discovery stays on the fixed catalog and does not launch CLIs until asked."""

from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys

import pytest

from kotoyose import child_local_login, cli_installer, print_cli_profiles
from kotoyose.child_catalog import catalog_document, host_connections
from kotoyose.child_pinned import login_spec, quota_command, status_spec
from kotoyose.child_tools import change_tool
from kotoyose.cli_install_spec import CliArtifact, CliInstallSpec
from kotoyose.provider_spec import LocalLogin
from kotoyose.child_components import _windows_system_cli, cli_choice, probe, providers
from kotoyose.child_ops import resolve_child_binary


def test_cli_choice_ignores_invalid_app_json(tmp_path):
    assert cli_choice(tmp_path, "claude") == "bundled"
    for raw in ("{", "[]", '"system"', "null"):
        (tmp_path / "app.json").write_text(raw, encoding="utf-8")
        assert cli_choice(tmp_path, "claude") == "bundled"
    (tmp_path / "app.json").write_text('{"claude_cli": "system", "codex_cli": "nope"}', encoding="utf-8")
    assert cli_choice(tmp_path, "claude") == "system"
    assert cli_choice(tmp_path, "codex") == "bundled"


def test_system_cli_prefers_exe_over_an_earlier_cmd(monkeypatch, tmp_path):
    earlier = tmp_path / "earlier"
    later = tmp_path / "later"
    earlier.mkdir()
    later.mkdir()
    cmd = earlier / "codex.cmd"
    exe = later / "codex.exe"
    cmd.write_text("@echo off\r\n", encoding="utf-8")
    exe.write_bytes(b"MZ")
    cmd.chmod(0o755)
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join((str(earlier), str(later))))
    monkeypatch.delenv("USERPROFILE", raising=False)
    row = next(row for row in providers() if row["id"] == "codex")
    assert _windows_system_cli(row) == exe


def test_probe_reports_presence_without_running_a_cli(monkeypatch, tmp_path):
    calls = []

    def fake_run(args, **_kwargs):
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 1)

    cli = tmp_path / "claude"
    cli.write_text("#!/bin/sh\necho leaked\n", encoding="utf-8")
    cli.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr("kotoyose.child_components.subprocess.run", fake_run)
    rows = {row["id"]: row for row in probe(tmp_path, versions=False)}
    assert calls == []
    assert rows["claude"]["system_cli"] is True
    assert rows["claude"]["system_version"] is None
    assert "path" not in rows["claude"]
    checked = {row["id"]: row for row in probe(tmp_path, versions=True)}
    assert calls == [[str(cli), "--version"]]
    assert checked["claude"]["system_cli"] is True
    assert checked["claude"]["system_version"] is None


def test_resolve_child_binary_uses_the_exe_inside_a_cmd_shim(monkeypatch, tmp_path):
    monkeypatch.setattr("kotoyose.child_ops.sys.platform", "win32")
    exe = tmp_path / "codex.exe"
    shim = tmp_path / "codex.cmd"
    exe.write_bytes(b"MZ")
    shim.write_text(f'@"{exe}" %*\r\n', encoding="utf-8")
    monkeypatch.setattr("kotoyose.child_components.selected_cli", lambda *_args, **_kwargs: shim)
    assert resolve_child_binary("codex", None, tmp_path) == exe

    script = tmp_path / "cli.js"
    script.write_text("", encoding="utf-8")
    shim.write_text(f'@"{script}" %*\r\n', encoding="utf-8")
    with pytest.raises(FileNotFoundError, match="did not resolve to an .exe"):
        resolve_child_binary("codex", None, tmp_path)


ROOT = Path(__file__).resolve().parents[1]
GOLDEN = json.loads((ROOT / "tests/fixtures/child_stage_a_golden.json").read_text(encoding="utf-8"))
PAYLOAD = runpy.run_path(str(ROOT / "child-app/installer/build_payload.py"))


def test_installer_repair_failure_dialog_names_the_log():
    installer = (ROOT / "child-app/installer/setup.nsi").read_text(encoding="utf-8")
    dialog = next(line for line in installer.splitlines() if "MessageBox" in line and "runtime packages could not be installed" in line)
    assert r"$LOCALAPPDATA\kotoyose-child\logs\repair.log" in dialog
    assert "(status: $0)" in dialog


def test_installer_falls_back_to_execwait_when_nsexec_cannot_start():
    installer = (ROOT / "child-app/installer/setup.nsi").read_text(encoding="utf-8")
    helper = installer.split("Function RunChildApp\n", 1)[1].split("FunctionEnd", 1)[0]
    assert 'Push "__nsexec_missing__"' in helper
    assert "nsExec::ExecToLog '$R0'" in helper
    assert '${If} $0 == "__nsexec_missing__"' in helper
    assert '${OrIf} $0 == "error"' in helper
    assert '${OrIf} $0 < 0' in helper
    assert "ExecWait '$R0' $0" in helper
    assert 'StrCpy $0 "exec-error"' in helper

    install = installer.split("Section -Install\n", 1)[1].split("SectionEnd", 1)[0]
    assert 'StrCpy $R0 \'"${APP}" repair\'' in install
    assert 'StrCpy $R0 \'"${APP}" repair --previous "$PreviousVersion"\'' in install
    assert 'StrCpy $R0 \'"${APP}" autostart $1\'' in install
    assert install.count("Call RunChildApp") == 2
    assert 'WriteRegStr HKCU "${RUN}" "KotoyoseChild" \'"${BACKGROUND}"\'' in install
    assert 'DeleteRegValue HKCU "${RUN}" "KotoyoseChild"' in install


def test_installer_and_uninstaller_confirm_before_closing_the_child():
    installer = (ROOT / "child-app/installer/setup.nsi").read_text(encoding="utf-8")
    for prefix, name in (("", ".onInit"), ("un.", "un.onInit")):
        init = installer.split(f"Function {name}\n", 1)[1].split("FunctionEnd", 1)[0]
        assert init.index("!insertmacro AcquireSetupMutex") < init.index(f"Call {prefix}StopChild")
        assert f'!insertmacro StopChild "{prefix}"' in installer
    flow = installer.split("Function ${PREFIX}StopChild", 1)[1].split("FunctionEnd", 1)[0]
    check, close = flow.split("close_child:\n", 1)
    assert check.index("!insertmacro CheckChildLock") < check.index('${If} $0 == "free"\n        Return')
    prompt, decline = check.split('"Kotoyose Child is running. Close it and continue?"', 1)
    assert "MB_YESNO|MB_DEFBUTTON2" in prompt
    assert "/SD IDYES IDYES close_child\n    SetErrorLevel 1\n    Quit" in decline
    assert close.index('" close ') < close.index("${If} $0 == 0\n        Return") < close.index("Quit")


def test_installer_checks_the_lock_without_starting_the_helper():
    installer = (ROOT / "child-app/installer/setup.nsi").read_text(encoding="utf-8")
    native = installer.split("!macro CheckChildLock\n", 1)[1].split("!macroend", 1)[0]
    flow = installer.split("Function ${PREFIX}StopChild", 1)[1].split("FunctionEnd", 1)[0]
    check, close = flow.split("close_child:\n", 1)
    assert "kernel32::CreateFileW" in native
    assert "kernel32::LockFile" in native
    assert "kernel32::CloseHandle" in native
    assert "stop-child.exe" not in native + check
    assert "nsExec::" not in native + check
    assert "nsExec::Exec /TIMEOUT=60000" in close

    for branch, codes in ((check, ["Win32 error $1"]), (close, ["nsExec $0", "nsExec $0", "helper exit code $0"])):
        dialogs = [line for line in branch.splitlines() if "MessageBox MB_ICONSTOP" in line]
        assert len(dialogs) == len(codes)
        for dialog, code in zip(dialogs, codes, strict=True):
            assert code in dialog
            assert dialog.endswith("/SD IDOK")


def test_installer_names_every_provider_repair_marker():
    installer = (ROOT / "child-app/installer/setup.nsi").read_text(encoding="utf-8")
    catalog = json.loads((ROOT / "src/kotoyose/child_components.json").read_text(encoding="utf-8"))
    assert catalog == json.loads(json.dumps(catalog_document(providers())))
    assert r'!include "${PAYLOAD}\provider-repairs.nsh"' in installer
    sdk = [row for row in catalog["providers"] if row["packages"]]
    markers = PAYLOAD["repair_markers"](sdk)
    assert markers.count("${FileExists}") == len(sdk)
    for row in sdk:
        assert f'{row["id"]}.needs-reinstall' in markers


def test_claude_and_codex_preserve_the_stage_a_catalog_and_pins():
    rows = {row["id"]: row for row in json.loads(json.dumps(providers()))}
    for before in GOLDEN["catalog"]:
        provider = before["id"]
        assert {key: rows[provider][key] for key in before} == before
        spec, flow = login_spec(provider, None)
        assert status_spec(provider, None) is spec
        pins = {"spec": asdict(spec), "flow": flow, "quota": quota_command(provider, None)}
        assert json.loads(json.dumps(pins)) == GOLDEN["login"][provider]


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is required to export the installer lock")
def test_windows_requirements_match_stage_a(tmp_path, monkeypatch):
    monkeypatch.chdir(ROOT)
    parts = tmp_path / "bin/parts"
    parts.mkdir(parents=True)
    PAYLOAD["main"](tmp_path)
    assert {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in parts.iterdir()} == GOLDEN["requirements_sha256"]


@pytest.mark.skipif(sys.platform != "linux", reason="Local PTY and Linux managed installer fixture")
async def test_cli_profile_data_drives_install_login_and_status(tmp_path, monkeypatch, capsys):
    login, _ = login_spec("claude", None)
    profile = replace(
        print_cli_profiles.AGY_PROFILE,
        id="example", provider="example", display_name="Example CLI", binary="example-cli",
        install=CliInstallSpec("1.2.3", (
            CliArtifact("linux-x64", "linux", "x64", ("https://example.invalid/cli",), "sha256:" + "a" * 64),
            CliArtifact("windows-x64", "windows", "x64", ("https://example.invalid/cli.exe",)),
        )),
        login=replace(login, command=("sign-in",), status_command=("session", "check")),
        login_flow="paste_code", child_local_login=LocalLogin((("linux", "pty"),)),
        terminal_login="command",
    )
    monkeypatch.setitem(print_cli_profiles._REGISTRY, profile.id, profile)
    monkeypatch.setattr("platform.machine", lambda: "x86_64")
    monkeypatch.setenv("PATH", str(tmp_path))
    log = tmp_path / "argv"
    source = f"""#!{sys.executable}
import sys
from pathlib import Path

if sys.argv[1:] == ["--version"]:
    print("1.2.3")
else:
    with Path({str(log)!r}).open("a") as stream:
        print(" ".join(sys.argv[1:]), file=stream)
    if sys.argv[1:] == ["session", "check"]:
        print('{{"loggedIn": true}}')
"""

    def fetch(spec, artifact, destination):
        assert spec is profile.install
        assert artifact.digest == "sha256:" + "a" * 64
        destination.write_text(source, encoding="utf-8")

    monkeypatch.setattr(cli_installer, "_fetch", fetch)
    row = next(row for row in providers() if row["id"] == profile.id)
    assert row["name"] == "Example CLI"
    assert row["packages"] == ()
    assert row["managed"] == {"version": "1.2.3", "platforms": ["linux-x64"]}
    assert row["login"] == {
        "command": ["example-cli", "sign-in"], "cli_name": "Example CLI", "flow": "paste_code",
        "transports": {"linux": "pty"}, "status": ("session", "check"),
    }
    host = host_connections()["example"]
    assert host["profile"] == "example"
    assert host["terminal"]["argv"] == ["example-cli", "sign-in"]

    change_tool(tmp_path, profile.id, "install")
    installed = next(row for row in probe(tmp_path, versions=False) if row["id"] == profile.id)
    assert installed["managed_available"] and installed["bundled_cli"]
    assert not installed["turns_supported"] and not installed["sdk_importable"]

    parent = child_local_login._Parent()
    await child_local_login.run(tmp_path, profile.id, "start", "linux", parent)
    await child_local_login.run(tmp_path, profile.id, "status", "linux", parent)
    assert log.read_text().splitlines() == ["sign-in", "session check", "session check"]
    assert [json.loads(line)["state"] for line in capsys.readouterr().out.splitlines()] == [
        "running", "logged_in", "logged_in",
    ]

    with monkeypatch.context() as scoped:
        scoped.setattr(cli_installer, "_binary_in_use", lambda _: True)
        with pytest.raises(cli_installer.CliInstallConflict, match="in use"):
            change_tool(tmp_path, profile.id, "remove")
    change_tool(tmp_path, profile.id, "remove")
    assert not next(row for row in probe(tmp_path, versions=False) if row["id"] == profile.id)["bundled_cli"]
