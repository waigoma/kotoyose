"""Admission descriptor for container-backed coding/read rooms."""

from __future__ import annotations

import sys


PROVIDER_WORKER_ID = "local-codex-host-coding-v1"
_PROVIDER_DESCRIPTOR = {
    "worker_id": PROVIDER_WORKER_ID, "os": "linux", "applications": [], "gpu": "none",
    "isolation_modes": ["sandbox"], "native": False, "workspace_locality": "local",
    "remote_connection": None, "workspace_bindings": [],
}


class CodexProviderExecutor:
    """An admission descriptor only; never registered as a CommandExecution worker."""
    worker_id = PROVIDER_WORKER_ID
    workspace_locality = "local"

    @staticmethod
    def public_snapshot():
        return {**_PROVIDER_DESCRIPTOR, "applications": [], "isolation_modes": ["sandbox"], "workspace_bindings": []}


def admit_provider_executor(requirements, *, require_host_scope: bool = False):
    """Validate worker requirements for per-Run container coding/read rooms."""
    from .execution_resources import ExecutionError
    if (requirements.os not in {None, "linux"} or requirements.applications
            or requirements.gpu != "none" or requirements.workspace_locality not in {"any", "local"}
            or requirements.native == "required"):
        raise ExecutionError("unsupported_capability", "Host coding provider cannot satisfy Worker requirements", 501)
    if sys.platform != "linux":
        raise ExecutionError("unsupported_capability", "Host coding requires Linux", 501)
    return CodexProviderExecutor()


async def stop_recovered_provider_scope(attempt_id: str, generation: int, evidence: dict | None) -> str:
    """Systemd provider scopes were removed in ADR-0010 Stage 7; fail closed without saved proof."""
    return "unconfirmed"
