"""Encrypted secret store backed by Postgres (or in-memory for dev/tests)."""

from __future__ import annotations

import contextvars
import errno
import hashlib
import os
import re
import secrets
import stat
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .process_env import get as process_env_get

log = structlog.get_logger()

MASTER_KEY_BYTES = 32
NONCE_BYTES = 12
SECRET_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")

_ACTIVE_STORE: SecretStore | None = None
_PRELOADED_SECRETS: contextvars.ContextVar[Mapping[str, str] | None] = (
    contextvars.ContextVar("kotoyose_preloaded_secrets", default=None)
)


class SecretStoreError(RuntimeError):
    """Base error for secret store operations."""


class SecretStoreDisabledError(SecretStoreError):
    """Raised when the secret store has no valid master key configured."""


class SecretKeyError(SecretStoreError):
    """Raised when a master key file fails validation or loading."""


class SecretNotFoundError(SecretStoreError):
    """Raised when a referenced secret name does not exist."""


class SecretDecryptionError(SecretStoreError):
    """Raised when decryption fails (wrong master key, AAD mismatch, or tampering)."""


def compute_key_id(master_key: bytes) -> str:
    """Return the first 8 hex characters of SHA-256(master_key)."""
    return hashlib.sha256(master_key).hexdigest()[:8]


def validate_secret_name(name: str) -> str:
    """Validate and return a secret identifier."""
    if not isinstance(name, str) or not SECRET_NAME_RE.fullmatch(name):
        raise ValueError(
            "secret name must be 1-128 characters of letters, digits, '_', '.', or '-'"
        )
    return name


def _build_aad(name: str, version: int, key_id: str) -> bytes:
    """Build AES-GCM AAD binding a ciphertext row to its name, version, and key_id."""
    return f"{name}|{version}|{key_id}".encode("utf-8")


def _mask_last4(value: str) -> str:
    """Return the last 4 characters only when value is at least 16 chars; otherwise empty."""
    return value[-4:] if len(value) >= 16 else ""


def encrypt_secret_value(
    master_key: bytes,
    *,
    name: str,
    version: int,
    value: str,
) -> tuple[bytes, bytes, str]:
    """Encrypt a plaintext secret with AES-256-GCM and AAD `name|version|key_id`."""
    key_id = compute_key_id(master_key)
    nonce = os.urandom(NONCE_BYTES)
    aad = _build_aad(name, version, key_id)
    ciphertext = AESGCM(master_key).encrypt(nonce, value.encode("utf-8"), aad)
    return ciphertext, nonce, key_id


def decrypt_secret_value(
    master_key: bytes,
    *,
    name: str,
    version: int,
    key_id: str,
    ciphertext: bytes,
    nonce: bytes,
) -> str:
    """Decrypt a secret row after verifying key_id and AES-GCM authentication tag."""
    loaded_key_id = compute_key_id(master_key)
    if key_id != loaded_key_id:
        raise SecretDecryptionError(
            f"Secret '{name}' was encrypted with key_id '{key_id}', "
            f"which does not match loaded master key '{loaded_key_id}'"
        )
    aad = _build_aad(name, version, loaded_key_id)
    try:
        plaintext = AESGCM(master_key).decrypt(nonce, ciphertext, aad)
    except InvalidTag as exc:
        raise SecretDecryptionError(
            f"Failed to decrypt secret '{name}' (wrong key or tampered row/AAD)"
        ) from exc
    return plaintext.decode("utf-8")


