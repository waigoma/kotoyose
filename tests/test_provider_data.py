"""Stage C1: data declarations, host delivery, child approval, and a fake CLI."""

from __future__ import annotations

import asyncio
from contextlib import ExitStack
import ctypes
import errno
import hashlib
from io import BytesIO
import json
import os
import socket
import stat
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from textwrap import dedent
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from kotoyose.acp_client import AcpClient
from kotoyose.app import app
from kotoyose.child_client import ChildRejected, child_session, enroll_child
from kotoyose.child_local_login import _Parent, run as run_local_login
from kotoyose.child_protocol import PROVIDER_DATA_FEATURE, PROVIDER_DATA_MCP_FEATURE, REPOSITORY_SKILLS_FEATURE, WORKSPACE_POLICY_FEATURE, decode_frame, encode_frame
from kotoyose.child_relay import fetch_child_login_status
from kotoyose.child_server import ChildHub
from kotoyose.child_turn import ChildTurns, ChildTurnError, _build_runtime, _prepare
from kotoyose.cli_install_spec import platform_key
from kotoyose.config import HarnessConfig
from kotoyose.print_cli_grok_profile import GROK_ACP_WRITE_ARGV, GROK_INTEGRATION_ENV, grok_profile
from kotoyose.provider_data import GROK_MCP_GUARD, parse_declaration, parse_offers, validate_catalog
from kotoyose.provider_data_child import (
    approve,
    host_fingerprint,
    offered,
    preview,
    revoke,
    store_offers,
    use_approved,
    validate_request,
)
from kotoyose import provider_data_download, provider_data_runtime
from kotoyose.provider_data_host import host_catalog, reload_catalog, upload_declaration
from kotoyose.provider_data_runtime import (
    DeclarationRejected,
    acp_data_env,
    data_env,
    install_local,
    installed_path,
    operation,
    track_process,
    untrack_process,
)
from kotoyose.print_cli_profiles import get_print_cli_profile
from kotoyose.runtime.base import RuntimeEvent, RuntimeResult, RuntimeTurnError
from kotoyose.runtime.child import ChildRuntimeSession
from kotoyose.session import get_or_resume_client

_FINGERPRINT = "ab" * 32
_OTHER_FINGERPRINT = "cd" * 32
_GROK_DECLARATION = Path(__file__).resolve().parents[1] / "examples/provider-declarations/grok-build.json"


@pytest.fixture(autouse=True)
def isolated_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    reload_catalog()
    yield
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "empty-xdg"))
    reload_catalog()


def _declaration(platform: str | None = None, **changes) -> bytes:
    key = platform or "linux-x64"
    body = {
        "schema": "kotoyose.provider-data/1",
        "identity": {
            "id": "fake-cli", "profile_id": "fake-cli",
            "display_name": "Fake CLI", "publisher": "Example",
        },
        "driver": {"id": "cli.acp", "contract_version": 1},
        "platforms": {
            key: {
                "max_capability": "read",
                "install": {
                    "version": "1.0.0",
                    "urls": ["https://example.com/fake-cli"],
                    "sha256": "ab" * 32,
                    "format": "raw",
                    "binary": "fake-cli",
                },
            },
        },
        "connection_kinds": ["subscription"],
        "network": {"download_hosts": ["example.com"], "login_hosts": [], "runtime_hosts": []},
        "operations": {
            "login": {"argv": ["login"], "timeout_seconds": 30},
            "status": {"argv": ["status"], "timeout_seconds": 15},
        },
        "acp": {"argv": ["acp", "--model", "{model}"], "placeholders": ["model"]},
    }
    for key_name, value in changes.items():
        body[key_name] = value
    return json.dumps(body, separators=(",", ":")).encode()


def _mutate(raw: bytes, *path, value) -> bytes:
    body = json.loads(raw)
    cursor = body
    for key in path[:-1]:
        cursor = cursor[key]
    cursor[path[-1]] = value
    return json.dumps(body, separators=(",", ":")).encode()


def test_schema_accepts_a_read_only_declaration_and_rejects_everything_else():
    accepted = parse_declaration(_declaration())
    assert accepted.id == "fake-cli"
    assert accepted.sha256 == hashlib.sha256(_declaration()).hexdigest()
    assert accepted.driver == "cli.acp"
    assert accepted.commands("linux-x64")[0]["argv"] == ["fake-cli", "--version"]
    env = data_env()
    assert "PATH" not in env
    assert not any("KEY" in key or "TOKEN" in key or "SECRET" in key for key in env)

    rejected = [
        ("external package schema", _mutate(_declaration(), "schema", value="kotoyose.provider/1"), "schema"),
        ("shell argv", _mutate(_declaration(), "acp", value={"argv": ["bash", "-c", "{model}"], "placeholders": ["model"]}), "shell"),
        ("script suffix", _mutate(_declaration(), "operations", value={
            "login": {"argv": ["login.py"], "timeout_seconds": 30},
            "status": {"argv": ["status"], "timeout_seconds": 15},
        }), "script"),
        ("credential token", _mutate(_declaration(), "operations", value={
            "login": {"argv": ["api-key"], "timeout_seconds": 30},
            "status": {"argv": ["status"], "timeout_seconds": 15},
        }), "credential"),
        ("home override", _mutate(_declaration(), "operations", value={
            "login": {"argv": ["HOME"], "timeout_seconds": 30},
            "status": {"argv": ["status"], "timeout_seconds": 15},
        }), "shell"),
        ("undeclared placeholder", _mutate(_declaration(), "acp", value={
            "argv": ["acp", "--model", "{model}"], "placeholders": ["cwd"],
        }), "placeholder"),
        ("brace placeholder", _mutate(_declaration(), "acp", value={
            "argv": ["acp", "--model", "{cwd}"], "placeholders": ["model"],
        }), "shell"),
        ("http artifact", _mutate(_declaration(), "platforms", value={
            "linux-x64": {"max_capability": "read", "install": {
                "version": "1.0.0", "urls": ["http://example.com/fake-cli"],
                "sha256": "ab" * 32, "format": "raw", "binary": "fake-cli",
            }},
        }), "HTTPS"),
        ("undeclared host", _mutate(_declaration(), "platforms", value={
            "linux-x64": {"max_capability": "read", "install": {
                "version": "1.0.0", "urls": ["https://evil.example/fake-cli"],
                "sha256": "ab" * 32, "format": "raw", "binary": "fake-cli",
            }},
        }), "declared host"),
        ("unknown driver", _mutate(_declaration(), "driver", value={"id": "made.up", "contract_version": 1}), "driver"),
        ("non-string driver", _mutate(_declaration(), "driver", "id", value=[]), "driver"),
        ("http driver", _mutate(_declaration(), "driver", value={"id": "openai.chat", "contract_version": 1}), "driver"),
        ("unknown field", _declaration()[:-1] + b',"shell":"bash"}', "unknown"),
        ("duplicate key", b'{"schema":"kotoyose.provider-data/1","schema":"kotoyose.provider-data/1"}', "duplicate"),
    ]
    for name, raw, match in rejected:
        with pytest.raises(ValueError, match=match):
            parse_declaration(raw)
    shadowed = parse_declaration(_mutate(_declaration(), "identity", value={
        "id": "agy", "profile_id": "fake-cli", "display_name": "Fake", "publisher": "Example",
    }))
    with pytest.raises(ValueError, match="collision"):
        validate_catalog([shadowed])


def test_host_loader_and_admin_api_keep_auth_and_report_invalid_files(monkeypatch):
    raw = _declaration()
    upload_declaration(raw)
    assert host_catalog().rows[0].id == "fake-cli"
    assert get_print_cli_profile("agy").data_declaration is None
    with pytest.raises(ValueError, match="collision"):
        upload_declaration(_mutate(raw, "identity", value={
            "id": "codex", "profile_id": "fake-codex", "display_name": "Nope", "publisher": "Example",
        }))
    assert [row.id for row in host_catalog().rows] == ["fake-cli"]

    directory = Path(os.environ["XDG_DATA_HOME"]) / "kotoyose" / "provider-declarations"
    broken = directory / "broken-cli.json"
    broken.write_bytes(b"{")

    monkeypatch.setattr("kotoyose.auth.API_KEY", None)
    with TestClient(app) as open_client:
        assert open_client.post("/admin/provider-declarations", content=raw).status_code == 403
        assert open_client.get("/admin/provider-declarations").status_code == 200

    monkeypatch.setattr("kotoyose.auth.API_KEY", "owner-secret")
    monkeypatch.setattr("kotoyose.auth.BASIC_AUTH_USER", "owner-name")
    csrf = {"X-Kotoyose-Admin": "1", "Origin": "http://testserver"}
    with TestClient(app) as client:
        assert client.post("/admin/provider-declarations", content=raw).status_code == 401
        assert client.post(
            "/admin/provider-declarations", content=raw, headers={"Authorization": "Bearer owner-secret"},
        ).status_code == 401
        assert client.post(
            "/admin/provider-declarations", content=raw, auth=("owner-name", "owner-secret"),
        ).status_code == 403
        with capture_logs() as logs:
            reloaded = client.post("/admin/provider-declarations/reload", headers=csrf, auth=("owner-name", "owner-secret"))
        assert reloaded.status_code == 200
        assert reloaded.json()["declarations"][1] == {
            "id": "broken-cli",
            "profile_id": None,
            "filename": "broken-cli.json",
            "status": "invalid",
            "reason": "invalid declaration JSON",
        }
        assert [row for row in logs if row["log_level"] == "warning"] == [{
            "event": "provider_data.invalid_declaration", "log_level": "warning",
            "declaration": "broken-cli", "reason": "invalid declaration JSON",
        }]
        assert broken.read_bytes() == b"{"
        assert [row["id"] for row in host_catalog().offers()] == ["fake-cli"]
        uploaded = client.post(
            "/admin/provider-declarations", content=raw,
            headers={**csrf, "Content-Type": "application/json"}, auth=("owner-name", "owner-secret"),
        )
        assert uploaded.status_code == 200
        body = uploaded.json()
        assert body["declarations"][0]["sha256"] == hashlib.sha256(raw).hexdigest()
        assert body["declarations"][0]["commands"]
        assert body["declarations"][0]["artifacts"][0]["url"] == "https://example.com/fake-cli"
        denied = client.post(
            "/admin/provider-declarations", content=b'{"schema":"no"}',
            headers={**csrf, "Content-Type": "application/json"}, auth=("owner-name", "owner-secret"),
        )
        assert denied.status_code == 422
        assert client.get("/admin/provider-declarations", auth=("owner-name", "owner-secret")).json()["declarations"]
        removed_invalid = client.delete(
            "/admin/provider-declarations/broken-cli.json", headers=csrf, auth=("owner-name", "owner-secret"),
        )
        assert removed_invalid.status_code == 200
        assert not broken.exists()
        removed = client.delete(
            "/admin/provider-declarations/fake-cli", headers=csrf, auth=("owner-name", "owner-secret"),
        )
        assert removed.status_code == 200
        assert removed.json()["declarations"] == []


def test_admin_refuses_removal_until_effective_config_references_are_removed(monkeypatch):
    raw = _mutate(_declaration(), "identity", "profile_id", value="fake-profile")
    catalog = upload_declaration(raw)
    path = Path(os.environ["XDG_DATA_HOME"]) / "kotoyose" / "provider-declarations" / "fake-cli.json"
    connection = {"provider": "fake-cli", "kind": "subscription", "data_policy": "no_training"}
    cfg = HarnessConfig(
        connections={
            "account": {**connection, "profile": "fake-profile"},
            "quota-view": {"provider": "codex", "kind": "subscription", "data_policy": "no_training", "quota_from": "account"},
        },
        routes={"read-route": {"driver": "cli.acp", "profile": "fake-profile", "connection": "account", "model": "test"}},
        models={
            "reader": {"route": "read-route", "agent": {"place": "host", "surface": "read"}},
            "delegator": {"delegates": ["reader"]},
            "reader-pool": {"candidates": ["reader"], "policy": "first_available"},
            "unrelated": {},
        },
    )
    monkeypatch.setattr("kotoyose.config._config", cfg)
    monkeypatch.setattr("kotoyose.auth.API_KEY", "owner-secret")
    monkeypatch.setattr("kotoyose.auth.BASIC_AUTH_USER", "owner-name")
    csrf = {"X-Kotoyose-Admin": "1", "Origin": "http://testserver"}

    with TestClient(app) as client:
        client.auth = ("owner-name", "owner-secret")
        response = client.delete("/admin/provider-declarations/fake-cli.json", headers=csrf)
        assert response.status_code == 422
        detail = response.json()["error"]["message"]
        for reference in (
            "connection 'account'", "connection 'quota-view'", "route 'read-route'",
            "alias 'reader'", "alias 'delegator'", "alias 'reader-pool'",
        ):
            assert reference in detail
        assert "unrelated" not in detail
        assert path.read_bytes() == raw
        assert host_catalog().rows == catalog.rows

        monkeypatch.setattr("kotoyose.config._config", HarnessConfig(connections={"account": connection}))
        response = client.delete("/admin/provider-declarations/fake-cli", headers=csrf)
        assert response.status_code == 422
        assert "connection 'account'" in response.json()["error"]["message"]

        monkeypatch.setattr("kotoyose.config._config", HarnessConfig(models={"unrelated": {}}))
        response = client.delete("/admin/provider-declarations/fake-cli", headers=csrf)
        assert response.status_code == 200
        assert response.json()["declarations"] == []
        assert not path.exists()
        assert not host_catalog().rows


