"""組み込み driver、host runtime、pool 戦略の登録表。

agent_spec は jail 内の Python からも読むため、依存は標準ライブラリに限る。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from threading import RLock
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .agent_spec import DriverManifest
    from .runtime.base import RuntimeProviderSpec


class Registry:
    def __init__(self) -> None:
        self._lock = RLock()
        self._drivers: dict[str, DriverManifest] = {}
        self.runtime_providers: dict[str, RuntimeProviderSpec] = {}
        self.routers: dict[str, Callable[..., Any]] = {}

    def register_builtin_drivers(self, rows: Iterable[DriverManifest]) -> None:
        with self._lock:
            for row in rows:
                if row.driver in self._drivers:
                    raise ValueError(f"duplicate driver {row.driver!r}")
                self._drivers[row.driver] = row

    def register_builtin_runtime(self, provider: str, spec: RuntimeProviderSpec) -> None:
        with self._lock:
            if provider in self.runtime_providers:
                raise ValueError(f"runtime provider {provider!r} is already registered")
            self.runtime_providers[provider] = spec

    def drivers(self) -> tuple[DriverManifest, ...]:
        return tuple(self._drivers.values())

    def driver(self, name: str) -> DriverManifest:
        return self._drivers[name]

    def has_driver(self, name: str) -> bool:
        return name in self._drivers

    def driver_for_provider(self, provider: str) -> DriverManifest:
        for row in self.drivers():
            if row.provider == provider:
                return row
        raise KeyError(provider)


registry = Registry()
