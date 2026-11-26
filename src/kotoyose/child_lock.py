"""State-directory exclusion shared with the Rust link app."""

from contextlib import contextmanager
import os
from pathlib import Path
import sys


@contextmanager
def child_lock(home: Path):
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(home / "instance.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError("child is already running") from exc
        yield
    finally:
        os.close(fd)