def test_stored_identifier_collisions_exclude_both_files_and_keep_unrelated_offers():
    raw = _declaration()
    upload_declaration(_GROK_DECLARATION.read_bytes())
    upload_declaration(raw)
    directory = Path(os.environ["XDG_DATA_HOME"]) / "kotoyose" / "provider-declarations"
    (directory / "duplicate.json").write_bytes(raw)

    catalog = reload_catalog()

    assert [offer["id"] for offer in catalog.offers()] == ["grok-build"]
    assert {entry.filename for entry in catalog.invalid} == {"duplicate.json", "fake-cli.json"}
    assert all("identifier collision" in entry.reason for entry in catalog.invalid)
    with pytest.raises(KeyError, match="fake-cli"):
        get_print_cli_profile("fake-cli")


def _child_home(path: Path, fingerprint: str = _FINGERPRINT) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "host.json").write_text(json.dumps({
        "fingerprint": fingerprint, "address": "127.0.0.1:1", "name": "host", "host_cert_pem": "",
    }), encoding="utf-8")
    (path / "policy.json").write_text(json.dumps({
        "workspace_roots": [str(path)], "adapters": ["fake-cli"], "mcp": {}, "logins": [],
    }), encoding="utf-8")
    return path


def _offer(raw: bytes, home: Path, fingerprint: str = _FINGERPRINT):
    row = parse_declaration(raw)
    store_offers(home, fingerprint, [row.offer()])
    return row


def test_approval_store_is_owner_only_and_stale_after_hash_or_fingerprint_changes(tmp_path):
    home = _child_home(tmp_path / "child")
    raw = _declaration(platform_key())
    row = _offer(raw, home)
    approve(home, row.id, sha256=row.sha256)
    approval = home / "provider-declarations" / "approved" / "fake-cli.json"
    assert stat.S_IMODE(approval.stat().st_mode) == 0o600
    assert stat.S_IMODE(approval.parent.stat().st_mode) == 0o700
    assert preview(home)["offers"][0]["status"] == "approved"

    changed = _mutate(raw, "identity", value={
        "id": "fake-cli", "profile_id": "fake-cli", "display_name": "Fake CLI 2", "publisher": "Example",
    })
    changed_row = _offer(changed, home)
    assert changed_row.sha256 != row.sha256
    assert preview(home)["offers"][0]["status"] == "stale"
    with pytest.raises(DeclarationRejected):
        operation(changed_row, "status", child_home=home)

    (home / "host.json").write_text(json.dumps({"fingerprint": _OTHER_FINGERPRINT}), encoding="utf-8")
    assert preview(home)["offers"][0]["status"] == "stale"
    with pytest.raises(DeclarationRejected):
        operation(row, "status", child_home=home)
    with pytest.raises(ValueError, match="current host"):
        approve(home, row.id, sha256=row.sha256)

    (home / "host.json").write_text(json.dumps({"fingerprint": _FINGERPRINT}), encoding="utf-8")
    _offer(raw, home)
    revoke(home, row.id)
    assert preview(home)["offers"][0]["status"] == "revoked"
    with pytest.raises(DeclarationRejected):
        operation(row, "status", child_home=home)
    approve(home, row.id, sha256=row.sha256)
    assert preview(home)["offers"][0]["status"] == "approved"

    home.chmod(0o777)
    store_offers(home, _FINGERPRINT, [row.offer()])
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    moved = tmp_path / "moved-declarations"
    stored = home / "provider-declarations"
    stored.rename(moved)
    stored.symlink_to(moved, target_is_directory=True)
    with pytest.raises(ValueError, match="unsafe"):
        store_offers(home, _FINGERPRINT, [row.offer()])


def _fake_cli(path: Path) -> bytes:
    path.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "args = sys.argv[1:]\n"
        "if args == ['--version']:\n"
        "    print('1.0.0')\n"
        "    raise SystemExit(0)\n"
        "if args in (['login'], ['status']):\n"
        "    raise SystemExit(0)\n"
        "if len(args) >= 2 and args[0] == 'acp' and args[1] == '--model':\n"
        "    for line in sys.stdin:\n"
        "        message = json.loads(line)\n"
        "        ident = message.get('id')\n"
        "        if ident is None:\n"
        "            continue\n"
        "        method = message.get('method')\n"
        "        if method == 'initialize':\n"
        "            result = {'protocolVersion': 1, 'agentCapabilities': {}}\n"
        "        elif method == 'session/new':\n"
        "            result = {'sessionId': 's1'}\n"
        "        elif method == 'session/prompt':\n"
        "            note = {'jsonrpc': '2.0', 'method': 'session/update', 'params': {\n"
        "                'sessionId': 's1', 'update': {'sessionUpdate': 'agent_message_chunk',\n"
        "                'content': {'type': 'text', 'text': 'fake-ok'}}}}\n"
        "            sys.stdout.write(json.dumps(note) + '\\n')\n"
        "            sys.stdout.flush()\n"
        "            result = {'stopReason': 'end_turn'}\n"
        "        else:\n"
        "            sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': ident, 'error': {'code': -32601, 'message': 'no'}}) + '\\n')\n"
        "            sys.stdout.flush()\n"
        "            continue\n"
        "        sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': ident, 'result': result}) + '\\n')\n"
        "        sys.stdout.flush()\n"
        "    raise SystemExit(0)\n"
        "raise SystemExit(2)\n",
        encoding="utf-8",
    )
    path.chmod(0o700)
    return path.read_bytes()


def _pinned(artifact: bytes, platform: str) -> bytes:
    body = json.loads(_declaration(platform))
    body["platforms"][platform]["install"]["sha256"] = hashlib.sha256(artifact).hexdigest()
    return json.dumps(body, separators=(",", ":")).encode()


def test_unapproved_declaration_never_starts_a_process(monkeypatch, tmp_path):
    started = []

    def refuse(*_args, **_kwargs):
        started.append(True)
        raise AssertionError("unapproved declaration started a process")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", refuse)
    home = _child_home(tmp_path / "child")
    raw = _declaration(platform_key())
    row = parse_declaration(raw)
    store_offers(home, _FINGERPRINT, [row.offer()])
    artifact = tmp_path / "fake-cli"
    artifact.write_bytes(b"#!/bin/sh\necho 1.0.0\n")
    with pytest.raises(DeclarationRejected):
        install_local(row, artifact, child_home=home)
    with pytest.raises(DeclarationRejected):
        operation(row, "login", child_home=home)
    payload = {
        "alias": "chat",
        "preset": {"route": "chat", "place": "host"},
        "route": {"driver": "cli.acp", "profile": "fake-cli", "connection": "main", "model": "fake"},
        "connection": {
            "provider": "fake-cli", "kind": "subscription", "data_policy": "unknown", "profile": "fake-cli",
        },
        "declaration_sha256": row.sha256, "capability": "read",
        "prompt": {"text": "hello"},
    }
    with use_approved(home), pytest.raises(ChildTurnError, match="unapproved or changed"):
        _prepare(home, payload)
    assert started == []


@pytest.mark.asyncio
async def test_fake_provider_is_served_approved_and_used_for_one_turn(tmp_path):
    key = platform_key()
    artifact_path = tmp_path / "artifact"
    pinned = _pinned(_fake_cli(artifact_path), key)
    row = parse_declaration(pinned)
    upload_declaration(pinned)
    home = _child_home(tmp_path / "child")
    store_offers(home, _FINGERPRINT, [row.offer()])
    approve(home, row.id, sha256=row.sha256)
    with use_approved(home):
        installed = install_local(row, artifact_path, child_home=home)
        assert installed.is_file()
        assert operation(row, "login", child_home=home) is True
        assert operation(row, "status", child_home=home) is True
        payload = {
            "alias": "chat",
            "preset": {"route": "chat", "place": "host"},
            "route": {"driver": "cli.acp", "profile": row.profile_id, "connection": "main", "model": "fake"},
            "connection": {
                "provider": row.id, "kind": "subscription", "data_policy": "no_training", "profile": row.profile_id,
            },
            "declaration_sha256": row.sha256, "capability": "read",
            "prompt": {"text": "hello"},
        }
        prepared = _prepare(home, payload)
        runtime = await _build_runtime(prepared)
        try:
            result = await runtime.ask("hello")
        finally:
            await runtime.disconnect()
            prepared["cleanup"]()
    assert result.text == "fake-ok"

    changed = _mutate(pinned, "identity", value={
        "id": row.id, "profile_id": row.profile_id, "display_name": "Changed", "publisher": "Example",
    })
    changed_row = parse_declaration(changed)
    store_offers(home, _FINGERPRINT, [changed_row.offer()])
    with pytest.raises(DeclarationRejected):
        operation(changed_row, "status", child_home=home)


@pytest.mark.asyncio
async def test_catalog_frame_is_offered_only_to_children_that_negotiate_it(tmp_path):
    raw = _declaration()
    upload_declaration(raw)
    upload_declaration(_GROK_DECLARATION.read_bytes())
    expected = hashlib.sha256(raw).hexdigest()
    hub = ChildHub(tmp_path / "host-hub", heartbeat_interval=0.05)
    await hub.start("127.0.0.1", 0)
    home = tmp_path / "child"
    try:
        await enroll_child(hub.issue_enrollment_code(f"127.0.0.1:{hub.port}"), home, name="box")
        async with child_session(home, heartbeat_interval=0.05) as session:
            await session.catalog_received.wait()
            assert "provider_catalog" in session.control_kinds
            fingerprint, rows = offered(home)
            assert fingerprint == host_fingerprint(home)
            assert {row.id for row in rows} == {"fake-cli", "grok-build"}
            assert next(row.sha256 for row in rows if row.id == "fake-cli") == expected
        async with child_session(
            home, heartbeat_interval=0.05, features=[PROVIDER_DATA_FEATURE],
        ) as old_parser:
            await old_parser.catalog_received.wait()
            assert [row.id for row in offered(home)[1]] == ["fake-cli"]
        async with child_session(
            home, heartbeat_interval=0.05, features=["codex-turns", "login-status"],
        ) as old:
            await old.ready.wait()
            await asyncio.sleep(0.05)
            assert "provider_catalog" not in old.control_kinds
        with pytest.raises(ChildRejected, match="version mismatch"):
            async with child_session(
                home, package_version="0.0.0-old", features=["codex-turns", "login-status"],
            ):
                pass
        received: list[dict] = []
        async with child_session(
            home, package_version="0.0.0-mixed", heartbeat_interval=0.05, received_turns=received,
        ) as mixed:
            await mixed.ready.wait()
            live = hub.live_for("box")
            assert live is not None and live.package_match is False
            assert await hub.send_frame("box", 4, "turn", {"alias": "chat"}) is True
            frame = live.mux.queue(4).get_nowait()
            assert frame.kind == "error" and frame.payload["message"] == "package version mismatch"
            await asyncio.sleep(0.05)
            assert received == []
            assert await hub.send_frame("box", 5, "login", {"provider": "fake-cli"}) is True
            login = live.mux.queue(5).get_nowait()
            assert login.kind == "login_error"
            assert await hub.send_frame("box", 0, "heartbeat", {}) is True
            assert await hub.send_frame("box", 0, "provider_catalog", {"offers": []}) is True
            assert await hub.send_frame("box", 8, "cancel", {}) is True
            assert live.mux.queue(8).empty()
            assert await hub.send_frame("box", 9, "release", {"conversation_key": "none"}) is True
            refused = live.mux.queue(9).get_nowait()
            assert refused.kind == "error"
            assert refused.payload["message"] == "package version mismatch"
    finally:
        await hub.stop()


def test_catalog_frame_bounds_are_enforced():
    empty = decode_frame(encode_frame(0, "provider_catalog", {"offers": []}))
    assert empty.kind == "provider_catalog"
    huge = {"id": "fake-cli", "sha256": "ab" * 32, "document": "x" * (16 * 1024 + 1)}
    with pytest.raises(ValueError, match="catalog"):
        decode_frame(encode_frame(0, "provider_catalog", {"offers": [huge]}))
    with pytest.raises(ValueError, match="catalog"):
        parse_offers([{"id": "fake-cli", "sha256": "nope", "document": _declaration().decode()}])


def _print_declaration(argv, **print_fields) -> bytes:
    body = json.loads(_declaration())
    body["driver"] = {"id": "cli.print", "contract_version": 1}
    del body["acp"]
    body["print"] = {
        "argv": list(argv), "placeholders": ["prompt"], "parser": "text", "mode": "read", **print_fields,
    }
    return json.dumps(body, separators=(",", ":")).encode()


