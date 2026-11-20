"""Execution v1 と chat metadata.kotoyose の互換契約を検証する。"""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from kotoyose.app import app
from kotoyose.execution_contract import ExecutionEnvelope
from kotoyose.models import ChatCompletionRequest

from tests.fixtures.api_keys import TEST_API_HEADERS

pytestmark = pytest.mark.usefixtures("client_api_key", "configured_chat_alias")


@pytest.fixture(autouse=True)
def disable_auth(monkeypatch):
    """このモジュールの既定状態を開発用の認証バイパスにする。"""
    monkeypatch.setattr("kotoyose.auth.API_KEY", None)


def _agent_execution() -> dict:
    """schema_version 1 の最小 agent execution を返す。"""
    return {
        "execution": {
            "schema_version": "1",
            "idempotency_key": "dispatch-1",
            "caller_run_ref": "run-1",
            "kind": "agent",
            "agent_definition_ref": "agent-definition-1",
            "runtime_profile": "profile-1",
            "workspace_ref": "workspace-1",
            "execution_grant_ref": "grant-1",
            "budgets_ref": "budget-1",
            "inputs": [
                {
                    "kind": "instructions",
                    "provenance": "trusted",
                    "content_ref": "instructions-1",
                }
            ],
        }
    }


def _command_execution() -> dict:
    """schema_version 1 の最小 command execution を返す。"""
    return {
        "execution": {
            "schema_version": "1",
            "idempotency_key": "dispatch-1",
            "caller_run_ref": "run-1",
            "kind": "command",
            "workspace_ref": "workspace-1",
            "execution_grant_ref": "grant-1",
            "budgets_ref": "budget-1",
            "command_spec_ref": "command-spec-1",
            "argv_ref": "argv-1",
            "cwd_ref": "cwd-1",
            "timeout_policy_ref": "timeout-1",
        }
    }


async def _request(method: str, path: str, *, json: dict | None = None, headers: dict | None = None):
    """ASGITransport 経由でアプリへ 1 リクエストを送る。"""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.request(method, path, json=json, headers=TEST_API_HEADERS if headers is None else headers)


@pytest.mark.parametrize("factory", [_agent_execution, _command_execution])
@pytest.mark.asyncio
async def test_minimal_execution_is_schema_valid_but_unsupported(factory):
    """agent / command の最小要求は検証後に型付き未対応応答となる。"""
    payload = factory()
    payload['execution']['preferences'] = {}
    parsed = ExecutionEnvelope.model_validate(payload)

    response = await _request("POST", "/v1/executions", json=payload)

    assert parsed.execution.kind in {"agent", "command"}
    if factory is _agent_execution:
        assert response.status_code == 422
    else:
        assert response.status_code == 501
        error = response.json()["error"]
        assert error["type"] == "unsupported_capability"
        assert error["retryable"] is False
        assert error["safe_to_retry"] is True


def test_execution_requirements_default_to_sandbox_and_native_is_schema_valid():
    """isolation 省略は sandbox、native 明示は schema 上受理される。"""
    defaulted = ExecutionEnvelope.model_validate(_agent_execution())
    native_payload = _agent_execution()
    native_payload["execution"]["requirements"] = {"isolation": "native"}
    native = ExecutionEnvelope.model_validate(native_payload)

    assert defaulted.execution.requirements.isolation == "sandbox"
    assert native.execution.requirements.isolation == "native"


@pytest.mark.asyncio
async def test_native_execution_dispatch_is_unsupported():
    """native を明示しても dispatch は 501 のままである。"""
    payload = _agent_execution()
    payload["execution"]["requirements"] = {"isolation": "native"}

    response = await _request("POST", "/v1/executions", json=payload)

    assert response.status_code == 422


@pytest.mark.parametrize(
    ("case", "mutate"),
    [
        ("missing_required_ref", lambda body: body["execution"].pop("workspace_ref")),
        ("blank_required_ref", lambda body: body["execution"].__setitem__("workspace_ref", " \t")),
        ("unknown_nested_field", lambda body: body["execution"].__setitem__("unexpected", True)),
        ("unknown_top_level_field", lambda body: body.__setitem__("unexpected", True)),
        (
            "agent_with_command_field",
            lambda body: body["execution"].__setitem__("command_spec_ref", "command-spec-1"),
        ),
        ("unknown_kind", lambda body: body["execution"].__setitem__("kind", "other")),
        ("unknown_schema", lambda body: body["execution"].__setitem__("schema_version", "2")),
        ("self_declared_principal", lambda body: body.__setitem__("principal", "caller-1")),
    ],
)
@pytest.mark.asyncio
async def test_execution_contract_rejects_invalid_requests(case, mutate):
    """必須 ref、未知 field / union 値、body principal の自己申告を拒否する。"""
    payload = _agent_execution()
    mutate(payload)

    response = await _request("POST", "/v1/executions", json=payload)

    assert response.status_code == 422, case


