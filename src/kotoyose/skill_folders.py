"""スキルのコピーと子機転送で共有するファイル名・容量・保存の境界。"""

from __future__ import annotations

import base64
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
import os
import re
from pathlib import Path
import stat
import unicodedata

from .child_sdk_skills import MAX_SKILLS, validate_legacy_skill_name, validate_skill_names

MAX_FOLDER_FILES = 64
MAX_FOLDER_BYTES = 1024 * 1024
MAX_TURN_FOLDER_BYTES = 1024 * 1024
MAX_PATH_BYTES = 100  # Windows MAX_PATH のうち、一時ルートとスキル名に余裕を残す。
_SKIPPED_NAMES = frozenset({"node_modules", "__pycache__"})
_EXTRA_WINDOWS_DEVICES = re.compile(r"CONIN\$|CONOUT\$|(?:COM|LPT)[¹²³]", re.IGNORECASE)
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)


@dataclass(frozen=True)
class SkillFolder:
    """検証済みの名前と、フォルダー相対の通常ファイル。"""

    name: str
    files: dict[str, bytes] = field(repr=False)

    def skill_text(self) -> str:
        """SKILL.md を UTF-8 として取得する。添付ファイルはバイト列のまま扱う。"""
        try:
            text = self.files["SKILL.md"].decode("utf-8")
            if "\x00" not in text:
                return text
        except (KeyError, UnicodeDecodeError):
            pass
        raise ValueError(f"skill '{self.name}' requires a UTF-8 SKILL.md without NUL")


def read_skill_folder(plugin: Path, name: str, remaining: int, *, missing_ok: bool = False) -> SkillFolder | None:
    """指定スキルの非隠しファイルを読む。明示 fallback 用に限り、フォルダーの欠落を許容する。"""
    validate_skill_names([name])
    files: dict[str, bytes] = {}
    limit = min(MAX_FOLDER_BYTES, remaining)

    def collect(directory: Path, fd: int | None, prefix: str, budget: int) -> int:
        with os.scandir(directory if fd is None else fd) as entries:
            for entry in entries:
                if _skip_entry(entry.name):
                    continue
                relative = prefix + entry.name
                _path_parts(relative)
                target = directory / entry.name
                # Windows の DirEntry.stat() は st_nlink、st_dev、st_ino を返さない。
                info = _lstat(target, fd)
                _reject_link(info)
                if stat.S_ISDIR(info.st_mode):
                    with _directory(target, fd) as subdir:
                        budget = collect(target, subdir, relative + "/", budget)
                    continue
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError(f"skill '{name}' contains a non-regular file")
                _reject_hardlink(info)
                if len(files) >= MAX_FOLDER_FILES:
                    raise ValueError(f"skill '{name}' exceeds {MAX_FOLDER_FILES} files")
                with os.fdopen(_open(target, _READ_FLAGS, fd), "rb") as stream:
                    actual = os.fstat(stream.fileno())
                    if not stat.S_ISREG(actual.st_mode) or not os.path.samestat(info, actual):
                        raise ValueError(f"skill '{name}' file changed while packaging")
                    _reject_link(actual)
                    _reject_hardlink(actual)
                    data = stream.read(budget + 1)
                if len(data) > budget:
                    raise _size_error(name, remaining)
                budget -= len(data)
                files[relative] = data
        return budget

    try:
        # The plugin path and each skills/<name> entry come from the host admin's configuration.
        # Skill bundles commonly link skills/<name> to a shared skill repository, so these two levels
        # are resolved once. Everything inside the resolved skill folder is still read without
        # following links, and links there are rejected.
        plugin = plugin.resolve(strict=not missing_ok)
        with _directory(plugin) as plugin_fd:
            skills = plugin / "skills"
            if missing_ok and not _exists(skills, plugin_fd):
                return None
            with _directory(skills, plugin_fd) as skills_fd:
                folder = skills / name
                if missing_ok and not _exists(folder, skills_fd):
                    return None
                if stat.S_ISLNK(_lstat(folder, skills_fd).st_mode):
                    folder = folder.resolve(strict=True)
                    with _directory(folder) as fd:
                        collect(folder, fd, "", limit)
                else:
                    with _directory(folder, skills_fd) as fd:
                        collect(folder, fd, "", limit)
    except FileNotFoundError:
        raise ValueError(f"skill '{name}' has an unavailable SKILL.md or folder") from None
    except OSError:
        raise ValueError(f"skill '{name}' has an unsafe or unreadable folder entry") from None
    _validate_paths(files)
    result = SkillFolder(name, dict(sorted(files.items())))
    result.skill_text()
    return result


