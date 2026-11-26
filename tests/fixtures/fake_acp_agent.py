"""Local ACP test double. All state and side effects are confined to the supplied cwd."""

import json
import os
from pathlib import Path
import subprocess
import sys

SCENARIO = sys.argv[1] if len(sys.argv) > 1 else "normal"
TRANSCRIPT = Path("acp-requests.jsonl")
SID = "fake-session"


def send(**message):
    print(json.dumps({"jsonrpc": "2.0", **message}), flush=True)


def update(**data):
    send(method="session/update", params={"sessionId": SID, "update": data})


def chunk(text):
    update(sessionUpdate="agent_message_chunk", content={"type": "text", "text": text})


def reply(msg, **result):
    send(id=msg["id"], result=result)


def record(message):
    with TRANSCRIPT.open("a") as stream:
        stream.write(json.dumps(message) + "\n")


def receive():
    line = sys.stdin.readline()
    if not line:
        raise SystemExit(0)
    msg = json.loads(line)
    record(msg)
    return msg


def prompt(msg):
    if SCENARIO == "oversize":
        print("x" * (1024 * 1024 + 1), flush=True)
        return
    if SCENARIO == "oversize_flood":
        while True:
            print("x" * (1024 * 1024 + 1), flush=True)
    chunk("hello ")
    if SCENARIO == "stray_response":
        # Grok Build 1.0.89 は自分の内部要求への応答を文字列 id で stdout に流す。
        send(id="skills-reload", result={"result": {"reloaded": 1}})
    if SCENARIO == "crash":
        sys.exit(2)
    if SCENARIO == "helper":
        helper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        Path("helper.pid").write_text(str(helper.pid))
        sys.exit(2)
    if SCENARIO == "cancel":
        notification = receive()
        assert notification == {
            "jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": SID},
        }
        reply(msg, stopReason="cancelled")
        return
    send(id="fs", method="fs/read_text_file", params={"sessionId": SID, "path": "/ignore"})
    assert receive()["error"]["code"] == -32601
    update(sessionUpdate="tool_call", toolCallId="1", title="Read", kind="read", status="pending")
    for name, kind in (("Read", "read"), ("Bash", "execute")):
        send(id=name, method="session/request_permission", params={
            "sessionId": SID,
            "toolCall": {"toolCallId": "1"} if name == "Read" else {"toolCallId": "2", "title": name, "kind": kind},
            "options": [{"kind": "allow_once", "optionId": "yes", "name": "Allow"},
                        {"kind": "reject_once", "optionId": "no", "name": "Reject"}],
        })
        if SCENARIO == "approval_updates" and name == "Read":
            chunk("waiting ")
        decision = receive()
        if decision.get("method") == "session/cancel":
            reply(msg, stopReason="cancelled")
            return
        assert decision["id"] == name
        assert decision["result"]["outcome"]["outcome"] == "selected"
    update(sessionUpdate="tool_call_update", toolCallId="1", status="completed")
    update(sessionUpdate="usage_update", used=50, size=500)
    chunk("world")
    reply(msg, stopReason="end_turn", usage={"inputTokens": 10, "outputTokens": 2})


def select_option(config_id, current, choices, *, category=None, grouped=False):
    options = [{"value": value, "name": name} for value, name in choices]
    if grouped:
        options = [{"group": "choices", "name": "Choices", "options": options}]
    return {"id": config_id, "type": "select", "name": config_id, "category": category,
            "currentValue": current, "options": options}


def session_options():
    mode = select_option("mode", "ask", [("ask", "Ask")], category="mode")
    if SCENARIO.startswith("agy_"):
        models = {"currentModelId": "gemini-4-neon-medium", "availableModels": [
            {"modelId": "gemini-4-neon-medium", "name": "Neon Medium"},
            {"modelId": "gemini-4-hydrogen-high", "name": "Hydrogen High"},
        ]}
        return [
            select_option("model", "gemini-4-neon", [
                ("gemini-4-neon", "Neon"), ("gemini-4-hydrogen", "Hydrogen"),
            ], category="model", grouped=True),
            select_option("thought_level", "high", [
                (level, level.title()) for level in ("high", "xhigh", "medium", "low")
            ], category="thought_level"),
            mode,
        ], models
    if SCENARIO.startswith("cursor_"):
        choices = [("default[]", "Auto"), ("grok-4.6[effort=high,fast=true]", "grok-4.6")]
        if SCENARIO == "cursor_ambiguous":
            choices[1] = (choices[1][0], "AUTO")
        return [select_option("model", choices[0][0], choices), mode], {
            "currentModelId": choices[0][0],
            "availableModels": [{"modelId": value, "name": name} for value, name in choices],
        }
    return [select_option("model", "default", [("model-x", "Test")], category="model")], None


