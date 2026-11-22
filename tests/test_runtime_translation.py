"""provider 共通設定の中間表現に対するテスト。"""

from __future__ import annotations

import json

import pytest

from kotoyose.config import (
    HarnessConfig,
    McpDefinition,
    ModelPreset,
    PromptFileDefinition,
    SkillDefinition,
)
from kotoyose.runtime.translation import apply_common_timeout, translate_runtime_config


def _make_bundle(base, name: str, skills: tuple[str, ...]):
    bundle = base / name
    for skill in skills:
        skill_dir = bundle / "skills" / skill
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(f"# {skill}\n", encoding="utf-8")
    (bundle / ".claude-plugin").mkdir(parents=True)
    (bundle / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": name, "version": "1.0.0"}), encoding="utf-8"
    )
    return bundle


def test_translate_runtime_config_resolves_all_common_resources(tmp_path):
    """named ref・inline・header・共通実行設定を 1 つの中間表現へ解決する。"""
    bundle = _make_bundle(tmp_path, "bundle", ("debug",))
    prompt = tmp_path / "prompt.md"
    prompt.write_text("共通指示", encoding="utf-8")
    config = HarnessConfig(
        skill_definitions={
            "debug": SkillDefinition(plugin=str(bundle), skill="debug")
        },
        mcps={
            "memory": McpDefinition(
                server={
                    "type": "http",
                    "url": "https://example.test/mcp",
                    "headers": {"Authorization": "Bearer {{header:X-Token}}"},
                }
            )
        },
        prompt_files={"role": PromptFileDefinition(path=str(prompt))},
    )
    preset = ModelPreset(
        skill_refs=["debug"],
        mcp_refs=["memory"],
        prompt_file_refs=["role"],
        required_request_headers=["X-Token"],
        effort="high",
        timeout_seconds=42,
    )

    translated = translate_runtime_config(config, preset, {"x-token": "secret"})

    assert translated.instructions == "共通指示"
    assert translated.mcp_servers["memory"]["headers"]["Authorization"] == "Bearer secret"
    assert [(skill.name, skill.path) for skill in translated.skills] == [
        ("debug", str((bundle / "skills" / "debug" / "SKILL.md").resolve()))
    ]
    assert translated.effort == "high"
    assert translated.timeout_seconds == 42


def test_translate_runtime_config_discovers_all_plugin_skills(tmp_path):
    """skills: all と自動選択の双方で bundle 内 Skill を安定順に列挙する。"""
    bundle = _make_bundle(tmp_path, "bundle", ("zeta", "alpha"))
    config = HarnessConfig()

    for skill_filter in (None, "all"):
        preset = ModelPreset(plugins=[str(bundle)], skills=skill_filter)
        translated = translate_runtime_config(config, preset)
        assert [skill.name for skill in translated.skills] == ["alpha", "zeta"]


def test_translate_runtime_config_rejects_selected_missing_skill(tmp_path):
    """明示選択された Skill の欠落を runtime 起動前に検出する。"""
    bundle = _make_bundle(tmp_path, "bundle", ("present",))
    preset = ModelPreset(plugins=[str(bundle)], skills=["missing"])

    with pytest.raises(ValueError, match="missing"):
        translate_runtime_config(HarnessConfig(), preset)


def test_apply_common_timeout_keeps_provider_override():
    """provider 固有 timeout は共通値より高い優先順位を保つ。"""
    assert apply_common_timeout({}, 30) == {"timeout": 30}
    assert apply_common_timeout({"timeout": 5}, 30) == {"timeout": 5}


def test_translate_runtime_config_reports_non_equivalent_explicit_fields():
    """未対応の権限・思考設定を適用済みと見せず capability 情報へ残す。"""
    preset = ModelPreset(
        provider="antigravity",
        tools=["Read"],
        deny=["Bash"],
        thinking="adaptive",
        effort="high",
        permission_mode="plan",
        bare=False,
        setting_sources=[],
        require_isolation=True,
    )

    translated = translate_runtime_config(HarnessConfig(), preset)

    assert translated.unsupported_fields == (
        "tools",
        "deny",
        "thinking",
        "permission_mode",
        "bare",
        "setting_sources",
        "require_isolation",
    )


def test_cursor_native_fields_are_not_reported_as_unsupported():
    """Cursor SDK 1.x へ native 変換する field は warning 対象から外す。"""
    preset = ModelPreset(
        provider="cursor",
        tools=["Read"],
        deny=["Bash"],
        setting_sources=[],
        require_isolation=True,
        bare=False,
    )

    translated = translate_runtime_config(HarnessConfig(), preset)

    assert "tools" not in translated.unsupported_fields
    assert "deny" not in translated.unsupported_fields
    assert "setting_sources" not in translated.unsupported_fields
    assert "require_isolation" not in translated.unsupported_fields
