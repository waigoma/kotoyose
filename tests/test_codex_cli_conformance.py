"""実 pinned CLI と偽 Responses API。実 DNS/upstream/model/資格情報を使用しない。"""

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from kotoyose.execution_jobs import WorkerResult
from kotoyose.native_codex import NativeCodexWorker
from tests.codex_fake_api import COMMAND, fake_api
from tests.test_native_codex import job, provider  # noqa: F401
from tests.codex_runtime_fixture import codex_rootfs  # noqa: F401
from tests.codex_subscription_fixture import subscription_job
from kotoyose import codex_egress


BINARY = os.environ.get("KOTOYOSE_CODEX_CONFORMANCE_BINARY")
pytestmark = pytest.mark.skipif(not BINARY, reason="Explicit reviewed 0.170.0 CLI fixture required")


@pytest.mark.parametrize("effort", [None, "max"])
@pytest.mark.parametrize("tool", [False, True])
@pytest.mark.parametrize("authentication", ["subscription", "api"])
async def test_real_cli_uses_only_admitted_provider_auth_model_and_dynamic_tools(provider, tmp_path, monkeypatch, tool, authentication, effort):
    callback = AsyncMock(return_value=WorkerResult("succeeded", 0, b"ok", b"", "confirmed", "completed"))
    credentials = subscription_job(tmp_path) if authentication == "subscription" else {"credential_value": "fake-provider-key"}
    monkeypatch.setenv("OPENAI_API_KEY", "host-api-key-must-never-be-selected")
    async with fake_api(monkeypatch, tool=tool) as calls:
        result = await NativeCodexWorker().execute(job(tmp_path, cli_binary=Path(BINARY),
            **credentials, instructions="Follow the supplied fixture transcript.", reasoning_effort=effort,
            tool_refs=("builtin:sandbox-exec@1",) if tool else (), max_turns=4, timeout_seconds=8,
            output_limit_bytes=65536), asyncio.Event(), callback, on_process_started=AsyncMock())
    assert (result.status, result.content, result.stop_confirmation) == ("succeeded", b"fixture answer", "confirmed"), result
    assert len(calls) == (2 if tool else 1)
    for request, body in calls:
        if authentication == "subscription":
            from kotoyose.codex_subscription import read_subscription
            token = read_subscription(credentials["codex_auth_file"]).access_token
            assert request.startswith(b"POST /backend-api/codex/responses HTTP/1.1\r\n")
            assert b"host: chatgpt.com\r\n" in request and b"chatgpt-account-id: fixture-account\r\n" in request
            assert f"authorization: Bearer {token}\r\n".encode() in request
            assert token not in str(body) and token.encode() not in result.content
        else:
            assert request.startswith(b"POST /v1/responses HTTP/1.1\r\n")
            assert b"host: api.openai.com\r\n" in request
            assert b"authorization: Bearer fake-provider-key\r\n" in request
        assert b"host-api-key-must-never-be-selected" not in request
        assert b"responses-lite" not in request and b"x-codex" not in request
        assert body["model"] == "gpt-6-luna" and "client_metadata" not in body
        assert body["stream"] is True and body["store"] is False
        assert body["reasoning"]["effort"] == (effort or "low")
        tools = [{key: item[key] for key in ("type", "name") if key in item} for item in body["tools"]]
        assert tools == ([{"type": "function", "name": "sandbox_exec"}] if tool else [])
        assert not any(item["type"] == "additional_tools" for item in body["input"])
    if tool:
        callback.assert_awaited_once_with(tuple(COMMAND), ".")
        outputs = [item for item in calls[1][1]["input"] if item["type"] == "function_call_output"]
        assert len(outputs) == 1 and outputs[0]["call_id"] == "call_fixture"
        assert "ok" in str(outputs[0]["output"])
    else:
        callback.assert_not_awaited()


@pytest.mark.parametrize("status,reason", [(401, "authentication_failed"), (429, "subscription_limit_reached")])
async def test_real_cli_stops_at_subscription_auth_or_quota_failure(provider, tmp_path, monkeypatch, status, reason):
    credentials = subscription_job(tmp_path)
    callback = AsyncMock()
    paid = AsyncMock(side_effect=AssertionError("paid fallback"))
    async with fake_api(monkeypatch, tool=False, status=status) as calls:
        monkeypatch.setattr(codex_egress, "connect_endpoint", paid)
        result = await NativeCodexWorker().execute(job(tmp_path, cli_binary=Path(BINARY), **credentials,
            timeout_seconds=8, output_limit_bytes=65536), asyncio.Event(), callback, on_process_started=AsyncMock())
    assert (result.status, result.reason, result.stop_confirmation) == ("failed", reason, "confirmed"), result
    assert len(calls) == 1
    callback.assert_not_awaited()
    paid.assert_not_awaited()


async def test_real_cli_never_receives_reflected_subscription_secret(provider, tmp_path, monkeypatch, caplog):
    from kotoyose.codex_subscription import read_subscription
    credentials = subscription_job(tmp_path)
    token = read_subscription(credentials["codex_auth_file"]).access_token
    async with fake_api(monkeypatch, tool=False, reflected_secret=token):
        result = await NativeCodexWorker().execute(job(tmp_path, cli_binary=Path(BINARY), **credentials,
            timeout_seconds=8, output_limit_bytes=65536), asyncio.Event(), AsyncMock(), on_process_started=AsyncMock())
    assert (result.status, result.content, result.stop_confirmation) == ("succeeded", b"[redacted]", "confirmed"), result
    assert token not in repr(result) + caplog.text


async def test_real_cli_rereads_revoked_login_before_tool_followup(provider, tmp_path, monkeypatch):
    from tests.codex_subscription_fixture import subscription_auth
    from kotoyose.execution_jobs import WorkerResult
    import time
    credentials = subscription_job(tmp_path)
    authorize = AsyncMock()
    paid = AsyncMock(side_effect=AssertionError("paid fallback"))

    async def tool(argv, cwd):
        subscription_auth(tmp_path, expiry=time.time() - 1)
        return WorkerResult("succeeded", 0, b"ok", b"", "confirmed", "completed")

    callback = AsyncMock(side_effect=tool)
    async with fake_api(monkeypatch, tool=True) as calls:
        monkeypatch.setattr(codex_egress, "connect_endpoint", paid)
        result = await NativeCodexWorker().execute(job(tmp_path, cli_binary=Path(BINARY), **credentials,
            provider_authorize=authorize, tool_refs=("builtin:sandbox-exec@1",), max_turns=4,
            timeout_seconds=8, output_limit_bytes=65536), asyncio.Event(), callback, on_process_started=AsyncMock())
    assert (result.status, result.reason, result.stop_confirmation) == ("failed", "authentication_failed", "confirmed"), result
    assert len(calls) == 1 and authorize.await_count == 4  # 接続前と DNS/TLS 後の両 gate。
    callback.assert_awaited_once()
    paid.assert_not_awaited()