def load_master_key_file(path: str | Path, *, allow_systemd_credential: bool = False) -> bytes:
    """Load and validate a 32-byte master key file (mode 0600, owned by service user)."""
    file_path = Path(path)
    try:
        fd = os.open(file_path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError as exc:
        raise SecretKeyError(f"Master key file does not exist: {file_path}") from exc
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise SecretKeyError(f"Master key path must not be a symlink: {file_path}") from exc
        raise SecretKeyError(f"Cannot open master key file {file_path}: {exc}") from exc

    try:
        try:
            st = os.fstat(fd)
        except OSError as exc:
            raise SecretKeyError(f"Cannot inspect master key file {file_path}: {exc}") from exc

        if not stat.S_ISREG(st.st_mode):
            raise SecretKeyError(f"Master key path is not a regular file: {file_path}")

        mode = stat.S_IMODE(st.st_mode)
        euid = os.geteuid()
        allowed_modes = (0o600, 0o400) if allow_systemd_credential else (0o600,)
        if mode not in allowed_modes:
            expected = "0400/0600" if allow_systemd_credential else "0600"
            raise SecretKeyError(
                f"Master key file {file_path} has unsafe permissions {oct(mode)}; expected {expected}"
            )

        allowed_uids = (euid, 0) if allow_systemd_credential else (euid,)
        if st.st_uid not in allowed_uids:
            raise SecretKeyError(
                f"Master key file {file_path} is owned by uid {st.st_uid}, expected uid {euid}"
            )

        chunks: list[bytes] = []
        total = 0
        try:
            while True:
                chunk = os.read(fd, 4096)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > MASTER_KEY_BYTES:
                    break
        except OSError as exc:
            raise SecretKeyError(f"Cannot read master key file {file_path}: {exc}") from exc
    finally:
        os.close(fd)

    data = b"".join(chunks)
    if len(data) != MASTER_KEY_BYTES:
        raise SecretKeyError(
            f"Master key file {file_path} must be exactly {MASTER_KEY_BYTES} bytes, got {len(data)}"
        )
    return data


def _write_new_master_key_file(
    path: str | Path,
    *,
    dir_mode: int | None = None,
) -> tuple[bytes, str]:
    """Atomically create a 32-byte random master key file with O_CREAT|O_EXCL and mode 0600."""
    target = Path(path)
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"Refusing to overwrite existing master key file: {target}")
    if dir_mode is not None:
        target.parent.mkdir(parents=True, exist_ok=True, mode=dir_mode)
        os.chmod(target.parent, dir_mode)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)

    key = secrets.token_bytes(MASTER_KEY_BYTES)
    fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        view = memoryview(key)
        written = 0
        while written < len(view):
            n = os.write(fd, view[written:])
            if n <= 0:
                raise OSError("short write while writing master key")
            written += n
        if written != MASTER_KEY_BYTES:
            raise OSError(f"expected to write {MASTER_KEY_BYTES} bytes, wrote {written}")
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    os.close(fd)
    os.chmod(target, 0o600)
    return key, compute_key_id(key)


def create_master_key_file(path: str | Path, *, dir_mode: int | None = None) -> str:
    """Create a 32-byte random master key at `path` with mode 0600 and return its key_id."""
    _, key_id = _write_new_master_key_file(path, dir_mode=dir_mode)
    return key_id


def default_master_key_path(environ: Mapping[str, str] | None = None) -> Path:
    """Return `${XDG_DATA_HOME:-~/.local/share}/kotoyose/master.key`."""
    env = os.environ if environ is None else environ
    xdg = (env.get("XDG_DATA_HOME") or "").strip()
    if xdg:
        base = Path(xdg)
    else:
        home = (env.get("HOME") or "").strip()
        base = (Path(home) if home else Path.home()) / ".local" / "share"
    return base / "kotoyose" / "master.key"


