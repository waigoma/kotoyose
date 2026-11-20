"""private PID namespace の外側 monitor と観測済み子 process の停止を照合する。"""

import asyncio
from pathlib import Path
from typing import Literal

from .process_identity import ProcessIdentity, capture_process_identity, stop_identified_process


_MAX_PROCESSES = 512


def _children(identity: ProcessIdentity) -> list[ProcessIdentity]:
    # namespace の外側 bwrap monitor の終了だけでは内部停止を証明できない。
    # 停止前に自分の subtree だけを列挙し、各 PID の identity を固定する。
    result = []
    pending = [identity]
    seen = {identity.pid}
    task_count = 0
    while pending:
        parent = pending.pop()
        if capture_process_identity(parent.pid) != parent:
            raise ValueError("process_identity_changed")
        for task in Path(f"/proc/{parent.pid}/task").iterdir():
            task_count += 1
            if task_count > _MAX_PROCESSES:
                raise ValueError("process_tree_limit")
            with (task / "children").open() as stream:
                content = stream.read(8193)
            if len(content) > 8192:
                raise ValueError("process_tree_limit")
            for value in content.split():
                pid = int(value)
                if pid in seen:
                    continue
                seen.add(pid)
                if len(seen) > _MAX_PROCESSES:
                    raise ValueError("process_tree_limit")
                try:
                    child = capture_process_identity(pid)
                    fields = Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()
                    if int(fields[1]) != parent.pid or child.uid != identity.uid or child.start_ticks < parent.start_ticks:
                        raise ValueError("process_identity_changed")
                except (FileNotFoundError, ProcessLookupError):
                    continue
                result.append(child)
                pending.append(child)
    return result


def _private_init(children: list[ProcessIdentity]) -> ProcessIdentity | None:
    host_namespace = Path("/proc/self/ns/pid").stat().st_ino
    for child in children:
        namespace = Path(f"/proc/{child.pid}/ns/pid").stat().st_ino
        if namespace == host_namespace:
            continue
        status = Path(f"/proc/{child.pid}/status").read_text()
        namespaces = next(line.split()[1:] for line in status.splitlines() if line.startswith("NSpid:"))
        if len(namespaces) > 1 and namespaces[-1] == "1":
            if capture_process_identity(child.pid) != child:
                raise ValueError("process_identity_changed")
            return child
    return None


async def stop_codex_process(identity: ProcessIdentity, grace: float,
                             namespace_policy: Literal["private_pid", "fixture_process"] = "private_pid",
                             *, namespace_init: ProcessIdentity | None = None) -> str:
    """観測済み subtree を pidfd で止め、全停止証明が揃った場合だけ confirmed を返す。

    列挙・identity・停止の失敗は unconfirmed。親も止めるが、未知 PID や process
    group への signal は使わない。採取数を制限し、停止部分を bounded timeout 内で
    並行する。production は private namespace init の identity と退出も要求する。
    init は入力送信前に scope 内で固定した identity も使用できる。この場合も
    戻り値だけでは task / pipe / egress の終了は証明せず、呼出側で別途照合する。
    namespace 証拠を伴わない親 PID の不在だけでは不十分。
    init の退出により採取後 fork/reparent した未知の子も kernel が終了させる。
    fixture_process は明示された偽 launcher のモデルなしテストだけで使う。
    """
    complete = True
    try:
        children = _children(identity)
        if namespace_policy == "private_pid" and namespace_init is None and _private_init(children) is None:
            complete = False
    except (OSError, ValueError, RuntimeError, StopIteration):
        children = []
        complete = (namespace_init is not None or
                    (namespace_policy == "fixture_process" and not Path(f"/proc/{identity.pid}").exists()))
    if namespace_init is not None and namespace_init not in children:
        children.append(namespace_init)
    try:
        async with asyncio.timeout(max(.1, grace) + 6):
            results = await asyncio.gather(*(stop_identified_process(item, grace) for item in [*children, identity]))
    except (TimeoutError, OSError, ValueError, RuntimeError):
        return "unconfirmed"
    return "confirmed" if complete and all(result == "confirmed" for result in results) else "unconfirmed"
