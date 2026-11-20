"""The Sol review selection retains the existing native isolation contract."""

import pytest

from kotoyose.codex_model_policy import model_catalog
from kotoyose.codex_preferences import resolve_codex_preferences


def test_sol_review_requires_exact_saved_xhigh_and_disables_tools():
    preferences = resolve_codex_preferences("gpt-6.1-sol", "xhigh")
    assert preferences.requested_effort == preferences.effort == "xhigh"
    catalog = model_catalog("gpt-6.1-sol", images=True, reasoning_effort=preferences.effort)["models"][0]
    assert catalog["slug"] == "gpt-6.1-sol"
    assert catalog["default_reasoning_level"] == "xhigh"
    assert [item["effort"] for item in catalog["supported_reasoning_levels"]] == ["xhigh"]
    assert catalog["shell_type"] == "disabled"
    assert catalog["apply_patch_tool_type"] is None
    assert catalog["input_modalities"] == ["text", "image"]
    assert catalog["multi_agent_version"] == "disabled"


@pytest.mark.parametrize("effort", [None, "low", "medium", "high", "max", "ultra"])
def test_sol_review_has_no_implicit_effort_or_downgrade(effort):
    with pytest.raises(ValueError, match="unsupported_codex_reasoning_effort"):
        resolve_codex_preferences("gpt-6.1-sol", effort)


def test_direct_sol_profile_keeps_historical_low_only_catalog():
    direct = model_catalog("gpt-6.1-sol")["models"][0]
    assert direct["default_reasoning_level"] == "low"
    assert [item["effort"] for item in direct["supported_reasoning_levels"]] == ["low"]
