"""組み込み driver、host runtime、pool 戦略の登録表。

agent_spec は jail 内の Python からも読むため、依存は標準ライブラリに限る。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from threading import RLock
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .agent_spec import DriverManifest
    from .provider_spec import ProviderSpec
    from .runtime.base import RuntimeProviderSpec


class Registry:
    def __init__(self) -> None:
        self._lock = RLock()
        self._drivers: dict[str, DriverManifest] = {}
        self._providers: dict[str, ProviderSpec] = {}
        self._driver_providers: dict[str, ProviderSpec] = {}
        self.runtime_providers: dict[str, RuntimeProviderSpec] = {}
        self._child_builders: dict[str, Callable[..., Any]] = {}
        # 試験が runtime を偽物に差し替える口。組み込みの登録より優先する。
        self._child_builder_overrides: dict[str, Callable[..., Any]] = {}
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

    def register_builtin_provider(self, spec: ProviderSpec) -> None:
        """宣言の driver が組み込み表にあり、同じ SDK 製品を指すことを検証する。"""
        with self._lock:
            if spec.id in self._providers:
                raise ValueError(f"duplicate provider {spec.id!r}")
            if not spec.drivers or len(spec.drivers) != len(set(spec.drivers)) or any(
                name in self._driver_providers
                or (driver := self._drivers.get(name)) is None
                or driver.provider != spec.id
                or driver.unmapped_backend == "print_cli"
                for name in spec.drivers
            ):
                raise ValueError(f"invalid drivers for provider {spec.id!r}")
            self._providers[spec.id] = spec
            self._driver_providers.update(dict.fromkeys(spec.drivers, spec))

    def provider(self, name: str) -> ProviderSpec | None:
        """組み込み SDK 製品の正規 ID を引く。"""
        return self._providers.get(name)

    def provider_for_driver(self, driver: str) -> ProviderSpec | None:
        """SDK driver に対応する子機宣言があれば返す。"""
        return self._driver_providers.get(driver)

    def register_builtin_child_builder(self, provider: str, builder: Callable[..., Any]) -> None:
        """遅延起動用の builder を登録する。引数は alias、preset、cwd、宣言の起動オプション。"""
        with self._lock:
            if provider not in self._providers:
                raise ValueError(f"unknown child runtime provider {provider!r}")
            if provider in self._child_builders:
                raise ValueError(f"child runtime provider {provider!r} is already registered")
            self._child_builders[provider] = builder

    def child_builder(self, provider: str) -> Callable[..., Any] | None:
        """子機用の遅延 builder があれば返す。"""
        return self._child_builder_overrides.get(provider) or self._child_builders.get(provider)

    def override_child_builder(self, provider: str, builder: Callable[..., Any] | None) -> Callable[..., Any] | None:
        """試験用: child の builder を差し替える (None で戻す)。前の差し替えを返す。"""
        with self._lock:
            previous = self._child_builder_overrides.pop(provider, None)
            if builder is not None:
                self._child_builder_overrides[provider] = builder
            return previous

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
