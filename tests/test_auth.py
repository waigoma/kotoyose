"""Playground の管理認証を HTTP 経由で確認する。"""

import pytest
from fastapi.testclient import TestClient

from kotoyose import auth
from kotoyose.app import app


@pytest.mark.parametrize("path", ["/playground", "/internal/playground/presets"])
def test_playground_accepts_basic_credentials(monkeypatch, path):
    monkeypatch.setattr(auth, "API_KEY", "secret-token")
    monkeypatch.setattr(auth, "BASIC_AUTH_USER", "kotoyose-service")
    response = TestClient(app).get(path, auth=("kotoyose-service", "secret-token"))
    assert response.status_code == 200


@pytest.mark.parametrize("path", ["/playground", "/internal/playground/presets"])
@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer secret-token"}])
def test_playground_requires_admin_authentication(monkeypatch, path, headers):
    monkeypatch.setattr(auth, "API_KEY", "secret-token")
    response = TestClient(app).get(path, headers=headers)
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == 'Basic realm="kotoyose-admin"'
