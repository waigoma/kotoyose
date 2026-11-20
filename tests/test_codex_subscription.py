"""サブスク方式・更新/失効・宛先・秘密越境の decision/state matrix。"""

import asyncio
import json
from pathlib import Path
import time
from unittest.mock import AsyncMock

import pytest

from kotoyose import codex_egress
from kotoyose.codex_egress import CodexEgress, parse_request
from kotoyose.codex_subscription import read_subscription
from kotoyose.execution_resources import ExecutionError, NativeRuntimeProfile
from kotoyose.native_codex import NativeCodexWorker
from kotoyose.provider_jail import provider_environment
from tests.codex_fake_api import fake_api
from tests.codex_subscription_fixture import subscription_auth, subscription_job
from tests.test_codex_egress import header
from tests.test_native_codex import job, provider  # noqa: F401
from tests.codex_runtime_fixture import codex_rootfs  # noqa: F401


def test_subscription_snapshot_uses_identity_and_never_contains_secrets(tmp_path):
    path, token = subscription_auth(tmp_path)
    auth = read_subscription(path)
    assert len(auth.identity) == 64 and auth.access_token == token
    assert token not in repr(auth) and auth.account_id not in repr(auth)
    assert not hasattr(auth, "refresh_token") and not hasattr(auth, "id_token")
    _, rotated = subscription_auth(tmp_path, token_label="rotated")
    assert rotated != token and read_subscription(path).identity == auth.identity


def test_safe_atomic_same_account_renewal_preserves_binding(tmp_path):
    from kotoyose.execution_resources import codex_authentication_binding, content_digest
    path, token = subscription_auth(tmp_path)
    identity = read_subscription(path).identity
    original = path.stat().st_ino
    renewed = tmp_path / "renewed"
    renewed.mkdir()
    replacement, new_token = subscription_auth(renewed, token_label="renewed")
    replacement.replace(path)
    assert path.stat().st_ino != original
    assert new_token != token and read_subscription(path).identity == identity
    binding = {"credential_mode": "chatgpt_subscription", "credential_name": "CODEX_CHATGPT_AUTH",
               "codex_auth_file": str(path), "codex_auth_identity": identity}
    public = codex_authentication_binding(binding)
    assert public["mode"] == "chatgpt_subscription" and len(public["binding_digest"]) == 64
    assert public["binding_digest"] == content_digest({"mode": "chatgpt_subscription", "account": identity})
    assert str(path) not in str(public) and token not in str(public) and new_token not in str(public)
    assert codex_authentication_binding({**binding, "codex_auth_identity": read_subscription(path).identity}) == public
    assert codex_authentication_binding({**binding, "codex_auth_file": "/different/auth.json"}) == public
    assert codex_authentication_binding({**binding, "credential_env": "DIFFERENT_ENV"}) == public
    assert codex_authentication_binding({**binding, "codex_auth_identity": "b" * 64}) != public


def test_parent_symlink_replacement_between_resolve_and_open_is_rejected(tmp_path, monkeypatch):
    parent = tmp_path / "source"
    parent.mkdir()
    path, _ = subscription_auth(parent)
    redirected = tmp_path / "redirected"
    redirected.mkdir()
    subscription_auth(redirected)
    original_resolve = Path.resolve

    def replace_after_resolve(self, *args, **kwargs):
        resolved = original_resolve(self, *args, **kwargs)
        if self == path:
            parent.rename(tmp_path / "original-source")
            parent.symlink_to(redirected, target_is_directory=True)
        return resolved

    monkeypatch.setattr(Path, "resolve", replace_after_resolve)
    with pytest.raises(ValueError, match="authentication_failed"):
        read_subscription(path)


@pytest.mark.parametrize("changes", [{"auth_mode": "apikey"}, {"auth_mode": "chatgptAuthTokens"},
    {"auth_mode": None}, {"OPENAI_API_KEY": "fake-paid-key"}, {"tokens": {}}, {"tokens": None},
    {"tokens": {"access_token": "invalid", "account_id": "fixture-account"}},
    {"tokens": {"access_token": "invalid", "account_id": "injected\r\nheader"}}])
