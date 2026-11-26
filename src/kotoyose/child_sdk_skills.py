"""Shared bounds for alias skills and prompts carried in a child turn."""

from __future__ import annotations

import os
import re
import stat

import yaml
from yaml.nodes import MappingNode, ScalarNode

MAX_SKILLS = 16
MAX_SKILL_BYTES = 32 * 1024
MAX_SKILLS_BYTES = 128 * 1024
MAX_TURN_FILE_BYTES = 256 * 1024

_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]{0,63}")
_WINDOWS_DEVICE = re.compile(r"CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9]", re.IGNORECASE)
_COMMAND = re.compile(r"!\s*`|(?:`{3,}|~{3,})[ \t]*!")
_METADATA = frozenset({
    "name", "description", "argument-hint", "disable-model-invocation", "user-invocable",
    "license", "compatibility", "metadata",
})


def validate_skill_names(names: list[str], *, legacy: bool = False) -> None:
    if len(names) > MAX_SKILLS:
        raise ValueError(f"child accepts up to {MAX_SKILLS} skills")
    seen = set()
    for name in names:
        if legacy and isinstance(name, str):
            validate_legacy_skill_name(name)
        elif not _valid_name(name):
            raise ValueError("invalid child skill name")
        key = name.casefold()
        if key in seen:
            raise ValueError(f"duplicate child skill name '{name}'")
        seen.add(key)


def _valid_name(name: object) -> bool:
    return isinstance(name, str) and _NAME.fullmatch(name) is not None and _WINDOWS_DEVICE.fullmatch(name) is None


def validate_legacy_skill_name(name: str) -> None:
    if (
        not name or name.endswith((".", " ")) or any(char in '<>:"/\\|?*' or ord(char) < 32 for char in name)
        or _WINDOWS_DEVICE.fullmatch(name.split(".", 1)[0].rstrip())
    ):
        raise ValueError("invalid child skill name")


def read_skill_file(name: str, path: str, remaining: int, *, label: str = "SKILL.md") -> str:
    limit = min(MAX_SKILL_BYTES, remaining)
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("not a regular file")
            raw = stream.read(limit + 1)
        if len(raw) <= limit:
            return raw.decode("utf-8")
    except (OSError, ValueError):
        raise ValueError(f"child skill '{name}' has an unavailable or invalid {label}") from None
    if limit == MAX_SKILL_BYTES:
        raise ValueError(f"child skill '{name}' exceeds {MAX_SKILL_BYTES} bytes")
    raise ValueError(f"child skills exceed {MAX_SKILLS_BYTES} bytes")


def validate_turn_files(
    prompts: object, skills: object, *, legacy: bool = False, native_skills: bool = True,
) -> None:
    if not isinstance(prompts, list) or not isinstance(skills, list):
        raise ValueError("invalid child prompt or skill list")
    if len(skills) > MAX_SKILLS:
        raise ValueError(f"child accepts up to {MAX_SKILLS} skills")
    if any(not isinstance(skill, dict) or skill.keys() != {"name", "text"} for skill in skills):
        raise ValueError("invalid child skill")
    validate_skill_names([skill["name"] for skill in skills], legacy=legacy)
    skill_bytes = 0
    for skill in skills:
        name, text = skill["name"], skill["text"]
        size = _text_size(text, f"child skill '{name}'")
        if size > MAX_SKILL_BYTES:
            raise ValueError(f"child skill '{name}' exceeds {MAX_SKILL_BYTES} bytes")
        skill_bytes += size
        if skill_bytes > MAX_SKILLS_BYTES:
            raise ValueError(f"child skills exceed {MAX_SKILLS_BYTES} bytes")
        if native_skills:
            validate_native_skill(name, text)
    total = skill_bytes
    for prompt in prompts:
        total += _text_size(prompt, "child prompt")
        if total > MAX_TURN_FILE_BYTES:
            raise ValueError(f"child prompts and skills exceed {MAX_TURN_FILE_BYTES} bytes")


def _text_size(text: object, label: str) -> int:
    if isinstance(text, str) and "\x00" not in text:
        try:
            return len(text.encode("utf-8"))
        except UnicodeEncodeError:
            pass
    raise ValueError(f"{label} must be UTF-8 text without NUL")


def validate_native_skill(name: str, text: str) -> None:
    """ネイティブ CLI に渡す指示文の frontmatter と展開記法を検証する。"""
    if _COMMAND.search(text):
        raise ValueError(f"child skill '{name}' contains command expansion")
    lines = text.lstrip("\ufeff \t\r\n").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if not lines or lines[0].strip() != "---":
        return
    try:
        end = next(index for index, line in enumerate(lines[1:], 1) if line.rstrip(" \t\r") == "---")
        node = yaml.compose("\n".join(lines[1:end]), Loader=yaml.SafeLoader)
        if node is None:
            return
        if isinstance(node, MappingNode):
            keys = set()
            for key, value in node.value:
                if not isinstance(key, ScalarNode) or key.value not in _METADATA or key.value in keys:
                    break
                keys.add(key.value)
                if key.value == "metadata" and isinstance(value, MappingNode):
                    if all(isinstance(item, ScalarNode) and isinstance(detail, ScalarNode) for item, detail in value.value):
                        continue
                if not isinstance(value, ScalarNode) or (key.value == "name" and not _valid_name(value.value)):
                    break
            else:
                return
    except (StopIteration, RecursionError, yaml.YAMLError):
        pass
    raise ValueError(f"child skill '{name}' has unsupported YAML frontmatter")
