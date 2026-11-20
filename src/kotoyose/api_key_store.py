"""Storage for client credentials; plaintext exists only during creation."""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kotoyose.api_keys (
    id bigserial PRIMARY KEY,
    name text NOT NULL UNIQUE,
    caller text,
    key_hash text NOT NULL UNIQUE,
    prefix text NOT NULL,
    last4 text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    last_used_at timestamptz,
    revoked_at timestamptz
);
"""


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


class ApiKeyStore:
    def __init__(self, pool=None):
        self.pool = pool
        self._rows = {}
        self._next_id = 1

    async def initialize(self):
        if self.pool is not None:
            await self.pool.execute(_SCHEMA)

    @staticmethod
    def _public(row):
        return {k: v for k, v in dict(row).items() if k != "key_hash"}

    async def list(self):
        if self.pool is not None:
            rows = await self.pool.fetch("SELECT * FROM kotoyose.api_keys ORDER BY id")
        else:
            rows = self._rows.values()
        return [self._public(row) for row in rows]

    async def create(self, name, caller=None):
        key = "kys_" + secrets.token_urlsafe(32)
        digest = _hash(key)
        if self.pool is not None:
            row = await self.pool.fetchrow(
                """INSERT INTO kotoyose.api_keys(name,caller,key_hash,prefix,last4)
                VALUES($1,$2,$3,$4,$5) RETURNING *""",
                name, caller, digest, key[:8], key[-4:],
            )
        else:
            if name in self._rows:
                raise ValueError("name already exists")
            row = dict(id=self._next_id, name=name, caller=caller, key_hash=digest,
                       prefix=key[:8], last4=key[-4:], created_at=datetime.now(timezone.utc),
                       last_used_at=None, revoked_at=None)
            self._next_id += 1
            self._rows[name] = row
        return self._public(row), key

    async def authenticate(self, key):
        # 発行する key は必ず kys_ で始まる。それ以外は DB に問い合わせない。
        # 要求ごとに接続を 1 本使うと、走っている実行の接続の順番を変えてしまう。
        if not isinstance(key, str) or not key.startswith("kys_"):
            return None
        digest = _hash(key)
        if self.pool is not None:
            row = await self.pool.fetchrow(
                """UPDATE kotoyose.api_keys SET last_used_at=now()
                WHERE key_hash=$1 AND revoked_at IS NULL
                  AND (last_used_at IS NULL OR last_used_at < now() - interval '1 minute')
                RETURNING *""", digest,
            )
            if row is None:
                row = await self.pool.fetchrow(
                    "SELECT * FROM kotoyose.api_keys WHERE key_hash=$1 AND revoked_at IS NULL", digest)
        else:
            row = next((r for r in self._rows.values()
                        if secrets.compare_digest(r['key_hash'], digest) and r['revoked_at'] is None), None)
            if row is not None:
                now = datetime.now(timezone.utc)
                if row['last_used_at'] is None or row['last_used_at'] < now - timedelta(minutes=1):
                    row['last_used_at'] = now
        return self._public(row) if row is not None else None

    async def revoke(self, name):
        if self.pool is not None:
            row = await self.pool.fetchrow(
                "UPDATE kotoyose.api_keys SET revoked_at=COALESCE(revoked_at,now()) WHERE name=$1 RETURNING *", name)
        else:
            row = self._rows.get(name)
            if row is not None and row['revoked_at'] is None:
                row['revoked_at'] = datetime.now(timezone.utc)
        return self._public(row) if row is not None else None

    async def delete(self, name):
        if self.pool is not None:
            row = await self.pool.fetchrow(
                "DELETE FROM kotoyose.api_keys WHERE name=$1 AND revoked_at IS NOT NULL RETURNING id", name)
            return row is not None
        row = self._rows.get(name)
        if row is None or row['revoked_at'] is None:
            return False
        del self._rows[name]
        return True