def test_wrong_or_ambiguous_auth_fails_closed(tmp_path, changes, caplog):
    path, token = subscription_auth(tmp_path, **changes)
    with pytest.raises(ValueError, match="^authentication_failed$") as error:
        read_subscription(path)
    assert token not in str(error.value) + caplog.text
    assert "fake-paid-key" not in str(error.value) + caplog.text


@pytest.mark.parametrize("expiry", [time.time() - 1, time.time() + 1, True, "future", None])
def test_expired_or_unknown_token_requires_host_cli_renewal(tmp_path, expiry):
    path, _ = subscription_auth(tmp_path, expiry=expiry if expiry is not None else False)
    with pytest.raises(ValueError, match="authentication_failed"):
        read_subscription(path)


@pytest.mark.parametrize("mode", ["symlink", "public", "oversized", "duplicate", "directory"])
def test_untrusted_auth_source_is_rejected(tmp_path, mode):
    path, _ = subscription_auth(tmp_path)
    if mode == "symlink":
        alias = tmp_path / "alias"
        alias.symlink_to(path)
        path = alias
    elif mode == "public":
        path.chmod(0o644)
    elif mode == "oversized":
        path.write_bytes(b"x" * 65537)
    elif mode == "duplicate":
        path.write_bytes(b'{"auth_mode":"chatgpt","auth_mode":"apikey"}')
    else:
        path.unlink()
        path.mkdir()
    with pytest.raises(ValueError, match="authentication_failed"):
        read_subscription(path)


@pytest.mark.parametrize("changes", [{"credential_env": "OPENAI_API_KEY"}, {"credential_name": "OPENAI_API_KEY"},
    {"credential_mode": "environment", "credential_env": "OPENAI_API_KEY"},
    {"codex_auth_file": None}, {"codex_auth_file": Path("relative")}, {"provider": "claude"},
    {"base_url": "https://api.openai.com/v1"}])
def test_subscription_profile_never_implies_api_or_host_fallback(tmp_path, changes):
    values = {"provider": "codex", "model": "gpt-6-luna", "cli_binary": Path("/usr/bin/sh"),
        "credential_mode": "chatgpt_subscription", "credential_name": "CODEX_CHATGPT_AUTH", "codex_auth_file": tmp_path / "auth.json"}
    with pytest.raises(ValueError):
        NativeRuntimeProfile(**{**values, **changes})


def test_subscription_capability_environment_excludes_all_host_credentials():
    env = provider_environment({"KOTOYOSE_CODEX_BROKER_CAPABILITY": "ephemeral-fake", "CODEX_ACCESS_TOKEN": "host",
        "GH_TOKEN": "host", "HTTPS_PROXY": "host", "CODEX_HOME": "/host"}, provider="codex")
    assert set(env) == {"KOTOYOSE_CODEX_BROKER_CAPABILITY", "CODEX_HOME", "OPENAI_BASE_URL", "PATH", "HOME", "LANG"}
    with pytest.raises(ValueError):
        provider_environment({**env, "OPENAI_API_KEY": "fake-paid"}, provider="codex")


def test_broker_pins_identity_rereads_updates_and_never_forwards_capability(tmp_path):
    path, token = subscription_auth(tmp_path)
    broker = CodexEgress(1, 65536, "fixed", "capability", None, auth_file=path, auth_identity=read_subscription(path).identity)
    request, _ = parse_request(header(fields={"Authorization": "Bearer capability"}))
    try:
        forwarded = broker.subscription_request(request)
        assert b"POST /backend-api/codex/responses HTTP/1.1" in forwarded
        assert b"host: chatgpt.com" in forwarded and b"api.openai.com" not in forwarded
        assert token.encode() in forwarded and b"capability" not in forwarded
        assert b"false-refresh-secret" not in forwarded and b"false-id-secret" not in forwarded
        _, rotated = subscription_auth(tmp_path, token_label="rotated")
        assert rotated.encode() in broker.subscription_request(request)
        assert token.encode() not in broker.subscription_request(request)
        subscription_auth(tmp_path, account="other-account")
        with pytest.raises(ValueError, match="authentication_failed"):
            broker.subscription_request(request)
        assert broker.redact(token.encode() + rotated.encode() + b"fixture-account") == b"[redacted]" * 3
    finally:
        broker.directory.cleanup()


