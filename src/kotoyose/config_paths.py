"""Standard XDG configuration and data directory resolution for Kotoyose."""

from __future__ import annotations

from collections.abc import Mapping
import os
from pathlib import Path

APP_DIR_NAME = "kotoyose"


def default_config_dir(environ: Mapping[str, str] | None = None) -> Path:
    """Return `${XDG_CONFIG_HOME:-~/.config}/kotoyose`."""
    env = os.environ if environ is None else environ
    xdg = (env.get("XDG_CONFIG_HOME") or "").strip()
    if xdg:
        base = Path(xdg).expanduser()
    else:
        home = (env.get("HOME") or "").strip()
        base = (Path(home).expanduser() if home else Path.home()) / ".config"
    return base / APP_DIR_NAME


def default_data_dir(environ: Mapping[str, str] | None = None) -> Path:
    """Return `${XDG_DATA_HOME:-~/.local/share}/kotoyose`."""
    env = os.environ if environ is None else environ
    xdg = (env.get("XDG_DATA_HOME") or "").strip()
    if xdg:
        base = Path(xdg).expanduser()
    else:
        home = (env.get("HOME") or "").strip()
        base = (Path(home).expanduser() if home else Path.home()) / ".local" / "share"
    return base / APP_DIR_NAME


def default_execution_config_path(environ: Mapping[str, str] | None = None) -> Path:
    """Return `${XDG_CONFIG_HOME:-~/.config}/kotoyose/execution.json`."""
    return default_config_dir(environ) / "execution.json"


def resolve_execution_config_path(environ: Mapping[str, str] | None = None) -> str | None:
    """Return the machine execution config path, or None when execution is not configured.

    `KOTOYOSE_EXECUTION_CONFIG` wins when set; otherwise the XDG default is used if the file exists.
    """
    env = os.environ if environ is None else environ
    explicit = (env.get("KOTOYOSE_EXECUTION_CONFIG") or "").strip()
    if explicit:
        return explicit
    default = default_execution_config_path(env)
    return str(default) if default.is_file() else None


def resolve_config_file_path(environ: Mapping[str, str] | None = None) -> str:
    """Resolve `KOTOYOSE_CONFIG`, preferring `./config.yml` then XDG `config.yml`."""
    env = os.environ if environ is None else environ
    explicit = (env.get("KOTOYOSE_CONFIG") or "").strip()
    if explicit:
        return explicit
    local = Path("config.yml")
    if local.exists():
        return str(local)
    xdg_candidate = default_config_dir(env) / "config.yml"
    if xdg_candidate.exists():
        return str(xdg_candidate)
    return "config.yml"
