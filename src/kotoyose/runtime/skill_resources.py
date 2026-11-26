"""Alias のスキル選択を配信用フォルダーと CLI 指示文に解決する。"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from ..child_sdk_skills import MAX_SKILL_BYTES, MAX_SKILLS_BYTES, read_skill_file, validate_native_skill, validate_skill_names, validate_turn_files
from ..config import HarnessConfig, ModelPreset, resolve_prompt_texts, resolve_skill_fallback_texts, strip_frontmatter
from ..skill_folders import (
    MAX_TURN_FOLDER_BYTES, SkillFolder, decode_skill_folders, encode_skill_folders, read_skill_folder, write_skill_folders,
)
from .base import SDKConnectionError
from .translation import RuntimeSkill, resolve_cli_prompt_texts, resolve_plugin_skills


def resolve_native_folders(
    config: HarnessConfig, preset: ModelPreset, *, inline: tuple[RuntimeSkill, ...] | None = None,
) -> tuple[SkillFolder, ...]:
    """個別参照を定義元のフォルダーから読み、ネイティブ用の本文を検証する。"""
    try:
        if inline is None:
            inline = _skills_selected_from_reference_plugins(config, preset)
        folders = _resolve_folders(config, preset, inline=inline)
        validate_native_folders(folders)
    except ValueError as exc:
        raise SDKConnectionError(str(exc)) from None
    return folders


def _skills_selected_from_reference_plugins(config: HarnessConfig, preset: ModelPreset) -> tuple[RuntimeSkill, ...]:
    if preset.skills is None or not preset.skill_refs:
        return ()
    plugins = list(dict.fromkeys(config.skill_definitions[ref].plugin for ref in preset.skill_refs))
    discovered = {skill.name: skill for skill in resolve_plugin_skills(plugins, "all", keep_source_path=True)}
    if preset.skills == "all":
        return tuple(discovered.values())
    return tuple(discovered[name] for name in preset.skills if name in discovered)


def validate_native_folders(folders: tuple[SkillFolder, ...]) -> None:
    """ネイティブ CLI 向けに展開記法と frontmatter を検査する。"""
    for folder in folders:
        validate_native_skill(folder.name, folder.skill_text())


def _resolve_folders(
    config: HarnessConfig, preset: ModelPreset, *, allow_fallback_only: bool = False,
    inline: tuple[RuntimeSkill, ...] = (),
) -> tuple[SkillFolder, ...]:
    definitions = [config.skill_definitions[ref] for ref in preset.skill_refs]
    sources = [(Path(item.plugin), item.skill, item.fallback_prompt) for item in definitions]
    referenced = {name for _, name, _ in sources}
    sources.extend((Path(skill.path).parents[2], skill.name, None) for skill in inline if skill.name not in referenced)
    validate_skill_names([name for _, name, _ in sources])
    folders = []
    remaining = MAX_TURN_FOLDER_BYTES
    for plugin, name, fallback in sources:
        folder = read_skill_folder(plugin, name, remaining, missing_ok=allow_fallback_only and fallback is not None)
        if folder is not None:
            remaining -= sum(map(len, folder.files.values()))
            folders.append(folder)
    return tuple(folders)


def folder_binding(folders: tuple[SkillFolder, ...]) -> str:
    """パスに依存しないセッション同一性を返す。"""
    return hashlib.sha256(json.dumps(encode_skill_folders(folders), sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class CliInstructions:
    """前置する本文と、起動方式に合わせて配置できるフォルダー。"""

    prompts: list[str]
    skills: list[dict[str, str]]
    folders: tuple[SkillFolder, ...]

    @classmethod
    def from_payload(cls, prompts: object, skills: object, folders: object) -> CliInstructions:
        """子機 turn または実行スナップショットの前置入力を検証する。"""
        validate_turn_files(prompts, skills, native_skills=False)
        decoded = decode_skill_folders(folders)
        if not {folder.name for folder in decoded}.issubset(skill["name"] for skill in skills):
            raise ValueError("skill folders require matching prefix skills")
        return cls(prompts, skills, decoded)

    def payload(self) -> dict:
        """配置パスを含まない、保存・転送用の値を返す。"""
        return {"prompts": self.prompts, "skills": self.skills, "skill_folders": encode_skill_folders(self.folders)}

    @property
    def binding(self) -> str:
        """本文とファイルをセッション同一性に含める。"""
        return folder_binding(self.folders) + self.text()

    def text(self) -> str:
        """パスを追加する前の指示文。"""
        if not self.skills:
            return "\n".join(self.prompts)
        return _compose_cli_instructions(self.prompts, self.skills)

    def place(self, root: Path, *, visible_root: Path | None = None) -> str:
        """非公開ルートに展開し、CLI に読ませるパスを指示文へ加える。"""
        if self.folders:
            write_skill_folders(root, self.folders)
        return self.render(visible_root or root)

    def place_grok(self, home: Path) -> str:
        """Publish full skill folders under the session HOME's native Grok root."""
        if not self.folders:
            return self.text()
        validate_native_folders(self.folders)
        agents = home / ".agents"
        agents.mkdir(mode=0o700)
        write_skill_folders(agents / "skills", self.folders)
        native = {folder.name for folder in self.folders}
        return CliInstructions(self.prompts, [skill for skill in self.skills if skill["name"] not in native], ()).text()

    def render(self, root: Path) -> str:
        """CLI が参照できるルートを使って指示を合成する。"""
        return _compose_cli_instructions(self.prompts, self.skills, folders=self.folders, folder_root=root)


