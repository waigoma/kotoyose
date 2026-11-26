"""runtime provider 抽象。

OpenAI 互換 API の外面を保ちながら、Claude / Codex / Cursor / Antigravity などの実行系を
差し替えではなく並存させるための内部境界を置く。
"""

from importlib import import_module

from .base import RuntimeEvent, RuntimeResult, RuntimeSession

_PROVIDERS = {
    "AntigravitySdkRuntimeSession": ".antigravity_sdk",
    "ClaudeRuntimeSession": ".claude",
    "CodexRuntimeSession": ".codex",
    "CursorRuntimeSession": ".cursor",
}


def __getattr__(name: str):
    module = _PROVIDERS.get(name)
    if module is None:
        raise AttributeError(name)
    return getattr(import_module(module, __name__), name)

__all__ = [
    "RuntimeEvent",
    "RuntimeResult",
    "RuntimeSession",
    *_PROVIDERS,
]
