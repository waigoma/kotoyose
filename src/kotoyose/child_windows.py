"""Windows の path、npm の .cmd、ログオン task。Linux のテストが偽物で同じ関数を呼ぶ。"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from xml.sax.saxutils import escape

TASK_NAME = "KotoyoseChild"
_BATCH = (".cmd", ".bat")
_SCRIPT_SUFFIXES = (".js", ".mjs", ".cjs")
_TASK_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"


def strip_extended_prefix(path: str) -> str:
    """`\\\\?\\` と `\\\\?\\UNC\\` を、比較できる普通の path に戻す。区切りは `\\` に揃える。"""
    collapsed = str(path).replace("/", "\\")
    upper = collapsed.upper()
    if upper.startswith("\\\\?\\UNC\\"):
        return "\\\\" + collapsed[8:]
    if upper.startswith("\\\\?\\"):
        return collapsed[4:]
    return collapsed


def windows_path_key(path: str) -> str:
    """drive と UNC を、大文字小文字を無視して比べるキーにする。"""
    return strip_extended_prefix(path).rstrip("\\").casefold()


def normalize_windows_path(path: str, *, long_path: Callable[[str], str], realpath: Callable[[str], str]) -> str:
    """8.3 を伸ばし、junction を解決し、拡張 prefix を外す。

    GetLongPathNameW が 8.3 の定義なので、`proj~1` のように見えるディレクトリ名も、
    API がそのまま返したならそのまま使う。正規表現で短縮名を拒否すると、その名前の
    本物のディレクトリを落とす。junction の先に短縮名が残ることがあるので、解決のあとでもう一度伸ばす。
    `..` が残っていたら拒否する。
    """
    current = strip_extended_prefix(long_path(str(path)))
    current = strip_extended_prefix(realpath(current))
    current = strip_extended_prefix(long_path(current))
    if ".." in re.split(r"[\\/]", current):
        raise OSError("unresolved windows path")
    return current


def _collapse_doubled_backslashes(path: str) -> str:
    """展開で重なった `\\` を畳む。UNC と `\\\\?\\` の先頭の `\\` は残す。"""
    unc = path.startswith("\\\\")
    collapsed = path
    while "\\\\" in collapsed:
        collapsed = collapsed.replace("\\\\", "\\")
    if unc and not collapsed.startswith("\\\\"):
        collapsed = "\\" + collapsed
    return collapsed


def resolve_windows_shim(command: str, args: list[str]) -> tuple[str, list[str]]:
    """npm の `.cmd` / `.bat` を、shell を通さず `node script` か exe にする。

    script があるときは、shim の本文から exe を拾わない。隣の `node.exe` があればそれ、
    無ければ `node` にする。引用の中の `=` は `SET` の代入なので無視する。
    それ以外の command はそのまま返す。中を実行はしない。
    """
    if not command.lower().endswith(_BATCH):
        return command, list(args)
    shim = Path(command)
    if not shim.is_file():
        raise ValueError(f"Windows command shim does not exist: {command}")
    directory = str(shim.parent)
    if not directory.endswith("\\"):
        directory += "\\"
    text = shim.read_text(encoding="utf-8", errors="replace")
    expanded = re.sub(r"%~dp0", lambda _match: directory, text, flags=re.IGNORECASE)
    expanded = re.sub(r"%dp0%", lambda _match: directory, expanded, flags=re.IGNORECASE)
    exe: str | None = None
    script: str | None = None
    for item in re.findall(r'"([^"]*)"', expanded):
        # `SET "_prog=%dp0%\\node.exe"` の引用は代入であって、起こす exe ではない。
        if not item or "%" in item or "=" in item:
            continue
        item = _collapse_doubled_backslashes(item)
        lowered = item.lower()
        if lowered.endswith(_SCRIPT_SUFFIXES):
            script = item
            break
        if exe is None and lowered.endswith(".exe"):
            exe = item
    if script is not None:
        sibling = shim.parent / "node.exe"
        node = str(sibling) if sibling.is_file() else "node"
        return node, [script, *args]
    if exe is not None:
        return exe, list(args)
    raise ValueError(f"cannot resolve Windows command shim: {command}")


def child_run_argv(*, which: Callable[[str], str | None], executable: str) -> list[str]:
    """`kotoyose-child run`。`.cmd` は task のコマンドにしない。"""
    for name in ("kotoyose-child.exe", "kotoyose-child"):
        found = which(name)
        if found and not found.lower().endswith(_BATCH):
            return [found, "run"]
    return [executable, "-m", "kotoyose.child_cli", "run"]


def require_interactive_logon_user(user_id: str) -> str:
    """対話ユーザーの `DOMAIN\\NAME`。SYSTEM と、ドメインの無い名前は拒否する。"""
    text = user_id.strip()
    folded = text.casefold()
    if folded in {"", "system", "nt authority\\system"} or "\\" not in text:
        raise ValueError("logon task must run as the interactive user")
    return text


def windows_logon_user_id(domain: str, user: str) -> str:
    """`USERDOMAIN` と `USERNAME` から、ログオン task の UserId を作る。"""
    domain = domain.strip()
    user = user.strip()
    if not domain or not user:
        raise ValueError("logon task must run as the interactive user")
    return require_interactive_logon_user(f"{domain}\\{user}")


def _xml_text(value: str) -> str:
    return escape(value, {"\"": "&quot;", "'": "&apos;"})


def logon_task_xml(*, user_id: str, run_argv: list[str]) -> str:
    """対話ログオンの Task Scheduler XML。実行時間の上限は無く、バッテリーでも動く。

    `schtasks /SC ONLOGON` は実行時間の既定が 72 時間で、バッテリーでは始まらず、
    ドメインの無い `/RU` はパスワードを求めることがある。XML の LogonTrigger と
    InteractiveToken で、そのユーザーの対話セッションに限る。
    """
    user_id = require_interactive_logon_user(user_id)
    if not run_argv or not str(run_argv[0]).strip():
        raise ValueError("logon task command is empty")
    command = _xml_text(run_argv[0])
    arguments = _xml_text(subprocess.list2cmdline(run_argv[1:]))
    user = _xml_text(user_id)
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="{_TASK_NS}">
  <Triggers>
    <LogonTrigger>
      <UserId>{user}</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>999</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <Arguments>{arguments}</Arguments>
    </Exec>
  </Actions>
</Task>
"""


def write_logon_task_xml(home: Path, xml: str) -> Path:
    """child の home に、BOM 付き UTF-16 で task XML を書く。呼び出し側が消す。"""
    directory = Path(home)
    directory.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="kotoyose-task-", suffix=".xml", dir=directory)
    os.close(fd)
    path = Path(name)
    path.write_text(xml, encoding="utf-16")
    return path


def logon_task_create_argv(*, xml_path: str, task_name: str = TASK_NAME) -> list[str]:
    """XML でログオン task を登録する schtasks 引数。shell は使わない。"""
    return ["schtasks.exe", "/Create", "/F", "/TN", task_name, "/XML", xml_path]


def logon_task_delete_argv(*, task_name: str = TASK_NAME) -> list[str]:
    """同じ名前のログオン task を消す schtasks 引数。"""
    return ["schtasks.exe", "/Delete", "/F", "/TN", task_name]