def test_offer_id_must_match_identity_and_the_approval_filename(tmp_path):
    row = parse_declaration(_declaration())
    forged = row.offer()
    forged["id"] = "other-cli"
    with pytest.raises(ValueError, match="identity"):
        parse_offers([forged])
    home = _child_home(tmp_path / "child")
    stored = _offer(_declaration(platform_key()), home)
    approve(home, stored.id, sha256=stored.sha256)
    directory = home / "provider-declarations" / "approved"
    (directory / "fake-cli.json").rename(directory / "other-cli.json")
    from kotoyose.provider_data_child import approvals

    assert approvals(home) == ()


def test_preview_is_only_the_validated_view_for_this_os(tmp_path):
    body = json.loads(_declaration(platform_key()))
    body["output_guard_env"] = ["FAKE_GUARD"]
    body["platforms"]["darwin-x64"] = body["platforms"][platform_key()]
    raw = json.dumps(body, separators=(",", ":")).encode()
    home = _child_home(tmp_path / "child")
    _offer(raw, home)
    offer = preview(home)["offers"][0]
    assert "document" not in offer
    assert offer["binary"] == "fake-cli"
    assert offer["version"] == "1.0.0"
    assert offer["output_guard_env"] == ["FAKE_GUARD"]
    assert offer["artifacts"] == [{
        "url": "https://example.com/fake-cli", "sha256": "sha256:" + "ab" * 32,
        "version": "1.0.0", "platform": platform_key(),
    }]
    assert offer["commands"][0]["argv"] == ["fake-cli", "--version"]
    assert offer["network"]["download_hosts"] == ["example.com"]
    assert "not sandboxed" in offer["rights"]


def test_display_name_and_publisher_reject_bidi_and_zero_width_characters():
    base = {
        "id": "fake-cli", "profile_id": "fake-cli", "display_name": "Fake CLI", "publisher": "Example",
    }
    for field, value in (
        ("display_name", "Fake\u202eCLI"),
        ("publisher", "Ex\u200bample"),
        ("display_name", "Fake\u2066CLI"),
        ("publisher", "A\ufeffB"),
    ):
        identity = {**base, field: value}
        with pytest.raises(ValueError, match="display"):
            parse_declaration(_mutate(_declaration(), "identity", value=identity))


def test_prompt_placeholder_cannot_become_an_option():
    for argv in (["{prompt}"], ["--prompt", "{prompt}"], ["-p", "{prompt}"]):
        with pytest.raises(ValueError, match="option"):
            parse_declaration(_print_declaration(argv))
    assert parse_declaration(_print_declaration(["--", "{prompt}"])).print_argv == ("--", "{prompt}")
    bound = parse_declaration(_print_declaration(["--prompt={prompt}"]))
    assert bound.print_argv == ("--prompt={prompt}",)
    stdin = parse_declaration(_print_declaration([], prompt="stdin"))
    assert stdin.prompt_via == "stdin"
    assert stdin.print_argv == ()


def test_model_value_starting_with_a_dash_is_rejected_before_substitution():
    from kotoyose.acp_spec import AcpSpec
    from kotoyose.print_cli_profiles import get_print_cli_profile

    with pytest.raises(ValueError, match="option"):
        AcpSpec(("acp", "--model", "{model}")).arguments("-evil")
    with pytest.raises(ValueError, match="option"):
        get_print_cli_profile("agy").render_argv(
            model="-grok", prompt="hello", timeout_seconds=1, surface="read",
        )


def test_data_env_drops_credential_names_and_keeps_the_login_location(monkeypatch):
    monkeypatch.setenv("HOME", "/home/owner")
    monkeypatch.setenv("USERPROFILE", r"C:\Users\owner")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "sekrit")
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("LANG", "C.UTF-8")
    env = data_env()
    assert env["HOME"] == "/home/owner"
    assert env["USERPROFILE"] == r"C:\Users\owner"
    assert env["LANG"] == "C.UTF-8"
    assert "PATH" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "GITHUB_TOKEN" not in env


@pytest.mark.parametrize("surface,place,sandboxed,write_allowed", [
    ("read", "host", False, False),
    ("write", "host", False, False),
    ("write", "isolated", False, True),
    ("write", "host", True, True),
])
def test_data_acp_permission_kinds(surface, place, sandboxed, write_allowed):
    from kotoyose.acp_turn import AcpPermissionPolicy
    from kotoyose.provider_data_runtime import read_permission_policy

    policy = (read_permission_policy() if surface == "read" else
              AcpPermissionPolicy(surface, place, sandboxed_child_write=sandboxed))
    options = [
        {"kind": "allow_once", "optionId": "yes"},
        {"kind": "reject_once", "optionId": "no"},
    ]
    reads = {"read", "search", "think"}
    writes = {"edit", "delete", "move", "execute"}
    for kind in reads | writes | {"fetch", "switch_mode", "other", "mcp", "unknown"}:
        _response, event = policy.decide({"toolCall": {"kind": kind, "title": kind}, "options": options})
        assert event.summary == ("allowed" if kind in reads or (write_allowed and kind in writes) else "denied")


def test_artifact_urls_reject_encoded_dot_and_slash_and_non_https():
    for url in (
        "https://example.com/%2e%2e/fake-cli",
        "https://example.com/a%2fb",
        "https://example.com/%2Fsecret",
        "http://example.com/fake-cli",
    ):
        raw = _mutate(_declaration(), "platforms", value={
            "linux-x64": {"max_capability": "read", "install": {
                "version": "1.0.0", "urls": [url], "sha256": "ab" * 32, "format": "raw", "binary": "fake-cli",
            }},
        })
        with pytest.raises(ValueError, match="URL"):
            parse_declaration(raw)


@pytest.mark.asyncio
async def test_upload_rejects_an_oversized_content_length_before_reading():
    from fastapi import HTTPException

    from kotoyose.routes.admin_provider_data import read_declaration_body

    class Request:
        def __init__(self, length, chunks):
            self.headers = {"content-length": str(length)}
            self._chunks = chunks
            self.reads = 0

        async def stream(self):
            for chunk in self._chunks:
                self.reads += 1
                yield chunk

    oversized = Request(16 * 1024 + 1, [b"x"])
    with pytest.raises(HTTPException) as caught:
        await read_declaration_body(oversized)
    assert caught.value.status_code == 413
    assert oversized.reads == 0

    streaming = Request(4, [b"abcd", b"e" * 20_000])
    with pytest.raises(HTTPException) as caught:
        await read_declaration_body(streaming)
    assert caught.value.status_code == 413
    assert streaming.reads == 2


def test_validate_command_is_the_only_gate_for_an_offer():
    row = parse_declaration(_declaration())
    forged = row.offer()
    forged["id"] = "other-cli"
    rejected = subprocess.run(
        [sys.executable, "-I", "-m", "kotoyose.provider_data_child", "validate"],
        input=json.dumps({"action": "offers", "offers": [forged]}).encode(),
        capture_output=True, check=False,
    )
    assert rejected.returncode != 0
    assert b"identity" in rejected.stderr
    accepted = subprocess.run(
        [sys.executable, "-I", "-m", "kotoyose.provider_data_child", "validate"],
        input=json.dumps({"action": "offers", "offers": [row.offer()]}).encode(),
        capture_output=True, check=False,
    )
    assert accepted.returncode == 0
    assert json.loads(accepted.stdout)["offers"][0]["id"] == "fake-cli"


def _operation_script(path: Path, mode_file: Path, marker: Path) -> bytes:
    path.write_text(
        f"#!{sys.executable}\n"
        "import os, subprocess, sys, time\n"
        "from pathlib import Path\n"
        f"mode = Path({str(mode_file)!r}).read_text(encoding='utf-8').strip()\n"
        "args = sys.argv[1:]\n"
        "if args == ['--version']:\n"
        "    print('1.0.0')\n"
        "    raise SystemExit(0)\n"
        "if args == ['login']:\n"
        "    raise SystemExit(0)\n"
        "if args != ['status']:\n"
        "    raise SystemExit(2)\n"
        "if mode == 'group':\n"
        f"    Path({str(marker)!r}).write_text(f'{{os.getpid()}} {{os.getpgrp()}}', encoding='utf-8')\n"
        "    raise SystemExit(0)\n"
        "if mode == 'big':\n"
        "    sys.stdout.write('x' * 200000)\n"
        "    raise SystemExit(0)\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        f"Path({str(marker)!r}).write_text(str(child.pid), encoding='utf-8')\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    path.chmod(0o700)
    return path.read_bytes()


def _installed(tmp_path: Path, mode: str):
    mode_file = tmp_path / "mode"
    marker = tmp_path / "child-pid"
    mode_file.write_text(mode, encoding="utf-8")
    artifact = tmp_path / "fake-cli"
    raw = _pinned(_operation_script(artifact, mode_file, marker), platform_key())
    body = json.loads(raw)
    body["operations"]["status"]["timeout_seconds"] = 1 if mode == "sleep" else 15
    raw = json.dumps(body, separators=(",", ":")).encode()
    row = parse_declaration(raw)
    home = _child_home(tmp_path / "child")
    store_offers(home, _FINGERPRINT, [row.offer()])
    approve(home, row.id, sha256=row.sha256)
    install_local(row, artifact, child_home=home)
    return home, row, marker


def test_declared_command_runs_in_its_own_group_and_caps_output(tmp_path):
    home, row, marker = _installed(tmp_path, "group")
    assert operation(row, "status", child_home=home) is True
    pid, group = marker.read_text(encoding="utf-8").split()
    assert pid == group
    (tmp_path / "mode").write_text("big", encoding="utf-8")
    with pytest.raises(ValueError, match="output"):
        operation(row, "status", child_home=home)


def test_revoke_and_timeout_stop_the_declared_process_group(tmp_path):
    home, row, marker = _installed(tmp_path, "sleep")
    import threading

    box: dict[str, object] = {}

    def run() -> None:
        try:
            box["ok"] = operation(row, "status", child_home=home)
        except Exception as exc:  # noqa: BLE001 - the timeout path reports the failure
            box["error"] = exc

    worker = threading.Thread(target=run)
    worker.start()
    deadline = time.time() + 5
    while not marker.exists() and time.time() < deadline:
        time.sleep(0.02)
    assert marker.exists()
    grandchild = int(marker.read_text(encoding="utf-8"))
    try:
        revoke(home, row.id)
        worker.join(5)
        assert not worker.is_alive()
        deadline = time.time() + 2
        while Path(f"/proc/{grandchild}").exists() and time.time() < deadline:
            time.sleep(0.02)
        assert not Path(f"/proc/{grandchild}").exists()
    finally:
        if Path(f"/proc/{grandchild}").exists():
            os.kill(grandchild, 9)


def test_grok_build_declaration_matches_the_host_pin_and_read_contract():
    row = parse_declaration(_GROK_DECLARATION.read_bytes())
    host = grok_profile()
    pin = host.install.artifact_for("linux-x64")
    assert row.id == row.profile_id == "grok-build"
    assert row.driver == "cli.acp"
    assert row.install.version == host.install.version
    assert tuple(item.artifact.platform for item in row.platforms) == ("linux-x64", "windows-x64")
    target = row.platform("linux-x64")
    assert target.capability == "mcp"
    assert all(target.supports(capability) for capability in ("read", "write", "mcp"))
    assert target.binary == host.binary
    assert target.artifact.digest == pin.digest
    assert target.artifact.url_templates == tuple(
        url.format(version=host.install.version, os=pin.os, arch=pin.arch) for url in pin.url_templates
    )
    assert row.runtime_hosts == host.egress_hosts
    assert row.login_hosts == host.login.url_hosts
    assert row.login == host.login
    assert row.login_flow == host.login_flow
    assert row.acp.argv == ("--sandbox", "read-only") + host.surfaces["read"].extra_args + host.acp.argv
    assert row.acp_write.argv == GROK_ACP_WRITE_ARGV
    assert row.mcp_guard == GROK_MCP_GUARD
    assert validate_catalog((row,)) == (row,)


def _grok_host_config() -> dict:
    return {
        "connections": {"dev": {
            "provider": "grok-build", "kind": "subscription", "profile": "grok-build", "on": "child:box",
            "data_policy": "no_training",
        }},
        "routes": {"grok": {"driver": "cli.acp", "profile": "grok-build", "connection": "dev", "model": "fake-model"}},
        "models": {"grok-read": {"route": "grok", "on": "child:box"}},
    }


def _grok_write_alias(config, workspace):
    config["models"]["grok-write"] = {
        "route": "grok", "on": "child:box", "agent": {"place": "host", "surface": "write"},
        "allow_cwd_override": True, "cwd_allowed_roots": [str(workspace)],
    }


def _grok_account(monkeypatch, home):
    import pwd

    real = pwd.getpwuid(os.getuid())
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(home), pw_name=real.pw_name, pw_gid=real.pw_gid))
    monkeypatch.setenv("HOME", str(home))


def _mock_landlock(monkeypatch):
    monkeypatch.setattr(provider_data_runtime, "platform_key", lambda: "linux-x64")

    def query_abi(number, attr, size, flags):
        assert (number, attr.value, size.value, flags.value) == (444, None, 0, 1)
        return 4

    syscall = Mock(side_effect=query_abi)
    monkeypatch.setattr(ctypes, "CDLL", lambda *args, **kwargs: SimpleNamespace(syscall=syscall))
    return syscall


