from types import SimpleNamespace
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from kotoyose.config import HarnessConfig, ModelPreset
from kotoyose.model_discovery import _cursor_model_details
from kotoyose.runtime.cursor import (
    _CURSOR_FALLBACK_EFFORTS,
    CursorRuntimeSession,
    _cursor_model_params,
    build_cursor_session,
)
from kotoyose.runtime.translation import translate_runtime_config


def _fallback_warnings(entries):
    """capture_logs の記録から effort 値域警告だけを取り出す。"""
    return [
        {"effort": entry["effort"]}
        for entry in entries
        if entry.get("event") == "cursor.effort_outside_fallback"
        and entry.get("log_level") == "warning"
    ]


def test_cursor_model_params_normalizes_yaml_scalars():
    assert _cursor_model_params({"fast": True, "effort": "xhigh", "budget": 3}) == {
        "fast": "true",
        "effort": "xhigh",
        "budget": "3",
    }


@pytest.mark.parametrize("value", [[], {"nested": {"x": 1}}, {"none": None}])
def test_cursor_model_params_rejects_non_scalar_values(value):
    if isinstance(value, list):
        with pytest.raises(ValueError, match="object"):
            _cursor_model_params(value)
    else:
        with pytest.raises(ValueError, match="string / number / boolean"):
            _cursor_model_params(value)


def test_cursor_builder_maps_common_effort_and_provider_params(tmp_path, monkeypatch):
    monkeypatch.setattr("kotoyose.config._config", HarnessConfig())
    session = build_cursor_session(
        ModelPreset(
            provider="cursor",
            model="grok-4.6",
            cwd=str(tmp_path),
            effort="xhigh",
            cursor={"model_params": {"fast": True}},
        ),
        None,
        None,
    )
    assert session.model_params == {"fast": "true", "effort": "xhigh"}


def test_cursor_provider_specific_effort_wins_over_common_effort(tmp_path, monkeypatch):
    monkeypatch.setattr("kotoyose.config._config", HarnessConfig())
    session = build_cursor_session(
        ModelPreset(
            provider="cursor",
            model="grok-4.6",
            cwd=str(tmp_path),
            effort="xhigh",
            cursor={"model_params": {"effort": "medium"}},
        ),
        None,
        None,
    )
    assert session.model_params["effort"] == "medium"


def test_cursor_effort_is_no_longer_reported_unsupported():
    translated = translate_runtime_config(
        HarnessConfig(), ModelPreset(provider="cursor", effort="xhigh")
    )
    assert "effort" not in translated.unsupported_fields


@pytest.mark.asyncio
async def test_cursor_agent_options_receive_model_params(monkeypatch):
    captured = {}

    class FakeClient:
        async def aclose(self):
            pass

    class FakeAgent:
        agent_id = "agent"

    async def launch_bridge(**kwargs):
        return FakeClient()

    async def create(options, *, client, api_key=None):
        captured["options"] = options
        return FakeAgent()

    monkeypatch.setenv("CURSOR_API_KEY", "test")
    monkeypatch.setattr("kotoyose.runtime.cursor.AsyncClient.launch_bridge", launch_bridge)
    monkeypatch.setattr("kotoyose.runtime.cursor.AsyncAgent.create", create)
    await CursorRuntimeSession(
        "grok-4.6",
        "/tmp",
        model_params={"effort": "xhigh", "fast": "true"},
    )._ensure_agent()

    model = captured["options"].model
    assert model["id"] == "grok-4.6"
    assert {(item["id"], item["value"]) for item in model["params"]} == {
        ("effort", "xhigh"),
        ("fast", "true"),
    }


def test_cursor_model_details_preserve_parameters_and_variants():
    values = [SimpleNamespace(value="low", display_name="Low"), SimpleNamespace(value="high", display_name="High")]
    parameter = SimpleNamespace(id="effort", display_name="Effort", values=values)
    variant = SimpleNamespace(
        display_name="Fast High",
        description="fast variant",
        is_default=True,
        params=[SimpleNamespace(id="fast", value="true"), SimpleNamespace(id="effort", value="high")],
    )
    model = SimpleNamespace(
        id="grok-4.6",
        display_name="Cursor Grok 4.6",
        description="model",
        parameters=[parameter],
        variants=[variant],
    )
    detail = _cursor_model_details([model])[0]
    assert detail["parameters"][0]["id"] == "effort"
    assert detail["parameters"][0]["values"][1]["value"] == "high"
    assert detail["variants"][0]["params"][0] == {"id": "fast", "value": "true"}


def test_example_exposes_cursor_model_params():
    root = Path(__file__).resolve().parents[1]
    example = (root / "config.example.yml").read_text()
    assert "model_params: {}" in example
    assert "prompt_file_refs: [coding-verification, coding-git]" in example


def test_cursor_common_effort_outside_fallback_is_forwarded_with_warning(
    tmp_path, monkeypatch
):
    """catalog を引けない値域外 effort は drop せず転送し、警告だけ残す。"""
    monkeypatch.setattr("kotoyose.config._config", HarnessConfig())
    with capture_logs() as cap:
        session = build_cursor_session(
            ModelPreset(
                provider="cursor",
                model="grok-4.6",
                cwd=str(tmp_path),
                effort="max",
            ),
            None,
            None,
        )
    # 以前は unsupported として黙殺されていたため、drop すると挙動が戻ってしまう。
    assert session.model_params["effort"] == "max"
    assert _fallback_warnings(cap) == [{"effort": "max"}]


def test_cursor_common_effort_inside_fallback_is_silent(tmp_path, monkeypatch):
    monkeypatch.setattr("kotoyose.config._config", HarnessConfig())
    with capture_logs() as cap:
        session = build_cursor_session(
            ModelPreset(
                provider="cursor",
                model="grok-4.6",
                cwd=str(tmp_path),
                effort="xhigh",
            ),
            None,
            None,
        )
    assert session.model_params["effort"] == "xhigh"
    assert _fallback_warnings(cap) == []


def test_cursor_explicit_model_params_effort_bypasses_fallback_warning(
    tmp_path, monkeypatch
):
    """provider 固有の明示指定は escape hatch なので値域チェックの対象外。"""
    monkeypatch.setattr("kotoyose.config._config", HarnessConfig())
    with capture_logs() as cap:
        session = build_cursor_session(
            ModelPreset(
                provider="cursor",
                model="grok-4.6",
                cwd=str(tmp_path),
                effort="max",
                cursor={"model_params": {"effort": "max"}},
            ),
            None,
            None,
        )
    assert session.model_params["effort"] == "max"
    assert _fallback_warnings(cap) == []


def test_cursor_fallback_effort_domain():
    assert sorted(_CURSOR_FALLBACK_EFFORTS) == ["high", "low", "medium", "xhigh"]
