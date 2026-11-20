"""Fail-fast, process-local coordination for print CLI login homes."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


class PrintCliHomeBusyError(RuntimeError):
    """The requested CLI operation conflicts with active work in the login home."""


@dataclass
class _Activity:
    shared: int = 0
    login: bool = False


# All acquisitions and releases are synchronous on the server event loop.
# Key by directory so aliases and different profiles sharing a home also conflict.
_homes: dict[Path, _Activity] = {}


def acquire_login_home(home: Path) -> Callable[[], None]:
    """Reserve a home exclusively until the returned release callback runs."""
    key = home.resolve()
    if (activity := _homes.get(key)) is not None:
        if activity.login:
            raise PrintCliHomeBusyError("CLI login in progress for this login home")
        raise PrintCliHomeBusyError("CLI runs or probes are active for this login home")
    _homes[key] = _Activity(login=True)
    return lambda: _homes.pop(key)


@contextmanager
def use_login_home(home: Path | str) -> Iterator[None]:
    """Allow concurrent runs and probes, raising immediately during a login."""
    key = Path(home).resolve()
    activity = _homes.setdefault(key, _Activity())
    if activity.login:
        raise PrintCliHomeBusyError("CLI login in progress for this login home")
    activity.shared += 1
    try:
        yield
    finally:
        activity.shared -= 1
        if not activity.shared:
            del _homes[key]
