"""実行 backend に依存しない command 入力と停止確認付き結果。"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

ExecutionStatus = Literal["succeeded", "failed", "cancelled", "timeout", "worker_lost"]
StopConfirmation = Literal["confirmed", "unconfirmed"]
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True, slots=True)
class CommandJob:
    """Caller が解決した workspace と、展開しない argv を実行 backend へ渡す。"""

    attempt_id: str
    generation: int
    workspace: Path
    argv: tuple[str, ...]
    cwd: str = "."
    env: dict[str, str] = field(default_factory=dict)
    timeout_seconds: float = 600.0
    grace_seconds: float = 5.0
    output_limit_bytes: int = 1024 * 1024
    memory_bytes: int = 512 * 1024 * 1024
    pids_limit: int = 128
    cpus: float = 1.0

    def __post_init__(self) -> None:
        """実行前に、CLI へそのまま渡せない入力を拒否する。"""
        _validate_identifier(self.attempt_id, "attempt_id")
        if isinstance(self.generation, bool) or not isinstance(self.generation, int):
            raise ValueError("generation must be an integer")
        if self.generation < 0:
            raise ValueError("generation must be non-negative")
        if not isinstance(self.workspace, Path):
            raise ValueError("workspace must be a pathlib.Path")
        if not isinstance(self.argv, tuple) or not self.argv:
            raise ValueError("argv must be a non-empty tuple")
        for index, argument in enumerate(self.argv):
            _validate_text(argument, f"argv[{index}]", allow_empty=index > 0)
        _validate_cwd(self.cwd)
        if not isinstance(self.env, dict):
            raise ValueError("env must be a dict")
        for key, value in self.env.items():
            if not isinstance(key, str) or _ENV_KEY_RE.fullmatch(key) is None:
                raise ValueError(f"invalid environment key: {key!r}")
            if key in {"HOME", "PATH"}:
                raise ValueError(f"environment key {key!r} is reserved")
            _validate_text(value, f"env[{key!r}]", allow_empty=True)
        _validate_positive_finite(self.timeout_seconds, "timeout_seconds")
        _validate_nonnegative_finite(self.grace_seconds, "grace_seconds")
        _validate_positive_integer(self.output_limit_bytes, "output_limit_bytes")
        _validate_positive_integer(self.memory_bytes, "memory_bytes")
        _validate_positive_integer(self.pids_limit, "pids_limit")
        _validate_positive_finite(self.cpus, "cpus")


@dataclass(frozen=True, slots=True)
class WorkerResult:
    """command 実行結果と停止確認の状態。"""

    status: ExecutionStatus
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    stop_confirmation: StopConfirmation
    reason: str


def _validate_text(value: object, field_name: str, *, allow_empty: bool = False) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    if "\x00" in value:
        raise ValueError(f"{field_name} must not contain NUL")
    if not value and not allow_empty:
        raise ValueError(f"{field_name} must not be empty")


def _validate_identifier(value: str, field_name: str) -> None:
    _validate_text(value, field_name)
    if _ID_RE.fullmatch(value) is None:
        raise ValueError(f"invalid {field_name}")


def _validate_cwd(cwd: str) -> None:
    _validate_text(cwd, "cwd")
    path = Path(cwd)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("cwd must be a relative path inside workspace")


def _validate_positive_integer(value: object, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")


def _validate_positive_finite(value: object, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a positive finite number")
    if not math.isfinite(float(value)) or float(value) <= 0:
        raise ValueError(f"{field_name} must be a positive finite number")


def _validate_nonnegative_finite(value: object, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a non-negative finite number")
    if not math.isfinite(float(value)) or float(value) < 0:
        raise ValueError(f"{field_name} must be a non-negative finite number")
