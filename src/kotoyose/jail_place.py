"""Shared JailPlace container foundation for isolated per-Run CLI execution.

Unifies container naming, egress socket staging, bounded resource specs,
relay argv construction, stop confirmation, and run directory cleanup across
`container_print`, `container_claude`, and `container_codex`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
import contextlib
import hashlib
import math
from pathlib import Path
import shutil
import tempfile
from typing import Any

from .agent_container import JOB_ID_RE, AgentContainerSpec, container_name
from .print_cli_egress import MAX_SOCKET_PATH_BYTES
from .provider_egress import RELAY_HOST

CONTAINER_HOME = "/home/agent"
CONTAINER_WORKSPACE = "/workspace"
CONTAINER_RELAY = "/opt/agent/egress_relay.py"
CONTAINER_SOCKET = "/run/egress/egress.sock"
RELAY_PATH = Path(__file__).with_name("egress_relay.py")
PROXY_URL = f"http://{RELAY_HOST}"
WALL_MARGIN_SECONDS = 60


def container_job_id_for(attempt_id: str, generation: int) -> str:
    """Return a valid container job ID for `(attempt_id, generation)`."""
    candidate = f"{attempt_id}-g{generation}"
    if JOB_ID_RE.fullmatch(candidate) is not None:
        return candidate
    return "job-" + hashlib.sha256(candidate.encode()).hexdigest()[:40]


def create_egress_socket_dir(root: str | Path, *, prefix: str = "kotoyose-eg-") -> Path:
    """Create a short 0700 directory so `<dir>/egress.sock` fits UNIX socket limits."""
    candidate: Path | None = None
    try:
        candidate = Path(tempfile.mkdtemp(prefix=prefix, dir=str(root)))
        if len(str(candidate / "egress.sock").encode("utf-8")) <= MAX_SOCKET_PATH_BYTES:
            return candidate
        shutil.rmtree(candidate, ignore_errors=True)
    except OSError:
        if candidate is not None:
            shutil.rmtree(candidate, ignore_errors=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir="/tmp"))


def bounded_limit(job_val: int | float, cfg_val: int | float) -> int | float:
    """Clamp a positive job resource request to the configured container ceiling."""
    if job_val > 0:
        return min(job_val, cfg_val)
    return cfg_val


def compute_wall_seconds(
    timeout_seconds: float,
    grace_seconds: float,
    margin_seconds: int = WALL_MARGIN_SECONDS,
) -> int:
    """Compute container lifetime seconds including grace and teardown margin."""
    return max(1, math.ceil(timeout_seconds + grace_seconds + margin_seconds))


def protected_ro_dirs(protected: Sequence[Mapping[str, Any]], workspace: Path) -> dict[str, Path]:
    """Map protected workspace entries to read-only `/workspace/<entry>` bind mounts."""
    return {f"{CONTAINER_WORKSPACE}/{entry['name']}": workspace / entry["name"] for entry in protected}


def build_connect_proxy_env(base_env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return non-secret environment variables directing HTTPS/HTTP through the local relay."""
    env = dict(base_env) if base_env is not None else {}
    env.update({
        "HTTPS_PROXY": PROXY_URL,
        "HTTP_PROXY": PROXY_URL,
        "NO_PROXY": "",
    })
    return env


def build_relay_exec_argv(
    container_cli: str,
    cli_args: Sequence[str],
    *,
    socket_path: str = CONTAINER_SOCKET,
) -> list[str]:
    """Build `docker exec` inner argv invoking the container binary via `egress_relay.py`."""
    return ["python3", CONTAINER_RELAY, socket_path, "--", container_cli, *cli_args]


async def remove_container_confirmed(container: Any, job_id: str, grace_seconds: int = 0) -> str:
    """Remove a Run container and downgrade removal exceptions to `unconfirmed`."""
    try:
        return await container.remove(job_id, grace_seconds)
    except Exception:
        return "unconfirmed"


def reap_late_process(task: asyncio.Task[Any]) -> None:
    """Kill a `docker exec` child process that completed after launch cancellation."""
    if task.cancelled() or task.exception() is not None:
        return
    late = task.result()
    if late.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            late.kill()


def container_identity(container: Any, job_id: str, container_id: str) -> dict[str, str]:
    """Return the persisted runtime container identity payload."""
    return {"name": container_name(job_id), "id": container_id, "instance": container.instance}


class JailPlace:
    """Container-backed Place lifecycle and spec builder over `AgentContainer`."""

    def __init__(self, container: Any, settings: Any, image_id: str | None) -> None:
        self.container = container
        self.settings = settings
        self.image_id = image_id

    def build_spec(
        self,
        *,
        job_id: str,
        workspace: Path,
        home: Path,
        egress_dir: Path | None,
        timeout_seconds: float,
        grace_seconds: float,
        env: Mapping[str, str],
        ro_files: Mapping[str, Path],
        rw_dirs: Mapping[str, Path] | None = None,
        ro_dirs: Mapping[str, Path] | None = None,
        workspace_readonly: bool = False,
        memory_bytes: int = 0,
        cpus: float = 0.0,
        pids_limit: int = 0,
    ) -> AgentContainerSpec:
        """Build an `AgentContainerSpec` applying configured resource ceilings."""
        if self.settings is None or not self.image_id:
            raise ValueError("Container settings and image_id are required")
        return AgentContainerSpec(
            job_id=job_id,
            image_id=self.image_id,
            workspace=workspace,
            home=home,
            agent_root=None,
            egress_dir=egress_dir,
            wall_seconds=compute_wall_seconds(timeout_seconds, grace_seconds),
            memory_bytes=int(bounded_limit(memory_bytes, self.settings.workspace_memory_bytes)),
            cpus=float(bounded_limit(cpus, self.settings.cpus)),
            pids_limit=int(bounded_limit(pids_limit, self.settings.pids_limit)),
            env=dict(env),
            ro_files=dict(ro_files),
            rw_dirs=dict(rw_dirs or {}),
            ro_dirs=dict(ro_dirs or {}),
            workspace_readonly=workspace_readonly,
        )

    async def start_container(self, spec: AgentContainerSpec) -> str:
        """Create and start the container via `AgentContainer.create`."""
        return await self.container.create(spec)

    async def remove_container(self, job_id: str, grace_seconds: int = 0) -> str:
        """Remove the container and return `confirmed` or `unconfirmed`."""
        return await remove_container_confirmed(self.container, job_id, grace_seconds)

    @staticmethod
    def cleanup_run_dirs(
        *,
        container_confirmation: str,
        egress_closed: bool,
        temp_home: Path | None = None,
        socket_dir: Path | None = None,
        extra_dirs: Sequence[Path] = (),
    ) -> None:
        """Clean up transient per-Run directories while preserving unconfirmed artifacts."""
        for directory in extra_dirs:
            shutil.rmtree(directory, ignore_errors=True)
        if temp_home is not None and container_confirmation == "confirmed":
            shutil.rmtree(temp_home, ignore_errors=True)
        if socket_dir is not None and container_confirmation == "confirmed" and egress_closed:
            shutil.rmtree(socket_dir, ignore_errors=True)
