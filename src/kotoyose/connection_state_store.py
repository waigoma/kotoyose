"""Per-connection health, expiry, plan, quota, and CLI version store."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import math
from typing import Any

VALID_AUTH_STATES = frozenset({"ok", "expiring", "expired", "logged_out", "login_required", "unknown"})
VALID_QUOTA_SOURCES = frozenset({
    "official_rpc", "official_stream", "metered", "claude_cli_usage",
    "agy_cli_usage", "print_cli_usage", "grok_acp_billing",
})

_UNSET = object()

_WINDOW_SORT_ORDER = {
    "5h": 0,
    "five_hour": 0,
    "primary": 1,
    "7d": 2,
    "seven_day": 2,
    "seven_day_sonnet": 3,
    "seven_day_opus": 4,
    "seven_day_oauth_apps": 5,
    "seven_day_cowork": 6,
    "secondary": 7,
    "overage": 8,
}


def _iso_or_none(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    if isinstance(value, str):
        return value
    return str(value)


def _dt_or_none(value: datetime | str | int | float | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if isinstance(value, str) and value.strip():
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    return None


def _normalize_quota_entry(raw: dict[str, Any], *, default_source: str | None = None) -> dict[str, Any] | None:
    window = raw.get("window") or raw.get("label")
    if not isinstance(window, str) or not window.strip():
        return None
    used_raw = raw.get("used_percent")
    if isinstance(used_raw, bool) or not isinstance(used_raw, (int, float)) or not math.isfinite(used_raw):
        return None
    used_percent = round(max(0.0, min(100.0, float(used_raw))), 2)
    source = raw.get("source") or default_source or "official_rpc"
    if source not in VALID_QUOTA_SOURCES:
        source = default_source or "official_rpc"
    resets_raw = raw.get("resets_at")
    resets_dt = _dt_or_none(resets_raw) if not isinstance(resets_raw, str) else _dt_or_none(resets_raw)
    entry: dict[str, Any] = {
        "window": window.strip(),
        "used_percent": used_percent,
        "resets_at": _iso_or_none(resets_dt) if resets_dt is not None else None,
        "source": source,
    }
    label = raw.get("label")
    if isinstance(label, str) and label.strip():
        entry["label"] = label.strip()
    resets_text = raw.get("resets_text")
    if isinstance(resets_text, str) and resets_text.strip():
        entry["resets_text"] = resets_text.strip()
    return entry


def normalize_quota_list(
    entries: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None,
    *,
    default_source: str | None = None,
) -> list[dict[str, Any]]:
    """Normalize quota entries to deterministic dicts matching `{window, used_percent, resets_at, source}`."""
    if not entries:
        return []
    indexed: list[tuple[int, int, dict[str, Any]]] = []
    for idx, entry in enumerate(entries):
        if isinstance(entry, dict):
            normalized = _normalize_quota_entry(entry, default_source=default_source)
            if normalized is not None:
                indexed.append((_WINDOW_SORT_ORDER.get(normalized["window"], 99), idx, normalized))
    indexed.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in indexed]


def default_connection_state(connection: str) -> dict[str, Any]:
    """Return the baseline connection state shape when no probe or runtime event exists yet."""
    return {
        "connection": connection,
        "auth": "unknown",
        "expires_at": None,
        "plan": None,
        "quota": [],
        "quota_source": None,
        "cli_version": None,
        "checked_at": None,
        "last_error": None,
    }


def _row_to_dict(row: Any) -> dict[str, Any]:
    raw_quota = row["quota"]
    if isinstance(raw_quota, str):
        try:
            raw_quota = json.loads(raw_quota)
        except ValueError:
            raw_quota = []
    quota = normalize_quota_list(
        raw_quota if isinstance(raw_quota, list) else [],
        default_source=row["quota_source"],
    )
    return {
        "connection": row["connection"],
        "auth": row["auth"],
        "expires_at": _iso_or_none(row["expires_at"]),
        "plan": row["plan"],
        "quota": quota,
        "quota_source": row["quota_source"],
        "cli_version": row["cli_version"],
        "checked_at": _iso_or_none(row["checked_at"]),
        "last_error": row["last_error"],
    }


def _sql_list(values: frozenset[str]) -> str:
    return ", ".join("'" + value.replace("'", "''") + "'" for value in sorted(values))


def _constraint_ddl() -> str:
    """CHECK 制約を VALID_AUTH_STATES / VALID_QUOTA_SOURCES から作り直す DDL。"""
    return f"""
    ALTER TABLE kotoyose.connection_state DROP CONSTRAINT IF EXISTS connection_state_auth_check;
    ALTER TABLE kotoyose.connection_state ADD CONSTRAINT connection_state_auth_check
        CHECK (auth IN ({_sql_list(VALID_AUTH_STATES)}));
    ALTER TABLE kotoyose.connection_state DROP CONSTRAINT IF EXISTS connection_state_quota_source_check;
    ALTER TABLE kotoyose.connection_state ADD CONSTRAINT connection_state_quota_source_check
        CHECK (quota_source IS NULL OR quota_source IN ({_sql_list(VALID_QUOTA_SOURCES)}));
    """


class ConnectionStateStore:
    """Async store for `kotoyose.connection_state` with in-memory fallback for tests/dev."""

    _SCHEMA = """
    CREATE SCHEMA IF NOT EXISTS kotoyose;
    CREATE TABLE IF NOT EXISTS kotoyose.connection_state (
        connection TEXT PRIMARY KEY,
        auth TEXT NOT NULL CHECK (auth IN ('ok', 'expiring', 'expired', 'logged_out', 'login_required', 'unknown')),
        expires_at TIMESTAMPTZ,
        plan TEXT,
        quota JSONB NOT NULL DEFAULT '[]'::jsonb,
        quota_source TEXT CHECK (quota_source IS NULL OR quota_source IN ('official_rpc', 'official_stream', 'metered', 'claude_cli_usage')),
        cli_version TEXT,
        checked_at TIMESTAMPTZ,
        last_error TEXT
    );
    """

    def __init__(self, pool: Any = None) -> None:
        self._pool = pool
        self._rows: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Create `kotoyose.connection_state` if backed by Postgres.

        `CREATE TABLE IF NOT EXISTS` は既存の CHECK を書き換えないため、許す値を
        Python 側の集合から毎回作り直す。値を足したときに本番の表が追従しないと
        probe が CheckViolationError で静かに失敗する (S5c で踏んだ)。
        """
        if self._pool is not None:
            await self._pool.execute(self._SCHEMA)
            await self._pool.execute(_constraint_ddl())

    async def get(self, connection: str) -> dict[str, Any] | None:
        """Return stored state dict for `connection`, or None if unrecorded."""
        if self._pool is None:
            row = self._rows.get(connection)
            if row is None:
                return None
            return {**row, "quota": [dict(item) for item in row["quota"]]}
        row = await self._pool.fetchrow(
            """
            SELECT connection, auth, expires_at, plan, quota, quota_source, cli_version, checked_at, last_error
            FROM kotoyose.connection_state
            WHERE connection = $1
            """,
            connection,
        )
        return _row_to_dict(row) if row is not None else None

    async def list(self) -> dict[str, dict[str, Any]]:
        """Return all stored states keyed by connection ID."""
        if self._pool is None:
            return {
                conn_id: {**row, "quota": [dict(item) for item in row["quota"]]}
                for conn_id, row in sorted(self._rows.items())
            }
        rows = await self._pool.fetch(
            """
            SELECT connection, auth, expires_at, plan, quota, quota_source, cli_version, checked_at, last_error
            FROM kotoyose.connection_state
            ORDER BY connection ASC
            """
        )
        return {row["connection"]: _row_to_dict(row) for row in rows}

    async def upsert(
        self,
        connection: str,
        *,
        auth: str | object = _UNSET,
        expires_at: datetime | str | None | object = _UNSET,
        plan: str | None | object = _UNSET,
        quota: list[dict[str, Any]] | object = _UNSET,
        quota_source: str | None | object = _UNSET,
        cli_version: str | None | object = _UNSET,
        checked_at: datetime | str | None | object = _UNSET,
        last_error: str | None | object = _UNSET,
    ) -> dict[str, Any]:
        """Insert or partially update a connection's state without erasing unspecified fields."""
        if auth is not _UNSET and auth not in VALID_AUTH_STATES:
            raise ValueError(f"Invalid connection auth state: {auth!r}")
        if quota_source is not _UNSET and quota_source is not None and quota_source not in VALID_QUOTA_SOURCES:
            raise ValueError(f"Invalid quota_source: {quota_source!r}")

        async with self._lock:
            existing = await self.get(connection)
            merged = dict(existing) if existing is not None else default_connection_state(connection)
            if auth is not _UNSET:
                merged["auth"] = auth
            if expires_at is not _UNSET:
                merged["expires_at"] = _iso_or_none(_dt_or_none(expires_at))
            if plan is not _UNSET:
                merged["plan"] = plan
            if quota_source is not _UNSET:
                merged["quota_source"] = quota_source
            if quota is not _UNSET:
                default_src = merged["quota_source"] if isinstance(merged["quota_source"], str) else None
                merged["quota"] = normalize_quota_list(quota if isinstance(quota, list) else [], default_source=default_src)
            if cli_version is not _UNSET:
                merged["cli_version"] = cli_version
            if checked_at is not _UNSET:
                merged["checked_at"] = _iso_or_none(_dt_or_none(checked_at))
            if last_error is not _UNSET:
                merged["last_error"] = last_error

            return await self._write_merged(connection, merged)

    async def _write_merged(self, connection: str, merged: dict[str, Any]) -> dict[str, Any]:
        if self._pool is None:
            stored = {**merged, "quota": [dict(item) for item in merged["quota"]]}
            self._rows[connection] = stored
            return {**stored, "quota": [dict(item) for item in stored["quota"]]}

        expires_dt = _dt_or_none(merged["expires_at"])
        checked_dt = _dt_or_none(merged["checked_at"])
        row = await self._pool.fetchrow(
            """
            INSERT INTO kotoyose.connection_state (
                connection, auth, expires_at, plan, quota, quota_source, cli_version, checked_at, last_error
            )
            VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8, $9)
            ON CONFLICT (connection) DO UPDATE SET
                auth = EXCLUDED.auth,
                expires_at = EXCLUDED.expires_at,
                plan = EXCLUDED.plan,
                quota = EXCLUDED.quota,
                quota_source = EXCLUDED.quota_source,
                cli_version = EXCLUDED.cli_version,
                checked_at = EXCLUDED.checked_at,
                last_error = EXCLUDED.last_error
            RETURNING connection, auth, expires_at, plan, quota, quota_source, cli_version, checked_at, last_error
            """,
            connection,
            merged["auth"],
            expires_dt,
            merged["plan"],
            json.dumps(merged["quota"]),
            merged["quota_source"],
            merged["cli_version"],
            checked_dt,
            merged["last_error"],
        )
        return _row_to_dict(row)

    async def merge_quota_window(
        self,
        connection: str,
        window_entry: dict[str, Any],
        *,
        quota_source: str,
        checked_at: datetime | None = None,
        clear_auth_failure: bool = False,
    ) -> dict[str, Any]:
        """Merge a single quota window (e.g. from Claude RateLimitEvent) into stored quota."""
        if quota_source not in VALID_QUOTA_SOURCES:
            raise ValueError(f"Invalid quota_source: {quota_source!r}")
        normalized = _normalize_quota_entry(window_entry, default_source=quota_source)
        if normalized is None:
            existing = await self.get(connection)
            return existing if existing is not None else default_connection_state(connection)

        now = checked_at or datetime.now(timezone.utc)
        async with self._lock:
            existing = await self.get(connection)
            merged = dict(existing) if existing is not None else default_connection_state(connection)
            by_window = {
                item["window"]: dict(item)
                for item in (merged.get("quota") or [])
                if isinstance(item, dict) and isinstance(item.get("window"), str)
            }
            by_window[normalized["window"]] = normalized
            merged["quota"] = normalize_quota_list(list(by_window.values()), default_source=quota_source)
            merged["quota_source"] = quota_source
            merged["checked_at"] = _iso_or_none(now)
            if clear_auth_failure and merged["auth"] in ("unknown", "expired", "logged_out"):
                merged["auth"] = "ok"
                merged["last_error"] = None
            return await self._write_merged(connection, merged)

    async def mark_auth_expired(
        self,
        connection: str,
        *,
        error: str | None = None,
        checked_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Mark a connection's auth state as `expired` due to a runtime/admission auth failure."""
        now = checked_at or datetime.now(timezone.utc)
        return await self.upsert(
            connection,
            auth="expired",
            checked_at=now,
            last_error=(error or "authentication_failed")[:240],
        )

    async def delete(self, connection: str) -> None:
        """Delete stored state for a removed connection."""
        async with self._lock:
            self._rows.pop(connection, None)
            if self._pool is not None:
                await self._pool.execute(
                    "DELETE FROM kotoyose.connection_state WHERE connection = $1",
                    connection,
                )


_ACTIVE_STORE: ConnectionStateStore | None = None


def set_connection_state_store(store: ConnectionStateStore | None) -> None:
    """Register the active process-wide ConnectionStateStore."""
    global _ACTIVE_STORE
    _ACTIVE_STORE = store


def get_connection_state_store() -> ConnectionStateStore:
    """Return the active ConnectionStateStore, falling back to an in-memory store."""
    global _ACTIVE_STORE
    if _ACTIVE_STORE is None:
        _ACTIVE_STORE = ConnectionStateStore(None)
    return _ACTIVE_STORE
