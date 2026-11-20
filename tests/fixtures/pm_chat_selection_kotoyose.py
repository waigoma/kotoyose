"""Consume an actual synthetic Hub serializer output through real Kotoyose chat.

Example (from repository root):
  PYTHONPATH=src python tests/fixtures/pm_chat_selection_kotoyose.py \
    --request /path/to/ordinary-pm-chat-request.json --output /tmp/paired.json

This does not start an HTTP listener, database, SDK subprocess or provider.
The SDK and model boundaries are explicit fakes; forbidden boundaries count
attempts and raise. Only ASGI in-process requests are permitted.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import os
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, Mock, patch

from httpx import ASGITransport, AsyncClient

from kotoyose import config as config_module, session
from kotoyose.app import app
from kotoyose.config import HarnessConfig
from kotoyose.routes import chat


async def run_contract(wire: dict) -> dict:
    assert wire["url"] == "http://127.0.0.1:1/v1/chat/completions", "Synthetic serializer target only"
    assert wire["method"] == "POST"
    assert wire["headers"]["Authorization"] == "Bearer fixture-kotoyose-token"
    assert wire["headers"]["X-Pm-Mcp-Token"] == "fixture-scoped-pm-token"
    assert wire["body"]["model"] == "neutral-registered-alias"
    assert wire["body"]["stream"] is True
    clients = []
    calls = {"fake_sdk_connects": 0, "fake_model_streams": 0, "provider_calls": 0, "network_calls": 0}

    def forbidden(kind):
        def fail(*args, **kwargs):
            calls[kind] += 1
            raise AssertionError("Forbidden external boundary: " + kind)
        return fail

    def build_sdk(*, options):
        client = AsyncMock()
        client.options = options

        async def connect():
            calls["fake_sdk_connects"] += 1

        client.connect.side_effect = connect
        clients.append(client)
        return client

    async def fake_stream(client, prompt):
        calls["fake_model_streams"] += 1
        yield "synthetic paired answer"
        yield (None, None, "fixture-session-id")

    with TemporaryDirectory(prefix="pm-chat-contract-") as temporary, ExitStack() as stack:
        root = Path(temporary)
        (root / "pm.txt").write_text("Synthetic TaskHub PM instructions.")
        (root / "memory.txt").write_text("Synthetic Memory Gateway instructions.")
        config = HarnessConfig.model_validate({
            "models": {"neutral-registered-alias": {
                "route": "existing-owner-route", "cwd": str(root),
                "tools": ["Read", "Grep", "Agent"], "mcp_refs": ["memory-taskhub"],
                "prompt_file_refs": ["taskhub-pm", "memory-gateway-discipline"],
                "required_request_headers": ["X-Pm-Mcp-Token"], "skills": ["fixture-skill"],
                "system_prompt_snapshot": False,
            }},
            "routes": {"existing-owner-route": {
                "driver": "claude.agent-sdk", "model": "synthetic/pm-model", "subscription_only": True,
                "endpoint": "https://synthetic-router.invalid/v1",
                "data_policy": "no_training",
                "credential": "env:SYNTHETIC_PAIR_TOKEN",
            }},
            "mcps": {"memory-taskhub": {"server": {
                "type": "http", "url": "https://synthetic-memory.invalid/mcp",
                "headers": {"Authorization": "Bearer {{header:X-Pm-Mcp-Token}}"},
            }}},
            "prompt_files": {"taskhub-pm": {"path": str(root / "pm.txt")},
                             "memory-gateway-discipline": {"path": str(root / "memory.txt")}},
        })
        stack.enter_context(patch.dict(os.environ, {"SYNTHETIC_PAIR_TOKEN": "fixture-route-token"}, clear=True))
        stack.enter_context(patch.object(config_module, "_config", config))
        stack.enter_context(patch("kotoyose.auth.API_KEY", None))
        stack.enter_context(patch("kotoyose.session_store.get_default_store", return_value=None))
        stack.enter_context(patch.object(session, "ClaudeSDKClient", side_effect=build_sdk))
        stack.enter_context(patch.object(session, "lookup_session", AsyncMock(return_value=None)))
        stack.enter_context(patch.object(session, "build_options", Mock(side_effect=AssertionError("Second resolver"))))
        stack.enter_context(patch.object(chat, "stream_claude", fake_stream))
        stack.enter_context(patch.object(chat, "ask_claude", AsyncMock(side_effect=AssertionError("Unexpected non-stream transport"))))
        for name in ["record_received", "record_completed", "record_failed", "_safe_upsert_session", "_rotate_hash_key"]:
            stack.enter_context(patch.object(chat, name, AsyncMock(return_value=1)))
        stack.enter_context(patch.object(chat, "start_event_writer", lambda *args: asyncio.Queue()))
        stack.enter_context(patch("socket.create_connection", forbidden("network_calls")))
        stack.enter_context(patch("urllib.request.urlopen", forbidden("network_calls")))
        stack.enter_context(patch("subprocess.Popen", forbidden("provider_calls")))
        stack.enter_context(patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=forbidden("provider_calls"))))
        stack.enter_context(patch("asyncio.create_subprocess_shell", AsyncMock(side_effect=forbidden("provider_calls"))))
        session.sessions.clear()
        session.session_cache_identities.clear()
        session.session_last_used.clear()
        try:
            async with AsyncClient(transport=ASGITransport(app), base_url="http://synthetic.invalid") as client:
                async def send(body):
                    return await client.post("/v1/chat/completions", json=body, headers=wire["headers"])

                first = await send(wire["body"])
                assert first.status_code == 200, first.text
                assert first.headers["X-Session-Event"] == "created"
                assert "data: [DONE]" in first.text and "synthetic paired answer" in first.text
                assert len(clients) == 1
                options = clients[0].options
                assert options.model == "synthetic/pm-model"
                assert options.env["ANTHROPIC_BASE_URL"] == "https://synthetic-router.invalid/v1"
                assert options.env["ANTHROPIC_API_KEY"] == "fixture-route-token"
                assert options.env["ANTHROPIC_AUTH_TOKEN"] == options.env["CLAUDE_CODE_OAUTH_TOKEN"] == ""
                assert options.fallback_model is None and options.tools == ["Read", "Grep", "Agent"]
                assert options.skills == ["fixture-skill"]
                assert options.mcp_servers["memory-taskhub"]["headers"]["Authorization"] == "Bearer fixture-scoped-pm-token"
                assert options.system_prompt["append"] == "Synthetic TaskHub PM instructions.\nSynthetic Memory Gateway instructions."
                assert options.system_prompt["snapshot"] is False
                cached = await send(wire["body"])
                assert cached.status_code == 200 and cached.headers["X-Session-Event"] == "cache_hit"
                assert len(clients) == 1
                config.routes["existing-owner-route"].endpoint = "https://changed-router.invalid/v1"
                changed = await send(wire["body"])
                assert changed.status_code == 200 and changed.headers["X-Session-Event"] == "created"
                assert len(clients) == 2
                clients[0].disconnect.assert_awaited_once()
                config.routes["existing-owner-route"].subscription_only = False
                removed = await send(wire["body"])
                assert removed.status_code == 501 and removed.json()["error"]["reason_code"] == "billing_policy_unavailable"
                missing_body = copy.deepcopy(wire["body"])
                missing_body["model"] = "unregistered-fixture-alias"
                missing = await send(missing_body)
                assert missing.status_code == 422 and missing.json()["error"]["reason_code"] == "pm_saved_preset_not_found"
                relaxed_body = copy.deepcopy(wire["body"])
                relaxed_body["metadata"]["kotoyose"]["pm_saved_preset"]["metered_fallback"] = "allowed"
                relaxed = await send(relaxed_body)
                assert relaxed.status_code == 422
                assert len(clients) == 2 and calls == {
                    "fake_sdk_connects": 2, "fake_model_streams": 3, "provider_calls": 0, "network_calls": 0,
                }
                return {"status": "passed", "transport": "real Kotoyose ASGI parser/route/session with fake SDK/model transport",
                        "results": {"first": first.status_code, "cache_hit": cached.status_code,
                                    "changed_route": changed.status_code, "policy_removed": removed.status_code,
                                    "missing_alias": missing.status_code, "relaxed_requirement": relaxed.status_code},
                        "preserved": ["tools", "MCP header", "memory and PM prompt files", "skills", "SSE", "session-event context repair"],
                        **calls, "upstream_billing_verified": False, "live_chat_sent": False}
        finally:
            session.sessions.clear()
            session.session_cache_identities.clear()
            session.session_last_used.clear()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    raw = args.request.read_bytes()
    result = asyncio.run(run_contract(json.loads(raw)))
    result["input_sha256"] = hashlib.sha256(raw).hexdigest()
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
