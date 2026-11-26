"""Atomic host catalog updates from the data directory and the owner console."""

from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import stat
from threading import RLock
from typing import TYPE_CHECKING

import structlog

from .config_paths import default_data_dir
from .print_cli_profiles import set_host_data_profiles, use_data_profiles
from .provider_data import (
    MAX_DOCUMENT, Declaration, identifier, parse_declaration, reserved_declaration_names,
    stored_declaration_identity, validate_catalog,
)
from .provider_data_storage import atomic_write

if TYPE_CHECKING:
    from .config import HarnessConfig

log = structlog.get_logger()
_lock = RLock()


@dataclass(frozen=True)
class InvalidDeclaration:
    filename: str
    id: str | None
    profile_id: str | None
    reason: str


@dataclass(frozen=True)
class Catalog:
    rows: tuple[Declaration, ...] = ()
    invalid: tuple[InvalidDeclaration, ...] = ()
    error: str | None = None

    def offers(self):
        return [row.offer() for row in self.rows]


_catalog = Catalog()


def host_catalog() -> Catalog:
    return _catalog


def _directory() -> Path:
    return default_data_dir().absolute() / "provider-declarations"


def _read(directory: Path) -> dict[Path, Declaration | InvalidDeclaration]:
    if directory.is_symlink():
        raise ValueError("provider declaration directory must not be a symlink")
    if not directory.exists():
        return {}
    if not directory.is_dir():
        raise ValueError("provider declaration storage must be a directory")

    entries = {}
    for path in sorted(directory.glob("*.json")):
        raw = b""
        try:
            if not stat.S_ISREG(path.lstat().st_mode):
                raise ValueError("expected a regular file")
            with path.open("rb") as source:
                raw = source.read(MAX_DOCUMENT + 1)
            entries[path] = parse_declaration(raw)
            continue
        except OSError as exc:
            reason = exc.strerror or "could not read declaration"
        except ValueError as exc:
            reason = str(exc)
        declaration_id, profile_id = stored_declaration_identity(raw)
        if declaration_id is None:
            try:
                declaration_id = identifier(path.stem)
            except ValueError:
                pass
        entries[path] = InvalidDeclaration(path.name, declaration_id, profile_id, reason)
    return entries


def _build_catalog(entries: dict[Path, Declaration | InvalidDeclaration]) -> Catalog:
    rows = {}
    invalid = []
    for path, entry in entries.items():
        if isinstance(entry, InvalidDeclaration):
            invalid.append(entry)
            continue
        try:
            validate_catalog((entry,))
        except ValueError as exc:
            invalid.append(InvalidDeclaration(path.name, entry.id, entry.profile_id, str(exc)))
        else:
            rows[path] = entry

    names = Counter(name for row in rows.values() for name in row.names)
    accepted = {}
    for path, row in rows.items():
        if any(names[name] > 1 for name in row.names):
            invalid.append(InvalidDeclaration(
                path.name, row.id, row.profile_id, f"provider declaration identifier collision: {row.id}",
            ))
        else:
            accepted[path] = row
    try:
        return Catalog(validate_catalog(accepted.values()), tuple(invalid))
    except ValueError as exc:
        invalid.extend(
            InvalidDeclaration(path.name, row.id, row.profile_id, str(exc)) for path, row in accepted.items()
        )
        return Catalog(invalid=tuple(invalid), error=str(exc))


def _publish(catalog: Catalog) -> Catalog:
    global _catalog
    set_host_data_profiles(tuple(row.profile() for row in catalog.rows))
    _catalog = catalog
    if catalog.error:
        log.warning("provider_data.catalog_error", reason=catalog.error)
    for entry in catalog.invalid:
        log.warning("provider_data.invalid_declaration", declaration=entry.id or repr(entry.filename), reason=entry.reason)
    return catalog


def _load_catalog() -> Catalog:
    try:
        return _build_catalog(_read(_directory()))
    except OSError as exc:
        return Catalog(error=exc.strerror or "could not read provider declaration directory")
    except ValueError as exc:
        return Catalog(error=str(exc))


@contextmanager
def catalog_for_config():
    """Make a disk snapshot available to config validation and publish it on success."""
    with _lock:
        catalog = _load_catalog()
        with use_data_profiles(tuple(row.profile() for row in catalog.rows)):
            yield catalog
        _publish(catalog)


def reload_catalog() -> Catalog:
    with _lock:
        return _publish(_load_catalog())


def _stored_path(directory: Path, entries: dict[Path, Declaration | InvalidDeclaration], provider: str) -> Path:
    canonical = directory / f"{provider}.json"
    if canonical in entries:
        return canonical
    matches = [path for path, entry in entries.items() if entry.id == provider]
    if len(matches) > 1:
        raise ValueError("multiple stored declarations use this id")
    return matches[0] if matches else canonical


def upload_declaration(raw: bytes) -> Catalog:
    row = parse_declaration(raw)
    with _lock:
        directory = _directory()
        entries = _read(directory)
        path = _stored_path(directory, entries, row.id)
        if isinstance(entries.get(path), Declaration) and entries[path].id != row.id:
            raise ValueError("provider declaration filename collision")
        entries[path] = row
        catalog = _build_catalog(entries)
        for entry in catalog.invalid:
            if entry.filename == path.name:
                raise ValueError(entry.reason)
        directory.mkdir(parents=True, exist_ok=True)
        atomic_write(path, raw)
        return _publish(catalog)


def _config_references(config: "HarnessConfig", names: set[str]) -> list[str]:
    connections = {name for name, conn in config.connections.items() if conn.provider in names or conn.profile in names}
    connections |= {name for name, conn in config.connections.items() if conn.quota_from in connections}
    routes = {name for name, route in config.routes.items() if route.profile in names or route.connection in connections}
    aliases = {name for name, preset in config.models.items() if preset.route in routes}
    while delegates := {
        name for name, preset in config.models.items() if name not in aliases and aliases.intersection(preset.delegates)
    }:
        aliases.update(delegates)
    aliases |= {name for name, pool in config.pools.items() if aliases.intersection(pool.candidates)}
    return [
        f"{kind} {name!r}"
        for kind, entries in (("connection", connections), ("route", routes), ("alias", aliases))
        for name in sorted(entries)
    ]


def remove_declaration(provider: str) -> Catalog:
    from .config import get_config

    with _lock:
        directory = _directory()
        entries = _read(directory)
        if provider.endswith(".json") and Path(provider).name == provider:
            path = directory / provider
        else:
            path = _stored_path(directory, entries, identifier(provider))
        if path not in entries:
            raise FileNotFoundError(path)
        entry = entries[path]
        names = {name for name in (entry.id, entry.profile_id) if name is not None} - reserved_declaration_names()
        if references := _config_references(get_config(), names):
            raise ValueError(
                f"Provider declaration {entry.id or path.name!r} is still referenced by {', '.join(references)}. "
                "Remove these references before deleting the declaration."
            )
        del entries[path]
        catalog = _build_catalog(entries)
        path.unlink()
        return _publish(catalog)