def load_master_key_from_env(
    environ: Mapping[str, str] | None = None,
    *,
    allow_default: bool = False,
    create_default: bool = False,
) -> tuple[bytes | None, str | None]:
    """Load the master key from systemd CREDENTIALS_DIRECTORY/kotoyose-master, KOTOYOSE_MASTER_KEY_FILE,
    or (when `allow_default=True`) the XDG default location.

    Returns (master_key, source_description). Returns (None, None) if unconfigured.
    Raises SecretKeyError if a key file is missing or invalid.
    """
    creds_dir = (process_env_get("CREDENTIALS_DIRECTORY") if environ is None else environ.get("CREDENTIALS_DIRECTORY")) or ""
    creds_dir = creds_dir.strip()
    if creds_dir:
        cred_path = Path(creds_dir) / "kotoyose-master"
        if cred_path.exists() or cred_path.is_symlink():
            return (
                load_master_key_file(cred_path, allow_systemd_credential=True),
                f"systemd:{cred_path}",
            )

    env_file = (process_env_get("KOTOYOSE_MASTER_KEY_FILE") if environ is None else environ.get("KOTOYOSE_MASTER_KEY_FILE")) or ""
    env_file = env_file.strip()
    if env_file:
        return load_master_key_file(env_file, allow_systemd_credential=False), env_file

    if not allow_default:
        return None, None

    default_path = default_master_key_path(environ)
    if default_path.exists() or default_path.is_symlink():
        return load_master_key_file(default_path, allow_systemd_credential=False), str(default_path)

    if create_default:
        try:
            key, key_id = _write_new_master_key_file(default_path, dir_mode=0o700)
        except FileExistsError:
            return load_master_key_file(default_path, allow_systemd_credential=False), str(default_path)
        except OSError as exc:
            raise SecretKeyError(f"Cannot create default master key file {default_path}: {exc}") from exc
        log.warning(
            "secret_store.master_key_created",
            path=str(default_path),
            key_id=key_id,
            message="Created new master key file; back it up now because encrypted secrets cannot be recovered without it",
        )
        return key, str(default_path)

    return None, None


@dataclass(frozen=True)
class _SecretRow:
    name: str
    purpose: str
    ciphertext: bytes
    nonce: bytes
    key_id: str
    last4: str
    created_at: datetime
    updated_at: datetime
    updated_by: str | None
    version: int


