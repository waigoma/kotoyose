"""Adapter × Place conformance matrix for host and jail execution boundaries."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AdapterPlaceCapabilities:
    """Verified confinement properties for a runtime adapter across places."""

    driver: str
    places: frozenset[str]
    can_disable_host_tools: bool
    scrubs_process_env: bool
    jail_network_none: bool
    jail_readonly_root: bool


ADAPTER_PLACE_MATRIX: dict[str, AdapterPlaceCapabilities] = {
    "claude.agent-sdk": AdapterPlaceCapabilities(
        driver="claude.agent-sdk",
        places=frozenset({"host", "jail"}),
        can_disable_host_tools=True,
        scrubs_process_env=True,
        jail_network_none=True,
        jail_readonly_root=True,
    ),
    "codex.app-server": AdapterPlaceCapabilities(
        driver="codex.app-server",
        places=frozenset({"jail"}),
        # Codex read-only sandbox still permits shell execution and reading host files.
        can_disable_host_tools=False,
        scrubs_process_env=True,
        jail_network_none=True,
        jail_readonly_root=True,
    ),
    "cli.print:agy": AdapterPlaceCapabilities(
        driver="cli.print:agy",
        places=frozenset({"jail"}),
        # `agy` has no none surface and `--mode plan` can still read host files.
        can_disable_host_tools=False,
        scrubs_process_env=True,
        jail_network_none=True,
        jail_readonly_root=True,
    ),
    "antigravity.agy": AdapterPlaceCapabilities(
        driver="antigravity.agy",
        places=frozenset({"jail"}),
        can_disable_host_tools=False,
        scrubs_process_env=True,
        jail_network_none=True,
        jail_readonly_root=True,
    ),
    "cursor.sdk": AdapterPlaceCapabilities(
        driver="cursor.sdk",
        places=frozenset(),
        can_disable_host_tools=False,
        scrubs_process_env=True,
        jail_network_none=False,
        jail_readonly_root=False,
    ),
}

HOST_CONFORMANT_DRIVERS: frozenset[str] = frozenset(
    driver
    for driver, cap in ADAPTER_PLACE_MATRIX.items()
    if "host" in cap.places and cap.can_disable_host_tools
)


def can_claim_host_place(driver: str) -> bool:
    """Return True only for adapters proven able to disable built-in host tools."""
    return driver in HOST_CONFORMANT_DRIVERS
