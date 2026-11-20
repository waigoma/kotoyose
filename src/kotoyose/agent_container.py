"""Agent CLI を載せる long-lived container の作成・exec・停止確認。

container の PID 1 は ``sleep <wall_seconds>`` (``--init`` で docker-init が包む) で、
command は ``docker exec`` で中へ送る。停止の証明は「container が存在しないこと」だけに置く。
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import shutil
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import structlog

from . import process_env
from .local_worker import _IMAGE_ID_RE

CONTAINER_PREFIX = "kotoyose-agent-"
# Kotoyose が所有者として使う instance label。execution 側と復旧・掃除で同じ値を使うため一箇所に置く。
DEFAULT_INSTANCE = "kotoyose"
# container 用の temp home の名前 prefix。container_codex / container_claude が mkdtemp で作る。
HOME_PREFIX = "kotoyose-agent-home-"
JOB_LABEL = "kotoyose.agent_job"
INSTANCE_LABEL = "kotoyose.agent_instance"
JOB_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
INSTANCE_RE = re.compile(r"[A-Za-z0-9_-]{1,32}")
_CONTAINER_ID_RE = re.compile(r"[0-9a-f]{12,64}")
ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SECRET_NAME_RE = re.compile(
    r"TOKEN|KEY|SECRET|PASSWORD|PASSWD|AUTH|CRED|COOKIE|BEARER|DSN|PRIVATE",
    re.IGNORECASE,
)
# secret_env として転送してはならない名前。PATH は docker client の探索を変え、
# DOCKER_* は接続先や認証を変えるため。
_RESERVED_SECRET_NAMES = frozenset({"PATH"})
_RESERVED_SECRET_PREFIX = "DOCKER_"

_CONTAINER_HOME = "/home/agent"
_CONTAINER_WORKSPACE = "/workspace"
_CONTAINER_AGENT_ROOT = "/opt/agent"
CONTAINER_RUNTIME_BUNDLE = f"{_CONTAINER_AGENT_ROOT}/runtime"
_CONTAINER_EGRESS = "/run/egress"
_CONTAINER_TMPFS = "/tmp:rw,exec,nosuid,size=2g"
_TARGET_RE = re.compile(r"/[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*")
_CONTROL_TIMEOUT_SECONDS = 30.0
_CREATE_TIMEOUT_SECONDS = 120.0
_REAP_TIMEOUT_SECONDS = 5.0
_CONTROL_OUTPUT_LIMIT = 1024 * 1024
_LIST_OUTPUT_LIMIT = 8 * 1024 * 1024
_RUN_OUTPUT_LIMIT = 1024 * 1024
_READ_CHUNK_SIZE = 64 * 1024

log = structlog.get_logger()


class AgentContainerError(RuntimeError):
    """docker 操作が失敗した、または結果を確定できなかった。"""


class AgentContainerTimeout(AgentContainerError):
    """run の timeout。docker exec の client だけを kill する (下記 run の docstring 参照)。"""


class AgentContainerOutputLimit(AgentContainerError):
    """出力が上限を超えた。"""


class AgentContainerOwnershipError(AgentContainerError):
    """job 名の container が別 job / 別 instance の label を持つため触らない。"""


@dataclass(frozen=True)
class AgentContainerSpec:
    """create に渡す immutable な設定。mount 先とリソース上限を固定する。"""

    job_id: str
    image_id: str
    workspace: Path
    home: Path
    # None なら /opt/agent を bind しない。必要な file は ro_files で個別に渡す。
    agent_root: Path | None
    egress_dir: Path | None
    wall_seconds: int
    memory_bytes: int
    cpus: float
    pids_limit: int
    env: Mapping[str, str] = field(default_factory=dict)
    labels: Mapping[str, str] = field(default_factory=dict)
    # container の絶対 path → host の regular file。bind は read-only。/opt/agent か /run/egress の下のみ。
    # agent_root を bind しない場合、docker が mount 先を作る (--read-only でも成立することを確認済み)。
    ro_files: Mapping[str, Path] = field(default_factory=dict)
    # container の絶対 path → host の directory。rw。/home/agent の下のみ。
    rw_dirs: Mapping[str, Path] = field(default_factory=dict)
    # read-only directory mounts at /workspace/<name> or CONTAINER_RUNTIME_BUNDLE.
    ro_dirs: Mapping[str, Path] = field(default_factory=dict)
    # 閲覧部屋は /workspace を ro にする。home (/home/agent) は別の rw mount のまま。
    workspace_readonly: bool = False


@dataclass(frozen=True, slots=True)
class _Inspect:
    id: str
    running: bool
    status: str
    labels: dict[str, str]


def is_secret_env_name(name: str) -> bool:
    """secret らしい環境変数名を検出する。値の内容は見ない。"""
    return _SECRET_NAME_RE.search(name) is not None


def container_name(job_id: str) -> str:
    """job ID から container 名を決める。不正な ID は ValueError。"""
    _validate_job_id(job_id)
    return CONTAINER_PREFIX + job_id


def _validate_job_id(job_id: object) -> None:
    if not isinstance(job_id, str) or JOB_ID_RE.fullmatch(job_id) is None:
        raise ValueError("job_id must match [A-Za-z0-9_-]{1,64}")


def _validate_env_value(name: str, value: object) -> None:
    if not isinstance(value, str) or "\x00" in value:
        raise ValueError(f"env value for {name} must be a string without NUL")


def _validate_grace(grace_seconds: object) -> int:
    if isinstance(grace_seconds, bool) or not isinstance(grace_seconds, int) or grace_seconds < 0:
        raise ValueError("grace_seconds must be a non-negative integer")
    return grace_seconds


def _validate_secret_name(name: str) -> None:
    if ENV_NAME_RE.fullmatch(name) is None:
        raise ValueError("secret env name is invalid")
    if name in _RESERVED_SECRET_NAMES or name.startswith(_RESERVED_SECRET_PREFIX):
        raise ValueError("secret env name is reserved for the docker client")


def _canonical_dir(path: object, what: str) -> str:
    """bind mount の source を検証し、canonical な絶対パスを返す。

    Docker の --mount CSV を壊す文字、symlink、root、存在しないものを拒否する。
    """
    if not isinstance(path, Path):
        raise TypeError(f"{what} must be a pathlib.Path")
    if path.is_symlink():
        raise ValueError(f"{what} must not be a symlink")
    try:
        canonical = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"{what} must resolve to an existing directory") from exc
    if canonical == Path("/") or not canonical.is_dir():
        raise ValueError(f"{what} must be a non-root directory")
    text = str(canonical)
    if any(character in text for character in (",", "\r", "\n")):
        raise ValueError(f"{what} must not contain comma or newline")
    return text


def _canonical_file(path: object, what: str) -> str:
    """bind mount の source として、symlink を含まない regular file の絶対 path を返す。"""
    if not isinstance(path, Path):
        raise TypeError(f"{what} must be a pathlib.Path")
    if path.is_symlink():
        raise ValueError(f"{what} must not be a symlink")
    try:
        canonical = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"{what} must resolve to an existing file") from exc
    if not canonical.is_file():
        raise ValueError(f"{what} must be a regular file")
    text = str(canonical)
    if any(character in text for character in (",", "\r", "\n")):
        raise ValueError(f"{what} must not contain comma or newline")
    return text


def _check_target(target: object, what: str, prefixes: tuple[str, ...], *, single_segment: bool = False) -> str:
    """container 側の mount 先を検証する。区切り文字や ``..`` を含む path は受け付けない。"""
    if not isinstance(target, str) or _TARGET_RE.fullmatch(target) is None or ".." in target.split("/"):
        raise ValueError(f"{what} target must be an absolute container path without special characters")
    matched = next((prefix for prefix in prefixes if target.startswith(prefix)), None)
    if matched is None or target == matched.rstrip("/"):
        raise ValueError(f"{what} target is outside its allowed area")
    if single_segment and "/" in target[len(matched):]:
        raise ValueError(f"{what} target must be a single path segment")
    return target


def _bind_mount(source: str, target: str, *, readonly: bool) -> list[str]:
    option = f"type=bind,src={source},dst={target}" + (",readonly" if readonly else "")
    return ["--mount", option]


async def _collect_capped(process, stdout: bytearray, stderr: bytearray, limit: int) -> None:
    """stdout / stderr を別々に上限付きで読み、終了まで待つ。

    呼び出し側は stdout と stderr の両方を読み切る前提で使う。片方を読まないと pipe が詰まる。
    """

    async def read(stream, buffer: bytearray) -> None:
        if stream is None:
            return
        while True:
            chunk = await stream.read(_READ_CHUNK_SIZE)
            if not chunk:
                return
            if len(buffer) + len(chunk) > limit:
                raise AgentContainerOutputLimit(f"output exceeded {limit} bytes")
            buffer.extend(chunk)

    readers = [
        asyncio.ensure_future(read(getattr(process, "stdout", None), stdout)),
        asyncio.ensure_future(read(getattr(process, "stderr", None), stderr)),
    ]
    try:
        await asyncio.gather(*readers)
        await process.wait()
    finally:
        for reader in readers:
            if not reader.done():
                reader.cancel()
        await asyncio.gather(*readers, return_exceptions=True)


class AgentContainer:
    """kotoyose-agent-<job_id> という名前の長寿命 container を操作する。

    instance は同じ docker host を共有する Kotoyose の複数プロセスを区別する label。
    sweep と remove はこの label が一致する container だけを扱う。
    """

    def __init__(self, docker_binary: str = "docker", *, instance: str) -> None:
        if not isinstance(docker_binary, str) or not docker_binary or "\x00" in docker_binary:
            raise ValueError("docker_binary must be a non-empty string")
        if not isinstance(instance, str) or INSTANCE_RE.fullmatch(instance) is None:
            raise ValueError("instance must match [A-Za-z0-9_-]{1,32}")
        if os.getuid() == 0:
            raise ValueError("root uid is not allowed for AgentContainer")
        self.docker_binary = docker_binary
        self.instance = instance

    async def create(self, spec: AgentContainerSpec) -> str:
        """container を作成して起動し、container ID を返す。

        失敗時は自動削除しない。name 衝突で既存 container を誤って消さないため、
        後始末は呼び出し側が remove で行う。
        """
        argv = self._create_argv(spec)
        rc, stdout, _ = await self._docker(*argv, timeout=_CREATE_TIMEOUT_SECONDS)
        container_id = stdout.decode("utf-8", "replace").strip()
        if rc != 0 or not container_id or "\n" in container_id:
            # docker の stderr は env 値を含み得るため、ここでは rc だけを報告する。
            raise AgentContainerError(f"docker run failed (exit {rc})")
        return container_id

    async def exec(
        self,
        job_id: str,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        secret_env: Mapping[str, str] | None = None,
        stdin: bool = True,
        workdir: str | None = None,
        limit: int | None = None,
    ) -> asyncio.subprocess.Process:
        """container 内で argv を起動し、stdio を呼び出し側へ渡す Process を返す。

        secret_env の値は docker client の環境変数としてだけ渡し、argv には名前しか載せない。
        呼び出し側は stdout と stderr の両方を読み切ること。片方を読まないと pipe が詰まる。
        """
        name = container_name(job_id)
        args = self._exec_argv(name, argv, env=env, secrets=secret_env, stdin=stdin, workdir=workdir)
        child_env: dict[str, str] | None = None
        if secret_env:
            child_env = process_env.build_minimal_env(
                extra_keys=("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"),
                overrides=secret_env,
            )
        stream_options = {} if limit is None else {"limit": limit}
        try:
            return await asyncio.create_subprocess_exec(
                self.docker_binary,
                *args,
                stdin=asyncio.subprocess.PIPE if stdin else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=child_env,
                **stream_options,
            )
        except OSError as exc:
            raise AgentContainerError("docker exec could not be started") from exc

    async def image_env(self, image_id: str) -> dict[str, str]:
        """image の ENV を name=value の辞書で返す。tag や不正な出力は拒否する。"""
        if not isinstance(image_id, str) or _IMAGE_ID_RE.fullmatch(image_id) is None:
            raise ValueError("image_id must be a full sha256 image ID; tags are not allowed")
        rc, stdout, _ = await self._docker(
            "image", "inspect", "--format", "{{json .Config.Env}}", image_id,
        )
        if rc != 0:
            raise AgentContainerError(f"docker image inspect failed (exit {rc})")
        try:
            decoded = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AgentContainerError("docker image inspect returned invalid output") from exc
        if decoded is None:
            return {}
        if not isinstance(decoded, list) or any(not isinstance(item, str) for item in decoded):
            raise AgentContainerError("docker image inspect returned an unexpected result")
        env: dict[str, str] = {}
        for item in decoded:
            key, separator, value = item.partition("=")
            if not separator or ENV_NAME_RE.fullmatch(key) is None or "\x00" in value:
                raise AgentContainerError("docker image inspect returned an invalid env entry")
            env[key] = value
        return env

    async def run(
        self,
        job_id: str,
        argv: Sequence[str],
        *,
        timeout: float,
        env: Mapping[str, str] | None = None,
    ) -> tuple[int, bytes, bytes]:
        """setup 用の command を待ち、(returncode, stdout, stderr) を返す。

        timeout したとき kill されるのは docker exec の client だけである。
        container 内で起動した process は、container が remove されるまで動き続ける。
        確実に止めるには remove を呼ぶこと。出力は stdout / stderr それぞれ 1 MiB まで。
        """
        process = await self.exec(job_id, argv, env=env, stdin=False)
        stdout = bytearray()
        stderr = bytearray()
        try:
            await asyncio.wait_for(_collect_capped(process, stdout, stderr, _RUN_OUTPUT_LIMIT), timeout)
        except asyncio.TimeoutError as exc:
            await _kill_and_reap(process)
            raise AgentContainerTimeout(f"exec exceeded {timeout:g} seconds") from exc
        except BaseException:
            await _kill_and_reap(process)
            raise
        return process.returncode, bytes(stdout), bytes(stderr)

    async def remove(self, job_id: str, grace_seconds: int) -> Literal["confirmed", "unconfirmed"]:
        """stop → rm -f -v の後、名前と ID の両方で存在しないことを確認する。

        label の job / instance が一致しない container は触らず AgentContainerOwnershipError を投げる。
        inspect 自体が失敗した場合は存在しないと断定できないため unconfirmed を返す。
        """
        name = container_name(job_id)
        grace = _validate_grace(grace_seconds)
        try:
            found = await self._inspect(name)
            if found is not None:
                if (found.labels.get(JOB_LABEL) != job_id
                        or found.labels.get(INSTANCE_LABEL) != self.instance):
                    raise AgentContainerOwnershipError("container name is not owned by this job and instance")
                await self._docker_quiet("stop", "--time", str(grace), found.id, timeout=grace + _CONTROL_TIMEOUT_SECONDS)
                # -v で anonymous volume も消す。残った volume は Kotoyose の管理外になるため。
                await self._docker_quiet("rm", "-f", "-v", found.id)
            if await self._inspect(name) is not None:
                return "unconfirmed"
            if found is not None and await self._inspect(found.id) is not None:
                return "unconfirmed"
            return "confirmed"
        except AgentContainerOwnershipError:
            raise
        except AgentContainerError:
            log.warning("agent_container.remove_unconfirmed", job_id=job_id)
            return "unconfirmed"

    async def exists(self, job_id: str) -> bool:
        """名前で inspect し、存在するかを返す。inspect 失敗は例外にする。"""
        return await self._inspect(container_name(job_id)) is not None

    async def owned_jobs(self) -> list[str]:
        """この instance が所有する kotoyose-agent container の job ID を列挙する。

        他 instance や label の無い container は列挙しない。名前と label が噛み合わないものも含めない。
        停止済みも含む。起動中だけが要るときは running_jobs。
        """
        return await self._labeled_jobs(running_only=False)

    async def running_jobs(self) -> list[str]:
        """起動中の、この instance の agent container の job ID。停止済みは数えない。"""
        return await self._labeled_jobs(running_only=True)

    async def _labeled_jobs(self, *, running_only: bool) -> list[str]:
        args = ["ps"]
        if running_only:
            # -a は停止済みも含む。起動中だけを数える受付では付けない。
            args.extend(["--filter", "status=running"])
        else:
            args.append("-a")
        args.extend([
            "--filter", f"label={JOB_LABEL}",
            "--filter", f"label={INSTANCE_LABEL}={self.instance}",
            "--format", '{{.Names}} {{.Label "kotoyose.agent_job"}}',
        ])
        rc, stdout, _ = await self._docker(
            *args, timeout=_CONTROL_TIMEOUT_SECONDS, output_limit=_LIST_OUTPUT_LIMIT,
        )
        if rc != 0:
            raise AgentContainerError(f"docker ps failed (exit {rc})")
        jobs: list[str] = []
        for line in stdout.decode("utf-8", "replace").splitlines():
            parts = line.split(" ")
            if len(parts) != 2:
                continue
            name, job_id = parts
            if JOB_ID_RE.fullmatch(job_id) is None or name != CONTAINER_PREFIX + job_id:
                continue
            jobs.append(job_id)
        return jobs

    async def sweep(
        self,
        keep: Callable[[str], bool],
        *,
        grace_seconds: int = 10,
    ) -> list[str]:
        """この instance の Kotoyose container のうち keep が False の job を削除し、確認できた job ID を返す。

        他 instance や label の無い container は列挙しない。所有確認に失敗した container は
        job ID だけ記録して飛ばす。停止未確認の container は返り値に入らない。
        """
        removed: list[str] = []
        for job_id in await self.owned_jobs():
            if keep(job_id):
                continue
            try:
                if await self.remove(job_id, grace_seconds) == "confirmed":
                    removed.append(job_id)
            except AgentContainerOwnershipError:
                log.warning("agent_container.sweep_skipped_foreign", job_id=job_id)
        return removed

    def _create_argv(self, spec: AgentContainerSpec) -> list[str]:
        """create の検証と argv 組み立てをまとめる。副作用は持たない。"""
        if not isinstance(spec, AgentContainerSpec):
            raise TypeError("spec must be an AgentContainerSpec")
        _validate_job_id(spec.job_id)
        if not isinstance(spec.image_id, str) or _IMAGE_ID_RE.fullmatch(spec.image_id) is None:
            raise ValueError("image_id must be a full sha256 image ID; tags are not allowed")
        for field_name in ("wall_seconds", "memory_bytes", "pids_limit"):
            value = getattr(spec, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{field_name} must be a positive integer")
        if (isinstance(spec.cpus, bool) or not isinstance(spec.cpus, (int, float))
                or not math.isfinite(spec.cpus) or spec.cpus <= 0):
            raise ValueError("cpus must be a positive finite number")

        workspace = _canonical_dir(spec.workspace, "workspace")
        home = _canonical_dir(spec.home, "home")
        agent_root = None if spec.agent_root is None else _canonical_dir(spec.agent_root, "agent_root")
        egress = None if spec.egress_dir is None else _canonical_dir(spec.egress_dir, "egress_dir")

        env_items: list[tuple[str, str]] = []
        for key in sorted(spec.env):
            if ENV_NAME_RE.fullmatch(key) is None:
                raise ValueError("env name is invalid")
            if key == "HOME":
                raise ValueError("HOME is fixed by AgentContainer")
            if is_secret_env_name(key):
                raise ValueError(
                    f"env {key} looks secret; pass it through exec(secret_env=...) or the egress broker"
                )
            _validate_env_value(key, spec.env[key])
            env_items.append((key, spec.env[key]))

        labels_items: list[tuple[str, str]] = []
        for key in sorted(spec.labels):
            value = spec.labels[key]
            if key in (JOB_LABEL, INSTANCE_LABEL):
                raise ValueError(f"label {key} is reserved")
            if not key or any(c in key for c in ("=", ",", "\n", "\r", "\x00")):
                raise ValueError("label key is invalid")
            if not isinstance(value, str) or any(c in value for c in ("\n", "\r", "\x00")):
                raise ValueError("label value is invalid")
            labels_items.append((key, value))

        extra = self._extra_mount_args(spec)
        argv = [
            "run", "-d",
            "--name", container_name(spec.job_id),
            "--pull=never",
            "--restart=no",
            "--label", f"{JOB_LABEL}={spec.job_id}",
            "--label", f"{INSTANCE_LABEL}={self.instance}",
        ]
        for key, value in labels_items:
            argv.extend(("--label", f"{key}={value}"))
        argv.extend((
            "--user", f"{os.getuid()}:{os.getgid()}",
            "--init",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--pids-limit", str(spec.pids_limit),
            "--memory", str(spec.memory_bytes),
            "--memory-swap", str(spec.memory_bytes),
            "--cpus", str(spec.cpus),
            "--network", "none",
            "--read-only",
            "--tmpfs", _CONTAINER_TMPFS,
        ))
        argv.extend(_bind_mount(workspace, _CONTAINER_WORKSPACE, readonly=spec.workspace_readonly))
        argv.extend(_bind_mount(home, _CONTAINER_HOME, readonly=False))
        if agent_root is not None:
            argv.extend(_bind_mount(agent_root, _CONTAINER_AGENT_ROOT, readonly=True))
        if egress is not None:
            argv.extend(_bind_mount(egress, _CONTAINER_EGRESS, readonly=True))
        argv.extend(extra)
        argv.extend(("--workdir", _CONTAINER_WORKSPACE, "--env", f"HOME={_CONTAINER_HOME}"))
        for key, value in env_items:
            argv.extend(("--env", f"{key}={value}"))
        argv.extend(("--entrypoint", "sleep", spec.image_id, str(spec.wall_seconds)))
        return argv

    @staticmethod
    def _extra_mount_args(spec: AgentContainerSpec) -> list[str]:
        """ro_files / rw_dirs / ro_dirs の検証済み mount 引数を返す。重複する mount 先は拒否する。"""
        reserved = {_CONTAINER_WORKSPACE, _CONTAINER_HOME, _CONTAINER_AGENT_ROOT, _CONTAINER_EGRESS}
        argv: list[str] = []

        def claim(target: str) -> None:
            if target in reserved:
                raise ValueError("mount target collides with a reserved mount")
            reserved.add(target)

        for target in sorted(spec.ro_files):
            _check_target(target, "ro_files", (_CONTAINER_AGENT_ROOT + "/", _CONTAINER_EGRESS + "/"))
            claim(target)
            argv.extend(_bind_mount(_canonical_file(spec.ro_files[target], f"ro_files {target}"), target, readonly=True))
        for target in sorted(spec.rw_dirs):
            _check_target(target, "rw_dirs", (_CONTAINER_HOME + "/",))
            claim(target)
            argv.extend(_bind_mount(_canonical_dir(spec.rw_dirs[target], f"rw_dirs {target}"), target, readonly=False))
        for target in sorted(spec.ro_dirs):
            if target != CONTAINER_RUNTIME_BUNDLE:
                _check_target(target, "ro_dirs", (_CONTAINER_WORKSPACE + "/",), single_segment=True)
            claim(target)
            argv.extend(_bind_mount(_canonical_dir(spec.ro_dirs[target], f"ro_dirs {target}"), target, readonly=True))
        return argv

    def _exec_argv(
        self,
        name: str,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None,
        secrets: Mapping[str, str] | None,
        stdin: bool,
        workdir: str | None,
    ) -> list[str]:
        if isinstance(argv, (str, bytes)) or not argv:
            raise ValueError("argv must be a non-empty sequence")
        for item in argv:
            if not isinstance(item, str) or "\x00" in item:
                raise ValueError("argv items must be strings without NUL")
        env = dict(env or {})
        secrets_map = dict(secrets or {})
        overlap = set(env) & set(secrets_map)
        if overlap:
            raise ValueError("a name cannot be both plain env and secret env")

        args = ["exec"]
        if stdin:
            args.append("-i")
        if workdir is not None:
            if not workdir.startswith("/") or "\x00" in workdir or "\n" in workdir:
                raise ValueError("workdir must be an absolute path")
            args.extend(("--workdir", workdir))
        for key in sorted(env):
            if ENV_NAME_RE.fullmatch(key) is None:
                raise ValueError("env name is invalid")
            if is_secret_env_name(key):
                raise ValueError(f"env {key} looks secret; use secret_env instead")
            _validate_env_value(key, env[key])
            args.extend(("-e", f"{key}={env[key]}"))
        for key in sorted(secrets_map):
            _validate_secret_name(key)
            _validate_env_value(key, secrets_map[key])
            # 値は子プロセス環境で渡すので、argv には名前だけを載せる。
            args.extend(("-e", key))
        args.append(name)
        args.extend(argv)
        return args

    async def _inspect(self, ref: str) -> _Inspect | None:
        """container を inspect する。存在しないときだけ None を返し、他の失敗は例外。"""
        rc, stdout, stderr = await self._docker(
            "inspect", "--type", "container", "--format", "{{json .}}", ref,
        )
        if rc != 0:
            if b"no such container" in stderr.lower():
                return None
            raise AgentContainerError(f"docker inspect failed (exit {rc})")
        try:
            decoded = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AgentContainerError("docker inspect returned invalid output") from exc
        if isinstance(decoded, list):
            if len(decoded) != 1:
                raise AgentContainerError("docker inspect returned an unexpected result")
            decoded = decoded[0]
        if not isinstance(decoded, dict):
            raise AgentContainerError("docker inspect returned an unexpected result")
        state = decoded.get("State")
        container_id = decoded.get("Id")
        config = decoded.get("Config")
        raw_labels = config.get("Labels") if isinstance(config, dict) else None
        if not isinstance(state, dict) or not isinstance(container_id, str) or not container_id:
            raise AgentContainerError("docker inspect returned an unexpected result")
        labels = {
            key: value
            for key, value in (raw_labels or {}).items()
            if isinstance(key, str) and isinstance(value, str)
        }
        return _Inspect(
            id=container_id,
            running=state.get("Running") is True,
            status=str(state.get("Status") or ""),
            labels=labels,
        )

    async def _docker(
        self,
        *args: str,
        timeout: float = _CONTROL_TIMEOUT_SECONDS,
        output_limit: int = _CONTROL_OUTPUT_LIMIT,
    ) -> tuple[int, bytes, bytes]:
        try:
            process = await asyncio.create_subprocess_exec(
                self.docker_binary,
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise AgentContainerError("docker could not be started") from exc
        stdout = bytearray()
        stderr = bytearray()
        try:
            await asyncio.wait_for(_collect_capped(process, stdout, stderr, output_limit), timeout)
        except asyncio.TimeoutError as exc:
            await _kill_and_reap(process)
            raise AgentContainerError("docker command timed out") from exc
        except AgentContainerOutputLimit as exc:
            await _kill_and_reap(process)
            raise AgentContainerError("docker output exceeded its limit") from exc
        except BaseException:
            await _kill_and_reap(process)
            raise
        return process.returncode, bytes(stdout), bytes(stderr)

    async def _docker_quiet(self, *args: str, timeout: float = _CONTROL_TIMEOUT_SECONDS) -> bool:
        """停止の試行を行う。結果は呼び出し側が inspect で確認する。"""
        try:
            rc, _, _ = await self._docker(*args, timeout=timeout)
        except AgentContainerError:
            return False
        return rc == 0


async def _kill_and_reap(process) -> None:
    """exec の client を kill して回収する。回収自体の失敗は握りつぶす。"""
    if getattr(process, "returncode", None) is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(process.wait(), _REAP_TIMEOUT_SECONDS)
    except (asyncio.TimeoutError, OSError, RuntimeError):
        pass


def filesystem_free_bytes(path: str) -> int:
    """path のあるファイルシステムで、非特権プロセスが使える空きバイト。"""
    info = os.statvfs(path)
    return info.f_bavail * info.f_frsize


def refuse_agent_room(running: int, free_bytes: int, limits) -> None:
    """同時数が上限以上、または空きが設定未満なら、caller 満杯と同じ形で断る。

    閾値は limits (AgentContainerConfig) だけを使う。同時数を先に見る。
    """
    from .execution_resources import ExecutionError
    if running >= limits.max_containers:
        raise ExecutionError(
            "capacity_exhausted", "Agent container capacity is full", 429, reason_code="containers",
        )
    if free_bytes < limits.min_free_disk_bytes:
        raise ExecutionError(
            "capacity_exhausted", "Workspace filesystem free space is below the configured minimum", 429,
            reason_code="disk",
        )


class AgentRoomCapacity:
    """部屋を増やす受付の前に、起動中の container 数と workspace の空きを設定と照合する。"""

    def __init__(self, limits, *,
                 running: Callable[[], Awaitable[int]] | None = None,
                 free_bytes: Callable[[str], int] | None = None,
                 container: AgentContainer | None = None) -> None:
        self.limits = limits
        self._running = running
        self._free_bytes = free_bytes if free_bytes is not None else filesystem_free_bytes
        self._container = container

    async def __call__(self, workspace_path: str, *, reserved: int = 0) -> None:
        from .execution_resources import ExecutionError
        try:
            if self._running is not None:
                running = await self._running()
            else:
                container = self._container if self._container is not None else AgentContainer(instance=DEFAULT_INSTANCE)
                running = len(await container.running_jobs())
        except AgentContainerError as exc:
            # 列挙できないまま通すと、見えない部屋の上に足してしまう。未分類の例外は
            # activation の見回りを止めるので、満杯と同じ待ちにする。
            raise ExecutionError(
                "capacity_exhausted", "Agent container count could not be read", 429,
                reason_code="containers_unknown",
            ) from exc
        try:
            free = self._free_bytes(workspace_path)
        except OSError as exc:
            raise ExecutionError(
                "capacity_exhausted", "Workspace filesystem free space could not be read", 429,
                reason_code="disk_unknown",
            ) from exc
        # 起動前の attempt と、台帳に無い起動中 container の大きい方。足すと二重に数える。
        refuse_agent_room(max(running, reserved), free, self.limits)


def container_identity_job(identity: object) -> tuple[str, str]:
    """process 記録の agent_container を検証し、(job_id, instance) を返す。不正な形は ValueError。

    名前は ``kotoyose-agent-<job_id>``、instance は label と同じ形、container ID は 16 進数だけ許す。
    """
    if not isinstance(identity, Mapping):
        raise ValueError("agent_container identity must be a mapping")
    name = identity.get("name")
    container_id = identity.get("id")
    instance = identity.get("instance")
    if not isinstance(name, str) or not name.startswith(CONTAINER_PREFIX):
        raise ValueError("agent_container name has an unexpected prefix")
    job_id = name[len(CONTAINER_PREFIX):]
    _validate_job_id(job_id)
    if not isinstance(instance, str) or INSTANCE_RE.fullmatch(instance) is None:
        raise ValueError("agent_container instance is invalid")
    if not isinstance(container_id, str) or _CONTAINER_ID_RE.fullmatch(container_id) is None:
        raise ValueError("agent_container id is invalid")
    return job_id, instance


async def recover_recorded_container(identity: object, *, expected_instance: str | None = None,
                                     ) -> Literal["confirmed", "unconfirmed"]:
    """再起動前に記録された container を、名前と label で確かめてから grace 0 で削除する。

    停止の証明は container が存在しないことだけに置く。identity が不正、docker が応答しない、
    所有 label が一致しない場合はいずれも unconfirmed で、何も削除しない。
    """
    try:
        job_id, instance = container_identity_job(identity)
        # 記録は自分で書いたものだが、他の instance の部屋に触れないよう定数に固定する。
        if instance != (expected_instance or DEFAULT_INSTANCE):
            raise ValueError("recorded container belongs to another instance")
        container = AgentContainer(instance=instance)
        return await container.remove(job_id, 0)
    except (ValueError, AgentContainerError):
        log.warning("agent_container.recovery_unconfirmed")
        return "unconfirmed"


def remove_leftover_homes(home_root: Path) -> list[str]:
    """home_root 直下の ``kotoyose-agent-home-*`` 実ディレクトリを削除し、削除した名前を返す。

    symlink は辿らず削除もしない。home_root 自体が / かユーザーの home の場合は拒否する。
    呼び出し側は、この home を使う container が残っていないことを保証すること。
    """
    if not isinstance(home_root, Path):
        raise TypeError("home_root must be a pathlib.Path")
    if home_root.is_symlink():
        raise ValueError("home_root must not be a symlink")
    try:
        root = home_root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError("home_root must resolve to an existing directory") from exc
    shared = {Path("/"), Path("/tmp"), Path("/var/tmp"), Path("/dev/shm"), Path.home().resolve()}
    if not root.is_dir() or root in shared:
        raise ValueError("home_root must be a dedicated non-root directory")
    removed: list[str] = []
    with os.scandir(root) as entries:
        names = sorted(entry.name for entry in entries if entry.name.startswith(HOME_PREFIX))
    for name in names:
        path = root / name
        if path.is_symlink() or not path.is_dir():
            continue
        try:
            shutil.rmtree(path)
        except OSError:
            # 部屋の uid が作った消せないファイルなどで 1 つ失敗しても、残りの掃除は続ける。
            log.warning("agent_container.home_cleanup_failed", name=name)
            continue
        removed.append(name)
    return removed