class SecretStore:
    """Async AES-256-GCM secret store backed by Postgres or in-memory dict."""

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS kotoyose.secrets (
        name TEXT PRIMARY KEY,
        purpose TEXT NOT NULL DEFAULT '',
        ciphertext BYTEA NOT NULL,
        nonce BYTEA NOT NULL,
        key_id TEXT NOT NULL,
        last4 TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_by TEXT,
        version INT NOT NULL DEFAULT 1
    );
    """

    def __init__(
        self,
        pool: Any = None,
        *,
        master_key: bytes | None = None,
        key_error: str | None = None,
        source: str | None = None,
    ) -> None:
        if master_key is not None and len(master_key) != MASTER_KEY_BYTES:
            raise SecretKeyError(f"Master key must be {MASTER_KEY_BYTES} bytes")
        self._pool = pool
        self._master_key = master_key
        self._key_id = compute_key_id(master_key) if master_key is not None else None
        self._key_error = key_error
        self._source = source
        self._rows: dict[str, _SecretRow] = {}

    @classmethod
    def from_env(
        cls,
        pool: Any = None,
        *,
        environ: Mapping[str, str] | None = None,
        allow_default: bool = False,
        create_default: bool = False,
    ) -> SecretStore:
        """Create a SecretStore configured from environment variables."""
        master_key, source = load_master_key_from_env(
            environ,
            allow_default=allow_default,
            create_default=create_default,
        )
        return cls(pool=pool, master_key=master_key, source=source)

    @property
    def is_configured(self) -> bool:
        """Whether a valid master key is loaded."""
        return self._master_key is not None

    @property
    def key_id(self) -> str | None:
        """Loaded master key identifier (8 hex chars) or None if disabled."""
        return self._key_id

    @property
    def key_error(self) -> str | None:
        """Error message recorded if master key startup validation failed."""
        return self._key_error

    def _require_key(self) -> bytes:
        if self._master_key is None:
            if self._key_error:
                raise SecretStoreDisabledError(f"secret store not configured: {self._key_error}")
            raise SecretStoreDisabledError("secret store not configured")
        return self._master_key

    async def initialize(self) -> None:
        """Create the kotoyose.secrets table when backed by a Postgres pool/connection."""
        if self._pool is not None:
            await self._pool.execute(self._SCHEMA)

    @staticmethod
    def _row_to_metadata(row: dict[str, Any] | _SecretRow) -> dict[str, Any]:
        if isinstance(row, _SecretRow):
            return {
                "name": row.name,
                "purpose": row.purpose,
                "set": True,
                "last4": row.last4,
                "created_at": row.created_at.isoformat(),
                "updated_at": row.updated_at.isoformat(),
                "updated_by": row.updated_by,
                "version": row.version,
                "key_id": row.key_id,
            }
        created = row["created_at"]
        updated = row["updated_at"]
        return {
            "name": row["name"],
            "purpose": row["purpose"] or "",
            "set": True,
            "last4": row["last4"] or "",
            "created_at": created.isoformat() if hasattr(created, "isoformat") else str(created),
            "updated_at": updated.isoformat() if hasattr(updated, "isoformat") else str(updated),
            "updated_by": row["updated_by"],
            "version": int(row["version"]),
            "key_id": row["key_id"],
        }

    async def list(self) -> list[dict[str, Any]]:
        """List stored secrets metadata (never ciphertext or plaintext values)."""
        self._require_key()
        if self._pool is None:
            return [self._row_to_metadata(self._rows[name]) for name in sorted(self._rows)]
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT name, purpose, key_id, last4, created_at, updated_at, updated_by, version
                FROM kotoyose.secrets
                ORDER BY name ASC
                """
            )
        return [self._row_to_metadata(dict(r)) for r in rows]

    async def exists(self, name: str) -> bool:
        """Check whether a secret exists without decrypting it."""
        validate_secret_name(name)
        if not self.is_configured:
            return False
        if self._pool is None:
            return name in self._rows
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT 1 FROM kotoyose.secrets WHERE name = $1", name)
        return row is not None

    async def metadata(self, name: str) -> dict[str, Any] | None:
        """Fetch metadata for one secret without decrypting its value."""
        validate_secret_name(name)
        self._require_key()
        if self._pool is None:
            row = self._rows.get(name)
            return self._row_to_metadata(row) if row is not None else None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT name, purpose, key_id, last4, created_at, updated_at, updated_by, version
                FROM kotoyose.secrets
                WHERE name = $1
                """,
                name,
            )
        return self._row_to_metadata(dict(row)) if row is not None else None

    async def set(
        self,
        name: str,
        value: str,
        *,
        purpose: str = "",
        updated_by: str | None = None,
    ) -> dict[str, Any]:
        """Encrypt and store (or replace) a secret, bumping its version."""
        validate_secret_name(name)
        master_key = self._require_key()
        if not isinstance(value, str) or not value:
            raise ValueError("secret value must be a non-empty string")
        purpose_clean = (purpose or "").strip()
        last4 = _mask_last4(value)
        now = datetime.now(timezone.utc)

        if self._pool is None:
            existing = self._rows.get(name)
            version = (existing.version + 1) if existing is not None else 1
            created_at = existing.created_at if existing is not None else now
            ciphertext, nonce, key_id = encrypt_secret_value(
                master_key,
                name=name,
                version=version,
                value=value,
            )
            stored = _SecretRow(
                name=name,
                purpose=purpose_clean,
                ciphertext=ciphertext,
                nonce=nonce,
                key_id=key_id,
                last4=last4,
                created_at=created_at,
                updated_at=now,
                updated_by=updated_by,
                version=version,
            )
            self._rows[name] = stored
            return self._row_to_metadata(stored)

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                existing = await conn.fetchrow(
                    "SELECT version FROM kotoyose.secrets WHERE name = $1 FOR UPDATE",
                    name,
                )
                version = int(existing["version"]) + 1 if existing is not None else 1
                ciphertext, nonce, key_id = encrypt_secret_value(
                    master_key,
                    name=name,
                    version=version,
                    value=value,
                )
                row = await conn.fetchrow(
                    """
                    INSERT INTO kotoyose.secrets (
                        name, purpose, ciphertext, nonce, key_id, last4,
                        created_at, updated_at, updated_by, version
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, now(), now(), $7, $8)
                    ON CONFLICT (name) DO UPDATE SET
                        purpose = EXCLUDED.purpose,
                        ciphertext = EXCLUDED.ciphertext,
                        nonce = EXCLUDED.nonce,
                        key_id = EXCLUDED.key_id,
                        last4 = EXCLUDED.last4,
                        updated_at = now(),
                        updated_by = EXCLUDED.updated_by,
                        version = EXCLUDED.version
                    RETURNING name, purpose, key_id, last4, created_at, updated_at, updated_by, version
                    """,
                    name,
                    purpose_clean,
                    ciphertext,
                    nonce,
                    key_id,
                    last4,
                    updated_by,
                    version,
                )
        return self._row_to_metadata(dict(row))

    async def get_with_version(self, name: str) -> tuple[str, int]:
        """Decrypt a secret and return `(plaintext, version)`."""
        validate_secret_name(name)
        master_key = self._require_key()
        if self._pool is None:
            row = self._rows.get(name)
            if row is None:
                raise SecretNotFoundError(f"Unknown secret '{name}'")
            plaintext = decrypt_secret_value(
                master_key,
                name=row.name,
                version=row.version,
                key_id=row.key_id,
                ciphertext=row.ciphertext,
                nonce=row.nonce,
            )
            return plaintext, row.version

        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT name, ciphertext, nonce, key_id, version
                FROM kotoyose.secrets
                WHERE name = $1
                """,
                name,
            )
        if row is None:
            raise SecretNotFoundError(f"Unknown secret '{name}'")
        plaintext = decrypt_secret_value(
            master_key,
            name=row["name"],
            version=int(row["version"]),
            key_id=row["key_id"],
            ciphertext=bytes(row["ciphertext"]),
            nonce=bytes(row["nonce"]),
        )
        return plaintext, int(row["version"])

    async def get(self, name: str) -> str:
        """Decrypt and return a secret's plaintext value."""
        plaintext, _ = await self.get_with_version(name)
        return plaintext

    async def delete(self, name: str) -> bool:
        """Delete a secret by name. Returns True if a row was removed."""
        validate_secret_name(name)
        self._require_key()
        if self._pool is None:
            return self._rows.pop(name, None) is not None
        async with self._pool.acquire() as conn:
            result = await conn.execute("DELETE FROM kotoyose.secrets WHERE name = $1", name)
        return result == "DELETE 1"

    async def rotate_key(self, old_key: bytes, new_key: bytes) -> int:
        """Re-encrypt all secrets from `old_key` to `new_key`. Returns updated row count."""
        if len(old_key) != MASTER_KEY_BYTES or len(new_key) != MASTER_KEY_BYTES:
            raise SecretKeyError(f"Master keys must be {MASTER_KEY_BYTES} bytes")
        if self._pool is None:
            rotated_rows: dict[str, _SecretRow] = {}
            for name, row in self._rows.items():
                plaintext = decrypt_secret_value(
                    old_key,
                    name=row.name,
                    version=row.version,
                    key_id=row.key_id,
                    ciphertext=row.ciphertext,
                    nonce=row.nonce,
                )
                new_ciphertext, new_nonce, new_key_id = encrypt_secret_value(
                    new_key,
                    name=row.name,
                    version=row.version,
                    value=plaintext,
                )
                rotated_rows[name] = _SecretRow(
                    name=row.name,
                    purpose=row.purpose,
                    ciphertext=new_ciphertext,
                    nonce=new_nonce,
                    key_id=new_key_id,
                    last4=row.last4,
                    created_at=row.created_at,
                    updated_at=datetime.now(timezone.utc),
                    updated_by=row.updated_by,
                    version=row.version,
                )
            self._rows = rotated_rows
            self._master_key = new_key
            self._key_id = compute_key_id(new_key)
            return len(rotated_rows)

        count = 0
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("LOCK TABLE kotoyose.secrets IN ACCESS EXCLUSIVE MODE")
                rows = await conn.fetch(
                    "SELECT name, ciphertext, nonce, key_id, version FROM kotoyose.secrets ORDER BY name FOR UPDATE"
                )
                for row in rows:
                    name = row["name"]
                    version = int(row["version"])
                    plaintext = decrypt_secret_value(
                        old_key,
                        name=name,
                        version=version,
                        key_id=row["key_id"],
                        ciphertext=bytes(row["ciphertext"]),
                        nonce=bytes(row["nonce"]),
                    )
                    new_ciphertext, new_nonce, new_key_id = encrypt_secret_value(
                        new_key,
                        name=name,
                        version=version,
                        value=plaintext,
                    )
                    await conn.execute(
                        """
                        UPDATE kotoyose.secrets
                        SET ciphertext = $2, nonce = $3, key_id = $4, updated_at = now()
                        WHERE name = $1
                        """,
                        name,
                        new_ciphertext,
                        new_nonce,
                        new_key_id,
                    )
                    count += 1
        self._master_key = new_key
        self._key_id = compute_key_id(new_key)
        return count

    def get_sync_memory(self, name: str) -> str:
        """Decrypt from the in-memory store synchronously (for sync callers/tests)."""
        validate_secret_name(name)
        master_key = self._require_key()
        if self._pool is not None:
            raise SecretStoreError("Synchronous secret read requires an async pre-resolved secret map")
        row = self._rows.get(name)
        if row is None:
            raise SecretNotFoundError(f"Unknown secret '{name}'")
        return decrypt_secret_value(
            master_key,
            name=row.name,
            version=row.version,
            key_id=row.key_id,
            ciphertext=row.ciphertext,
            nonce=row.nonce,
        )

    def exists_sync_memory(self, name: str) -> bool:
        """Check whether a secret exists in the in-memory store synchronously."""
        validate_secret_name(name)
        if not self.is_configured or self._pool is not None:
            return False
        return name in self._rows


