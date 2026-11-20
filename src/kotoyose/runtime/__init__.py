"""runtime provider 抽象。

OpenAI 互換 API の外面を保ちながら、Claude / Codex / Cursor / Antigravity などの実行系を
差し替えではなく並存させるための内部境界を置く。
"""

from .antigravity import AntigravityRuntimeSession
from .antigravity_sdk import AntigravitySdkRuntimeSession
from .base import RuntimeEvent, RuntimeResult, RuntimeSession
from .claude import ClaudeRuntimeSession
from .codex import CodexRuntimeSession
from .cursor import CursorRuntimeSession

__all__ = [
    "RuntimeEvent",
    "RuntimeResult",
    "RuntimeSession",
    "AntigravityRuntimeSession",
    "AntigravitySdkRuntimeSession",
    "ClaudeRuntimeSession",
    "CodexRuntimeSession",
    "CursorRuntimeSession",
]