@pytest.mark.asyncio
async def test_capabilities_expose_only_direct_chat_as_supported():
    """保証済み capability は direct_chat だけで、他は unsupported と公開する。"""
    response = await _request("GET", "/v1/capabilities")

    assert response.status_code == 200
    capabilities = response.json()["capabilities"]
    assert capabilities["direct_chat"]["status"] == "supported"
    assert all(
        item["status"] == "unsupported"
        for name, item in capabilities.items()
        if name != "direct_chat"
    )


@pytest.mark.parametrize(
    ("method", "path", "payload", "expected_authenticated_status"),
    [
        ("GET", "/v1/capabilities", None, 200),
        ("POST", "/v1/executions", _agent_execution(), 422),
    ],
)
@pytest.mark.asyncio
async def test_execution_api_requires_bearer_auth(
    monkeypatch, method, path, payload, expected_authenticated_status
):
    """GET / POST とも無認証・誤認証を拒否し、正しい Bearer は通す。"""
    monkeypatch.setattr("kotoyose.auth.API_KEY", "secret-token")

    missing = await _request(method, path, json=payload, headers={})
    wrong = await _request(
        method,
        path,
        json=payload,
        headers={"Authorization": "Bearer wrong-token"},
    )
    correct = await _request(
        method,
        path,
        json=payload,
        headers=TEST_API_HEADERS,
    )

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert correct.status_code == expected_authenticated_status


@pytest.mark.asyncio
@pytest.mark.usefixtures("configured_chat_alias")
async def test_chat_unsupported_kotoyose_requirements_are_rejected_before_side_effects():
    """未保証の sandbox / durable_attempts 要求は provider / DB 前に 501 とする。"""
    resume = AsyncMock()
    received = AsyncMock()
    payload = {
        "model": "kotoyose-chat",
        "messages": [{"role": "user", "content": "hello"}],
        "metadata": {
            "kotoyose": {
                "schema_version": "1",
                "required_capabilities": ["sandbox", "durable_attempts"],
            }
        },
    }
    with (
        patch("kotoyose.routes.chat.get_or_resume_client", new=resume),
        patch("kotoyose.routes.chat.record_received", new=received),
    ):
        response = await _request("POST", "/v1/chat/completions", json=payload)

    assert response.status_code == 501
    error = response.json()["error"]
    assert error["type"] == "unsupported_capability"
    assert error["capabilities"] == ["sandbox", "durable_attempts"]
    resume.assert_not_awaited()
    received.assert_not_awaited()


@pytest.mark.parametrize(
    "kotoyose",
    [
        {"schema_version": "1", "required_capabilities": ["not-a-capability"]},
        {"schema_version": "2", "required_capabilities": []},
        {"schema_version": "1", "required_capabilities": [], "unexpected": True},
    ],
)
@pytest.mark.asyncio
async def test_chat_kotoyose_unknown_requirement_schema_or_field_is_422(kotoyose):
    """未知 required capability、schema、kotoyose field は明示的に 422 とする。"""
    response = await _request(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "kotoyose-chat",
            "messages": [{"role": "user", "content": "hello"}],
            "metadata": {"kotoyose": kotoyose},
        },
    )

    assert response.status_code == 422


def test_chat_request_keeps_legacy_parse_and_other_metadata():
    """従来の request を受理し、Kotoyose 外 metadata を保持する。"""
    legacy = ChatCompletionRequest.model_validate(
        {"model": "kotoyose-chat", "messages": [{"role": "user", "content": "hello"}]}
    )
    metadata = {"client_tag": "openwebui", "trace": {"id": "trace-1"}}
    extended = ChatCompletionRequest.model_validate(
        {
            "model": "kotoyose-chat",
            "messages": [{"role": "user", "content": "hello"}],
            "metadata": metadata,
        }
    )

    assert legacy.metadata is None
    assert extended.metadata is not None
    assert extended.metadata.model_dump(exclude_none=True) == metadata


@pytest.mark.asyncio
@pytest.mark.usefixtures("configured_chat_alias")
async def test_chat_other_metadata_allows_normal_response_without_provider_or_db():
    """Kotoyose 外 metadata は保存可能なまま通常 chat 応答を通過させる。"""
    resume = AsyncMock(return_value=(AsyncMock(), False))
    received = AsyncMock(return_value=None)
    with (
        patch("kotoyose.routes.chat.get_or_resume_client", new=resume),
        patch("kotoyose.routes.chat.record_received", new=received),
        patch("kotoyose.routes.chat.ask_claude", new=AsyncMock(return_value=("ok", None, None))),
        patch("kotoyose.routes.chat.record_completed", new=AsyncMock()),
        patch("kotoyose.routes.chat._rotate_hash_key", new=AsyncMock(return_value="stored-key")),
    ):
        response = await _request(
            "POST",
            "/v1/chat/completions",
            json={
                "model": "kotoyose-chat",
                "messages": [{"role": "user", "content": "hello"}],
                "metadata": {"client_tag": "openwebui", "trace": {"id": "trace-1"}},
            },
        )

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "ok"
    received.assert_awaited_once()