def set_secret_store(store: SecretStore | None) -> None:
    """Register the active process-wide SecretStore."""
    global _ACTIVE_STORE
    _ACTIVE_STORE = store


def get_secret_store() -> SecretStore:
    """Return the active SecretStore, or initialize one from environment variables."""
    global _ACTIVE_STORE
    if _ACTIVE_STORE is None:
        try:
            _ACTIVE_STORE = SecretStore.from_env()
        except SecretKeyError as exc:
            log.error("secret_store.disabled", error=str(exc))
            _ACTIVE_STORE = SecretStore(key_error=str(exc))
    return _ACTIVE_STORE


@contextmanager
def use_resolved_secrets(secrets: Mapping[str, str]) -> Iterator[None]:
    """Bind pre-resolved async secrets into the current execution context."""
    current = _PRELOADED_SECRETS.get()
    merged = {**(current or {}), **secrets}
    token = _PRELOADED_SECRETS.set(merged)
    try:
        yield
    finally:
        _PRELOADED_SECRETS.reset(token)


async def resolve_secret_async(name: str, store: SecretStore | None = None) -> str:
    """Resolve a secret by name using the provided or active SecretStore."""
    target = store or get_secret_store()
    return await target.get(name)


def resolve_secret_sync(name: str, store: SecretStore | None = None) -> str:
    """Synchronously resolve a secret from preloaded context or active in-memory store."""
    preloaded = _PRELOADED_SECRETS.get()
    if preloaded is not None and name in preloaded:
        return preloaded[name]
    target = store or get_secret_store()
    return target.get_sync_memory(name)
