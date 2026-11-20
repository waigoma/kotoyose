"""Controller-owned saved Codex preferences; never imported by the stdlib-only launcher."""

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .codex_model_policy import CodexEffort, _ALIAS_DEFAULT_EFFORT, validate_reasoning_effort


class CodexModelPreferences(BaseModel):
    """Only non-security preferences copied from a saved alias at admission."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    requested_effort: CodexEffort | None
    effort: CodexEffort
    timeout_seconds: float | None = Field(default=None, gt=0, allow_inf_nan=False)

    @field_validator("timeout_seconds", mode="before")
    @classmethod
    def numeric_timeout(cls, value):
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise ValueError("Codex preset timeout must be a finite positive number")
        return value

    @model_validator(mode="after")
    def no_effort_downgrade(self):
        if self.requested_effort is not None and self.requested_effort != self.effort:
            raise ValueError("Codex requested and effective effort differ")
        return self


def resolve_codex_preferences(model: str, requested_effort: str | None, timeout_seconds=None) -> CodexModelPreferences:
    effort = requested_effort if requested_effort is not None else _ALIAS_DEFAULT_EFFORT.get(model)
    validate_reasoning_effort(model, effort)
    return CodexModelPreferences(requested_effort=requested_effort, effort=effort, timeout_seconds=timeout_seconds)