def encode_skill_folders(folders: Sequence[SkillFolder]) -> list[dict]:
    """検証済みフォルダーを JSON 内の相対パスと Base64 に変換する。"""
    return [
        {"name": folder.name, "files": {path: base64.b64encode(data).decode("ascii") for path, data in folder.files.items()}}
        for folder in folders
    ]


def decode_skill_folders(payload: object) -> tuple[SkillFolder, ...]:
    """ファイル名とデコード後の容量を再検証する。不正入力は ValueError。"""
    if not isinstance(payload, list) or len(payload) > MAX_SKILLS:
        raise ValueError(f"invalid skill folders (up to {MAX_SKILLS} skills)")
    if any(not isinstance(item, dict) or item.keys() != {"name", "files"} for item in payload):
        raise ValueError("invalid skill folder")
    validate_skill_names([item["name"] for item in payload])
    remaining = MAX_TURN_FOLDER_BYTES
    folders = []
    for item in payload:
        name, encoded = item["name"], item["files"]
        if not isinstance(encoded, dict) or "SKILL.md" not in encoded:
            raise ValueError(f"skill '{name}' requires SKILL.md")
        if len(encoded) > MAX_FOLDER_FILES:
            raise ValueError(f"skill '{name}' exceeds {MAX_FOLDER_FILES} files")
        _validate_paths(encoded)
        files = {}
        budget = min(MAX_FOLDER_BYTES, remaining)
        for path, value in encoded.items():
            if not isinstance(value, str):
                raise ValueError(f"invalid base64 in skill '{name}'")
            if len(value) > 4 * ((budget + 2) // 3):
                raise _size_error(name, remaining)
            try:
                raw = base64.b64decode(value, validate=True)
            except ValueError:
                raise ValueError(f"invalid base64 in skill '{name}'") from None
            if len(raw) > budget:
                raise _size_error(name, remaining)
            budget -= len(raw)
            files[path] = raw
        remaining -= sum(map(len, files.values()))
        folder = SkillFolder(name, files)
        folder.skill_text()
        folders.append(folder)
    return tuple(folders)


def write_skill_folders(root: Path, folders: Sequence[SkillFolder]) -> None:
    """新規ルートに排他的に配置して書き込みを禁止する。検証・I/O の失敗時は ValueError。"""
    try:
        root.mkdir(mode=0o700)
        with _directory(root) as fd:
            for folder in folders:
                validate_legacy_skill_name(folder.name)
                _validate_paths(folder.files)
                skill_dir = root / folder.name
                _mkdir(skill_dir, fd)
                with _directory(skill_dir, fd) as skill_fd:
                    for relative, data in folder.files.items():
                        _write_file(skill_dir, skill_fd, _path_parts(relative), data)
        if os.name != "nt":
            _make_directories_read_only(root)
    except OSError as exc:
        raise ValueError("could not write skill folders") from exc


def write_skill_plugin(root: Path, folders: Sequence[SkillFolder]) -> Path:
    """セッション専用の最小プラグインを作る。root は新規パス。"""
    root.mkdir(mode=0o700)
    with _directory(root) as fd:
        _write_file(root, fd, [".claude-plugin", "plugin.json"], b'{"name":"plugin"}\n')
        write_skill_folders(root / "skills", folders)
        if os.name != "nt":
            _make_directories_read_only(root / ".claude-plugin", fd)
            os.chmod(root if fd is None else fd, 0o500)
    return root


def _size_error(name: str, remaining: int) -> ValueError:
    if remaining < MAX_FOLDER_BYTES:
        return ValueError(f"turn skill folders exceed {MAX_TURN_FOLDER_BYTES} bytes")
    return ValueError(f"skill '{name}' exceeds {MAX_FOLDER_BYTES} bytes")


def _skip_entry(name: str) -> bool:
    return name.startswith(".") or name in _SKIPPED_NAMES


def _path_parts(path: object) -> list[str]:
    if not isinstance(path, str):
        raise ValueError("invalid skill file path")
    try:
        if len(path.encode("utf-8")) > MAX_PATH_BYTES:
            raise ValueError("invalid skill file path")
        parts = path.split("/")
        for part in parts:
            validate_legacy_skill_name(part)
            if _skip_entry(part) or _EXTRA_WINDOWS_DEVICES.fullmatch(part.split(".", 1)[0].rstrip()):
                raise ValueError("invalid skill file path")
        return parts
    except ValueError:
        raise ValueError("invalid skill file path") from None


def _validate_paths(files: dict) -> None:
    seen: dict[str, tuple[str, bool]] = {}
    for path in files:
        parts = _path_parts(path)
        for index in range(1, len(parts) + 1):
            prefix = "/".join(parts[:index])
            entry = prefix, index == len(parts)
            key = unicodedata.normalize("NFKC", prefix).casefold()
            if seen.setdefault(key, entry) != entry:
                raise ValueError("conflicting skill file paths")


def _reject_link(info: os.stat_result) -> None:
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise ValueError("skill folder contains a symlink or reparse point")


def _reject_hardlink(info: os.stat_result) -> None:
    if info.st_nlink > 1:
        raise ValueError("skill folder contains a hardlink")


def _exists(path: Path, parent: int | None) -> bool:
    try:
        _lstat(path, parent)
    except FileNotFoundError:
        return False
    return True


def _lstat(path: Path, parent: int | None) -> os.stat_result:
    return path.lstat() if parent is None else os.stat(path.name, dir_fd=parent, follow_symlinks=False)


def _open(path: Path, flags: int, parent: int | None, mode: int = 0o600) -> int:
    return os.open(path if parent is None else path.name, flags, mode, dir_fd=parent)


@contextmanager
def _directory(path: Path, parent: int | None = None) -> Iterator[int | None]:
    info = _lstat(path, parent)
    _reject_link(info)
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError("invalid skill directory")
    if os.open not in os.supports_dir_fd or os.scandir not in os.supports_fd:
        # Windows では lstat で reparse point を検査する。展開先は新規の非公開ルート内。
        yield None
        return
    fd = _open(path, _READ_FLAGS | os.O_DIRECTORY, parent)
    try:
        if not os.path.samestat(info, os.fstat(fd)):
            raise ValueError("skill directory changed while opening")
        yield fd
    finally:
        os.close(fd)


def _mkdir(path: Path, parent: int | None) -> None:
    os.mkdir(path if parent is None else path.name, mode=0o700, dir_fd=parent)


def _write_file(directory: Path, parent: int | None, parts: list[str], data: bytes) -> None:
    target = directory / parts[0]
    if len(parts) == 1:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        with os.fdopen(_open(target, flags, parent), "wb") as stream:
            stream.write(data)
            if os.name != "nt":
                os.fchmod(stream.fileno(), 0o400)
        if os.name == "nt":
            target.chmod(stat.S_IREAD)
        return
    if not _exists(target, parent):
        _mkdir(target, parent)
    with _directory(target, parent) as fd:
        _write_file(target, fd, parts[1:], data)


def _make_directories_read_only(directory: Path, parent: int | None = None) -> None:
    with _directory(directory, parent) as fd:
        with os.scandir(directory if fd is None else fd) as entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    _make_directories_read_only(directory / entry.name, fd)
        # TemporaryDirectory の削除処理は、必要に応じて所有者の書き込み権限を戻す。
        os.chmod(directory if fd is None else fd, 0o500)
