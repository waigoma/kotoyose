"""既存 CLI の ChatGPT 認証を読み取り、broker の外へ credential を出さない。"""

import base64
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import time

from .codex_request_policy import parse_json


AUTH_NAME = "CODEX_CHATGPT_AUTH"
CAPABILITY_ENV = "KOTOYOSE_CODEX_BROKER_CAPABILITY"
_MAX_AUTH_BYTES = 64 * 1024


@dataclass(frozen=True)
class SubscriptionCredential:
    """TLS upstream にだけ送る認証。repr と永続 snapshot は値を含めない。"""

    access_token: str = field(repr=False)
    account_id: str = field(repr=False)

    @property
    def identity(self) -> str:
        """更新可能な token 自体でなく account の digest を admission に固定する。"""
        return hashlib.sha256(self.account_id.encode()).hexdigest()


def _open_auth_file(path: Path) -> int:
    # resolve と open の間に親ディレクトリを symlink へ差し替えられても辿らない。
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for component in path.parts[1:-1]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                            dir_fd=directory)
            os.close(directory)
            directory = child
        return os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                       dir_fd=directory)
    finally:
        os.close(directory)


class _AuthJsonParseError(ValueError):
    """Raised when auth.json or its JWT claims fail JSON parsing (e.g. concurrent rewrite)."""


def _read_chatgpt_auth(path: Path) -> tuple[str, str, float]:
    """Safely open and validate ChatGPT auth.json, returning (token, account_id, exp_epoch)."""
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise ValueError()
    descriptor = _open_auth_file(path)
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or metadata.st_mode & 0o077 or metadata.st_size > _MAX_AUTH_BYTES):
            raise ValueError()
        try:
            value = parse_json(stream.read(_MAX_AUTH_BYTES + 1))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise _AuthJsonParseError() from exc
    if (not isinstance(value, dict) or value.get("auth_mode") != "chatgpt"
            or value.get("OPENAI_API_KEY") is not None):
        raise ValueError()
    tokens = value["tokens"]
    token, account = tokens["access_token"], tokens["account_id"]
    if (not isinstance(token, str) or not token or len(token) > 16384
            or not isinstance(account, str) or not account or len(account) > 256
            or any(ord(c) < 33 or ord(c) > 126 for c in token + account)):
        raise ValueError()
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError()
    try:
        claims = parse_json(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise _AuthJsonParseError() from exc
    expiration = claims["exp"]
    if not isinstance(expiration, (float, int)) or isinstance(expiration, bool) or not math.isfinite(expiration):
        raise ValueError()
    return token, account, float(expiration)


def read_subscription_expiry(path: Path) -> datetime | None:
    """Read only the UTC `exp` timestamp claim from a Codex auth.json file without exposing tokens."""
    try:
        _, _, expiration = _read_chatgpt_auth(path)
        return datetime.fromtimestamp(expiration, tz=timezone.utc)
    except (OSError, ValueError, TypeError, KeyError):
        return None


def read_subscription(path: Path) -> SubscriptionCredential:
    """canonical な所有者専用 auth.json を bounded read し、方式と期限を照合する。

    refresh/id token は返さず、保存・refresh・再ログインは既存 host CLI に委ねる。
    JWT の claim は期限確認にだけ使用し、利用資格は推測しない。真偽は upstream
    が検証する。失効・不明形式・API key は authentication_failed で停止する。
    """
    for attempt in range(2):
        try:
            token, account, expiration = _read_chatgpt_auth(path)
            if expiration <= time.time() + 30:
                raise ValueError()
            return SubscriptionCredential(token, account)
        except _AuthJsonParseError:
            if attempt == 0:
                time.sleep(0.1)
                continue
            raise ValueError("authentication_failed") from None
        except (OSError, ValueError, TypeError, KeyError):
            raise ValueError("authentication_failed") from None
    raise ValueError("authentication_failed")
