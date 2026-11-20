"""SDK 宣言・lock・実際のテスト環境が同じ依存セットを使うことを確認する。"""

from importlib.metadata import version
from pathlib import Path
import tomllib

from packaging.requirements import Requirement
import pytest


ROOT = Path(__file__).resolve().parents[1]
SDK_PACKAGES = ("claude-agent-sdk", "cursor-sdk", "openai-codex")


@pytest.mark.parametrize("name", SDK_PACKAGES)
def test_locked_sdk_satisfies_project_dependency(name):
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    requirements = {requirement.name: requirement for requirement in map(Requirement, project["project"]["dependencies"])}
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    locked = {package["name"]: package["version"] for package in lock["package"]}
    assert locked[name] in requirements[name].specifier


@pytest.mark.parametrize("name", (*SDK_PACKAGES, "openai-codex-cli-bin"))
def test_installed_sdk_matches_lock(name):
    """uv run の再解決や古い venv で、検証対象の SDK がすり替わるのを防ぐ。"""
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    locked = {package["name"]: package["version"] for package in lock["package"]}
    assert version(name) == locked[name]


def test_codex_sdk_and_bundled_cli_versions_match():
    assert version("openai-codex-cli-bin") == version("openai-codex")