def test_data_write_alias_requires_child_placement_and_write_argv(tmp_path):
    upload_declaration(_GROK_DECLARATION.read_bytes())
    config = _grok_host_config()
    _grok_write_alias(config, tmp_path)
    assert HarnessConfig.model_validate(config).preset_for("grok-write").agent.surface == "write"
    config["models"].pop("grok-read")
    config["connections"]["dev"]["on"] = config["models"]["grok-write"]["on"] = "local"
    with pytest.raises(ValueError, match="write requires on: child"):
        HarnessConfig.model_validate(config)

    upload_declaration(_declaration())
    config["connections"]["dev"].update(provider="fake-cli", profile="fake-cli", on="child:box")
    config["routes"]["grok"]["profile"] = "fake-cli"
    config["models"]["grok-write"]["on"] = "child:box"
    with pytest.raises(ValueError, match="write_argv"):
        HarnessConfig.model_validate(config)


@pytest.mark.parametrize("maximum, surface, error", [
    ("mcp", "write", None),
    ("mcp", "read", "mcp_refs require"),
    ("write", "write", "mcp_refs require"),
])
def test_data_mcp_refs_require_a_write_alias_and_mcp_declaration(tmp_path, maximum, surface, error):
    upload_declaration(_mutate(_GROK_DECLARATION.read_bytes(), "platforms", "linux-x64", "max_capability", value=maximum))
    config = _grok_host_config()
    _grok_write_alias(config, tmp_path)
    alias = config["models"]["grok-write"]
    alias["agent"]["surface"] = surface
    alias["mcp_refs"] = ["files"]
    if error is None:
        assert HarnessConfig.model_validate(config).preset_for("grok-write").mcp_refs == ["files"]
    else:
        with pytest.raises(ValueError, match=error):
            HarnessConfig.model_validate(config)


@pytest.mark.parametrize("alias", ["grok-read", "grok-write"])
def test_host_config_rejects_inline_mcp_for_child_data_aliases(tmp_path, alias):
    upload_declaration(_GROK_DECLARATION.read_bytes())
    config = _grok_host_config()
    _grok_write_alias(config, tmp_path)
    servers = {"files": {"command": "host-server"}}
    validated = HarnessConfig.model_validate(config)
    config["models"][alias]["mcp_servers"] = servers
    with pytest.raises(ValueError, match="child aliases cannot set mcp_servers"):
        HarnessConfig.model_validate(config)
    validated.models[alias].mcp_servers = servers
    with pytest.raises(ValueError, match=rf"{alias}: child data provider aliases cannot set inline mcp_servers"):
        validated._validate_profile_references()


def test_grok_private_acp_home_and_environment(tmp_path, monkeypatch):
    row = parse_declaration(_GROK_DECLARATION.read_bytes())
    home = tmp_path / "child"
    home.mkdir(mode=0o700)
    real_home = tmp_path / "owner"
    real_home.mkdir(mode=0o700)
    grok_home = real_home / ".grok"
    grok_home.mkdir(mode=0o700)
    _grok_account(monkeypatch, real_home)
    for key in ("USERPROFILE", "TMPDIR", "GROK_HOME", "XDG_CONFIG_HOME", "GROK_CLAUDE_HOOKS_ENABLED", "API_TOKEN"):
        monkeypatch.setenv(key, str(real_home / "inherited"))
    with ExitStack() as first, ExitStack() as second:
        env = acp_data_env(row, home, first)
        private = Path(env["HOME"])
        assert stat.S_IMODE(private.stat().st_mode) == 0o700
        assert {key: value for key, value in env.items() if str(real_home) in value} == {"GROK_HOME": str(real_home / ".grok")}
        assert {key: value for key, value in env.items() if key.endswith("_ENABLED")} == dict(GROK_INTEGRATION_ENV)
        assert acp_data_env(row, home, second)["HOME"] != env["HOME"]
    assert not private.exists()
    assert data_env()["HOME"] == str(real_home)
    assert "GROK_HOME" not in data_env()
    monkeypatch.delenv("HOME")
    with ExitStack() as leases:
        assert acp_data_env(row, home, leases)["GROK_HOME"] == str(grok_home)

    grok_home.rmdir()
    grok_home.symlink_to(home, target_is_directory=True)
    with ExitStack() as leases, pytest.raises(DeclarationRejected, match="without symlinks"):
        acp_data_env(row, home, leases)


@pytest.mark.parametrize("problem, error", [
    ("env-relative", "HOME to match"),
    ("env-parent", "HOME to match"),
    ("env-mismatch", "HOME to match"),
    ("account-relative", "absolute OS account home"),
    ("account-parent", "absolute OS account home"),
    ("missing", "existing OS account home and ~/.grok directory"),
    ("file", "directory without symlinks"),
    ("home-group-write", "not group/world-writable"),
    ("grok-world-write", "not group/world-writable"),
    ("home-owner", "owned by the current user"),
    ("grok-owner", "owned by the current user"),
])
def test_isolated_grok_rejects_unsafe_login_directories(tmp_path, monkeypatch, problem, error):
    row = parse_declaration(_GROK_DECLARATION.read_bytes())
    real_home = tmp_path / "owner"
    real_home.mkdir(mode=0o700)
    grok_home = real_home / ".grok"
    grok_home.mkdir(mode=0o700)
    _grok_account(monkeypatch, real_home)
    if problem in {"env-relative", "env-parent", "env-mismatch"}:
        monkeypatch.setenv("HOME", {"env-relative": "owner", "env-parent": str(real_home / "../owner"),
                                    "env-mismatch": str(tmp_path / "other")}[problem])
    elif problem in {"account-relative", "account-parent"}:
        _grok_account(monkeypatch, "owner" if problem == "account-relative" else real_home / "../owner")
    elif problem in {"missing", "file"}:
        grok_home.rmdir()
        if problem == "file":
            grok_home.touch()
    elif problem == "home-group-write":
        real_home.chmod(0o775)
        monkeypatch.setattr("kotoyose.provider_data_runtime._private_group", lambda gid, account: False)
    elif problem == "grok-world-write":
        grok_home.chmod(0o707)
    else:
        wrong_owner = real_home if problem == "home-owner" else grok_home
        lstat = Path.lstat

        def lstat_with_wrong_owner(path):
            info = lstat(path)
            if path == wrong_owner:
                return SimpleNamespace(st_mode=info.st_mode, st_uid=info.st_uid + 1, st_gid=info.st_gid)
            return info

        monkeypatch.setattr(Path, "lstat", lstat_with_wrong_owner)
    with ExitStack() as leases, pytest.raises(DeclarationRejected, match=error):
        acp_data_env(row, tmp_path / "child", leases)
    assert not (tmp_path / "child").exists()


@pytest.fixture
def grok_write_turn(tmp_path, monkeypatch):
    monkeypatch.setattr("kotoyose.cli_install_spec.platform_key", lambda: "linux-x64")
    (tmp_path / "child-parent").mkdir(mode=0o700)
    home = _child_home(tmp_path / "child-parent/child")
    real_home = tmp_path / "owner-parent/owner"
    real_home.mkdir(mode=0o700, parents=True)
    (real_home / ".grok").mkdir(mode=0o700)
    _grok_account(monkeypatch, real_home)
    _mock_landlock(monkeypatch)
    row = _offer(_GROK_DECLARATION.read_bytes(), home)
    approve(home, row.id, sha256=row.sha256, capability="write")
    (home / "policy.json").write_text(json.dumps({
        "workspace_roots": [str(tmp_path)], "adapters": [row.id], "mcp": {}, "logins": [],
    }))
    config = _grok_host_config()
    _grok_write_alias(config, tmp_path)
    return home, row, {
        "alias": "grok-write", "preset": config["models"]["grok-write"],
        "route": config["routes"]["grok"], "connection": config["connections"]["dev"],
        "declaration_sha256": row.sha256, "capability": "write",
    }


@pytest.mark.parametrize("approval, surface, refs, requested, inline, error", [
    ("read", "write", ["files"], "mcp", False, "has not approved mcp"),
    ("write", "write", ["files"], "mcp", False, "has not approved mcp"),
    ("mcp", "read", ["files"], "mcp", False, "mcp_refs require a write surface"),
    ("mcp", "write", ["files"], "write", False, "must match"),
    ("mcp", "write", [], "mcp", False, "must match"),
    ("mcp", "write", ["missing"], "mcp", False, "unknown MCP 'missing'"),
    ("mcp", "write", ["files"], "mcp", True, "cannot set mcp_servers"),
    ("mcp", "write", ["files"], "mcp", False, None),
    ("mcp", "write", [], "write", False, None),
    ("mcp", "read", [], "read", False, None),
])
def test_child_mcp_gates(tmp_path, grok_write_turn, approval, surface, refs, requested, inline, error):
    home, row, payload = grok_write_turn
    workspace = tmp_path / "work"
    workspace.mkdir()
    policy_file = home / "policy.json"
    policy = json.loads(policy_file.read_text())
    policy["mcp"] = {
        "files": {"command": "local-server", "args": ["serve"], "env": {"SETTING": "local"}},
        "unused": {"command": "unused-server", "args": []},
    }
    policy_file.write_text(json.dumps(policy))
    approve(home, row.id, sha256=row.sha256, capability=approval)
    payload.update(cwd=str(workspace), capability=requested)
    payload["preset"]["agent"]["surface"] = surface
    payload["preset"]["mcp_refs"] = refs
    if inline:
        payload["preset"]["mcp_servers"] = {"files": {"command": "host-server"}}
    with use_approved(home):
        if error is not None:
            with pytest.raises(ChildTurnError, match=error):
                _prepare(home, payload)
        else:
            prepared = _prepare(home, payload)
            try:
                assert set(prepared["config"].mcps) == set(refs)
                if refs:
                    assert prepared["config"].mcps["files"].server == policy["mcp"]["files"]
            finally:
                prepared["cleanup"]()


@pytest.mark.parametrize("name, valid", [
    ("Files-2", True), ("2files", True), ("files__other", False), ("file_name", False),
    ("files.v2", False), ("file server", False), ("日本語", False), ("-files", False), ("files\n", False),
])
def test_data_child_mcp_requires_unambiguous_server_names(tmp_path, grok_write_turn, name, valid):
    home, row, payload = grok_write_turn
    workspace = tmp_path / "work"
    workspace.mkdir()
    policy_file = home / "policy.json"
    policy = json.loads(policy_file.read_text())
    policy["mcp"] = {name: {"command": "local-server", "args": []}}
    policy_file.write_text(json.dumps(policy))
    approve(home, row.id, sha256=row.sha256, capability="mcp")
    payload.update(cwd=str(workspace), capability="mcp")
    payload["preset"]["mcp_refs"] = [name]
    with use_approved(home):
        if valid:
            prepared = _prepare(home, payload)
            try:
                assert set(prepared["config"].mcps) == {name}
            finally:
                prepared["cleanup"]()
        else:
            with pytest.raises(ChildTurnError, match="data provider MCP names in mcp_refs must match"):
                _prepare(home, payload)


async def test_child_config_validation_keeps_mcp_env_out_of_error_frame_and_log(tmp_path, grok_write_turn):
    home, row, payload = grok_write_turn
    workspace = tmp_path / "work"
    workspace.mkdir()
    policy_file = home / "policy.json"
    policy = json.loads(policy_file.read_text())
    secret = "test-local-mcp-secret"
    policy["mcp"] = {"files": {"command": "local-server", "args": [], "env": {"": secret}}}
    policy_file.write_text(json.dumps(policy))
    approve(home, row.id, sha256=row.sha256, capability="mcp")
    payload.update(cwd=str(workspace), capability="mcp", prompt={"text": "search"})
    payload["preset"]["mcp_refs"] = ["files"]
    send = AsyncMock()
    with use_approved(home), capture_logs() as logs:
        await ChildTurns(home, send, None)._handle_turn(2, payload)
    _, kind, body = send.call_args.args
    send.assert_awaited_once()
    assert kind == "error" and body["message"] == "Invalid child turn configuration."
    warning, = [log for log in logs if log["event"] == "child.turn.invalid_configuration"]
    assert warning["error_types"] == ["value_error"]
    assert secret not in repr((body, logs))


@pytest.mark.parametrize("workspace, error", [
    ("child-parent", "overlaps the child home"),
    ("child-parent/child", "overlaps the child home"),
    ("child-parent/child/data", "overlaps the child home"),
    ("owner-parent", "OS account home"),
    ("owner-parent/owner", "OS account home"),
    ("owner-parent/owner/.grok", "overlaps GROK_HOME"),
    ("owner-parent/owner/.grok/data", "overlaps GROK_HOME"),
])
def test_prepare_refuses_write_cwd_overlapping_protected_paths(tmp_path, grok_write_turn, workspace, error):
    home, _, payload = grok_write_turn
    cwd = tmp_path / workspace
    cwd.mkdir(parents=True, exist_ok=True)
    alias = tmp_path / "workspace-link"
    alias.symlink_to(cwd, target_is_directory=True)
    with use_approved(home), pytest.raises(ChildTurnError, match=error):
        _prepare(home, {**payload, "cwd": str(alias)})


