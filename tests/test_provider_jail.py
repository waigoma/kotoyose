"""Provider CLI launcher の argv、環境 allowlist、filesystem / PID 隔離を検証する。"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from kotoyose.provider_jail import build_jail_command, main, provider_environment


def _env(**credentials: str) -> dict[str, str]:
    return provider_environment({"PATH": "/host/path", **credentials})


def test_build_jail_command_preserves_all_cli_arguments_without_secret():
    """SDK が固定した argv は shell を通さず、順序と文字列を保ったまま末尾へ渡す。"""
    args = ["--settings", "/tmp/provider.json", "--mcp-config", '{"x": true}', ""]
    command = build_jail_command(Path("/usr/bin/env"), args)

    assert command[-len(args) :] == args
    assert command[-len(args) - 1] == "/runtime/claude"
    assert "sh" not in command[: -len(args)]
    assert all("super-secret" not in value for value in command)


def test_jail_command_masks_host_root_and_managed_configuration():
    """空 root と列挙 mount により host home、managed settings、project files を公開しない。"""
    command = build_jail_command(Path("/usr/bin/env"), [])

    assert command[: command.index("--tmpfs", 1) + 2] == [
        "bwrap", "--unshare-all", "--share-net", "--die-with-parent", "--new-session",
        "--cap-drop", "ALL", "--tmpfs", "/",
    ]
    assert ("--tmpfs", "/home") == tuple(command[command.index("--tmpfs", command.index("/tmp") + 1) :][:2])
    assert ("--dir", "/home/kotoyose") == tuple(command[command.index("--dir", command.index("/home") + 1) :][:2])
    assert "/srv/sample-user" not in command
    assert "/etc/claude-code" not in command
    assert "/home/.claude" not in command
    assert command.count("--ro-bind") >= 5


@pytest.mark.parametrize(
    "source",
    [
        {},
        {"ANTHROPIC_API_KEY": "one", "CLAUDE_CODE_OAUTH_TOKEN": "two"},
        {"ANTHROPIC_API_KEY": "", "CLAUDE_CODE_OAUTH_TOKEN": ""},
    ],
)
def test_provider_environment_rejects_missing_or_ambiguous_credentials(source):
    """credential がない、空、複数名に存在する場合は provider を起動しない。"""
    with pytest.raises(ValueError, match="exactly one"):
        provider_environment(source)


def test_provider_environment_is_exact_allowlist_and_keeps_only_selected_secret():
    """固定値と選択した credential だけを child env に残す。"""
    source = {
        "KOTOYOSE_NATIVE_CLAUDE_BINARY": "/tmp/claude",
        "CLAUDE_CODE_OAUTH_TOKEN": "secret-token",
        "ANTHROPIC_API_KEY": "",
        "ANTHROPIC_AUTH_TOKEN": "",
        "HTTPS_PROXY": "http://proxy.invalid",
        "HOST_SECRET": "do-not-forward",
        "TERM": "xterm",
    }
    environment = provider_environment(source)

    assert environment == {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": "/home/kotoyose",
        "LANG": "C.UTF-8",
        "CLAUDE_CODE_ENTRYPOINT": "sdk-py",
        "CLAUDE_CODE_OAUTH_TOKEN": "secret-token",
    }


def test_main_executes_bwrap_with_allowlisted_environment_and_no_secret_in_argv(monkeypatch):
    """launcher は最後に execve し、管理者 env や secret を argv へ複製しない。"""
    monkeypatch.setenv("KOTOYOSE_NATIVE_CLAUDE_BINARY", "/usr/bin/env")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret-token")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    with (
        patch("kotoyose.provider_jail.sys.argv", ["native-claude", "--help", "--settings", "cfg"]),
        patch("kotoyose.provider_jail.shutil.which", return_value="/usr/bin/bwrap"),
        patch("kotoyose.provider_jail.os.execve", side_effect=RuntimeError("captured")) as execve,
    ):
        with pytest.raises(RuntimeError, match="captured"):
            main()

    executable, argv, environment = execve.call_args.args
    assert executable == "/usr/bin/bwrap"
    assert argv[-4:] == ["/runtime/claude", "--help", "--settings", "cfg"]
    assert "secret-token" not in argv
    assert "KOTOYOSE_NATIVE_CLAUDE_BINARY" not in environment
    assert environment["ANTHROPIC_API_KEY"] == "secret-token"


def test_binary_requires_absolute_canonical_executable_and_allows_symlink(tmp_path):
    """relative、directory、非 executable は拒み、executable への symlink は canonical 化する。"""
    with pytest.raises(ValueError, match="absolute"):
        build_jail_command(Path("../usr/bin/env"), [])
    with pytest.raises(ValueError, match="executable"):
        build_jail_command(tmp_path, [])
    plain = tmp_path / "plain"
    plain.write_text("not executable")
    with pytest.raises(ValueError, match="executable"):
        build_jail_command(plain, [])
    link = tmp_path / "claude-link"
    link.symlink_to("/usr/bin/env")
    command = build_jail_command(link, [])
    runtime_bind = command.index("/runtime/claude") - 2
    assert command[runtime_bind : runtime_bind + 3] == ["--ro-bind", "/usr/bin/env", "/runtime/claude"]


def test_real_bwrap_hides_host_secret_environment_and_filesystem():
    """課金 provider を呼ばず、shell binary で env と host file の不可視性を確認する。"""
    if not Path("/usr/bin/sh").is_file() or not Path("/usr/bin/bwrap").exists():
        pytest.skip("bwrap or /usr/bin/sh is unavailable")
    command = build_jail_command(
        Path("/usr/bin/sh"),
        [
            "-c",
            "test \"$CLAUDE_CODE_OAUTH_TOKEN\" = provider-secret && "
            "test -z \"${HOST_ONLY+x}\" && "
            'test ! -e "$1" && test ! -e /etc/claude-code && '
            "test ! -e /tmp/host-secret-marker",
            "fixture-sh",
            str(Path.home()),
        ],
    )
    result = subprocess.run(
        command,
        env=provider_environment({
            "CLAUDE_CODE_OAUTH_TOKEN": "provider-secret",
            "HOST_ONLY": "host-secret",
            "HTTPS_PROXY": "http://proxy.invalid",
        }),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""


def test_native_claude_wrapper_resolves_src_from_an_unrelated_cwd():
    """wrapper は cwd や repository の Python 環境に依存せず provider jail を呼び出す。"""
    if not Path("/usr/bin/env").is_file() or not Path("/usr/bin/bwrap").exists():
        pytest.skip("bwrap or /usr/bin/env is unavailable")
    wrapper = Path(__file__).resolve().parents[1] / "scripts" / "native-claude"
    source = {
        "PATH": "/usr/bin:/bin",
        "KOTOYOSE_NATIVE_CLAUDE_BINARY": "/usr/bin/env",
        "ANTHROPIC_API_KEY": "provider-secret",
    }
    result = subprocess.run(
        [str(wrapper), "-0"], cwd="/tmp", env=source, capture_output=True, timeout=10, check=False,
    )
    assert result.returncode == 0, result.stderr.decode()
    environment = dict(item.split(b"=", 1) for item in result.stdout.split(b"\0") if item)
    assert environment == {
        b"PATH": b"/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        b"HOME": b"/home/kotoyose",
        b"LANG": b"C.UTF-8",
        b"CLAUDE_CODE_ENTRYPOINT": b"sdk-py",
        b"ANTHROPIC_API_KEY": b"provider-secret",
        # bwrap は --chdir の結果として固定された PWD を自動設定する。
        b"PWD": b"/tmp",
    }


def test_real_bwrap_die_with_parent_stops_provider_descendant():
    """parent launcher kill 後に PID namespace 内の child が残らない。"""
    if not Path("/usr/bin/sh").is_file() or not Path("/usr/bin/bwrap").exists():
        pytest.skip("bwrap or /usr/bin/sh is unavailable")
    marker = "123.4567"
    process = subprocess.Popen(
        build_jail_command(Path("/usr/bin/sh"), ["-c", f"sleep {marker}"]),
        env=_env(ANTHROPIC_API_KEY="provider-secret"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        time.sleep(0.25)
        process.kill()
        process.wait(timeout=3)
        time.sleep(0.1)
        ps = subprocess.run(["ps", "-eo", "args="], capture_output=True, text=True, check=True)
        assert marker not in ps.stdout
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
