"""skills 非対応 provider への skill fallback 注入。"""

from pathlib import Path

import pytest

from kotoyose.config import (
    HarnessConfig,
    PromptFileDefinition,
    ModelPreset,
    SkillDefinition,
    resolve_skill_fallback_texts,
    strip_frontmatter,
)
from kotoyose.runtime.translation import translate_runtime_config


@pytest.fixture
def plugin(tmp_path: Path) -> Path:
    """SKILL.md を 1 本持つ最小の plugin directory を作る。"""
    root = tmp_path / "plug"
    (root / "skills" / "demo").mkdir(parents=True)
    (root / ".claude-plugin").mkdir()
    (root / ".claude-plugin" / "plugin.json").write_text('{"name": "plug"}')
    (root / "skills" / "demo" / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Triggers on X\n---\n\n# Demo\n\n本文です。\n",
        encoding="utf-8",
    )
    return root


def _config(plugin: Path, **definition_kwargs) -> HarnessConfig:
    return HarnessConfig(
        skill_definitions={
            "demo": SkillDefinition(plugin=str(plugin), skill="demo", **definition_kwargs)
        }
    )


def test_strip_frontmatter_removes_only_the_leading_block():
    assert strip_frontmatter("---\na: 1\n---\n# H\n") == "# H\n"
    assert strip_frontmatter("# H\n") == "# H\n"
    # 閉じが無いものは本文とみなす (壊れた入力で全部消さない)。
    assert strip_frontmatter("---\nno close") == "---\nno close"


def test_fallback_uses_skill_md_and_drops_frontmatter(plugin: Path):
    (name, text), = resolve_skill_fallback_texts(
        _config(plugin).skill_definitions, ["demo"]
    )
    assert name == "demo"
    assert text.startswith("# Demo")
    # frontmatter の trigger 条件が本文へ混ざらないこと。
    assert "Triggers" not in text


def test_fallback_prompt_overrides_skill_md(plugin: Path, tmp_path: Path):
    condensed = tmp_path / "short.md"
    condensed.write_text("常時前置する凝縮版。\n", encoding="utf-8")
    cfg = _config(plugin, fallback_prompt=str(condensed))
    (_, text), = resolve_skill_fallback_texts(cfg.skill_definitions, ["demo"])
    assert text == "常時前置する凝縮版。"


def test_missing_fallback_file_is_skipped(plugin: Path, tmp_path: Path):
    cfg = _config(plugin, fallback_prompt=str(tmp_path / "absent.md"))
    assert resolve_skill_fallback_texts(cfg.skill_definitions, ["demo"]) == []


def test_unknown_skill_ref_is_skipped(plugin: Path):
    assert resolve_skill_fallback_texts(_config(plugin).skill_definitions, ["nope"]) == []


@pytest.mark.parametrize("provider", ["claude", "codex", "antigravity"])
def test_capable_providers_keep_native_injection(plugin: Path, provider: str):
    """native 注入できる provider では本文を前置しない (二重にしない)。"""
    cfg = _config(plugin)
    translated = translate_runtime_config(
        cfg, ModelPreset(provider=provider, skill_refs=["demo"])
    )
    assert translated.instructions is None
    assert [skill.name for skill in translated.skills] == ["demo"]
    assert translated.plugin_paths == (str(plugin),)


def test_cursor_receives_the_skill_text_as_prompt(plugin: Path):
    cfg = _config(plugin)
    translated = translate_runtime_config(
        cfg, ModelPreset(provider="cursor", skill_refs=["demo"])
    )
    assert translated.instructions is not None
    assert "# Demo" in translated.instructions
    # 前置した skill は「落とした」ではないので未対応リソースから外れる。
    assert translated.skills == ()
    assert translated.plugin_paths == ()


def test_fallback_is_appended_after_prompt_file_refs(plugin: Path, tmp_path: Path):
    """記載順は prompt_file_refs が先、skill fallback が後。"""
    first = tmp_path / "first.md"
    first.write_text("さきに置く文。\n", encoding="utf-8")
    cfg = HarnessConfig(
        skill_definitions={"demo": SkillDefinition(plugin=str(plugin), skill="demo")},
        prompt_files={"first": PromptFileDefinition(path=str(first))},
    )
    translated = translate_runtime_config(
        cfg,
        ModelPreset(provider="cursor", skill_refs=["demo"], prompt_file_refs=["first"]),
    )
    assert translated.instructions.index("さきに置く文") < translated.instructions.index("# Demo")


def test_inline_plugins_survive_the_fallback(plugin: Path, tmp_path: Path):
    """skill_refs 由来でない inline plugin は落とさない。"""
    other = tmp_path / "other"
    (other / ".claude-plugin").mkdir(parents=True)
    (other / ".claude-plugin" / "plugin.json").write_text('{"name": "other"}')
    cfg = _config(plugin)
    translated = translate_runtime_config(
        cfg,
        ModelPreset(provider="cursor", skill_refs=["demo"], plugins=[str(other)]),
    )
    assert translated.plugin_paths == (str(other),)