async def test_acp_rechecks_write_workspace_before_spawn(tmp_path, monkeypatch, grok_write_turn):
    from kotoyose.runtime import acp

    home, _, payload = grok_write_turn
    cwd = tmp_path / "owner-parent/owner/project"
    cwd.mkdir()
    spawn = AsyncMock()
    monkeypatch.setattr(acp.AcpClient, "spawn", spawn)
    monkeypatch.setattr(acp, "resolve_print_cli_binary", lambda *_: tmp_path / "fake-grok")
    monkeypatch.setattr(provider_data_runtime, "managed_binary", lambda *_: tmp_path / "fake-grok")
    with use_approved(home):
        prepared = _prepare(home, {**payload, "cwd": str(cwd)})
        try:
            runtime = await _build_runtime(prepared)
            cwd.rmdir()
            cwd.symlink_to(home, target_is_directory=True)
            with pytest.raises(RuntimeTurnError, match="overlaps the child home"):
                await runtime.ask("write")
            spawn.assert_not_called()
        finally:
            prepared["cleanup"]()


@pytest.mark.parametrize("capability", ["read", "write", "mcp", "host-http-mcp"])
async def test_grok_child_prepends_skills_in_each_linux_acp_mode(tmp_path, monkeypatch, grok_write_turn, capability):
    from kotoyose.runtime import acp
    from tests.test_acp import FAKE_ACP, seen

    relayed = capability == "host-http-mcp"
    if relayed:
        capability = "mcp"
    relay_mcps = {"files": {"server": {
        "type": "http", "url": "http://127.0.0.1:49821/mcp/0", "headers": {"Authorization": "Bearer fixture"}
    }}} if relayed else None
    home, row, payload = grok_write_turn
    payload.update(capability=capability, skill_format="prompt_prefix", prompts=["brief"],
                   skills=[{"name": "review", "text": "本文"}])
    if capability == "read":
        payload.update(alias="grok-read", preset=_grok_host_config()["models"]["grok-read"])
    else:
        workspace = tmp_path / "work"
        workspace.mkdir()
        payload["cwd"] = str(workspace)
    if capability == "mcp":
        approve(home, row.id, sha256=row.sha256, capability="mcp")
        payload["preset"]["mcp_refs"] = ["host:files" if relayed else "files"]
        policy_file = home / "policy.json"
        policy = json.loads(policy_file.read_text())
        policy["mcp"] = {"files": {"command": "local-server", "args": []}}
        policy_file.write_text(json.dumps(policy))

    fake = tmp_path / "fake-grok"
    fake.write_text(f"#!{sys.executable}\n" + f"""
import runpy, sys
assert sys.argv[1:3] == ["--sandbox", {('read-only' if capability == 'read' else 'strict')!r}]
sys.argv[1:] = ["grok_mcp_empty"]
runpy.run_path({str(FAKE_ACP)!r}, run_name="__main__")
""")
    fake.chmod(0o700)
    monkeypatch.setattr(acp, "resolve_print_cli_binary", lambda *_: fake)
    monkeypatch.setattr(provider_data_runtime, "managed_binary", lambda *_: fake)
    with use_approved(home):
        prepared = _prepare(home, payload, relay_mcps=relay_mcps)
        runtime = await _build_runtime(prepared)
        try:
            await runtime.ask("question")
            messages = seen(runtime.cwd)
            prompt, = [msg["params"]["prompt"] for msg in messages if msg.get("method") == "session/prompt"]
            if relayed:
                session_new, = [msg["params"] for msg in messages if msg.get("method") == "session/new"]
                assert session_new["mcpServers"] == [{
                    "type": "http", "name": "files", "url": "http://127.0.0.1:49821/mcp/0",
                    "headers": [{"name": "Authorization", "value": "Bearer fixture"}],
                }]
            assert prompt == [{"type": "text", "text": "## Skill: review\n\n本文\n\nbrief\n\nquestion"}]
        finally:
            await runtime.disconnect()
            prepared["cleanup"]()


@pytest.mark.parametrize("capability", ["read", "write", "mcp"])
@pytest.mark.parametrize("stage", ["prepare", "spawn"])
@pytest.mark.parametrize("landlock", ["abi-4", "abi-5", "ENOSYS", "EOPNOTSUPP", "abi-0", "abi-1", "abi-2", "abi-3"])
async def test_grok_acp_requires_landlock(tmp_path, monkeypatch, grok_write_turn, capability, stage, landlock):
    from kotoyose.runtime import acp

    home, row, payload = grok_write_turn
    payload["capability"] = capability
    if capability == "read":
        payload.update(alias="grok-read", preset=_grok_host_config()["models"]["grok-read"])
    else:
        workspace = tmp_path / "work"
        workspace.mkdir()
        payload["cwd"] = str(workspace)
    if capability == "mcp":
        approve(home, row.id, sha256=row.sha256, capability="mcp")
        payload["preset"]["mcp_refs"] = ["files"]
        policy_file = home / "policy.json"
        policy = json.loads(policy_file.read_text())
        policy["mcp"] = {"files": {"command": "local-server", "args": []}}
        policy_file.write_text(json.dumps(policy))

    binary = tmp_path / "fake-grok"
    spawn = AsyncMock(side_effect=acp.AcpError("fake_spawn"))
    monkeypatch.setattr(acp.AcpClient, "spawn", spawn)
    monkeypatch.setattr(acp, "resolve_print_cli_binary", lambda *_: binary)
    monkeypatch.setattr(provider_data_runtime, "managed_binary", lambda *_: binary)
    with use_approved(home):
        prepared = _prepare(home, payload) if stage == "spawn" else None
        try:
            syscall = _mock_landlock(monkeypatch)
            query_abi = syscall.side_effect

            def probe(*args):
                query_abi(*args)
                if landlock in {"ENOSYS", "EOPNOTSUPP"}:
                    ctypes.set_errno(getattr(errno, landlock))
                    return -1
                return int(landlock.removeprefix("abi-"))

            syscall.side_effect = probe
            available = landlock in {"abi-4", "abi-5"}
            if stage == "prepare":
                if available:
                    prepared = _prepare(home, payload)
                else:
                    with pytest.raises(ChildTurnError, match=r"Landlock ABI 4 or later \(kernel 6.7\+\)"):
                        _prepare(home, payload)
            else:
                runtime = await _build_runtime(prepared)
                with pytest.raises(RuntimeTurnError, match="fake_spawn" if available else "requires Linux Landlock ABI 4"):
                    await runtime.ask("turn")
            if stage == "spawn" and available:
                spawn.assert_awaited_once()
                assert spawn.call_args.args[0][:3] == [str(binary), "--sandbox", "read-only" if capability == "read" else "strict"]
            else:
                spawn.assert_not_called()
            syscall.assert_called_once()
        finally:
            if prepared is not None:
                prepared["cleanup"]()


@pytest.mark.parametrize("initial, reapproval, replacement, stops", [
    ("write", "read", False, True),
    ("mcp", "write", False, True),
    ("mcp", "read", False, True),
    ("write", "mcp", False, False),
    ("write", "write", True, True),
    ("write", "write", False, False),
    ("read", "write", False, False),
])
def test_reapproval_stops_provider_processes_only_when_the_grant_is_reduced_or_replaced(
    grok_write_turn, initial, reapproval, replacement, stops,
):
    home, row, _ = grok_write_turn
    previous = approve(home, row.id, sha256=row.sha256, capability=initial)
    if replacement:
        row = _offer((row.document + "\n").encode(), home)
    stopped = []
    track_process(row.id, 1, lambda: stopped.append(row.id))
    track_process("other", 2, lambda: stopped.append("other"))
    try:
        approval = approve(home, row.id, sha256=row.sha256, capability=reapproval)
        assert (approval.row.sha256, approval.capability) == (row.sha256, reapproval)
        assert (approval.generation != previous.generation) == stops
        assert stopped == ([row.id] if stops else [])
    finally:
        untrack_process(row.id, 1)
        untrack_process("other", 2)


