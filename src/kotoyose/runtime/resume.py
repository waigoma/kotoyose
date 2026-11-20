"""Native API の再開保証。互換 Session の接続能力とは独立して申告する。"""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..execution_contract import ResumeMode


@dataclass(frozen=True)
class ResumeSupport:
    """native は session ref の保持と、その ref による開始の両方を保証する場合だけ含める。"""

    modes: tuple[ResumeMode, ...]
    reason: str


@runtime_checkable
class ResumeAdapter(Protocol):
    """execute が NativeAgentJob の再開 mode/ref を守る adapter の追加契約。"""

    def resume_support(self) -> ResumeSupport:
        """この構成で保証できる mode を返す。未検証の mode は含めない。"""
        ...

    def resume_session_ref(self, session_id: str | None) -> str | None:
        """取得済み ID の状態を保持して継続開始できる場合だけ、永続化可能な ref を返す。"""
        ...
