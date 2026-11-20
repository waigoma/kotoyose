"""Each configured provider has a corresponding runtime adapter."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROVIDERS = ("claude", "codex", "cursor", "antigravity")


def test_provider_list_agrees_with_runtime_adapters():
    literal = re.search(
        r'provider: Literal\[([^\]]*)\] = "claude"',
        (ROOT / "src/kotoyose/config.py").read_text(encoding="utf-8"),
    )
    assert literal, "ModelPreset.provider の Literal が見つかりません"
    declared = set(re.findall(r'"([^"]+)"', literal.group(1)))
    assert declared == set(PROVIDERS), "テスト側の PROVIDERS が古びています"

    # runtime adapter modules
    adapters = {
        path.stem
        for path in (ROOT / "src/kotoyose/runtime").glob("*.py")
        if path.stem not in {"__init__", "base", "translation"}
    }
    assert declared <= adapters, f"adapter が無い Provider: {declared - adapters}"
