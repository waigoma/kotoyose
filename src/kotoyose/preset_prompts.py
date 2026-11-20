"""Admission-pinned native preset instructions; no runtime authority or file paths."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


MAX_PRESET_PROMPT_FILES = 16
MAX_PRESET_PROMPT_FILE_BYTES = 64 * 1024
MAX_PRESET_PROMPT_TOTAL_BYTES = 256 * 1024


class PresetPromptSnapshot(BaseModel):
    """Immutable ordered text and identity, stored only in the private snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["1"] = "1"
    refs: tuple[str, ...] = Field(min_length=1, max_length=MAX_PRESET_PROMPT_FILES)
    texts: tuple[str, ...] = Field(min_length=1, max_length=MAX_PRESET_PROMPT_FILES)
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_content(self):
        from .execution_resources import content_digest
        if (len(self.refs) != len(self.texts) or len(set(self.refs)) != len(self.refs)
                or any(not ref or len(ref) > 256 or "\x00" in ref for ref in self.refs)
                or any("\x00" in text or len(text.encode("utf-8")) > MAX_PRESET_PROMPT_FILE_BYTES for text in self.texts)
                or sum(len(text.encode("utf-8")) for text in self.texts) > MAX_PRESET_PROMPT_TOTAL_BYTES
                or content_digest(self.model_dump(mode="json", exclude={"digest"})) != self.digest):
            raise ValueError("Invalid preset prompt snapshot")
        return self


def resolve_preset_prompts(config, preset) -> PresetPromptSnapshot | None:
    """Use the same selected alias as model/effort; read every explicit ref once."""
    from .config import resolve_prompt_texts
    from .execution_resources import content_digest
    if not isinstance(preset.prompt_file_refs, (list, tuple)):
        raise ValueError("Preset prompt references are invalid")
    refs = tuple(preset.prompt_file_refs)
    if not refs:
        return None
    if (len(refs) > MAX_PRESET_PROMPT_FILES
            or any(not isinstance(ref, str) or not ref or len(ref) > 256 or "\x00" in ref for ref in refs)
            or len(set(refs)) != len(refs)):
        raise ValueError("Preset prompt references are invalid")
    try:
        paths = config.resolve_prompt_files(preset)
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("Preset prompt references are unavailable") from exc
    texts = resolve_prompt_texts(paths, strict_limits=(MAX_PRESET_PROMPT_FILE_BYTES, MAX_PRESET_PROMPT_TOTAL_BYTES))
    payload = {"schema_version": "1", "refs": refs, "texts": tuple(texts)}
    return PresetPromptSnapshot(**payload, digest=content_digest(payload))


def stored_preset_prompts(value) -> PresetPromptSnapshot:
    """Validate persisted data without leaking its body through validation errors."""
    try:
        return PresetPromptSnapshot.model_validate(value)
    except (ValueError, TypeError) as exc:
        raise ValueError("Invalid preset prompt snapshot") from exc
