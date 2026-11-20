"""共有 vector を真値として TaskHub と引数の正準表現を固定する。"""

import json
from pathlib import Path

import pytest

from kotoyose.client_tool_digest import (
    client_tool_arguments_canonical,
    client_tool_arguments_digest,
)


_VECTORS = json.loads(
    (Path(__file__).parent / "fixtures" / "client_tool_arguments_digest_vectors.json").read_text(
        encoding="utf-8"
    )
)


@pytest.mark.parametrize("vector", _VECTORS["valid"], ids=lambda vector: vector["name"])
def test_client_tool_arguments_match_valid_vectors(vector):
    assert client_tool_arguments_canonical(vector["arguments"]) == vector["canonical"]
    assert client_tool_arguments_digest(vector["arguments"]) == vector["sha256"]


@pytest.mark.parametrize("vector", _VECTORS["invalid"], ids=lambda vector: vector["name"])
def test_client_tool_arguments_reject_invalid_vectors(vector):
    # JSON で表現できない値は、共有 fixture 内の Python 式だけから再現する。
    value = eval(vector["construct"]["python"], {"__builtins__": {}, "float": float})
    with pytest.raises(ValueError):
        client_tool_arguments_canonical(value)
    with pytest.raises(ValueError):
        client_tool_arguments_digest(value)
