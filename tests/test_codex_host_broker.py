"""Host builtin request policy; no sockets, authentication, or upstream connections."""

import copy
import json
from pathlib import Path

import pytest

from kotoyose.codex_coding_policy import validate_builtin_tools
from kotoyose.codex_egress import CodexEgress
from kotoyose.codex_request_policy import validate_input


def tools():
    return [
        {"type": "function", "name": "exec_command", "strict": False, "parameters": {
            "type": "object", "properties": {"cmd": {"type": "string"}, "workdir": {"type": "string"}},
            "required": ["cmd"], "additionalProperties": False}},
        {"type": "function", "name": "write_stdin", "strict": False, "parameters": {
            "type": "object", "properties": {"session_id": {"type": "number"}, "chars": {"type": "string"}},
            "required": ["session_id"], "additionalProperties": False}},
        {"type": "custom", "name": "apply_patch", "format": {"type": "grammar", "syntax": "lark",
            "definition": Path(__file__).with_name("fixtures").joinpath("codex_apply_patch_0_160_0.lark").read_text()}},
    ]


def test_reviewed_direct_builtin_surface():
    validate_builtin_tools(tools())


@pytest.mark.parametrize("mutation", ["extra", "duplicate", "legacy", "namespace", "grammar", "schema", "required", "hosted"])
def test_tool_surface_escalation_fails_closed(mutation):
    value = tools()
    if mutation == "extra": value.append({"type": "function", "name": "web_search"})
    elif mutation == "duplicate": value[1] = copy.deepcopy(value[0])
    elif mutation == "legacy": value[0]["name"] = "sandbox_exec"
    elif mutation == "namespace": value[0] = {"type": "namespace", "name": "exec_command", "tools": []}
    elif mutation == "grammar": value[2]["format"]["definition"] += "\nunknown: /.+/\n"
    elif mutation == "schema": value[0]["parameters"]["properties"]["environment_id"] = {"type": "string"}
    elif mutation == "required": value[0]["parameters"]["required"] = []
    elif mutation == "hosted": value[0]["type"] = "computer"
    with pytest.raises(ValueError, match="egress_denied"):
        validate_builtin_tools(value)


def test_builtin_history_keeps_only_the_reviewed_tool_kinds():
    history = [
        {"type": "function_call", "name": "exec_command", "call_id": "c1", "arguments": '{"cmd":"true"}'},
        {"type": "function_call_output", "call_id": "c1", "output": "completed"},
        {"type": "custom_tool_call", "name": "apply_patch", "call_id": "c2", "input": "patch"},
        {"type": "custom_tool_call_output", "call_id": "c2", "output": "applied"},
    ]
    validate_input(history, True, coding=True)
    with pytest.raises(ValueError):
        validate_input(history, True)
    for item in [
        {"type": "function_call", "name": "sandbox_exec", "arguments": "{}"},
        {"type": "custom_tool_call", "name": "other", "call_id": "c", "input": "x"},
        {"type": "additional_tools", "tools": []},
        {"type": "message", "role": "user", "content": [{"type": "input_image", "image_url": "https://private/", "detail": "high"}]},
    ]:
        with pytest.raises(ValueError):
            validate_input([item], True, coding=True)


def test_host_coding_has_no_api_key_fallback(tmp_path):
    with pytest.raises(ValueError, match="invalid_job"):
        CodexEgress(1, 4096, "gpt-6-luna", "fake", None, coding=True)
    broker = CodexEgress(1, 65536, "gpt-6-luna", "fake", None, auth_file=tmp_path / "synthetic-auth",
                         auth_identity="fixture", reasoning_effort="max", coding=True)
    try:
        payload = {"model": "gpt-6-luna", "stream": True, "store": False, "reasoning": {"effort": "max"},
                   "tools": tools(), "input": [], "service_tier": "auto"}
        request = b"POST /v1/responses HTTP/1.1\r\nauthorization: Bearer fake\r\n\r\n"
        assert json.loads(broker.validate_request(request, json.dumps(payload).encode())) == payload
        for change in ({"model": "different"}, {"service_tier": "priority"}, {"service_tier": "fast"},
                       {"reasoning": {"effort": "low"}}, {"tools": []}):
            with pytest.raises(ValueError):
                broker.validate_request(request, json.dumps({**payload, **change}).encode())
    finally:
        broker.directory.cleanup()
