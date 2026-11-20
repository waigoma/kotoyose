"""Terra is admitted only at the explicitly saved high effort."""

import pytest

from kotoyose.codex_model_policy import model_catalog
from kotoyose.codex_preferences import resolve_codex_preferences


def test_terra_accepts_exact_saved_high():
    preferences = resolve_codex_preferences("gpt-6.1-terra", "high")
    assert preferences.requested_effort == preferences.effort == "high"
    catalog = model_catalog("gpt-6.1-terra", reasoning_effort=preferences.effort, coding=True)["models"][0]
    assert catalog["slug"] == "gpt-6.1-terra"
    assert catalog["default_reasoning_level"] == "high"
    assert [item["effort"] for item in catalog["supported_reasoning_levels"]] == ["high"]


@pytest.mark.parametrize("effort", [None, "low", "medium", "xhigh", "max"])
def test_terra_has_no_implicit_or_other_effort(effort):
    with pytest.raises(ValueError, match="unsupported_codex_reasoning_effort"):
        resolve_codex_preferences("gpt-6.1-terra", effort)