def main():
    authenticated = False
    while True:
        msg = receive()
        method = msg["method"]
        params = msg["params"]
        if method == "initialize":
            assert params["protocolVersion"] == 1
            assert params["clientCapabilities"] == {
                "fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False,
            }
            if SCENARIO == "grok_mcp_initialize_only":
                send(method="_x.ai/mcp/servers_updated", params={"sessionId": SID, "mcpServers": []})
            reply(
                msg, protocolVersion=2 if SCENARIO == "protocol" else 1,
                agentCapabilities={
                    "loadSession": SCENARIO != "no_load",
                    "mcpCapabilities": {"http": SCENARIO != "no_http"},
                },
                authMethods=[{"id": "local-home", "name": "Home"}] if SCENARIO == "auth" else [],
            )
        elif method == "authenticate":
            assert params == {"methodId": "local-home"}
            authenticated = True
            reply(msg)
        elif method in {"session/new", "session/load"}:
            assert os.path.isabs(params["cwd"])
            assert isinstance(params["mcpServers"], list)
            if SCENARIO == "auth" and not authenticated:
                send(id=msg["id"], error={"code": -32000, "message": "Login required"})
                continue
            config, models = session_options()
            state = {"configOptions": config}
            if models is not None:
                state["models"] = models
            if method == "session/load":
                assert params["sessionId"] == SID
                chunk("prior context must not be emitted")
                reply(msg, **state)
            else:
                reports = {
                    "grok_mcp_empty": {"sessionId": SID, "mcpServers": []},
                    "grok_mcp_empty_request": {"sessionId": SID, "mcpServers": []},
                    "grok_mcp_empty_unscoped": {"mcpServers": []},
                    "grok_mcp_empty_other_session": {"sessionId": "previous", "mcpServers": []},
                    "grok_mcp_native": {"sessionId": SID, "mcpServers": [{"name": "native-files"}, {"name": "native-db"}]},
                    "grok_mcp_native_request": {"sessionId": SID, "mcpServers": [{"name": "native-files"}]},
                    "grok_mcp_invalid": {"sessionId": SID, "mcpServers": None},
                }
                if SCENARIO.endswith("_request") and SCENARIO in reports:
                    send(id="mcp-update", method="_x.ai/mcp/servers_updated", params=reports[SCENARIO])
                    assert receive() == {"jsonrpc": "2.0", "id": "mcp-update", "result": {}}
                elif SCENARIO in reports:
                    send(method="_x.ai/mcp/servers_updated", params=reports[SCENARIO])
                reply(msg, sessionId=SID, **state)
        elif method == "session/set_model":
            assert params["sessionId"] == SID
            assert params["modelId"] in {item["modelId"] for item in models["availableModels"]}
            if SCENARIO.endswith("_fallback"):
                send(id=msg["id"], error={"code": -32601, "message": "Method not found"})
            elif SCENARIO.endswith("_error"):
                send(id=msg["id"], error={"code": -32602, "message": "Model unavailable"})
            else:
                models["currentModelId"] = params["modelId"]
                reply(msg)
        elif method == "session/set_config_option":
            assert params.keys() == {"sessionId", "configId", "value"}
            assert params["sessionId"] == SID
            option = next(item for item in config if item["id"] == params["configId"])
            choices = option["options"]
            if "group" in choices[0]:
                choices = [choice for group in choices for choice in group["options"]]
            assert params["value"] in {choice["value"] for choice in choices}
            if SCENARIO == "config_error":
                send(id=msg["id"], error={"code": -32602, "message": "Config unavailable"})
                continue
            if SCENARIO != "config_unconfirmed":
                option["currentValue"] = params["value"]
            if SCENARIO.startswith("agy_") and option["id"] == "model":
                config[1]["currentValue"] = "medium"
            if SCENARIO == "agy_reset_model" and option["id"] == "thought_level":
                config[0]["currentValue"] = "gemini-4-neon"
            reply(msg, configOptions=config)
        elif method == "session/prompt":
            assert params["sessionId"] == SID
            assert params["prompt"][0]["type"] == "text"
            prompt(msg)
        elif method == "session/cancel":
            pass
        else:
            raise AssertionError(method)


if __name__ == "__main__":
    main()
