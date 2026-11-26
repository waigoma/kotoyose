"""Windows child (K4). The suite runs on Linux and drives the Windows branches with fakes."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_POSIX_ONLY = ("fcntl", "pty", "termios", "tty", "pwd", "grp", "resource")


def test_child_turn_and_acp_import_without_posix_only_modules():
    """The claude turn the child actually builds, and kotoyose-acp, never load PTY or pwd modules."""
    script = r"""
import asyncio
import sys
for name in ("fcntl", "pty", "termios", "tty", "pwd", "grp", "resource"):
    sys.modules[name] = None
import kotoyose.acp_agent
import kotoyose.child_cli
import kotoyose.child_client
import kotoyose.child_turn
import kotoyose.runtime.child
from kotoyose.config import HarnessConfig
import kotoyose.session as session

class Fake:
    def __init__(self, options=None):
        self.options = options
    async def connect(self):
        return None
    async def disconnect(self):
        return None

session.ClaudeSDKClient = Fake
config = HarnessConfig.model_validate({
    "connections": {"c": {"provider": "claude", "kind": "subscription", "data_policy": "no_training",
                          "config_dir": "/tmp/claude-login"}},
    "routes": {"r": {"driver": "claude.agent-sdk", "connection": "c", "model": "claude-haiku-5"}},
    "models": {"a": {"route": "r", "cwd": "/tmp", "allow_cwd_override": True, "cwd_allowed_roots": ["/tmp"]}},
})

async def main():
    runtime = await kotoyose.child_turn._build_runtime({"alias": "a", "config": config, "cwd": "/tmp"})
    await runtime.disconnect()

asyncio.run(main())
print("IMPORT_OK")
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "IMPORT_OK" in result.stdout