def resolve_cli_skill_texts(config: HarnessConfig, preset: ModelPreset) -> list[dict[str, str]]:
    """旧形式の前置用に、容量制限つきでスキルの本文だけを読む。"""
    definitions = config.skill_definitions
    validate_skill_names([definitions[ref].skill for ref in preset.skill_refs])
    return [
        {"name": definitions[ref].skill, "text": text}
        for ref, text in resolve_skill_fallback_texts(definitions, preset.skill_refs, bounded=True)
    ]


def resolve_cli_resources(config: HarnessConfig, preset: ModelPreset) -> CliInstructions:
    """ACP と print が共有するスキル本文とフォルダーを解決する。"""
    try:
        if not preset.skill_refs:
            texts = resolve_prompt_texts(config.resolve_prompt_files(preset))
            return CliInstructions(["\n".join(texts)], [], ())
        folders = _resolve_folders(config, preset, allow_fallback_only=True)
        by_name = {folder.name: folder for folder in folders}
        remaining = MAX_SKILLS_BYTES
        skills = []
        for ref in preset.skill_refs:
            definition = config.skill_definitions[ref]
            name = definition.skill
            if definition.fallback_prompt:
                text = read_skill_file(name, definition.fallback_prompt, remaining, label="fallback_prompt")
            else:
                text = by_name[name].skill_text()
            size = len(text.encode("utf-8"))
            if size > MAX_SKILL_BYTES:
                raise ValueError(f"child skill '{name}' exceeds {MAX_SKILL_BYTES} bytes")
            remaining -= size
            if remaining < 0:
                raise ValueError(f"child skills exceed {MAX_SKILLS_BYTES} bytes")
            text = strip_frontmatter(text.replace("\r\n", "\n").replace("\r", "\n")).strip()
            if not text:
                raise ValueError(f"skill '{name}' has no fallback body")
            skills.append({"name": name, "text": text})
        result = CliInstructions(resolve_cli_prompt_texts(config, preset), skills, folders)
        result.text()
        return result
    except ValueError as exc:
        raise SDKConnectionError(str(exc)) from None


def _compose_cli_instructions(
    prompts: list[str], skills: list[dict[str, str]], *,
    folders: tuple[SkillFolder, ...] = (), folder_root: Path | None = None,
) -> str:
    """名前付き指示を合成し、配置済みの添付ファイルを CLI の読み取りツールへ案内する。"""
    validate_turn_files(prompts, skills, native_skills=False)
    by_name = {folder.name: folder for folder in folders}
    sections = []
    for skill in skills:
        name = skill["name"]
        section = f"## Skill: {name}\n\n{skill['text']}"
        folder = by_name.get(name)
        if folder is not None and folder_root is not None:
            directory = json.dumps(str(folder_root / name), ensure_ascii=False)
            files = json.dumps(sorted(folder.files), ensure_ascii=False)
            section += (
                f"\n\nThis skill's files are at {directory}. "
                f"Use your file-reading tool with paths relative to that directory. Files: {files}"
            )
        sections.append(section)
    instructions = "\n".join(prompt.replace("\r\n", "\n").replace("\r", "\n") for prompt in prompts)
    return "\n\n".join(part for part in (*sections, instructions) if part)
