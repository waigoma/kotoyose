"""Real admin-route round trips must retain explicit execution environments.

Use only the admin router: no application startup, database, provider process,
deployment, network request, or real credentials are needed for these tests.
"""

from copy import deepcopy

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient


_OMITTED = object()
_PROVIDERS = ("claude", "codex", "cursor", "antigravity")


@pytest.fixture
def admin_client(tmp_path, monkeypatch):
    path = tmp_path / "config.yml"
    path.write_text("models: {}\n", encoding="utf-8")
    monkeypatch.setenv("KOTOYOSE_CONFIG", str(path))
    monkeypatch.delenv("KOTOYOSE_EXECUTION_CONFIG", raising=False)
    monkeypatch.delenv("KOTOYOSE_COMPATIBILITY_CONFIG", raising=False)

    # Import after selecting the temporary configuration. Restore the prior
    # singleton on teardown so this module can run beside the full admin suite.
    from kotoyose import config
    from kotoyose.routes import admin

    monkeypatch.setattr(config, "_config", config.get_config())
    app = FastAPI()
    app.include_router(admin.router)
    app.dependency_overrides[admin.require_basic_auth] = lambda: None
    with TestClient(app) as client:
        yield client, path


def _reload(client):
    response = client.post("/admin/reload")
    assert response.status_code == 200, response.text


def _get_config(client):
    response = client.get("/admin/config")
    assert response.status_code == 200, response.text
    return response.json()


def _seed(admin_client, *, environment=_OMITTED, provider="codex", sandbox="read-only", runtime_provider=None):
    client, path = admin_client
    prompt = path.parent / "review.txt"
    prompt.write_text("Review the local fixture.\n", encoding="utf-8")
    preset = {
        "provider": provider,
        "model": "fixture-model",
        "cwd": str(path.parent),
        "prompt_file_refs": ["review-prompt"],
        "effort": "xhigh",
        "codex": {"timeout": 120},
    }
    if sandbox is not _OMITTED:
        preset["codex"]["sandbox"] = sandbox
    if environment is not _OMITTED:
        preset["execution_environment"] = environment
    data = {
        "models": {"review": deepcopy(preset), "unrelated": {"provider": "codex", "model": "other-model"}},
        "prompt_files": {"review-prompt": {"description": "Fixture prompt", "path": str(prompt)}},
    }
    if runtime_provider is not None:
        driver = {"claude": "claude.agent-sdk", "codex": "codex.app-server", "cursor": "cursor.sdk",
                  "antigravity": "antigravity.sdk"}[runtime_provider]
        connection = {"provider": runtime_provider, "kind": "subscription", "data_policy": "no_training"}
        if runtime_provider == "claude":
            connection["secret"] = "claude-oauth"  # Claude の subscription は setup-token の secret が必須
        data["connections"] = {"host": connection}
        data["routes"] = {"review-route": {"driver": driver, "model": "runtime-model", "connection": "host"}}
        for preset in (data["models"]["review"],):
            preset.pop("provider")
            preset.pop("model")
            preset["route"] = "review-route"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    _reload(client)
    return data


def _saved(admin_client):
    return yaml.safe_load(admin_client[1].read_text(encoding="utf-8"))


@pytest.mark.parametrize("environment", ["isolated", "host"])
@pytest.mark.parametrize("sandbox", [_OMITTED, None, "read-only", "workspace-write", "danger-full-access"],
                         ids=["omitted-sandbox", "null-sandbox", "read-only", "workspace-write", "danger-full-access"])
def test_full_model_round_trip_preserves_explicit_environment_and_fields(admin_client, environment, sandbox):
    client, _ = admin_client
    original = _seed(admin_client, environment=environment, sandbox=sandbox)
    before = _get_config(client)
    projected = before["models"]["review"]
    for key in ("provider", "model", "prompt_file_refs", "effort", "codex"):
        assert projected[key] == original["models"]["review"][key]

    # This is a full, unchanged PUT of exactly what GET returned, as performed
    # by reopening and saving an existing preset in the admin client.
    response = client.put("/admin/models/review", json=projected)
    assert response.status_code == 200, response.text
    persisted = _saved(admin_client)["models"]["review"]
    _reload(client)
    after = _get_config(client)
    assert persisted.get("execution_environment") == environment
    assert after["models"]["review"].get("execution_environment") == environment
    assert projected["execution_environment"] == environment
    assert persisted["codex"] == original["models"]["review"]["codex"]
    assert after == before


