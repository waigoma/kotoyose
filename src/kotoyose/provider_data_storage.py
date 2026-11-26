"""Atomic private state files for data provider catalogs and approvals."""

from contextlib import contextmanager
from pathlib import Path
import os
import tempfile


def _windows() -> bool:
    return os.name == "nt"


def atomic_write(path: Path, raw: bytes, *, child: bool = False) -> None:
    if child and _windows():
        from .provider_data_windows import storage_directory, storage_file

        storage_directory(path.parent)
        try:
            storage_file(path)
        except FileNotFoundError:
            pass
    elif child and path.is_symlink():
        raise ValueError("provider state file must not be a symlink")
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".provider-", delete=False) as out:
        temporary = Path(out.name)
        try:
            with out:
                out.write(raw)
                out.flush()
                os.fsync(out.fileno())
            if child and _windows():
                storage_file(temporary)
            temporary.replace(path)
            if os.name == "posix":
                os.chmod(path, 0o600)
                fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        finally:
            temporary.unlink(missing_ok=True)


def restrict_owner_directory(path: Path) -> None:
    """Require a user-owned child home and restrict its access before writing approvals."""
    import stat

    path = path.absolute()
    if _windows():
        from .provider_data_windows import storage_directory

        storage_directory(path)
        return
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("unsafe provider storage directory")
    if stat.S_IMODE(info.st_mode) != 0o700:
        os.chmod(path, 0o700)


def private_directory(path: Path) -> Path:
    """Create a directory beneath an owner-controlled path, then check its permissions."""
    import stat

    path = path.absolute()
    if _windows():
        from .provider_data_windows import storage_directory

        return storage_directory(path, create=True)
    # The system root can have a mapped UID in a user namespace.
    system_owner = Path(path.anchor).stat().st_uid
    for parent in reversed((path, *path.parents)):
        try:
            info = parent.lstat()
        except FileNotFoundError:
            parent.mkdir(mode=0o700, exist_ok=True)
            info = parent.lstat()
        if (
            not stat.S_ISDIR(info.st_mode) or info.st_uid not in {system_owner, os.getuid()}
            or (info.st_mode & 0o022 and not (
                info.st_uid == system_owner and info.st_mode & stat.S_ISVTX and parent != path
            ))
        ):
            raise ValueError("unsafe provider storage directory")
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("provider storage directory must have mode 0700")
    return path


def private_read(path: Path, limit: int) -> bytes:
    if _windows():
        from .provider_data_windows import storage_file

        storage_file(path)
        with path.open("rb") as source:
            data = source.read(limit + 1)
    else:
        data = _posix_read(path, limit)
    if len(data) > limit:
        raise ValueError("provider storage file exceeds its size limit")
    return data


def _posix_read(path: Path, limit: int) -> bytes:
    import stat

    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError("provider storage file must be owner-only")
        return source.read(limit + 1)


@contextmanager
def locked(directory: Path, *, blocking: bool = True):
    path = private_directory(directory)
    if _windows():
        with _windows_lock(path / ".lock", blocking=blocking):
            yield path
        return
    import fcntl
    fd = os.open(path / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        import stat

        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError("unsafe provider lock")
        fcntl.flock(fd, fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield path
    finally:
        os.close(fd)


@contextmanager
def _windows_lock(path: Path, *, blocking: bool):
    import msvcrt

    from .provider_data_windows import storage_file

    try:
        storage_file(path)
    except FileNotFoundError:
        try:
            path.touch(exist_ok=False)
        except FileExistsError:
            pass
        storage_file(path)
    with path.open("r+b") as lock:
        # msvcrt permits locking a range past the end of an empty file.
        msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
        try:
            yield
        finally:
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
