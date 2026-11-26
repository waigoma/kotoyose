"""host が持つ child 台帳と、一回限りの登録コード。

Postgres には置かない。証明書と同じディレクトリのファイルで、DB が無い起動でも残す。
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import threading
import time
from typing import TYPE_CHECKING

from .child_protocol import ChildRejected, enrollment_parts, pack_enrollment_code

if TYPE_CHECKING:
    from .child_workspace import WorkspacePolicyReport

ENROLLMENT_TTL_SECONDS = 600
ENROLLMENT_ATTEMPTS = 3
_NAME_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-")


def valid_child_name(name: str) -> bool:
    return bool(name) and len(name) <= 64 and name[0].isalnum() and all(char in _NAME_CHARS for char in name)


def child_on_name(value: str) -> str | None:
    """`child:<名前>` の名前。`local` や綴りの違う値は None。"""
    prefix = "child:"
    if not isinstance(value, str) or not value.startswith(prefix):
        return None
    name = value[len(prefix):]
    if not valid_child_name(name):
        return None
    return name


class ChildRegistry:
    def __init__(self, path: Path, secret: bytes):
        if len(secret) != 32:
            raise ValueError("host secret must be 32 bytes")
        self.path = path
        self._secret = secret
        self._lock = threading.Lock()
        self._data = {"children": [], "codes": []}
        if path.is_file():
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                self._data["children"] = list(loaded.get("children") or [])
                self._data["codes"] = list(loaded.get("codes") or [])

    def issue(self, *, address: str, fingerprint: str, ttl_seconds: int, attempts: int = ENROLLMENT_ATTEMPTS) -> tuple[str, str]:
        now_ms = int(time.time() * 1000)
        expires_ms = now_ms + ttl_seconds * 1000 if ttl_seconds > 0 else now_ms - 1000
        nonce = secrets.token_urlsafe(32)
        code_id = secrets.token_hex(16)
        payload = {
            "exp": expires_ms,
            "fp": fingerprint,
            "host": address,
            "id": code_id,
            "nonce": nonce,
            "v": 1,
        }
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        signature = hmac.new(self._secret, body, hashlib.sha256).digest()
        code = pack_enrollment_code(body, signature)
        expires_at = datetime.fromtimestamp(expires_ms / 1000, timezone.utc).isoformat()
        with self._lock:
            self._data["codes"].append({
                "attempts_left": attempts,
                "expires_at": expires_ms,
                "id": code_id,
                "secret_hash": hashlib.sha256(nonce.encode()).hexdigest(),
                "used": False,
            })
            self._save_locked()
        return code, expires_at

    def consume(self, code: str) -> dict:
        payload, body, signature = enrollment_parts(code)
        with self._lock:
            record = next((item for item in self._data["codes"] if item.get("id") == payload.get("id")), None)
            if record is None:
                raise ChildRejected("enrollment code rejected")
            if record.get("used"):
                raise ChildRejected("enrollment code already used")
            if int(record.get("attempts_left") or 0) <= 0:
                raise ChildRejected("enrollment code attempts exhausted")
            if int(record.get("expires_at") or 0) <= int(time.time() * 1000):
                record["attempts_left"] = int(record["attempts_left"]) - 1
                self._save_locked()
                raise ChildRejected("enrollment code expired")
            expected = hmac.new(self._secret, body, hashlib.sha256).digest()
            nonce = str(payload.get("nonce") or "")
            nonce_hash = hashlib.sha256(nonce.encode()).hexdigest()
            signature_ok = hmac.compare_digest(expected, signature)
            nonce_ok = hmac.compare_digest(nonce_hash, str(record.get("secret_hash") or ""))
            if not signature_ok or not nonce_ok:
                record["attempts_left"] = int(record["attempts_left"]) - 1
                self._save_locked()
                raise ChildRejected("enrollment code rejected")
            record["used"] = True
            record["attempts_left"] = int(record["attempts_left"]) - 1
            self._save_locked()
            return payload

    def certificate_pems(self) -> list[str]:
        with self._lock:
            return [item["cert_pem"] for item in self._data["children"] if isinstance(item.get("cert_pem"), str)]

    def enroll(self, *, name: str, fingerprint: str, cert_pem: str) -> dict:
        if not valid_child_name(name):
            raise ChildRejected("child name is invalid")
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            for item in self._data["children"]:
                if item.get("fingerprint") == fingerprint and item.get("name") != name and not item.get("revoked"):
                    raise ChildRejected("child certificate already enrolled")
            current = next((item for item in self._data["children"] if item.get("name") == name), None)
            if current is not None and not current.get("revoked"):
                raise ChildRejected("child name already enrolled")
            if current is not None:
                current.pop("workspace_policy", None)
                current.update({
                    "adapters": [],
                    "cert_pem": cert_pem,
                    "enrolled_at": now,
                    "fingerprint": fingerprint,
                    "last_heartbeat": None,
                    "mcp": [],
                    "protocol": None,
                    "revoked": False,
                    "version": None,
                })
                self._save_locked()
                return dict(current)
            record = {
                "adapters": [],
                "cert_pem": cert_pem,
                "enrolled_at": now,
                "fingerprint": fingerprint,
                "generation": 1,
                "last_heartbeat": None,
                "mcp": [],
                "name": name,
                "protocol": None,
                "revoked": False,
                "version": None,
            }
            self._data["children"].append(record)
            self._save_locked()
            return dict(record)

    def get(self, name: str) -> dict | None:
        with self._lock:
            found = next((item for item in self._data["children"] if item.get("name") == name), None)
            return dict(found) if found is not None else None

    def by_fingerprint(self, fingerprint: str) -> dict | None:
        with self._lock:
            found = next((item for item in self._data["children"] if item.get("fingerprint") == fingerprint), None)
            return dict(found) if found is not None else None

    def children(self) -> list[dict]:
        with self._lock:
            return [dict(item) for item in self._data["children"]]

    def revoke(self, name: str) -> dict:
        with self._lock:
            found = next((item for item in self._data["children"] if item.get("name") == name), None)
            if found is None:
                raise KeyError(name)
            if not found.get("revoked"):
                found["generation"] = int(found.get("generation") or 1) + 1
                found["revoked"] = True
                self._save_locked()
            return dict(found)

    def record_hello(
        self, name: str, *, protocol: int, package_version: str, adapters: list[str], mcp: list[dict],
        workspace_policy: WorkspacePolicyReport | None = None,
        build_id: str | None = None,
    ) -> None:
        """Replace the last hello, clearing the report if the child no longer supplies it."""
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            found = next((item for item in self._data["children"] if item.get("name") == name), None)
            if found is None:
                return
            found["protocol"] = protocol
            found["version"] = package_version
            if build_id is None:
                found.pop("build_id", None)
            else:
                found["build_id"] = build_id
            found["adapters"] = list(adapters)
            found["mcp"] = list(mcp)
            if workspace_policy is None:
                found.pop("workspace_policy", None)
            else:
                found["workspace_policy"] = workspace_policy
            found["last_heartbeat"] = now
            self._save_locked()

    def touch(self, name: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            found = next((item for item in self._data["children"] if item.get("name") == name), None)
            if found is None:
                return
            found["last_heartbeat"] = now
            self._save_locked()

    def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        payload = json.dumps(self._data, indent=2, sort_keys=True).encode()
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.path)
        os.chmod(self.path, 0o600)