def test_no_skill_refs_changes_nothing(plugin: Path):
    translated = translate_runtime_config(
        _config(plugin), ModelPreset(provider="cursor")
    )
    assert translated.instructions is None
    assert translated.skills == ()


def test_shared_plugin_survives_when_one_skill_fails_to_resolve(
    plugin: Path, tmp_path: Path
):
    """1 plugin が複数 skill を束ねるとき、未解決の skill を握り潰さない。

    handled 側の plugin を無条件に落とすと、同じ plugin を指す未解決の skill が
    「注入もされず warning にも出ない」まま消える。実際 config.yml の
    gates-authoring と git-conventions は同じ plugin を共有している。
    """
    # SKILL.md の欠落は _resolve_skills が loud に弾くため、未解決になるのは
    # fallback_prompt を指定してその実体が無い場合。
    (plugin / "skills" / "broken").mkdir(parents=True)
    (plugin / "skills" / "broken" / "SKILL.md").write_text("# Broken\n", encoding="utf-8")
    cfg = HarnessConfig(
        skill_definitions={
            "demo": SkillDefinition(plugin=str(plugin), skill="demo"),
            "broken": SkillDefinition(
                plugin=str(plugin),
                skill="broken",
                fallback_prompt=str(tmp_path / "absent.md"),
            ),
        }
    )
    translated = translate_runtime_config(
        cfg, ModelPreset(provider="cursor", skill_refs=["demo", "broken"])
    )
    # demo は本文で届く。
    assert "# Demo" in translated.instructions
    # broken は解決できていないので、plugin path を残して可視のままにする。
    # (resolve_resources は skill_ref ごとに plugin を積むため重複しうる)
    assert str(plugin) in translated.plugin_paths


def test_shared_plugin_is_dropped_when_every_skill_resolves(plugin: Path):
    """全部解決できたときは plugin path も用済みとして落とす。"""
    (plugin / "skills" / "second").mkdir(parents=True)
    (plugin / "skills" / "second" / "SKILL.md").write_text("# Second\n", encoding="utf-8")
    cfg = HarnessConfig(
        skill_definitions={
            "demo": SkillDefinition(plugin=str(plugin), skill="demo"),
            "second": SkillDefinition(plugin=str(plugin), skill="second"),
        }
    )
    translated = translate_runtime_config(
        cfg, ModelPreset(provider="cursor", skill_refs=["demo", "second"])
    )
    assert "# Demo" in translated.instructions
    assert "# Second" in translated.instructions
    assert translated.plugin_paths == ()


def test_blank_fallback_prompt_is_rejected(plugin: Path):
    """空白だけの指定は PromptFileDefinition.path と同じく弾く。"""
    with pytest.raises(ValueError, match="空白だけ"):
        SkillDefinition(plugin=str(plugin), skill="demo", fallback_prompt="   ")


def test_fallback_prompt_expands_home_shorthand(plugin: Path):
    definition = SkillDefinition(
        plugin=str(plugin), skill="demo", fallback_prompt="~/x.md"
    )
    assert not definition.fallback_prompt.startswith("~")


def test_skill_refs_and_empty_skills_cannot_coexist(plugin: Path):
    """無効化手段の記述を固定する。`skills: []` は併記できない (pitfall #11)。"""
    with pytest.raises(ValueError, match="同時に指定できません"):
        ModelPreset(provider="cursor", skill_refs=["demo"], skills=[])


def test_skills_all_does_not_suppress_the_fallback(plugin: Path):
    """fallback は skill_refs を見るため、skills: "all" 併記でも前置は起きる。"""
    translated = translate_runtime_config(
        _config(plugin),
        ModelPreset(provider="cursor", skill_refs=["demo"], skills="all"),
    )
    assert "# Demo" in translated.instructions


def test_non_utf8_fallback_fails_loudly(plugin: Path, tmp_path: Path):
    """encoding の誤りは resolve_prompt_texts と同じく loud に落とす。

    path の綴り誤りは黙ってスキップ (prompt_file_refs と同じ寛容さ) だが、
    encoding の誤りは黙って劣化させる価値が無い。
    """
    broken = tmp_path / "sjis.md"
    broken.write_bytes("日本語".encode("cp932"))
    cfg = _config(plugin, fallback_prompt=str(broken))
    with pytest.raises(ValueError, match="UTF-8"):
        resolve_skill_fallback_texts(cfg.skill_definitions, ["demo"])


def test_inline_plugin_survives_even_when_it_equals_a_handled_skill_plugin(
    plugin: Path,
):
    """inline plugins は skill_refs 由来ではないので、path が重なっても残す。"""
    translated = translate_runtime_config(
        _config(plugin),
        ModelPreset(provider="cursor", skill_refs=["demo"], plugins=[str(plugin)]),
    )
    assert "# Demo" in translated.instructions
    assert translated.plugin_paths == (str(plugin),)