def test_cli_login_import_does_not_load_posix_only_modules():
    """PTY modules stay out of cli_login and cli_installer until a login or install runs."""
    script = r"""
import sys
for name in ("fcntl", "pty", "termios", "tty", "pwd", "grp", "resource"):
    sys.modules[name] = None
import kotoyose.cli_login
import kotoyose.cli_installer
print("IMPORT_OK")
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "IMPORT_OK" in result.stdout


def test_windows_child_home_is_localappdata(monkeypatch):
    from kotoyose.child_identity import default_child_home

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("KOTOYOSE_CHILD_HOME", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\Alice\AppData\Local")
    monkeypatch.setenv("HOME", r"D:\not-used")
    assert default_child_home() == Path(r"C:\Users\Alice\AppData\Local") / "kotoyose-child"
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setenv("USERPROFILE", r"C:\Users\Alice")
    assert default_child_home() == Path(r"C:\Users\Alice") / "AppData" / "Local" / "kotoyose-child"


def test_windows_private_files_do_not_require_unix_modes(monkeypatch, tmp_path):
    """chmod is meaningless on Windows. Creating the child home must not fail without it."""
    from kotoyose.child_identity import ensure_child_identity, ensure_default_policy

    monkeypatch.setattr(sys, "platform", "win32")

    def boom(*_args, **_kwargs):
        raise AssertionError("chmod")

    monkeypatch.setattr(Path, "chmod", boom)
    ensure_default_policy(tmp_path)
    identity = ensure_child_identity(tmp_path)
    assert (tmp_path / "policy.json").is_file()
    assert identity.key_file.is_file()
    assert identity.certificate_file.is_file()


def test_windows_api_key_file_skips_unix_mode_bits(monkeypatch, tmp_path):
    from kotoyose.acp_agent import _api_key

    key = tmp_path / "key"
    key.write_text("kys_file\n", encoding="utf-8")
    key.chmod(0o666)
    monkeypatch.setattr(sys, "platform", "win32")
    assert _api_key({"KOTOYOSE_API_KEY_FILE": str(key)}) == "kys_file"


def test_host_lexical_cwd_is_case_insensitive_for_windows_roots():
    from kotoyose.chat_policy import _lexically_inside

    assert _lexically_inside(r"C:\work\proj\src", [r"c:\Work\Proj"])
    assert _lexically_inside(r"c:/work/proj", [r"C:\work\proj"])
    assert not _lexically_inside(r"C:\work\projx", [r"C:\work\proj"])
    assert _lexically_inside(r"\\server\share\proj", [r"\\server\share"])
    assert not _lexically_inside(r"C:\other", [r"C:\work\proj"])


def test_windows_cwd_expands_short_names_junctions_and_extended_prefixes(monkeypatch):
    from kotoyose.child_turn import ChildTurnError, _resolve_cwd_windows

    def long_path(path: str) -> str:
        return path.replace("PROJEC~1", "ProjectLong")

    def realpath(path: str) -> str:
        text = path.replace("/", "\\")
        folded = text.casefold()
        if folded == r"c:\work\link\proj":
            return r"\\?\C:\Work\real\proj"
        if folded == r"c:\work\link":
            return r"\\?\C:\Work\real"
        if "projec~1" in folded:
            raise OSError("short name left for long_path")
        return text

    monkeypatch.setattr("kotoyose.child_turn._long_path", long_path)
    monkeypatch.setattr("kotoyose.child_turn._realpath", realpath)
    policy = SimpleNamespace(workspace_roots=[r"C:\work\ProjectLong", r"C:\work\link"])
    resolved = _resolve_cwd_windows(r"C:\work\PROJEC~1\src", policy, alias_roots=[r"C:\work\ProjectLong"])
    assert str(resolved).replace("/", "\\").casefold() == r"c:\work\projectlong\src"
    via_junction = _resolve_cwd_windows(r"C:\work\link\proj", policy, alias_roots=[r"c:/work/real"])
    assert str(via_junction).replace("/", "\\").casefold() == r"c:\work\real\proj"
    with pytest.raises(ChildTurnError, match="workspace_roots"):
        _resolve_cwd_windows(r"D:\elsewhere", policy, alias_roots=[r"C:\work\real"])
    monkeypatch.setattr("kotoyose.child_turn._long_path", lambda path: path)
    with pytest.raises(ChildTurnError, match="cwd does not exist"):
        _resolve_cwd_windows(r"C:\work\PROJEC~1", policy, alias_roots=[r"C:\work"])


def test_windows_login_dir_uses_userprofile_and_does_not_replace_it(monkeypatch):
    from kotoyose.child_turn import _strip_connection, _use_local_cli_login

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("USERPROFILE", r"C:\Users\Alice")
    monkeypatch.setenv("HOME", r"D:\not-the-profile")
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    local = _use_local_cli_login(_strip_connection({"provider": "claude", "kind": "subscription", "config_dir": r"D:\host"}))
    assert local["config_dir"] == r"C:\Users\Alice\.claude"
    assert os_environ_unchanged(r"C:\Users\Alice", r"D:\not-the-profile")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", r"C:\Users\Alice\.claude-other")
    again = _use_local_cli_login(_strip_connection({"provider": "claude", "kind": "subscription"}))
    assert again["config_dir"] == r"C:\Users\Alice\.claude-other"


def os_environ_unchanged(profile: str, home: str) -> bool:
    import os
    return os.environ["USERPROFILE"] == profile and os.environ["HOME"] == home


def test_windows_default_claude_dir_is_case_insensitive_and_leaves_profile_env(monkeypatch, tmp_path):
    from kotoyose.config import HarnessConfig
    from kotoyose.runtime.claude_auth import build_auth_env, claude_dir_is_default

    assert claude_dir_is_default(r"c:/users/alice/.claude", r"C:\Users\Alice\.claude", windows=True)
    assert not claude_dir_is_default(r"C:\Users\Alice\other\.claude", r"C:\Users\Alice\.claude", windows=True)
    monkeypatch.setattr(sys, "platform", "win32")
    profile = tmp_path / "Profile"
    monkeypatch.setenv("USERPROFILE", str(profile))
    monkeypatch.setenv("HOME", str(tmp_path / "not-used"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

    def config_for(path: Path):
        return HarnessConfig.model_validate({
            "connections": {"c": {"provider": "claude", "kind": "subscription", "data_policy": "no_training",
                                  "config_dir": str(path)}},
            "routes": {"r": {"driver": "claude.agent-sdk", "connection": "c", "model": "claude-haiku-5"}},
            "models": {"a": {"route": "r"}},
        })

    default = config_for(_case_flip(profile) / ".claude")
    env = build_auth_env(default.preset_for("a"), default, lambda: None)
    assert "CLAUDE_CONFIG_DIR" not in env
    assert "HOME" not in env and "USERPROFILE" not in env
    custom = config_for(profile / "other-login")
    custom_env = build_auth_env(custom.preset_for("a"), custom, lambda: None)
    assert custom_env["CLAUDE_CONFIG_DIR"] == str(profile / "other-login")
    assert "HOME" not in custom_env and "USERPROFILE" not in custom_env


def _case_flip(path: Path) -> Path:
    text = str(path)
    flipped = "".join(ch.upper() if ch.islower() else ch.lower() for ch in text)
    return Path(flipped)


def test_cmd_shim_resolves_to_node_or_exe_without_a_shell(tmp_path):
    from kotoyose.child_turn import ChildTurnError, _policy_mcp

    node_shim = tmp_path / "tool.cmd"
    node_shim.write_text(
        '@ECHO off\r\n"%~dp0node_modules\\tool\\bin\\tool.js" %*\r\n',
        encoding="utf-8",
    )
    resolved = _policy_mcp({"command": str(node_shim), "args": ["--flag"], "description": "files"})
    assert resolved["server"]["command"] == "node"
    assert resolved["server"]["args"][0].endswith("tool.js")
    assert resolved["server"]["args"][1:] == ["--flag"]
    exe_shim = tmp_path / "tool.bat"
    exe_shim.write_text('@ECHO off\r\n"C:\\Tools\\foo.exe" %*\r\n', encoding="utf-8")
    assert _policy_mcp({"command": str(exe_shim), "args": []})["server"]["command"] == r"C:\Tools\foo.exe"
    assert _policy_mcp({"command": "files", "args": ["a"]})["server"]["command"] == "files"
    broken = tmp_path / "broken.cmd"
    broken.write_text("@ECHO off\r\n", encoding="utf-8")
    with pytest.raises(ChildTurnError, match="cannot resolve"):
        _policy_mcp({"command": str(broken), "args": []})


def test_bundled_claude_on_windows_is_the_exe_not_a_cmd_shim():
    from kotoyose.claude_cli import bundled_executable_name

    assert bundled_executable_name("win32") == "claude.exe"
    assert bundled_executable_name("linux") == "claude"


def test_turn_job_assigns_before_resume_and_kills_with_taskkill_backstop():
    from kotoyose.child_process_tree import (
        CREATE_SUSPENDED,
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
        TurnJob,
        kill_on_close_information,
        suspend_and_assign,
        taskkill_argv,
    )

    assert kill_on_close_information().BasicLimitInformation.LimitFlags == JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE == 0x2000
    kernel = _FakeKernel()
    job = TurnJob(kernel)
    seen = {}

    def real(*args):
        seen["flags"] = args[5]
        return (10, 11, 42, 3)

    suspend_and_assign(real, job, ("claude.exe", "args", None, None, 1, 0, None, None, None))
    assert seen["flags"] & CREATE_SUSPENDED
    names = [call[0] for call in kernel.calls]
    assert names.index("assign") < names.index("resume")
    flags, resume = job.creation_flags(CREATE_SUSPENDED)
    assert flags == CREATE_SUSPENDED and resume is False
    job.kill()
    assert ("terminate", job.handle, 1) in kernel.calls
    assert ("close", job.handle) in kernel.calls
    assert ("taskkill", 42) not in kernel.calls
    assert taskkill_argv(42) == ["taskkill.exe", "/PID", "42", "/T", "/F"]
    failed = _FakeKernel(assign_ok=False)
    broken = TurnJob(failed)
    suspend_and_assign(real, broken, ("claude.exe", "args", None, None, 1, 0, None, None, None))
    assert "resume" in [call[0] for call in failed.calls]
    broken.kill()
    assert ("taskkill", 42) in failed.calls


class _FakeKernel:
    def __init__(self, *, assign_ok: bool = True):
        self.calls = []
        self.assign_ok = assign_ok
        self._next = 100

    def create_job(self):
        self._next += 1
        self.calls.append(("create", self._next))
        return self._next

    def set_kill_on_close(self, handle):
        self.calls.append(("kill_on_close", handle))

    def assign(self, job, process):
        self.calls.append(("assign", job, process))
        return self.assign_ok

    def resume(self, thread):
        self.calls.append(("resume", thread))

    def terminate(self, job, code):
        self.calls.append(("terminate", job, code))

    def close(self, handle):
        self.calls.append(("close", handle))

    def taskkill(self, pid):
        self.calls.append(("taskkill", pid))


def test_windows_utils_popen_is_patched_even_though_it_subclasses_the_original():
    from kotoyose.child_process_tree import TURN_PIDS, install_popen_tracking

    class Base:
        def __init__(self, *args, **kwargs):
            self.pid = 42

    class WindowsPopen(Base):
        """Bound to Base at class creation, the way asyncio.windows_utils.Popen binds subprocess.Popen."""

    subprocess_mod = SimpleNamespace(Popen=Base)
    windows_utils = SimpleNamespace(Popen=WindowsPopen)
    pids: set[int] = set()
    token = TURN_PIDS.set(pids)
    restore = install_popen_tracking(subprocess_mod, windows_utils)
    try:
        assert windows_utils.Popen(["claude"]).pid in pids
        assert subprocess_mod.Popen is not windows_utils.Popen
        pids.clear()
        assert subprocess_mod.Popen(["other"]).pid in pids
    finally:
        restore()
        TURN_PIDS.reset(token)


def test_kernel32_adapter_sets_kill_on_close_and_taskkill_is_not_a_shell(monkeypatch):
    from kotoyose.child_process_tree import JOB_OBJECT_EXTENDED_LIMIT, Kernel32Adapter, TurnJob

    class Dll:
        def __init__(self):
            self.info = None

        def CreateJobObjectW(self, _a, _b):
            return 7

        def SetInformationJobObject(self, handle, kind, info, size):
            assert handle == 7 and kind == JOB_OBJECT_EXTENDED_LIMIT and size > 0
            self.flags = info._obj.BasicLimitInformation.LimitFlags
            return 1

        def AssignProcessToJobObject(self, job, process):
            assert job == 7 and process == 10
            return 0

        def ResumeThread(self, thread):
            assert thread == 11
            return 1

        def TerminateJobObject(self, job, code):
            assert job == 7 and code == 1
            return 1

        def CloseHandle(self, handle):
            assert handle == 7
            return 1

    seen = {}

    def popen(argv, **kwargs):
        seen["argv"] = list(argv)
        seen["shell"] = kwargs.get("shell")
        return SimpleNamespace()

    monkeypatch.setattr("kotoyose.child_process_tree.subprocess.Popen", popen)
    dll = Dll()
    job = TurnJob(Kernel32Adapter(dll))
    job.attach(10, 11, 42, resume=True)
    job.kill()
    assert dll.flags == 0x2000
    assert seen["shell"] is False
    assert seen["argv"] == ["taskkill.exe", "/PID", "42", "/T", "/F"]

    browser_job = TurnJob(Kernel32Adapter(dll))
    browser_job.release()
    assert dll.flags == 0


def test_posix_turn_still_tracks_popen_and_kills_the_group():
    from kotoyose.child_process_tree import TURN_PIDS, install_turn_popen, kill_pids, uninstall_turn_popen

    pids: set[int] = set()
    token = TURN_PIDS.set(pids)
    install_turn_popen()
    proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        assert proc.pid in pids
        kill_pids(pids)
        proc.wait(timeout=2)
        assert proc.returncode is not None
    finally:
        uninstall_turn_popen()
        TURN_PIDS.reset(token)
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)


def test_console_stop_falls_back_when_the_loop_rejects_signal_handlers():
    from kotoyose.child_cli import arm_stop

    stop = asyncio.Event()
    scheduled = []

    class Loop:
        def add_signal_handler(self, sig, callback):
            raise NotImplementedError

        def call_soon_threadsafe(self, callback):
            scheduled.append(callback)

    class Signals:
        SIGINT = 2
        SIGBREAK = 21
        SIGTERM = 15
        handlers = {}

        def signal(self, sig, handler):
            self.handlers[sig] = handler

    signals = Signals()
    arm_stop(stop, loop=Loop(), signal_module=signals)
    assert set(signals.handlers) == {2, 21, 15}
    signals.handlers[2](2, None)
    assert scheduled == [stop.set]


def test_loop_signal_handlers_stay_when_the_platform_supports_them():
    from kotoyose.child_cli import arm_stop

    seen = []

    class Loop:
        def add_signal_handler(self, sig, callback):
            seen.append((sig, callback))

    class Signals:
        SIGINT = 2
        SIGTERM = 15

        def signal(self, *_args):
            raise AssertionError("signal.signal")

    stop = asyncio.Event()
    arm_stop(stop, loop=Loop(), signal_module=Signals())
    assert [sig for sig, _callback in seen] == [2, 15]
    seen[0][1]()
    assert stop.is_set()


def test_install_task_is_windows_only_and_the_argv_is_an_interactive_logon_task(capsys, tmp_path):
    from kotoyose.child_cli import install_logon_task, main
    from kotoyose.child_windows import logon_task_delete_argv

    assert main(["install-task"]) == 2
    assert main(["uninstall-task"]) == 2
    assert "Windows-only" in capsys.readouterr().err
    seen = {}

    def runner(argv, check, shell):
        seen["argv"] = list(argv)
        seen["shell"] = shell
        xml_path = Path(argv[argv.index("/XML") + 1])
        seen["xml"] = xml_path.read_bytes()
        seen["path"] = xml_path
        return SimpleNamespace(returncode=0)

    code = install_logon_task(
        user_id=r"DESKTOP\Alice",
        which=lambda name: r"C:\Tools\kotoyose-child.exe" if name == "kotoyose-child.exe" else None,
        executable=r"C:\Program Files\Python\python.exe",
        runner=runner,
        home=tmp_path,
    )
    assert code == 0 and seen["shell"] is False
    xml_path = seen["path"]
    assert seen["argv"] == ["schtasks.exe", "/Create", "/F", "/TN", "KotoyoseChild", "/XML", str(xml_path)]
    assert seen["xml"].startswith((b"\xff\xfe", b"\xfe\xff"))
    root = _task_xml(seen["xml"])
    assert _task_text(root, "Triggers/LogonTrigger/UserId") == r"DESKTOP\Alice"
    assert _task_text(root, "Actions/Exec/Command") == r"C:\Tools\kotoyose-child.exe"
    assert _task_text(root, "Actions/Exec/Arguments") == "run"
    assert not xml_path.exists()
    assert "SYSTEM" not in seen["argv"]
    skipped = {}

    def cmd_which(name):
        skipped["name"] = name
        return r"C:\npm\kotoyose-child.cmd" if name == "kotoyose-child" else None

    install_logon_task(
        user_id=r"DESKTOP\Alice",
        which=cmd_which,
        executable=r"C:\Program Files\Python\python.exe",
        runner=runner,
        home=tmp_path,
    )
    root = _task_xml(seen["xml"])
    assert _task_text(root, "Actions/Exec/Command") == r"C:\Program Files\Python\python.exe"
    assert _task_text(root, "Actions/Exec/Arguments") == "-m kotoyose.child_cli run"
    assert not seen["path"].exists()
    assert logon_task_delete_argv() == ["schtasks.exe", "/Delete", "/F", "/TN", "KotoyoseChild"]
    with pytest.raises(ValueError, match="interactive"):
        install_logon_task(user_id="SYSTEM", which=cmd_which, executable="python", runner=runner, home=tmp_path)
    with pytest.raises(ValueError, match="interactive"):
        install_logon_task(user_id=r"NT AUTHORITY\SYSTEM", which=cmd_which, executable="python", runner=runner, home=tmp_path)


_TASK_NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


def _task_xml(payload: str | bytes):
    import xml.etree.ElementTree as ET

    if isinstance(payload, str):
        payload = payload.encode("utf-16")
    return ET.fromstring(payload)


def _task_text(root, path: str) -> str | None:
    return root.findtext("t:" + path.replace("/", "/t:"), namespaces=_TASK_NS)


_NPM_SHIM = """\
@ECHO off
GOTO start
:find_dp0
SET dp0=%~dp0
EXIT /b
:start
SETLOCAL
CALL :find_dp0