def test_host_config_can_load_the_data_profile_at_startup(tmp_path):
    upload_declaration(_GROK_DECLARATION.read_bytes())
    path = tmp_path / "host-routing.yaml"
    path.write_text(json.dumps(_grok_host_config()))
    env = {**os.environ, "KOTOYOSE_CONFIG": str(path), "PYTHON_DOTENV_DISABLED": "1"}
    result = subprocess.run(
        [sys.executable, "-I", "-c", dedent("""\
            from kotoyose.config import get_config
            cfg = get_config()
            route = cfg.routes["grok"]
            conn = cfg.connections[route.connection]
            preset = cfg.preset_for("grok-read")
            assert preset.on == conn.on == "child:box"
            assert preset.provider == "cli_acp"
            assert conn.provider == route.profile == "grok-build"
            assert conn.secret is conn.config_dir is None
        """)],
        env=env, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_startup_omits_invalid_data_profile_dependencies_until_reload(tmp_path):
    from kotoyose.print_cli_grok_profile import GROK_ACP_ARGV

    fixed = _GROK_DECLARATION.read_bytes()
    invalid = _mutate(fixed, "acp", "argv", value=list(GROK_ACP_ARGV))
    directory = Path(os.environ["XDG_DATA_HOME"]) / "kotoyose" / "provider-declarations"
    directory.mkdir(parents=True)
    stored = directory / "operator-grok.json"
    stored.write_bytes(invalid)
    config = _grok_host_config()
    config["connections"]["dev"]["data_policy"] = "unknown"
    config["models"]["grok-read"].update(bare=True, agent={"place": "host", "surface": "read"})
    config["models"].update({
        "unaffected": {},
        "delegates-to-grok": {"delegates": ["grok-read"]},
        "grok-pool": {"candidates": ["grok-read"], "policy": "first_available"},
    })
    config["clients"] = {
        "example": {"models": ["unaffected", " grok-read ", "grok-pool"]},
        "grok-only": {"models": ["grok-read"]},
    }
    config["training_acks"] = [{
        "alias": "grok-read", "capabilities": ["tools"], "digest": "a" * 64,
        "reasons": ["unknown_training_policy"], "acknowledged_by": "owner-test", "at": "2026-11-24",
    }]
    path = tmp_path / "host-routing.yaml"
    path.write_text(json.dumps(config))
    env = {**os.environ, "KOTOYOSE_CONFIG": str(path), "PYTHON_DOTENV_DISABLED": "1", "KOTOYOSE_TOKEN": "owner-test"}
    result = subprocess.run(
        [sys.executable, "-I", "-c", dedent("""\
            import sys
            from pathlib import Path

            from fastapi.testclient import TestClient
            from kotoyose.app import app
            from kotoyose.config import BASIC_AUTH_USER, get_config
            from kotoyose.provider_data_host import host_catalog

            fixed, stored, routing = map(Path, sys.argv[1:])
            invalid_bytes, yaml_bytes = stored.read_bytes(), routing.read_bytes()
            csrf = {"X-Kotoyose-Admin": "1", "Origin": "http://testserver"}
            cfg = get_config()
            assert set(cfg.models) == {"unaffected"}
            assert not (cfg.connections or cfg.routes or cfg.pools or host_catalog().offers())
            assert cfg.clients["example"].models == ["unaffected"]
            assert cfg.clients["grok-only"].models == []
            assert cfg.preset_for("unaffected")
            assert len(cfg.startup_warnings) == 9

            with TestClient(app) as client:
                client.auth = (BASIC_AUTH_USER, "owner-test")
                assert client.get("/health").json()["status"] == "ok"
                listing = client.get("/admin/provider-declarations").json()
                assert listing["startup_warnings"] == list(cfg.startup_warnings)
                [row] = listing["declarations"]
                assert row["id"] == row["profile_id"] == "grok-build"
                assert row["status"] == "invalid"
                assert row["reason"] == "unsupported declaration arguments"
                assert stored.read_bytes() == invalid_bytes
                assert client.post("/admin/reload", headers=csrf).status_code == 422
                assert get_config() is cfg
                save = client.put("/admin/settings/provider-order", headers=csrf, json={"provider_order": ["grok"]})
                assert save.status_code == 422
                assert routing.read_bytes() == yaml_bytes

                response = client.post("/admin/provider-declarations", headers=csrf, content=fixed.read_bytes())
                assert response.status_code == 200, response.text
                assert response.json()["startup_warnings"]
                assert stored.read_bytes() == fixed.read_bytes()
                response = client.post("/admin/reload", headers=csrf)
                assert response.status_code == 200, response.text
                active = get_config()
                assert active.preset_for("grok-read").on == "child:box"
                assert set(active.models) == {"unaffected", "grok-read", "delegates-to-grok"}
                assert set(active.connections) == {"dev"}
                assert set(active.routes) == {"grok"}
                assert set(active.pools) == {"grok-pool"}
                assert active.clients["example"].models == ["unaffected", "grok-read", "grok-pool"]
                assert client.get("/admin/provider-declarations").json()["startup_warnings"] == []
                assert routing.read_bytes() == yaml_bytes

                stored.write_bytes(invalid_bytes)
                assert client.post("/admin/reload", headers=csrf).status_code == 422
                assert get_config() is active
                assert host_catalog().offers()[0]["id"] == "grok-build"
                response = client.post("/admin/provider-declarations/reload", headers=csrf)
                assert response.json()["declarations"][0]["status"] == "invalid"
                assert not host_catalog().offers()
                assert client.get("/admin/config").json()["alias_data_policies"]["grok-read"] == {
                    "data_policy": "unknown", "training_ack": False,
                }
        """), str(_GROK_DECLARATION), str(stored), str(path)],
        env=env, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    output = result.stdout + result.stderr
    assert output.count("config.startup_skipped") == 9
    assert "provider_data.invalid_declaration" in output
    assert "grok-build" in output and "unsupported declaration arguments" in output


def test_unknown_builtin_profile_is_fatal_even_with_a_stored_name(tmp_path):
    upload_declaration(_GROK_DECLARATION.read_bytes())
    config = _grok_host_config()
    config["connections"]["dev"].update(provider="grok", profile="grok-bulid")
    config["routes"]["grok"]["profile"] = "grok-bulid"
    directory = Path(os.environ["XDG_DATA_HOME"]) / "kotoyose" / "provider-declarations"
    (directory / "grok-bulid.json").write_bytes(b"{")
    path = tmp_path / "host-routing.yaml"
    path.write_text(json.dumps(config))
    result = subprocess.run(
        [sys.executable, "-I", "-c", "import kotoyose.config"],
        env={**os.environ, "KOTOYOSE_CONFIG": str(path), "PYTHON_DOTENV_DISABLED": "1"},
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode != 0
    assert "Unknown print CLI profile: 'grok-bulid'" in result.stderr


def test_missing_file_leaves_unknown_profile_as_a_validation_error(monkeypatch, tmp_path):
    from kotoyose.config import _load_yaml_config

    path = tmp_path / "host-routing.yaml"
    path.write_text(json.dumps(_grok_host_config()))
    monkeypatch.setenv("KOTOYOSE_CONFIG", str(path))

    with pytest.raises(ValueError, match="Unknown print CLI profile: 'grok-build'"):
        _load_yaml_config(startup=True)


def test_startup_can_identify_a_missing_profile_by_its_stored_provider(monkeypatch, tmp_path):
    from kotoyose.config import _load_yaml_config

    upload_declaration(_GROK_DECLARATION.read_bytes())
    config = _grok_host_config()
    del config["connections"]["dev"]["profile"]
    config["routes"]["grok"]["profile"] = "previous-grok-profile"
    path = tmp_path / "host-routing.yaml"
    path.write_text(json.dumps(config))
    monkeypatch.setenv("KOTOYOSE_CONFIG", str(path))

    active = _load_yaml_config(startup=True)

    assert set(active.connections) == {"dev"}
    assert not active.routes and not active.models
    assert len(active.startup_warnings) == 2
    assert "data profile 'previous-grok-profile' is unavailable" in active.startup_warnings[0]


class _DownloadBody(httpx.AsyncByteStream):
    def __init__(self, *chunks):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


@pytest.mark.parametrize("scenario", ["success", "redirect", "private", "size", "hash", "offline", "timeout"])
def test_approved_child_download(monkeypatch, tmp_path, scenario):
    artifact = b"#!/bin/sh\necho 1.0.0\n"
    raw = _pinned(artifact, platform_key())
    raw = _mutate(raw, "network", "download_hosts", value=["example.com", "mirror.example.com"])
    home = _child_home(tmp_path / "child")
    row = _offer(raw, home)
    approve(home, row.id, sha256=row.sha256)
    requests = []
    resolutions = []

    def resolve(host, port, *_args):
        resolutions.append((host, port))
        if scenario == "offline":
            raise socket.gaierror("offline")
        address = "192.168.1.2" if scenario == "private" else "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, port))]

    async def respond(request):
        requests.append(request)
        assert request.url.scheme == "https"
        assert request.url.host == "93.184.216.34"
        assert request.headers["Host"] == request.extensions["sni_hostname"] == "example.com"
        if request.url.path == "/fake-cli" and scenario in {"success", "redirect"}:
            location = "/release" if scenario == "success" else "https://mirror.example.com/release"
            return httpx.Response(302, headers={"Location": location})
        if scenario == "timeout":
            await asyncio.Future()
        if scenario == "size":
            return httpx.Response(200, stream=_DownloadBody(b"x" * 16, b"x" * 16))
        return httpx.Response(200, stream=_DownloadBody(b"wrong" if scenario == "hash" else artifact))

    real_client = httpx.AsyncClient

    def client(**kwargs):
        assert not kwargs["trust_env"] and not kwargs["follow_redirects"]
        return real_client(transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(provider_data_download.httpx, "AsyncClient", client)
    if scenario == "size":
        monkeypatch.setattr(provider_data_download, "MAX_ARTIFACT_BYTES", 24)
    if scenario == "timeout":
        monkeypatch.setattr(provider_data_download, "_DOWNLOAD_TIMEOUT", 0.1)
    request = {"action": "install", "id": row.id, "home": str(home), "sha256": row.sha256}
    assert validate_request({"action": "advertise", "home": str(home)}) == {"providers": []}
    if scenario == "success":
        assert validate_request(request) == {"installed": True}
        binary = installed_path(row, home)
        assert binary is not None and binary.read_bytes() == artifact
        assert stat.S_IMODE(binary.stat().st_mode) == 0o500
        assert stat.S_IMODE(binary.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE((binary.parent / "manifest.json").stat().st_mode) == 0o600
        assert [r.url.path for r in requests] == ["/fake-cli", "/release"]
        assert resolutions == [("example.com", 443), ("example.com", 443)]
        assert validate_request({"action": "advertise", "home": str(home)})["providers"][0]["sha256"] == row.sha256
    else:
        error = {
            "redirect": "original download host", "private": "non-public IP", "size": "download size limit",
            "hash": "SHA-256 mismatch", "offline": "DNS lookup failed", "timeout": "download timed out",
        }[scenario]
        with pytest.raises(ValueError, match=error):
            validate_request(request)
        assert installed_path(row, home) is None
        assert not any(path.is_file() and path.stat().st_mode & 0o111 for path in (home / "tools").rglob("*"))
        if scenario in {"private", "offline"}:
            assert requests == []
        if scenario == "redirect":
            assert resolutions == [("example.com", 443)]


@pytest.mark.asyncio
@pytest.mark.skipif(platform_key() != "linux-x64", reason="the example pins only Linux x64")
@pytest.mark.parametrize("stop", ["cancel", "downgrade"])
async def test_grok_child_device_login_status_and_acp_turns(tmp_path, capsys, monkeypatch, stop):
    from kotoyose.routes import admin_children

    body = json.loads(_GROK_DECLARATION.read_bytes())
    body.pop("native_skill_layout")
    owner_home = tmp_path / "owner"
    owner_home.mkdir(mode=0o700)
    _grok_account(monkeypatch, owner_home)
    _mock_landlock(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    signed_in = owner_home / ".grok" / "signed-in"
    notes = workspace / "notes"
    pids = workspace / "pids"
    late_pids = workspace / "late-pids"
    notes.write_text("fake file contents")
    source = tmp_path / "fake-grok"
    source.write_text(f"#!{sys.executable}\n" + dedent(f"""\
        import json
        import os
        from pathlib import Path
        import subprocess
        import sys
        import time
        args = sys.argv[1:]
        owner_home = Path({str(owner_home)!r})
        grok_home = Path(os.environ.get("GROK_HOME", Path.home() / ".grok"))
        signed_in = grok_home / "signed-in"
        if args in (["--version"], ["models"], ["login", "--device-auth"]):
            assert Path.home() == owner_home
            assert "GROK_HOME" not in os.environ
        if args == ["--version"]:
            print("grok 1.0.89")
            sys.exit(0)
        if args == ["models"]:
            if signed_in.exists():
                sys.exit(int(signed_in.read_text()))
            print("You are not authenticated.", file=sys.stderr)
            sys.exit(0)
        if args == ["login", "--device-auth"]:
            assert sys.stdout.isatty()
            print("https://auth.x.ai@unofficial.example/device", flush=True)
            print("https://auth.x.ai/device ABCD-1234", flush=True)
            print("internal diagnostics", flush=True)
            time.sleep(0.5)
            grok_home.mkdir(mode=0o700)
            signed_in.write_text("0")
            sys.exit(0)

        is_write = args[:2] == ["--sandbox", "strict"]
        acp = {body["acp"]!r}
        assert args == acp["write_argv" if is_write else "argv"][:-2] + ["fake-model", "stdio"]
        assert Path.home().name.startswith("kotoyose-acp-home-")
        assert Path.home() != owner_home
        assert grok_home == owner_home / ".grok"
        assert all(os.environ[k] == v for k, v in {body["isolation"]["env"]!r}.items())
        assert signed_in.exists()
        def send(**message):
            print(json.dumps({{"jsonrpc": "2.0", **message}}), flush=True)
        for line in sys.stdin:
            message = json.loads(line)
            method = message.get("method")
            if "id" not in message:
                continue
            if method == "initialize":
                result = {{"protocolVersion": 1, "agentCapabilities": {{}}}}
            elif method == "session/new":
                servers = message["params"]["mcpServers"]
                is_mcp = bool(servers)
                if is_mcp:
                    assert is_write
                    assert servers == [{{
                        "name": "files", "command": "local-server", "args": ["serve"],
                        "env": [{{"name": "SETTING", "value": "local"}}],
                    }}]
                    native = [{{"name": "native-files"}}] if Path("native-mcp").exists() else []
                    send(method="_x.ai/mcp/servers_updated", params={{"sessionId": "s1", "mcpServers": native}})
                result = {{"sessionId": "s1"}}
            elif method == "session/prompt":
                prompt = message["params"]["prompt"][-1]["text"]
                assert is_write == (prompt != "read the notes")
                assert is_mcp == (prompt == "search with MCP")
                if is_write:
                    assert Path.cwd() == Path({str(workspace)!r})
                if prompt == "hold":
                    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
                    Path({str(pids)!r}).write_text(f"{{os.getpid()}} {{child.pid}}")
                    time.sleep(60)
                choice = "yes" if is_write else "no"
                for kind, expected in (("edit", choice), ("execute", "no"), ("move", "no"), ("read", "yes"),
                                       ("other", "yes" if is_mcp else "no")):
                    call = {{"toolCallId": kind, "kind": kind, "title": kind}}
                    if kind == "other":
                        call["title"] = "files__search"
                        call["rawInput"] = {{"variant": "UseTool", "tool_name": "files__search", "tool_input": {{}}}}
                    send(id=kind, method="session/request_permission", params={{
                        "sessionId": "s1", "toolCall": call,
                        "options": [{{"kind": "allow_once", "optionId": "yes"}},
                                    {{"kind": "reject_once", "optionId": "no"}}],
                    }})
                    reply = json.loads(sys.stdin.readline())
                    assert reply["id"] == kind
                    assert reply["result"]["outcome"] == {{"outcome": "selected", "optionId": expected}}
                if is_mcp and Path("late-native-mcp").exists():
                    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
                    Path({str(late_pids)!r}).write_text(f"{{os.getpid()}} {{child.pid}}")
                    send(method="_x.ai/mcp/servers_updated", params={{
                        "sessionId": "s1", "mcpServers": [{{"name": "late-native-files"}}],
                    }})
                    time.sleep(60)
                send(method="session/update", params={{"sessionId": "s1", "update": {{
                    "sessionUpdate": "agent_message_chunk",
                    "content": {{"type": "text", "text": Path({str(notes)!r}).read_text()}},
                }}}})
                result = {{"stopReason": "end_turn"}}
            else:
                send(id=message["id"], error={{"code": -32601, "message": "unknown method"}})
                continue
            send(id=message["id"], result=result)
    """))
    body["platforms"]["linux-x64"]["install"]["sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    raw = json.dumps(body).encode()
    upload_declaration(raw)
    row = parse_declaration(raw)
    config = _grok_host_config()
    _grok_write_alias(config, workspace)
    config["models"]["grok-write"].update(tools=["edit", "execute", "read"], deny=["execute"])
    config["models"]["grok-mcp"] = {
        **config["models"]["grok-write"],
        "mcp_refs": ["files"], "tools": ["edit", "execute", "read", "files__search"],
    }
    config["mcps"] = {"files": {"server": {"command": "host-server", "args": [], "env": {"SETTING": "host"}}}}
    cfg = HarnessConfig.model_validate(config)
    hub = ChildHub(tmp_path / "hub")
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(child_hub=hub)))
    monkeypatch.setattr(admin_children, "get_config", lambda: cfg)
    await hub.start("127.0.0.1", 0)
    home = tmp_path / "child"
    try:
        await enroll_child(hub.issue_enrollment_code(f"127.0.0.1:{hub.port}"), home, name="box")
        (home / "policy.json").write_text(json.dumps({
            "workspace_roots": [str(workspace)], "adapters": [row.id], "logins": [],
            "mcp": {
                "files": {"command": "local-server", "args": ["serve"], "env": {"SETTING": "local"}},
                "unused": {"command": "unused-server", "args": []},
            },
        }))
        async with child_session(home) as session:
            await asyncio.wait_for(session.catalog_received.wait(), 5)
            approve(home, row.id, sha256=row.sha256)
            install_local(row, source, child_home=home)
            capsys.readouterr()
            await run_local_login(home, row.id, "status", "linux", _Parent())
            assert json.loads(capsys.readouterr().out) == {"state": "logged_out"}
            assert await fetch_child_login_status("box", row.id, row.profile_id, 5) == "logged_out"
            capsys.readouterr()
            await run_local_login(home, row.id, "start", "linux", _Parent())
            views = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
            assert views == [
                {"state": "running"},
                {"state": "running", "url": "https://auth.x.ai/device", "device_code": "ABCD-1234"},
                {"state": "logged_in"},
            ]
            assert await fetch_child_login_status("box", row.id, row.profile_id, 5) == "logged_in"
            signed_in.write_text("1")
            assert await fetch_child_login_status("box", row.id, row.profile_id, 5) == "unknown"
            signed_in.write_text("0")

        async with child_session(home):
            assert hub.list_children()[0]["providers"][0]["capability"] == "read"
            view = (await admin_children.list_children(request))["children"][0]
            assert [row["alias"] for row in view["alias_warnings"] if "write approval" in row["message"]] == ["grok-write"]
            runtime, _ = await get_or_resume_client(None, "grok-write", str(workspace), config_snapshot=cfg)
            with pytest.raises(RuntimeTurnError, match="child owner has not approved write"):
                await runtime.ask("write the notes")
            approve(home, row.id, sha256=row.sha256, capability="write")

        async with child_session(home):
            assert hub.list_children()[0]["providers"][0]["capability"] == "write"
            offer = preview(home)["offers"][0]
            assert offer["approved_capability"] == "write"
            assert not any("write approval" in item["message"] for item in
                           (await admin_children.list_children(request))["children"][0]["alias_warnings"])
            runtime, _ = await get_or_resume_client(None, "grok-mcp", str(workspace), config_snapshot=cfg)
            with pytest.raises(RuntimeTurnError, match="child owner has not approved mcp"):
                await runtime.ask("search with MCP")
            runtime, resumed = await get_or_resume_client(None, "grok-read", config_snapshot=cfg)
            assert isinstance(runtime, ChildRuntimeSession) and not resumed
            async with asyncio.timeout(10):
                results = [item async for item in runtime.stream("read the notes")]
            assert [(item.tool_name, item.summary) for item in results if isinstance(item, RuntimeEvent)] == [
                ("edit", "denied"), ("execute", "denied"), ("move", "denied"), ("read", "allowed"), ("other", "denied"),
            ]
            assert next(item for item in results if isinstance(item, RuntimeResult)).text == notes.read_text()

            for cwd, error in [(None, "requires a cwd"), (str(owner_home), "outside workspace_roots")]:
                runtime, _ = await get_or_resume_client(None, "grok-write", cwd, config_snapshot=cfg)
                with pytest.raises(RuntimeTurnError, match=error):
                    await runtime.ask("write the notes")
            runtime, _ = await get_or_resume_client(None, "grok-write", str(workspace), config_snapshot=cfg)
            async with asyncio.timeout(10):
                results = [item async for item in runtime.stream("write the notes")]
            assert [(item.tool_name, item.summary) for item in results if isinstance(item, RuntimeEvent)] == [
                ("edit", "allowed"), ("execute", "denied"), ("move", "denied"), ("read", "allowed"), ("other", "denied"),
            ]

            hold = asyncio.create_task(runtime.ask("hold"))
            try:
                async with asyncio.timeout(10):
                    while not pids.exists():
                        if hold.done():
                            await hold
                        await asyncio.sleep(0.02)
                    leader, child = (int(value) for value in pids.read_text().split())
                assert os.getpgid(leader) == os.getpgid(child) == leader
                if stop == "downgrade":
                    async with asyncio.timeout(10):
                        helper = await asyncio.create_subprocess_exec(
                            sys.executable, "-I", "-m", "kotoyose.provider_data_child", "validate",
                            env={"PYTHON_DOTENV_DISABLED": "1", "PATH": os.environ["PATH"]},
                            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                        )
                        stdout, stderr = await helper.communicate(json.dumps({
                            "action": "approve", "home": str(home), "id": row.id,
                            "sha256": row.sha256, "capability": "read",
                        }).encode())
                        assert helper.returncode == 0, stderr.decode()
                        assert json.loads(stdout)["offers"][0]["approved_capability"] == "read"
                        with pytest.raises(RuntimeTurnError):
                            await asyncio.shield(hold)
            finally:
                if not hold.done():
                    hold.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await hold

            def alive(pid):
                try:
                    return Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1][0] != "Z"
                except FileNotFoundError:
                    return False

            async with asyncio.timeout(10):
                while alive(leader) or alive(child):
                    await asyncio.sleep(0.02)

        approve(home, row.id, sha256=row.sha256, capability="mcp")
        received = []
        async with child_session(
            home, features=[PROVIDER_DATA_FEATURE, REPOSITORY_SKILLS_FEATURE, WORKSPACE_POLICY_FEATURE], received_turns=received,
        ):
            view = (await admin_children.list_children(request))["children"][0]
            assert view["providers"][0]["capability"] == "mcp"
            assert [(item["alias"], PROVIDER_DATA_MCP_FEATURE in item["message"]) for item in view["alias_warnings"]] == [
                ("grok-mcp", True),
            ]
            runtime, _ = await get_or_resume_client(None, "grok-mcp", str(workspace), config_snapshot=cfg)
            with pytest.raises(RuntimeTurnError, match="does not advertise data provider MCP turn support"):
                await runtime.ask("search with MCP")
            assert received == []
            runtime, _ = await get_or_resume_client(None, "grok-read", config_snapshot=cfg)
            assert (await runtime.ask("read the notes")).text == notes.read_text()
        async with child_session(home):
            view = (await admin_children.list_children(request))["children"][0]
            assert view["providers"][0]["capability"] == "mcp"
            assert PROVIDER_DATA_MCP_FEATURE in view["features"]
            assert view["alias_warnings"] == []
            runtime, _ = await get_or_resume_client(None, "grok-mcp", str(workspace), config_snapshot=cfg)
            native = workspace / "native-mcp"
            native.touch()
            with pytest.raises(RuntimeTurnError, match=r"native-files.*grok mcp disable <name>"):
                await runtime.ask("search with MCP")
            native.unlink()
            late_native = workspace / "late-native-mcp"
            late_native.touch()
            with monkeypatch.context() as patch:
                cancel = AsyncMock()
                patch.setattr(AcpClient, "cancel", cancel)
                with pytest.raises(RuntimeTurnError, match=r"late-native-files.*grok mcp disable <name>") as raised:
                    await runtime.ask("search with MCP")
                assert raised.value.terminal_reason == "grok_mcp_guard_failed"
                assert raised.value.retryable is False
                cancel.assert_not_called()
            late_native.unlink()
            leader, child = (int(value) for value in late_pids.read_text().split())
            async with asyncio.timeout(10):
                while alive(leader) or alive(child):
                    await asyncio.sleep(0.02)
            results = [item async for item in runtime.stream("search with MCP")]
            assert [(item.tool_name, item.summary) for item in results if isinstance(item, RuntimeEvent)] == [
                ("edit", "allowed"), ("execute", "denied"), ("move", "denied"), ("read", "allowed"), ("other", "allowed"),
            ]
            assert results[-1].text == notes.read_text()
    finally:
        await hub.stop()


def test_grok_acp_argv_without_the_read_only_sandbox_is_rejected():
    from kotoyose.print_cli_grok_profile import GROK_ACP_ARGV, GROK_READ_FLAGS

    document = json.loads(_GROK_DECLARATION.read_text())
    for argv in (GROK_ACP_ARGV, GROK_READ_FLAGS + GROK_ACP_ARGV):
        document["acp"]["argv"] = list(argv)
        with pytest.raises(ValueError):
            parse_declaration(json.dumps(document).encode())


def test_grok_write_requires_the_strict_argv_and_validated_isolation():
    from kotoyose.print_cli_grok_profile import GROK_ACP_ARGV

    document = json.loads(_GROK_DECLARATION.read_bytes())
    approved = document["acp"].pop("write_argv")
    document.pop("native_skill_layout")
    with pytest.raises(ValueError, match="requires.*write_argv"):
        parse_declaration(json.dumps(document).encode())
    for argv in (GROK_ACP_ARGV, ["--sandbox", "workspace", *approved[2:]], approved[2:]):
        document["acp"]["write_argv"] = list(argv)
        with pytest.raises(ValueError, match="unsupported declaration arguments"):
            parse_declaration(json.dumps(document).encode())
    document["acp"]["write_argv"] = approved
    isolation = document.pop("isolation")
    with pytest.raises(ValueError, match="requires Grok isolation"):
        parse_declaration(json.dumps(document).encode())
    for changes in (
        {"private_home": False}, {"keep_home_env": ["GROK_HOME", "HOME"]},
        {"env": {**isolation["env"], "GROK_CLAUDE_MCPS_ENABLED": "1"}},
        {"env": {**isolation["env"], "PATH": "/usr/bin"}},
    ):
        document["isolation"] = {**isolation, **changes}
        with pytest.raises(ValueError, match="unsupported declaration isolation"):
            parse_declaration(json.dumps(document).encode())


@pytest.mark.parametrize("guard, error", [
    (None, "MCP capability requires a validated ACP mcp_guard"),
    ("unknown_guard", "unsupported ACP mcp_guard"),
    ("_x.ai/mcp/servers_updated", "unsupported ACP mcp_guard"),
])
def test_mcp_declaration_requires_an_allowlisted_guard(guard, error):
    document = json.loads(_GROK_DECLARATION.read_bytes())
    if guard is None:
        del document["acp"]["mcp_guard"]
    else:
        document["acp"]["mcp_guard"] = guard
    with pytest.raises(ValueError, match=error):
        parse_declaration(json.dumps(document).encode())


def test_isolated_grok_accepts_a_user_private_group(tmp_path, monkeypatch):
    """Ubuntu の既定 (umask 002 + user private group) で作られた 775 の ~/.grok は受け入れる。"""
    import grp
    import pwd

    row = parse_declaration(_GROK_DECLARATION.read_bytes())
    real_home = tmp_path / "owner"
    real_home.mkdir(mode=0o700)
    grok_home = real_home / ".grok"
    grok_home.mkdir()
    grok_home.chmod(0o775)
    _grok_account(monkeypatch, real_home)
    account = pwd.getpwuid(os.getuid())
    shared = SimpleNamespace(gr_name="staff", gr_mem=["someone-else"])
    private = SimpleNamespace(gr_name=account.pw_name, gr_mem=[])
    from kotoyose import provider_data_runtime as runtime

    real_account = SimpleNamespace(pw_name=account.pw_name, pw_gid=grok_home.stat().st_gid)
    monkeypatch.setattr(grp, "getgrgid", lambda gid: private)
    assert runtime._private_group(grok_home.stat().st_gid, real_account)
    monkeypatch.setattr(grp, "getgrgid", lambda gid: shared)
    assert not runtime._private_group(grok_home.stat().st_gid, real_account)
    monkeypatch.setattr(runtime, "_private_group", lambda gid, account: True)
    with ExitStack() as leases:
        env = acp_data_env(row, tmp_path / "child", leases)
        assert env["GROK_HOME"] == str(grok_home)


def test_bundled_provider_bytes_and_owner_api(monkeypatch):
    from kotoyose.provider_data_host import bundled_declaration_bytes, bundled_declarations

    raw = (Path(__file__).resolve().parents[1] / "examples/provider-declarations/grok-build.json").read_bytes()
    assert bundled_declaration_bytes("grok-build") == raw
    assert any(row.id == "grok-build" for row in bundled_declarations())
    with pytest.raises(FileNotFoundError):
        bundled_declaration_bytes("../grok-build")
    monkeypatch.setattr("kotoyose.auth.API_KEY", "owner-secret")
    monkeypatch.setattr("kotoyose.auth.BASIC_AUTH_USER", "owner-name")
    endpoint = "/admin/provider-declarations/bundled/grok-build"
    csrf = {"X-Kotoyose-Admin": "1", "Origin": "http://testserver"}
    with TestClient(app) as client:
        assert client.post(endpoint).status_code == 401
        assert client.post(endpoint, headers={"Authorization": "Bearer owner-secret"}).status_code == 401
        client.auth = ("owner-name", "owner-secret")
        preset = next(row for row in client.get("/admin/provider-declarations").json()["bundled"] if row["id"] == "grok-build")
        assert preset["id"] == "grok-build"
        assert preset["installed"] is False
        assert preset["document"].encode() == raw
        assert client.post(endpoint).status_code == 403
        response = client.post(endpoint, headers=csrf)
        assert response.status_code == 200
        preset = next(row for row in response.json()["bundled"] if row["id"] == "grok-build")
        assert preset["installed"] is True
        assert preset["up_to_date"] is True
        assert response.json()["declarations"][0]["document"].encode() == raw
        assert response.json()["declarations"][0]["platforms"]
        assert client.post("/admin/provider-declarations/bundled/missing", headers=csrf).status_code == 404


@pytest.mark.parametrize("provider", ["grok-build", "agy-cli", "cursor-cli"])
def test_all_bundled_presets_match_examples(provider):
    from kotoyose.provider_data_host import bundled_declaration_bytes, bundled_declarations

    raw = (_GROK_DECLARATION.parent / f"{provider}.json").read_bytes()
    assert bundled_declaration_bytes(provider) == raw
    row = parse_declaration(raw)
    assert row.id == provider
    assert len(validate_catalog(bundled_declarations())) == 3
    if provider != "grok-build":
        assert row.acp.argv == ("acp",)
        assert next(command for command in row.commands("linux-x64") if command["operation"] == "acp")["placeholders"] == {}
    if provider == "agy-cli":
        assert row.login_flow == "paste_code"
        assert row.login.code_prompt_pattern == r"(?i)paste the authorization code"
        assert row.login.matches_status(0, {"status": "SUCCESS"})
        assert not row.login.matches_status(0, {"status": "FAILURE"})


def _archive_declaration(layout, bad_member=None):
    executable = b"#!/bin/sh\necho 1.0.0\n"
    stream = BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        files = [("vendor/fake", executable, 0o755)] if layout == "member" else [
            ("dist/bin/fake", executable, 0o755), ("dist/lib/data", b"resource", 0o644),
            ("dist/helper", b"helper", 0o755),
        ]
        for name, content, mode in files:
            member = tarfile.TarInfo(name)
            member.mode, member.size = mode, len(content)
            archive.addfile(member, BytesIO(content))
        if bad_member:
            member = tarfile.TarInfo("../escaped" if bad_member == "traversal" else "dist/link")
            if bad_member == "symlink":
                member.type, member.linkname = tarfile.SYMTYPE, "/etc/passwd"
            archive.addfile(member)
    payload = stream.getvalue()
    doc = json.loads(_declaration(platform_key()))
    pin = doc["platforms"][platform_key()]["install"]
    pin.update(format="tar.gz", sha256=hashlib.sha256(payload).hexdigest())
    pin.update({"member": "vendor/fake"} if layout == "member" else {"directory": "dist", "entrypoint": "bin/fake"})
    return json.dumps(doc).encode(), payload, executable


@pytest.mark.parametrize("layout", ["member", "directory"])
@pytest.mark.parametrize("method", ["local", "stream", "download"])
def test_archive_install_integrity_and_permissions(tmp_path, monkeypatch, layout, method):
    raw, payload, executable = _archive_declaration(layout)
    row = parse_declaration(raw)
    upload_declaration(raw)
    home = _child_home(tmp_path / "child")
    store_offers(home, _FINGERPRINT, [row.offer()])
    approve(home, row.id, sha256=row.sha256)
    assert preview(home)["offers"][0]["can_install"] is True
    if method == "local":
        source = tmp_path / "artifact.tar.gz"
        source.write_bytes(payload)
        path = install_local(row, source, child_home=home)
    elif method == "stream":
        path = provider_data_runtime.install_stream(row, BytesIO(payload), child_home=home, source="test")
    else:
        async def fetch(urls, hosts, output):
            output.write(payload)
            output.seek(0)
            return urls[0]

        monkeypatch.setattr(provider_data_download, "_fetch", fetch)
        provider_data_download.install_download(row, child_home=home)
        path = installed_path(row, home)
    assert installed_path(row, home) == path
    assert path.read_bytes() == executable
    release = path.parent if layout == "member" else path.parent.parent
    assert not (release / "artifact.part").exists()
    manifest = json.loads((release / "manifest.json").read_bytes())
    assert manifest["artifact"] == "sha256:" + hashlib.sha256(payload).hexdigest()
    assert manifest["executable"] == hashlib.sha256(executable).hexdigest()
    assert manifest["version"] == "1.0.0"
    assert stat.S_IMODE(path.stat().st_mode) == 0o500
    if layout == "directory":
        assert stat.S_IMODE((release / "lib").stat().st_mode) == 0o700
        assert stat.S_IMODE((release / "lib/data").stat().st_mode) == 0o400
        assert stat.S_IMODE((release / "helper").stat().st_mode) == 0o500
    path.chmod(0o700)
    path.write_bytes(b"modified")
    path.chmod(0o500)
    assert installed_path(row, home) is None


@pytest.mark.parametrize("layout", ["member", "directory"])
@pytest.mark.parametrize("bad_member", ["traversal", "symlink"])
def test_archive_rejects_unsafe_members(tmp_path, layout, bad_member):
    raw, payload, _ = _archive_declaration(layout, bad_member)
    row = parse_declaration(raw)
    upload_declaration(raw)
    home = _child_home(tmp_path / "child")
    store_offers(home, _FINGERPRINT, [row.offer()])
    approve(home, row.id, sha256=row.sha256)
    with pytest.raises(ValueError, match="Unsafe"):
        provider_data_runtime.install_stream(row, BytesIO(payload), child_home=home, source="test")
    assert installed_path(row, home) is None


@pytest.mark.parametrize("layout", [
    {"format": "raw", "member": "binary"},
    {"format": "tar.gz"},
    {"format": "tar.gz", "directory": "dist"},
    {"format": "tar.gz", "directory": "dist", "entrypoint": "../binary"},
    {"format": "tar.gz", "directory": "dist", "entrypoint": "binary", "member": "binary"},
])
def test_archive_schema_rejects_invalid_layout(layout):
    doc = json.loads(_declaration())
    doc["platforms"]["linux-x64"]["install"].update(layout)
    with pytest.raises(ValueError):
        parse_declaration(json.dumps(doc).encode())


async def _bump_fetch(urls, hosts, output):
    assert all(url.startswith("https://") for url in urls)
    output.write(b"new distribution")
    output.seek(0)
    return urls[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["grok-build", "agy-cli", "cursor-cli"])
async def test_bump_version_all_products(provider):
    from kotoyose.provider_data_bump import bump_declaration_version
    from kotoyose.provider_data_host import bundled_declaration_bytes

    document = bundled_declaration_bytes(provider).decode()
    result = await bump_declaration_version(document, "2.0.0", fetcher=_bump_fetch)
    updated = parse_declaration(result["document"].encode())
    assert updated.install.version == result["version"] == "2.0.0"
    assert updated.sha256 == result["sha256"]
    assert "native_skill_layout" not in json.loads(result["document"])
    for platform in result["platforms"]:
        assert all("2.0.0" in url for url in platform["urls"])
        assert platform["sha256"] == hashlib.sha256(b"new distribution").hexdigest()


@pytest.mark.asyncio
async def test_bump_overrides_and_validation():
    from kotoyose.provider_data_bump import bump_declaration_version

    document = _declaration().decode()
    fetcher = AsyncMock(side_effect=_bump_fetch)
    with pytest.raises(ValueError, match="override required"):
        await bump_declaration_version(document, "2.0.0", fetcher=fetcher)
    with pytest.raises(ValueError, match="declared host"):
        await bump_declaration_version(document, "2.0.0", url_overrides={"linux-x64": ["https://evil.com/cli"]}, fetcher=fetcher)
    for version in ("latest", "1.2", "1.2.3/evil", None):
        with pytest.raises(ValueError, match="version"):
            await bump_declaration_version(document, version, fetcher=fetcher)
    fetcher.assert_not_called()
    result = await bump_declaration_version(
        document, "2.0.0", url_overrides={"linux-x64": ["https://example.com/new-cli"]}, fetcher=fetcher,
    )
    assert result["platforms"][0]["urls"] == ["https://example.com/new-cli"]


def test_bump_version_api_preview_apply_and_owner_security(monkeypatch):
    monkeypatch.setattr("kotoyose.auth.API_KEY", "owner-secret")
    monkeypatch.setattr("kotoyose.auth.BASIC_AUTH_USER", "owner-name")
    fetcher = AsyncMock(side_effect=_bump_fetch)
    monkeypatch.setattr(provider_data_download, "_fetch", fetcher)
    endpoint = "/admin/provider-declarations/agy-cli/bump-version"
    csrf = {"X-Kotoyose-Admin": "1", "Origin": "http://testserver"}
    with TestClient(app) as client:
        assert client.post(endpoint, json={"version": "2.0.0"}).status_code == 401
        assert client.post(endpoint, headers={"Authorization": "Bearer owner-secret"}, json={"version": "2.0.0"}).status_code in (401, 403)
        client.auth = ("owner-name", "owner-secret")
        assert client.post(endpoint, json={"version": "2.0.0"}).status_code == 403
        fetcher.assert_not_called()
        response = client.post(endpoint, headers=csrf, json={"version": "2.0.0"})
        assert response.status_code == 200, response.text
        assert response.json()["applied"] is False
        assert not host_catalog().rows
        response = client.post(endpoint, headers=csrf, json={"version": "2.0.0", "apply": True})
        assert response.status_code == 200, response.text
        assert response.json()["applied"] is True
        assert response.json()["catalog"]["declarations"][0]["sha256"] == response.json()["sha256"]
        assert host_catalog().rows[0].install.version == "2.0.0"
        # 同梱版ではなく、適用済みの版から URL を更新する。
        response = client.post(endpoint, headers=csrf, json={"version": "3.0.0"})
        assert response.status_code == 200
        assert "3.0.0" in response.json()["platforms"][0]["urls"][0]
        count = fetcher.call_count
        assert client.post(endpoint, headers=csrf, json={
            "version": "2.0.0", "url_overrides": {"linux-x64": ["https://evil.com/artifact"]},
        }).status_code == 422
        assert client.post(endpoint, headers=csrf, json={"version": "latest"}).status_code == 422
        assert client.post(endpoint, headers=csrf, json={"version": "2.0.0", "apply": "false"}).status_code == 422
        assert client.post("/admin/provider-declarations/missing/bump-version", headers=csrf, json={"version": "2.0.0"}).status_code == 404
        assert fetcher.call_count == count
        fetcher.side_effect = provider_data_download.DownloadError("offline")
        assert client.post(endpoint, headers=csrf, json={"version": "3.0.0"}).status_code == 422
    monkeypatch.setattr("kotoyose.auth.API_KEY", None)
    with TestClient(app) as client:
        assert client.post(endpoint, headers=csrf, json={"version": "2.0.0"}).status_code == 403


@pytest.mark.parametrize("layout", ["member", "directory"])
def test_archive_pin_is_checked_before_extraction(tmp_path, monkeypatch, layout):
    raw, payload, _ = _archive_declaration(layout)
    row = parse_declaration(raw)
    home = _child_home(tmp_path / "child")
    store_offers(home, _FINGERPRINT, [row.offer()])
    approve(home, row.id, sha256=row.sha256)
    extract = Mock(side_effect=AssertionError("extraction ran before pin verification"))
    monkeypatch.setattr(provider_data_runtime, "_extract_archive", extract)
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        provider_data_runtime.install_stream(row, BytesIO(payload + b"corrupt"), child_home=home, source="test")
    extract.assert_not_called()


@pytest.mark.parametrize("layout", ["member", "directory"])
def test_archive_detects_version_check_modification(tmp_path, monkeypatch, layout):
    raw, payload, _ = _archive_declaration(layout)
    row = parse_declaration(raw)
    home = _child_home(tmp_path / "child")
    store_offers(home, _FINGERPRINT, [row.offer()])
    approve(home, row.id, sha256=row.sha256)

    def run(binary, *args, **kwargs):
        binary.chmod(0o700)
        binary.write_bytes(b"changed during version check")
        binary.chmod(0o500)
        return subprocess.CompletedProcess([], 0, b"1.0.0", b"")

    monkeypatch.setattr(provider_data_runtime, "_run", run)
    with pytest.raises(ValueError, match="changed during"):
        provider_data_runtime.install_stream(row, BytesIO(payload), child_home=home, source="test")
    assert installed_path(row, home) is None


@pytest.mark.parametrize("field,value", [
    ("executable", None), ("executable", "short"), ("executable", "a" * 64),
    ("artifact", "sha256:" + "a" * 64), ("version", "9.0.0"),
])
def test_archive_manifest_integrity(tmp_path, field, value):
    from kotoyose.provider_data_storage import atomic_write

    raw, payload, _ = _archive_declaration("directory")
    row = parse_declaration(raw)
    home = _child_home(tmp_path / "child")
    store_offers(home, _FINGERPRINT, [row.offer()])
    approve(home, row.id, sha256=row.sha256)
    path = provider_data_runtime.install_stream(row, BytesIO(payload), child_home=home, source="test")
    manifest = path.parent.parent / "manifest.json"
    record = json.loads(manifest.read_bytes())
    record[field] = value
    atomic_write(manifest, json.dumps(record).encode(), child=True)
    assert installed_path(row, home) is None
