"""Each configured provider has a corresponding runtime adapter."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROVIDER_ADAPTERS = {
    "claude": "claude",
    "codex": "codex",
    "cursor": "cursor",
    "antigravity": "antigravity_sdk",
    "cli_acp": "acp",
    "print_cli": "print_cli",
}


def test_provider_list_agrees_with_runtime_adapters():
    literal = re.search(
        r'provider: Literal\[([^\]]*)\] = "claude"',
        (ROOT / "src/kotoyose/config.py").read_text(encoding="utf-8"),
    )
    assert literal, "ModelPreset.provider の Literal が見つかりません"
    declared = set(re.findall(r'"([^"]+)"', literal.group(1)))
    assert declared == PROVIDER_ADAPTERS.keys(), "テスト側の PROVIDER_ADAPTERS が古びています"

    # runtime adapter modules
    adapters = {
        path.stem
        for path in (ROOT / "src/kotoyose/runtime").glob("*.py")
        if path.stem not in {"__init__", "base", "translation"}
    }
    missing = {provider for provider, adapter in PROVIDER_ADAPTERS.items() if adapter not in adapters}
    assert not missing, f"adapter が無い Provider: {missing}"