IF EXIST "%dp0%\\node.exe" (
  SET "_prog=%dp0%\\node.exe"
) ELSE (
  SET "_prog=node"
  SET PATHEXT=%PATHEXT:;.JS;=;%
)

endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%"  "%dp0%\\node_modules\\@scope\\pkg\\dist\\cli.js" %*
"""


def test_npm_shim_uses_the_sibling_node_and_only_the_script(tmp_path):
    from kotoyose.child_windows import _collapse_doubled_backslashes, resolve_windows_shim

    assert _collapse_doubled_backslashes("C:\\foo\\\\bar") == "C:\\foo\\bar"
    assert _collapse_doubled_backslashes("\\\\server\\share\\\\bin") == "\\\\server\\share\\bin"
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    shim = shim_dir / "cli.cmd"
    shim.write_text(_NPM_SHIM, encoding="utf-8")
    command, args = resolve_windows_shim(str(shim), ["--flag"])
    assert command == "node"
    assert args[0].endswith("node_modules\\@scope\\pkg\\dist\\cli.js")
    assert "\\\\" not in args[0]
    assert "_prog" not in command and "_prog" not in args[0]
    assert args[1:] == ["--flag"]
    sibling = shim_dir / "node.exe"
    sibling.write_bytes(b"")
    command, args = resolve_windows_shim(str(shim), [])
    assert command == str(sibling)
    assert args[0].endswith("node_modules\\@scope\\pkg\\dist\\cli.js")


def test_logon_task_xml_escapes_the_command_and_drops_the_execution_cap():
    from kotoyose.child_windows import logon_task_create_argv, logon_task_xml, windows_logon_user_id

    user_id = windows_logon_user_id("DESKTOP", "Alice")
    xml = logon_task_xml(user_id=user_id, run_argv=[r"C:\Tools\A&B\kotoyose-child.exe", "run"])
    root = _task_xml(xml)
    assert _task_text(root, "Triggers/LogonTrigger/UserId") == r"DESKTOP\Alice"
    assert _task_text(root, "Principals/Principal/UserId") == r"DESKTOP\Alice"
    assert _task_text(root, "Principals/Principal/LogonType") == "InteractiveToken"
    assert _task_text(root, "Principals/Principal/RunLevel") == "LeastPrivilege"
    assert _task_text(root, "Settings/MultipleInstancesPolicy") == "IgnoreNew"
    assert _task_text(root, "Settings/DisallowStartIfOnBatteries") == "false"
    assert _task_text(root, "Settings/StopIfGoingOnBatteries") == "false"
    assert _task_text(root, "Settings/ExecutionTimeLimit") == "PT0S"
    assert _task_text(root, "Settings/RestartOnFailure/Interval") == "PT1M"
    assert _task_text(root, "Settings/RestartOnFailure/Count") == "999"
    assert _task_text(root, "Actions/Exec/Command") == r"C:\Tools\A&B\kotoyose-child.exe"
    assert _task_text(root, "Actions/Exec/Arguments") == "run"
    assert logon_task_create_argv(xml_path=r"C:\child\task.xml") == [
        "schtasks.exe", "/Create", "/F", "/TN", "KotoyoseChild", "/XML", r"C:\child\task.xml",
    ]
    with pytest.raises(ValueError, match="interactive"):
        windows_logon_user_id("", "Alice")
    with pytest.raises(ValueError, match="interactive"):
        windows_logon_user_id("NT AUTHORITY", "SYSTEM")


def test_literal_tilde_digit_directory_is_kept_and_dotdot_is_not(monkeypatch):
    from kotoyose.child_turn import ChildTurnError, _resolve_cwd_windows

    monkeypatch.setattr("kotoyose.child_turn._long_path", lambda path: path)
    monkeypatch.setattr("kotoyose.child_turn._realpath", lambda path: path)
    policy = SimpleNamespace(workspace_roots=[r"C:\work"])
    kept = _resolve_cwd_windows(r"C:\work\proj~1", policy, alias_roots=[r"C:\work"])
    assert str(kept).replace("/", "\\").casefold() == r"c:\work\proj~1"
    with pytest.raises(ChildTurnError, match="cwd does not exist"):
        _resolve_cwd_windows(r"C:\work\proj\..\outside", policy, alias_roots=[r"C:\work"])

    def long_path(path: str) -> str:
        return path.replace("SHORT~1", "longname")

    def realpath(path: str) -> str:
        if path.replace("/", "\\").casefold() == r"c:\work\link":
            return r"C:\work\SHORT~1"
        return path

    monkeypatch.setattr("kotoyose.child_turn._long_path", long_path)
    monkeypatch.setattr("kotoyose.child_turn._realpath", realpath)
    expanded = _resolve_cwd_windows(r"C:\work\link", policy, alias_roots=[r"C:\work"])
    assert str(expanded).replace("/", "\\").casefold() == r"c:\work\longname"


def test_console_stop_ignores_a_closed_loop():
    from kotoyose.child_cli import arm_stop

    class Loop:
        def add_signal_handler(self, sig, callback):
            raise NotImplementedError

        def call_soon_threadsafe(self, callback):
            raise RuntimeError("Event loop is closed")

    class Signals:
        SIGINT = 2
        SIGBREAK = 21
        SIGTERM = 15
        handlers = {}

        def signal(self, sig, handler):
            self.handlers[sig] = handler

    signals = Signals()
    arm_stop(asyncio.Event(), loop=Loop(), signal_module=signals)
    signals.handlers[2](2, None)


def test_kill_pids_taskkills_only_unassigned_pids_and_then_does_nothing(monkeypatch):
    from kotoyose import child_process_tree as tree

    monkeypatch.setattr(sys, "platform", "win32")
    kernel = _FakeKernel()
    monkeypatch.setattr(tree, "windows_kernel", lambda: kernel)
    pids = tree.new_turn_pids()
    job = tree._job_for(pids)
    try:
        job.attach(10, 11, 41, resume=True)
        kernel.assign_ok = False
        job.attach(12, 13, 42, resume=True)
        tree.kill_pids(pids)
        assert ("taskkill", 42) in kernel.calls
        assert ("taskkill", 41) not in kernel.calls
        calls = len(kernel.calls)
        tree._JOBS[id(pids)] = job
        tree.kill_pids(pids)
        assert kernel.calls[calls:] == []
    finally:
        tree._JOBS.pop(id(pids), None)


def test_kill_pids_without_a_job_does_not_taskkill_recycled_pids(monkeypatch):
    from kotoyose.child_process_tree import kill_pids

    monkeypatch.setattr(sys, "platform", "win32")
    seen = []
    monkeypatch.setattr("kotoyose.child_process_tree.subprocess.Popen", lambda *args, **kwargs: seen.append(args))
    kill_pids({99})
    kill_pids({99})
    assert seen == []


def _prepared(tmp_path: Path) -> dict:
    return {
        "fingerprint": "same",
        "temps": [],
        "cleanup": lambda: None,
        "alias": "a",
        "provider": "stub",
        "config": None,
        "cwd": str(tmp_path),
    }


async def test_warm_session_keeps_one_pid_set_and_discard_releases_it(tmp_path, monkeypatch):
    from kotoyose.child_process_tree import TURN_PIDS
    from kotoyose.child_protocol import Frame
    from kotoyose.child_turn import ChildTurns
    from kotoyose.runtime.base import RuntimeResult

    class Runtime:
        def __init__(self):
            self.seen = []
            self.disconnected = False
            self.mode = "done"
            self.started = asyncio.Event()

        async def disconnect(self):
            self.disconnected = True

        async def stream(self, prompt):
            self.seen.append(TURN_PIDS.get())
            self.started.set()
            if self.mode == "block":
                await asyncio.Event().wait()
            yield RuntimeResult(text="ok")

    runtime = Runtime()
    builds = {"n": 0}
    killed: list[set[int]] = []

    async def build(_prepared_body):
        builds["n"] += 1
        return runtime

    monkeypatch.setattr("kotoyose.child_turn._prepare", lambda home, payload: _prepared(tmp_path))
    monkeypatch.setattr("kotoyose.child_turn._build_runtime", build)
    monkeypatch.setattr("kotoyose.child_turn.kill_pids", lambda pids: killed.append(pids))

    async def send(*_args):
        return None

    turns = ChildTurns(tmp_path, send, [])
    payload = {"conversation_key": "conv", "prompt": {"text": "hi"}}
    turns.handle(Frame(1, "turn", dict(payload)))
    await asyncio.wait_for(turns._turns[1].task, 2)
    assert builds["n"] == 1
    assert len(turns._pid_sets) == 1
    owned = turns._pid_sets[0]
    assert runtime.seen[0] is owned
    assert killed == []

    turns.handle(Frame(2, "turn", dict(payload)))
    await asyncio.wait_for(turns._turns[2].task, 2)
    assert builds["n"] == 1
    assert len(turns._pid_sets) == 1 and turns._pid_sets[0] is owned
    assert runtime.seen[1] is owned
    assert turns._warm["conv"].pids is owned

    turns.handle(Frame(3, "turn", {"prompt": {"text": "once"}}))
    await asyncio.wait_for(turns._turns[3].task, 2)
    assert len(turns._pid_sets) == 1 and turns._pid_sets[0] is owned
    assert killed[-1] is not owned

    await turns._drop("conv")
    assert runtime.disconnected
    assert turns._pid_sets == []
    assert killed[-1] is owned


async def test_cancel_of_a_warm_turn_kills_that_pid_set(tmp_path, monkeypatch):
    from kotoyose.child_process_tree import TURN_PIDS
    from kotoyose.child_protocol import Frame
    from kotoyose.child_turn import ChildTurns
    from kotoyose.runtime.base import RuntimeResult

    class Runtime:
        def __init__(self):
            self.seen = []
            self.mode = "done"
            self.started = asyncio.Event()

        async def disconnect(self):
            return None

        async def stream(self, prompt):
            self.seen.append(TURN_PIDS.get())
            self.started.set()
            if self.mode == "block":
                await asyncio.Event().wait()
            yield RuntimeResult(text="ok")

    runtime = Runtime()
    killed: list[set[int]] = []
    async def build(_prepared_body):
        return runtime

    monkeypatch.setattr("kotoyose.child_turn._prepare", lambda home, payload: _prepared(tmp_path))
    monkeypatch.setattr("kotoyose.child_turn._build_runtime", build)
    monkeypatch.setattr("kotoyose.child_turn.kill_pids", lambda pids: killed.append(pids))

    async def send(*_args):
        return None

    turns = ChildTurns(tmp_path, send, [])
    payload = {"conversation_key": "conv", "prompt": {"text": "hi"}}
    turns.handle(Frame(1, "turn", dict(payload)))
    await asyncio.wait_for(turns._turns[1].task, 2)
    owned = turns._pid_sets[0]
    runtime.mode = "block"
    runtime.started.clear()
    turns.handle(Frame(2, "turn", dict(payload)))
    live = turns._turns[2]
    await runtime.started.wait()
    assert runtime.seen[-1] is owned
    assert live.pids is owned
    turns._cancel(2)
    with pytest.raises(asyncio.CancelledError):
        await live.task
    for _ in range(20):
        if not turns._warm and not turns._pid_sets:
            break
        await asyncio.sleep(0)
    assert killed[0] is owned
    assert turns._pid_sets == []


async def test_failed_runtime_build_releases_the_pid_set(tmp_path, monkeypatch):
    from kotoyose.child_protocol import Frame
    from kotoyose.child_turn import ChildTurns

    killed: list[set[int]] = []

    async def build(_prepared_body):
        raise RuntimeError("boom")

    monkeypatch.setattr("kotoyose.child_turn._prepare", lambda home, payload: _prepared(tmp_path))
    monkeypatch.setattr("kotoyose.child_turn._build_runtime", build)
    monkeypatch.setattr("kotoyose.child_turn.kill_pids", lambda pids: killed.append(pids))

    async def send(*_args):
        return None

    turns = ChildTurns(tmp_path, send, [])
    turns.handle(Frame(1, "turn", {"conversation_key": "conv", "prompt": {"text": "hi"}}))
    await asyncio.wait_for(turns._turns[1].task, 2)
    assert turns._pid_sets == []
    assert len(killed) == 1
    assert "conv" not in turns._warm


@pytest.mark.parametrize(("text", "want"), [
    (r'{"workspace_roots": ["D:\Programming\ai\tmp"]}', "D:\\Programming\\ai\\tmp"),
    (r'{"workspace_roots": ["D:\\Programming\\ai\\tmp"]}', "D:\\Programming\\ai\\tmp"),
    (r'{"workspace_roots": ["D:\tmp"]}', "D:\\tmp"),
    (r'{"workspace_roots": ["C:\new\users"]}', "C:\\new\\users"),
    (r'{"workspace_roots": ["\\server\share\x"]}', "\\\\server\\share\\x"),
    ('{"workspace_roots": ["/srv/a/b"]}', "/srv/a/b"),
])
def test_policy_accepts_pasted_windows_paths(text, want):
    # Explorer paths pasted without JSON escaping: \t / \n / \u must not silently turn into other characters.
    from kotoyose.child_identity import parse_policy_text

    assert parse_policy_text(text)["workspace_roots"] == [want]


def test_policy_still_rejects_broken_json():
    import json

    from kotoyose.child_identity import parse_policy_text

    with pytest.raises(json.JSONDecodeError):
        parse_policy_text('{"workspace_roots": [')
