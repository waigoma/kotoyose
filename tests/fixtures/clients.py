"""Test helpers for Clients, which live in config.yml (ADR-0011)."""

from __future__ import annotations

from typing import Any

from kotoyose import config as config_module
from kotoyose.config import Client, get_clients


def clients(_setup: Any = None) -> dict[str, Client]:
    """Return the configured Clients; *_setup* lets callers run a config builder first."""
    return get_clients()


def set_client(name: str, client: Client) -> None:
    """Add or replace one Client in the in-memory config."""
    current = config_module.get_config()
    updated = dict(current.clients)
    updated[name] = client
    config_module._config = current.model_copy(update={"clients": updated})