@pytest.mark.parametrize("provider, environment, sandbox", [
    ("cursor", "host", _OMITTED),
    ("antigravity", "host", _OMITTED),
    ("codex", "isolated", "full-access"),
])
def test_other_supported_modes_preserve_saved_preferences(admin_client, provider, environment, sandbox):
    client, _ = admin_client
    _seed(admin_client, provider=provider, environment=environment, sandbox=sandbox)
    before = _get_config(client)
    projected = before["models"]["review"]
    assert projected["execution_environment"] == environment
    response = client.put("/admin/models/review", json=projected)
    assert response.status_code == 200, response.text
    _reload(client)
    assert _get_config(client) == before
    saved = _saved(admin_client)["models"]["review"]
    assert saved["execution_environment"] == environment
    assert saved["codex"] == projected["codex"]


@pytest.mark.parametrize("provider", _PROVIDERS)
@pytest.mark.parametrize("environment", [_OMITTED, None], ids=["omitted", "null"])
def test_historical_omission_survives_model_round_trip(admin_client, provider, environment):
    client, _ = admin_client
    _seed(admin_client, environment=environment, provider=provider, sandbox=_OMITTED)
    before = _get_config(client)
    preset = before["models"]["review"]
    assert "execution_environment" not in preset
    assert "sandbox" not in preset["codex"]
    response = client.put("/admin/models/review", json=preset)
    assert response.status_code == 200, response.text
    _reload(client)
    assert _get_config(client) == before
    saved = _saved(admin_client)["models"]["review"]
    assert "execution_environment" not in saved
    assert "sandbox" not in saved["codex"]


@pytest.mark.parametrize("environment", ["isolated", "host"])
def test_route_round_trip_uses_effective_codex_provider(admin_client, environment):
    client, _ = admin_client
    _seed(admin_client, environment=environment, provider="claude", runtime_provider="codex")
    before = _get_config(client)
    projected = before["models"]["review"]
    assert "provider" not in projected
    assert projected["route"] == "review-route"
    assert projected["execution_environment"] == environment
    response = client.put("/admin/models/review", json=projected)
    assert response.status_code == 200, response.text
    _reload(client)
    assert _get_config(client) == before
    assert _saved(admin_client)["models"]["review"]["execution_environment"] == environment

    from kotoyose.config import get_config

    effective = get_config().preset_for("review")
    assert effective.provider == "codex"
    assert effective.model == "runtime-model"
    assert effective.execution_environment == environment
    assert effective.codex == projected["codex"]


@pytest.mark.parametrize("environment", ["isolated", "host"])
def test_unrelated_settings_and_preset_saves_do_not_reset_environment(admin_client, environment):
    client, _ = admin_client
    _seed(admin_client, environment=environment)
    before = _get_config(client)
    response = client.put("/admin/settings", json={"anthropic_base_url": "https://fixture.invalid/api"})
    assert response.status_code == 200, response.text
    _reload(client)
    after_settings = _get_config(client)
    assert after_settings["settings"]["anthropic_base_url"] == "https://fixture.invalid/api"
    assert after_settings["models"] == before["models"]

    unrelated = after_settings["models"]["unrelated"]
    unrelated["effort"] = "high"
    response = client.put("/admin/models/unrelated", json=unrelated)
    assert response.status_code == 200, response.text
    _reload(client)
    after = _get_config(client)
    assert after["models"]["unrelated"]["effort"] == "high"
    assert after["models"]["review"]["execution_environment"] == environment
    assert after["models"]["review"] == before["models"]["review"]
    saved = _saved(admin_client)
    assert saved["models"]["review"]["execution_environment"] == environment


@pytest.mark.parametrize("provider, environment", [
    ("claude", "isolated"), ("claude", "host"), ("cursor", "isolated"),
    ("antigravity", "isolated"), ("codex", "invalid-mode"),
])
@pytest.mark.parametrize("use_route", [False, True], ids=["direct-provider", "runtime-provider"])
def test_invalid_effective_provider_or_mode_is_rejected_without_mutation(admin_client, provider, environment, use_route):
    client, path = admin_client
    # The baseline must itself be valid. For Claude runtime references, start
    # with a historical (unspecified) environment and try to enable it via PUT.
    baseline_environment = _OMITTED if use_route and provider == "claude" else "host"
    _seed(admin_client, environment=baseline_environment, runtime_provider=provider if use_route else None)
    before = _get_config(client)
    payload = deepcopy(before["models"]["review"])
    payload["execution_environment"] = environment
    if not use_route:
        payload["provider"] = provider
    prior_bytes = path.read_bytes()
    response = client.put("/admin/models/review", json=payload)
    assert response.status_code == 422, response.text
    assert "execution_environment" in response.text
    assert path.read_bytes() == prior_bytes
    assert _get_config(client) == before
    _reload(client)
    assert _get_config(client) == before
