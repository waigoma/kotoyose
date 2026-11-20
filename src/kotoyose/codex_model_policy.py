"""固定 CLI の tool/transport metadata。stdlib-only launcher からも import する。"""

import re
from typing import Literal


CodexEffort = Literal["low", "medium", "high", "xhigh", "max"]
# Luna metadata: Codex rust-v0.160.0, commit a956835d020762cb2b570053af06f643a11c0ecc:
# rust-v0.170.0 の同じ catalog で Luna の effort と既定値 medium の一致を確認済み。
# codex-rs/models-manager/models.json:527,563-585. This pins metadata, not entitlement.
_REVIEWED_EFFORTS = {
    "gpt-6-luna": ("low", "medium", "high", "xhigh", "max"),
    # Sol review is deliberately limited to an explicitly saved xhigh selection.
    "gpt-6.1-sol": ("xhigh",),
    # Terra is admitted for coding at the saved high selection only (owner decision 2026-11-12).
    "gpt-6.1-terra": ("high",),
}
# Omitted alias effort retains the existing native default; it is not the upstream medium default.
_ALIAS_DEFAULT_EFFORT = {"gpt-6-luna": "low"}


def validate_reasoning_effort(model: str, effort: str) -> str:
    """Fail closed on unreviewed model/effort pairs; never remap max to xhigh/low."""
    if effort not in _REVIEWED_EFFORTS.get(model, ()):
        raise ValueError("unsupported_codex_reasoning_effort")
    return effort


def model_catalog(model: str, *, images: bool = False, reasoning_effort: str | None = None, coding: bool = False) -> dict:
    """network discovery に依存せず同じ model slug の制限付き catalog を返す。"""
    if not isinstance(model, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._:-]{0,127}", model):
        raise ValueError("invalid_codex_model")
    # Direct native profiles keep the historical low-only catalog unchanged.
    efforts = ("low",) if reasoning_effort is None else _REVIEWED_EFFORTS.get(model, ())
    selected = "low" if reasoning_effort is None else validate_reasoning_effort(model, reasoning_effort)
    return {"models": [{
        "slug": model, "display_name": model, "description": "Kotoyose restricted native execution",
        "base_instructions": "Follow the developer instructions and use only the admitted tools.",
        "supported_reasoning_levels": [{"effort": effort, "description": "Bounded native execution"} for effort in efforts],
        "default_reasoning_level": selected, "shell_type": "unified_exec" if coding else "disabled", "visibility": "hide",
        "supported_in_api": True, "priority": 0, "availability_nux": None, "upgrade": None,
        "support_verbosity": False, "default_verbosity": None, "apply_patch_tool_type": "freeform" if coding else None,
        "truncation_policy": {"mode": "bytes", "limit": 65536}, "experimental_supported_tools": [],
        "input_modalities": ["text", "image"] if images else ["text"], "supports_search_tool": False, "use_responses_lite": False,
        "tool_mode": "direct", "multi_agent_version": "disabled",
        "include_apps_usage_instructions": False, "supports_reasoning_summary_parameter": False,
    }]}
