"""Memory Gateway の active example 契約を固定する回帰テスト。"""

from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_active_examples_do_not_reference_retired_memory_service():
    """利用者がコピーする設定へ旧 8100 endpoint を再混入させない。"""
    for relative_path in (".env.example", "config.example.yml", "tests/fixtures/public-recipes.yml"):
        content = (REPO_ROOT / relative_path).read_text()
        assert "127.0.0.1:8100" not in content
        assert "/memory/log" not in content


def test_memory_resource_uses_gateway_endpoint_and_authorization():
    """個別 MCP リソース memory_gateway が末尾 slash 付き MCP URL と Bearer header を例示する。

    最小スターター (config.example.yml) ではなく、設定例を含む
    tests/fixtures/public-recipes.yml を参照する。
    """
    config = yaml.safe_load((REPO_ROOT / "tests/fixtures/public-recipes.yml").read_text())
    gateway = config["mcps"]["memory_gateway"]["server"]

    assert gateway["url"] == "http://127.0.0.1:8101/mcp/"
    assert gateway["headers"]["Authorization"].startswith("Bearer ")
