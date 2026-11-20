"""provider私有のsession状態。公開progressや作業メモには内容を転送しない。"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from claude_agent_sdk import SessionStore


def session_scope(principal: str, workspace_id: str, provider: str) -> str:
    """認証済みCallerとworkspaceの所有から固定キーを導く。request headerは使わない。"""
    return hashlib.sha256(json.dumps([principal, workspace_id, provider]).encode()).hexdigest()


def retention_binding(config, provider: str) -> dict | None:
    """新規admissionへ固定する保存方式。未設定時は何も補完しない。"""
    if provider == 'claude' and getattr(config, 'native_claude_session_store', None) == 'postgres':
        return {'version': 1, 'provider': 'claude', 'kind': 'scoped_postgres', 'max_session_bytes': 32*1024*1024}
    if provider == 'codex' and (root := getattr(config, 'native_codex_session_root', None)) is not None:
        return {'version': 1, 'provider': 'codex', 'kind': 'sessions_directory',
                'root': str(root)}
    return None


def private_directory(path: Path) -> Path:
    """明示したcontroller領域だけ作る。symlink、他owner、公開permissionは拒否する。"""
    if not path.is_absolute():
        raise ValueError('provider_session_directory_unavailable')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return checked_directory(path)


def checked_directory(path: Path) -> Path:
    """既存領域を検査するだけで、不在時に再作成しない。"""
    info = path.stat()
    if (not path.is_absolute() or path.resolve(strict=True) != path
            or not path.is_dir() or info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise ValueError('provider_session_directory_unavailable')
    return path


class ScopedSessionStore:
    """既存の公式SessionStore実装へ、Task所有のnamespaceでopaque entryを渡す。"""

    def __init__(self, store, scope: str):
        if re.fullmatch('[0-9a-f]{64}', scope) is None:
            raise ValueError('invalid_session_scope')
        self.store, self.scope = store, 'native-' + scope
        self.failed = False

    def _key(self, key):
        return {**key, 'project_key': self.scope}

    async def append(self, key, entries):
        """entryを解釈、要約、公開せず、同じ所有領域へ保存する。"""
        try:
            await self.store.append(self._key(key), entries)
        except BaseException:
            self.failed = True
            raise

    async def load(self, key):
        """指定したsessionだけを公式SDKへ返す。他Taskの履歴を探索しない。"""
        return await self.store.load(self._key(key))

    async def retained_ref(self, session_id: str | None) -> str | None:
        """mirror が完全に保持した指定 ID だけを返す。失敗・不在・不完全 prefix は再開しない。"""
        if not session_id or self.failed:
            return None
        try:
            return session_id if await self.load({'session_id': session_id}) else None
        except Exception:
            return None

    @classmethod
    async def available(cls, store: SessionStore | None, scope: str, session_id: str) -> bool:
        """保存方式が有効で、指定した所有領域に完全な session が残る場合だけ真を返す。"""
        if store is None:
            return False
        try:
            return await cls(store, scope).retained_ref(session_id) is not None
        except ValueError:
            return False

    async def list_subkeys(self, key):
        """公式SDKが明示したsessionのsubagent状態だけを復元する。"""
        method = getattr(self.store, 'list_subkeys', None)
        return await method(self._key(key)) if method is not None else []


class CodexSessionDirectory:
    """認証を含むHOME全体ではなく、公式rolloutのsessionsだけを保持する。"""

    def __init__(self, root: Path):
        self.root = private_directory(root)
        info = self.root.stat()
        self.identity = (info.st_dev, info.st_ino)

    def _check_root(self) -> None:
        checked_directory(self.root)
        info = self.root.stat()
        if self.identity != (info.st_dev, info.st_ino):
            raise ValueError('provider_session_directory_changed')

    def directory(self, scope: str) -> Path:
        """所有キーに対応する一つのprivateディレクトリを返す。"""
        if re.fullmatch('[0-9a-f]{64}', scope) is None:
            raise ValueError('invalid_session_scope')
        self._check_root()
        return private_directory(self.root/scope)

    @staticmethod
    def reference(thread_id: str, rollout_path: str, *, home: str = '/home/kotoyose') -> str:
        """公式thread/startが返したlocatorをopaque refへ固定する。home は provider 側の HOME。"""
        prefix = f'{home}/.codex/sessions/'
        if not isinstance(thread_id, str) or not isinstance(rollout_path, str) or not rollout_path.startswith(prefix):
            raise ValueError('invalid_provider_session_locator')
        relative = rollout_path[len(prefix):]
        if (re.fullmatch('[0-9a-fA-F-]{36}', thread_id) is None or thread_id not in Path(relative).name
                or any(p in {'', '.', '..'} for p in relative.split('/')) or '\\' in relative):
            raise ValueError('invalid_provider_session_locator')
        encoded = base64.urlsafe_b64encode(relative.encode()).decode().rstrip('=')
        return f'codex:{thread_id}:{encoded}'

    def retained(self, scope: str, ref: str) -> bool:
        """保存済みrefの正確なfileだけ検査する。sessionディレクトリの列挙は行わない。"""
        try:
            kind, thread_id, encoded = ref.split(':', 2)
            relative = base64.urlsafe_b64decode(encoded + '='*(-len(encoded)%4)).decode()
            if kind != 'codex' or self.reference(thread_id, '/home/kotoyose/.codex/sessions/'+relative) != ref:
                return False
            if re.fullmatch('[0-9a-f]{64}', scope) is None:
                return False
            self._check_root()
            root = checked_directory(self.root/scope)
            path = root/relative
            info = path.stat()
            return (path.is_file() and path.resolve(strict=True) == path and info.st_uid == os.getuid()
                    and info.st_nlink == 1 and info.st_size > 0)
        except (OSError, ValueError, UnicodeError):
            return False