@pytest.mark.parametrize("connector", ["connect_endpoint", "connect_subscription_endpoint"])
async def test_subscription_and_api_reject_mixed_private_dns(monkeypatch, connector):
    resolve = AsyncMock(return_value=[(2, 1, 6, "", ("8.8.8.8", 443)), (2, 1, 6, "", ("192.168.1.1", 443))])
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    connect = AsyncMock()
    monkeypatch.setattr(asyncio, "open_connection", connect)
    with pytest.raises(ValueError):
        await getattr(codex_egress, connector)()
    assert resolve.call_args.args == ("chatgpt.com" if "subscription" in connector else "api.openai.com", 443)
    connect.assert_not_awaited()


async def test_subscription_tls_fixes_ip_sni_and_public_ca(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1")
    monkeypatch.setenv("SSL_CERT_FILE", "/private/cert")
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", AsyncMock(return_value=[(2, 1, 6, "", ("8.8.8.8", 443))]))
    connect = AsyncMock(return_value=("reader", "writer"))
    monkeypatch.setattr(asyncio, "open_connection", connect)
    assert await codex_egress.connect_subscription_endpoint() == ("reader", "writer")
    assert connect.call_args.args == ("8.8.8.8", 443)
    assert connect.call_args.kwargs["server_hostname"] == "chatgpt.com"
    assert connect.call_args.kwargs["ssl"].check_hostname


@pytest.mark.parametrize("changes", [{"credential_value": "fake-paid"}, {"codex_auth_file": None}, {"codex_auth_identity": None},
    {"credential_name": "OPENAI_API_KEY", "credential_value": "fake-paid"}])
async def test_subscription_job_cannot_select_api_implicitly(provider, tmp_path, changes):
    values = subscription_job(tmp_path)
    started = AsyncMock()
    result = await provider.execute(job(tmp_path, **{**values, **changes}), asyncio.Event(), AsyncMock(), on_process_started=started)
    assert (result.status, result.reason) == ("failed", "invalid_job")
    started.assert_not_awaited()


@pytest.mark.parametrize("status,reason", [(401, "authentication_failed"), (403, "subscription_denied"), (429, "subscription_limit_reached")])
async def test_broker_auth_and_quota_errors_stop_without_api_fallback(tmp_path, monkeypatch, status, reason, caplog):
    values = subscription_job(tmp_path)
    broker = CodexEgress(2, 65536, "fixed", "capability", None, auth_file=values["codex_auth_file"], auth_identity=values["codex_auth_identity"])
    paid = AsyncMock(side_effect=AssertionError("paid fallback"))
    async with fake_api(monkeypatch, tool=False, status=status) as calls:
        monkeypatch.setattr(codex_egress, "connect_endpoint", paid)
        path = await broker.start()
        for _ in range(2):
            reader, writer = await asyncio.open_unix_connection(path)
            body = b'{"model":"fixed","stream":true,"store":false}'
            writer.write(header(length=len(body), fields={"Authorization": "Bearer capability"}) + body)
            await writer.drain()
            assert b"502" in await reader.read()
            writer.close()
            await writer.wait_closed()
            assert broker.failure_reason == reason
            if status == 429:
                assert broker.failure_limit.limit_window == "rate" and broker.failure_limit.reset_at is None
            else:
                assert broker.failure_limit is None
        assert len(calls) == 1
    assert await broker.close(1) == "confirmed"
    if status == 429:
        from datetime import datetime, timedelta, timezone
        reader = asyncio.StreamReader()
        reader.feed_data(b"HTTP/1.1 429 Too Many Requests\r\nRetry-After: 60\r\nContent-Length: 0\r\n\r\n")
        reader.feed_eof()
        quota = CodexEgress(2, 65536, "fixed", "capability", None)
        before = datetime.now(timezone.utc)
        with pytest.raises(ValueError, match="subscription_limit_reached"):
            await quota.subscription_response(reader, AsyncMock())
        assert before + timedelta(seconds=60) <= quota.failure_limit.reset_at <= datetime.now(timezone.utc) + timedelta(seconds=60)
        assert 'retry_after' not in quota.failure_limit.model_dump_json()
        await quota.close(1)
    paid.assert_not_awaited()
    assert "false-refresh-secret" not in caplog.text


async def test_each_request_is_authorized_before_any_upstream(tmp_path, monkeypatch):
    values = subscription_job(tmp_path)
    authorize = AsyncMock(side_effect=ValueError("grant_expired"))
    broker = CodexEgress(2, 65536, "fixed", "capability", None, auth_file=values["codex_auth_file"],
                        auth_identity=values["codex_auth_identity"], authorize=authorize)
    connect = AsyncMock()
    monkeypatch.setattr(codex_egress, "connect_endpoint", connect)
    monkeypatch.setattr(codex_egress, "connect_subscription_endpoint", connect)
    path = await broker.start()
    reader, writer = await asyncio.open_unix_connection(path)
    body = b'{"model":"fixed","stream":true,"store":false}'
    writer.write(header(length=len(body), fields={"Authorization": "Bearer capability"}) + body)
    await writer.drain()
    assert b"502" in await reader.read()
    writer.close()
    await writer.wait_closed()
    assert await broker.close(1) == "confirmed"
    authorize.assert_awaited_once()
    connect.assert_not_awaited()


@pytest.mark.parametrize("code,reason_code,expected", [
    ("authentication_failed", None, "authentication_failed"),
    ("permission_denied", None, "permission_denied"),
    ("request_conflict", None, "request_conflict"),
    ("request_conflict", "authentication_binding_mismatch", "authentication_binding_mismatch"),
    ("private-unexpected-code", "private-unexpected-reason", "provider_error"),
])
async def test_authorization_errors_are_allowlisted_without_exposing_message(tmp_path, monkeypatch, caplog,
                                                                           code, reason_code, expected):
    values = subscription_job(tmp_path)
    broker = CodexEgress(2, 65536, "fixed", "capability", None, auth_file=values["codex_auth_file"],
        auth_identity=values["codex_auth_identity"],
        authorize=AsyncMock(side_effect=ExecutionError(code, "false-private-error-value", reason_code=reason_code)))
    connect = AsyncMock(side_effect=AssertionError("no provider request"))
    monkeypatch.setattr(codex_egress, "connect_subscription_endpoint", connect)
    path = await broker.start()
    reader, writer = await asyncio.open_unix_connection(path)
    body = b'{"model":"fixed","stream":true,"store":false}'
    writer.write(header(length=len(body), fields={"Authorization": "Bearer capability"}) + body)
    await writer.drain()
    response = await reader.read()
    assert b"502" in response and broker.failure_reason == expected
    assert b"false-private-error-value" not in response and "false-private-error-value" not in caplog.text
    writer.close()
    await writer.wait_closed()
    assert await broker.close(1) == "confirmed"
    connect.assert_not_awaited()


@pytest.mark.parametrize("status,reason", [(401, "authentication_failed"), (403, "subscription_denied"),
                                          (429, "subscription_limit_reached")])
async def test_first_auth_failure_fences_another_request_waiting_for_tls(tmp_path, monkeypatch, status, reason):
    values = subscription_job(tmp_path)
    broker = CodexEgress(3, 65536, "fixed", "capability", None,
                        auth_file=values["codex_auth_file"], auth_identity=values["codex_auth_identity"])
    blocked, release = asyncio.Event(), asyncio.Event()
    connects = 0
    paid = AsyncMock(side_effect=AssertionError("paid fallback"))
    async with fake_api(monkeypatch, tool=False, status=status) as calls:
        connect = codex_egress.connect_subscription_endpoint

        async def connect_with_first_request_waiting():
            nonlocal connects
            connects += 1
            connection = await connect()
            if connects == 1:
                blocked.set()
                await release.wait()
            return connection

        monkeypatch.setattr(codex_egress, "connect_subscription_endpoint", connect_with_first_request_waiting)
        monkeypatch.setattr(codex_egress, "connect_endpoint", paid)
        path = await broker.start()
        body = b'{"model":"fixed","stream":true,"store":false}'
        waiting_reader, waiting_writer = await asyncio.open_unix_connection(path)
        waiting_writer.write(header(length=len(body), fields={"Authorization": "Bearer capability"}) + body)
        await waiting_writer.drain()
        await asyncio.wait_for(blocked.wait(), 1)
        failing_reader, failing_writer = await asyncio.open_unix_connection(path)
        failing_writer.write(header(length=len(body), fields={"Authorization": "Bearer capability"}) + body)
        await failing_writer.drain()
        assert b"502" in await failing_reader.read()
        assert broker.failure_reason == reason and len(calls) == 1
        release.set()
        assert b"502" in await waiting_reader.read()
        assert broker.failure_reason == reason and len(calls) == 1
        for writer in (waiting_writer, failing_writer):
            writer.close()
            await writer.wait_closed()
        assert await broker.close(1) == "confirmed"
    paid.assert_not_awaited()


async def test_chunked_response_redacts_secret_across_arbitrary_chunks(tmp_path, monkeypatch):
    path, token = subscription_auth(tmp_path)
    broker = CodexEgress(2, 65536, "fixed", "capability", None, auth_file=path, auth_identity=read_subscription(path).identity)
    request, _ = parse_request(header())
    broker.subscription_request(request)
    reader = asyncio.StreamReader()
    data = b"data: " + token.encode() + b" fixture-account\n\n"
    chunks = [data[:20], data[20:50], data[50:]]
    response = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nX-Secret: " + token.encode() + b"\r\n\r\n"
    response += b"".join(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n" for chunk in chunks) + b"0\r\n\r\n"
    reader.feed_data(response)
    reader.feed_eof()
    from unittest.mock import Mock
    writer = Mock(drain=AsyncMock())
    try:
        await broker.subscription_response(reader, writer)
        result = writer.write.call_args.args[0]
        assert token.encode() not in result and b"fixture-account" not in result and b"X-Secret" not in result
        assert b"data: [redacted] [redacted]\n\n" in result
        headers, body = result.split(b"\r\n\r\n")
        assert f"Content-Length: {len(body)}".encode() in headers
    finally:
        broker.directory.cleanup()

    # Exercise both actual forwarding paths against synthetic loopback responses.
    for subscription in (False, True):
        options = {'auth_file': path, 'auth_identity': read_subscription(path).identity} if subscription else {}
        broker = CodexEgress(2, 65536, 'fixed', 'capability', None, **options)
        socket_path = await broker.start()
        async def send():
            incoming, outgoing = await asyncio.open_unix_connection(socket_path)
            request_body = b'{"model":"fixed","stream":true,"store":false}'
            outgoing.write(header(length=len(request_body), fields={'Authorization': 'Bearer capability'}) + request_body)
            await outgoing.drain()
            wire = await incoming.read()
            outgoing.close()
            await outgoing.wait_closed()
            return wire
        from contextlib import asynccontextmanager
        @asynccontextmanager
        async def evidence_server(status):
            calls = []
            async def respond(reader, writer):
                headers = await reader.readuntil(b'\r\n\r\n')
                length = next(int(line.split(b':', 1)[1]) for line in headers.split(b'\r\n') if line.lower().startswith(b'content-length:'))
                await reader.readexactly(length)
                calls.append(None)
                body = (b'data: ' + json.dumps({'type': 'response.completed', 'response': {'id': str(len(calls)), 'status': 'completed'}}).encode() + b'\n\n') if status == 200 else b''
                writer.write(f'HTTP/1.1 {status} Fixture\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n'.encode() + body)
                await writer.drain()
                writer.close()
                await writer.wait_closed()
            server = await asyncio.start_server(respond, '127.0.0.1', 0)
            port = server.sockets[0].getsockname()[1]
            monkeypatch.setattr(codex_egress, 'connect_endpoint', lambda: asyncio.open_connection('127.0.0.1', port))
            monkeypatch.setattr(codex_egress, 'connect_subscription_endpoint', lambda: asyncio.open_connection('127.0.0.1', port))
            try:
                yield calls
            finally:
                server.close()
                await server.wait_closed()
        try:
            async with evidence_server(200) as calls:
                assert b'HTTP/1.1 200 ' in await send()
                assert b'HTTP/1.1 200 ' in await send()
                assert len(calls) == 2
            assert broker.response_counter.value() == 2
            async with evidence_server(429):
                await send()
            assert broker.response_counter.value() == 2
        finally:
            await broker.close(1)
