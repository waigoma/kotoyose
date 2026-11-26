"""Exact-value redaction at the child turn's outbound boundary."""

from __future__ import annotations

import json
import os
import re
from typing import Any

from .provider_spec import BUILTIN_PROVIDERS

_REDACTED = "[REDACTED]"


class ChildOutputGuard:
    """child のプロセス環境にある API キーの値を、turn の出力を送る前に置換する。

    CLI のログイン資格情報 (Codex の auth.json、Claude の資格情報ファイル) は読まない。
    所有者の規則で、Kotoyose は公式のログインの token を読まない・運ばない (ADR-0001)。
    """

    def __init__(self, env_names: tuple[str, ...] | str, *, extra_values: tuple[str, ...] = ()) -> None:
        if isinstance(env_names, str):
            env_names = next((spec.child_turn.output_guard_env for spec in BUILTIN_PROVIDERS if spec.id == env_names), ())
        self._env_names = env_names
        self._extra_values = extra_values
        self._secrets: set[str] = set()
        self._pattern: re.Pattern[str] | None = None
        self._pending = ""
        self._refresh()

    def _refresh(self) -> None:
        values = {*self._extra_values, *(value for name in self._env_names if (value := os.environ.get(name)))}
        if not values.issubset(self._secrets):
            self._secrets.update(values)
            # SDK が tool payload を JSON 文字列に変換した場合にも照合する。
            self._secrets.update(json.dumps(value, ensure_ascii=False)[1:-1] for value in values)
            ordered = sorted(self._secrets, key=lambda value: (-len(value), value))
            self._pattern = re.compile("|".join(map(re.escape, ordered)))

    def delta(self, text: str) -> str:
        """安全な本文だけを返し、delta 境界で分割された認証値の候補を保留する。"""
        self._refresh()
        if self._pattern is None:
            return text
        combined = self._pending + text
        end = len(combined)
        for secret in self._secrets:
            for size in range(min(len(combined), len(secret) - 1), len(combined) - end, -1):
                if combined.endswith(secret[:size]):
                    end = len(combined) - size
                    break
        # 保留する接尾辞より手前で始まった一致は、値全体を置換する。
        released = []
        offset = 0
        for match in self._pattern.finditer(combined):
            if match.start() >= end:
                break
            released.extend((combined[offset:match.start()], _REDACTED))
            offset = match.end()
        self._pending = combined[max(end, offset):]
        released.append(combined[offset:end])
        return "".join(released)

    def flush(self) -> str:
        """正常終了時に、保留中の本文を置換して返す。"""
        self._refresh()
        pending, self._pending = self._pending, ""
        return self._redact(pending)

    def redact(self, value: dict) -> dict:
        """構造を保ったまま、入れ子に含まれる文字列も置換する。"""
        self._refresh()
        return self._redact(value)

    def _redact(self, value: Any) -> Any:
        if self._pattern is None:
            return value
        if isinstance(value, str):
            return self._pattern.sub(_REDACTED, value)
        if isinstance(value, dict):
            return {self._redact(key): self._redact(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._redact(item) for item in value]
        return value
