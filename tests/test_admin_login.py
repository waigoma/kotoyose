"""Tests for admin console session login, cookies, rate limiting, CSRF, and UI integration."""

from contextlib import contextmanager
import base64
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re

from fastapi.testclient import TestClient
import pytest
from structlog.testing import capture_logs

from kotoyose import auth
from kotoyose.app import app
from kotoyose.config import reload_config
from kotoyose.routes import admin_ui


_CONSOLE_DIR = Path(__file__).resolve().parents[1] / "src/kotoyose/static/console"
_FAKE_USER = "admin-test-user"
_FAKE_TOKEN = "fake-test-kotoyose-token"


@contextmanager
def _client(**kwargs):
    """lifespan を起動しない client。起動すると structlog の設定が替わり他の試験の log 捕捉を壊す。"""
    yield TestClient(app, **kwargs)


@pytest.fixture
def secured_env(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yml"
    cfg.write_text("models: {}\n", encoding="utf-8")
    monkeypatch.setenv("KOTOYOSE_CONFIG", str(cfg))
    monkeypatch.setattr("kotoyose.auth.API_KEY", _FAKE_TOKEN)
    monkeypatch.setattr("kotoyose.auth.BASIC_AUTH_USER", _FAKE_USER)
    auth.clear_admin_auth_state()
    reload_config()
    yield cfg
    auth.clear_admin_auth_state()
    reload_config()


@pytest.fixture
def dev_env(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yml"
    cfg.write_text("models: {}\n", encoding="utf-8")
    monkeypatch.setenv("KOTOYOSE_CONFIG", str(cfg))
    monkeypatch.setattr("kotoyose.auth.API_KEY", None)
    auth.clear_admin_auth_state()
    reload_config()
    yield cfg
    auth.clear_admin_auth_state()
    reload_config()


@pytest.fixture
def fake_clock(monkeypatch):
    now = [1_700_000_000.0]
    monkeypatch.setattr("kotoyose.auth._now", lambda: now[0])
    return now


def _cookie_value(res) -> str:
    return res.cookies.get(auth.ADMIN_SESSION_COOKIE) or ""


def _csrf_headers(origin: str = "http://testserver") -> dict[str, str]:
    return {"X-Kotoyose-Admin": "1", "Origin": origin}


def test_login_page_serves_console_style_and_css(secured_env):
    with _client() as client:
        res = client.get("/admin/login")
        assert res.status_code == 200
        assert "text/html" in res.headers["content-type"]
        assert "/admin/login/console.css" in res.text
        assert 'name="username"' in res.text
        assert 'name="password"' in res.text

        css = client.get("/admin/login/console.css")
        assert css.status_code == 200
        assert "--accent: #3fb9a6" in css.text

        version_match = re.search(r"/admin/login/console\.css\?v=([0-9a-f]{12})", res.text)
        assert version_match
        asset_res = client.get(
            f"/admin/ui/console/{version_match.group(1)}/login.html",
            auth=(_FAKE_USER, _FAKE_TOKEN),
        )
        assert asset_res.status_code == 404


def test_login_json_and_form_success_and_session_entropy(secured_env, fake_clock):
    with _client() as client:
        res = client.post(
            "/admin/login",
            json={"username": _FAKE_USER, "password": _FAKE_TOKEN},
        )
        assert res.status_code == 200
        body = res.json()
        assert body["user"] == _FAKE_USER
        assert body["next"] == "/admin/ui"
        expected_expiry = datetime.fromtimestamp(
            fake_clock[0] + auth.ADMIN_SESSION_IDLE_SECONDS,
            tz=timezone.utc,
        ).isoformat()
        assert body["expires_at"] == expected_expiry

        sid = _cookie_value(res)
        # 32 random bytes urlsafe-base64 encoded without padding is 43 chars (padded 44).
        padded = sid + "=" * (-len(sid) % 4)
        assert len(base64.urlsafe_b64decode(padded)) == 32

        sess_res = client.get("/admin/session")
        assert sess_res.status_code == 200
        assert sess_res.json()["user"] == _FAKE_USER

    with _client() as form_client:
        form_res = form_client.post(
            "/admin/login",
            data={"username": _FAKE_USER, "password": _FAKE_TOKEN},
            follow_redirects=False,
        )
        assert form_res.status_code == 303
        assert form_res.headers["location"] == "/admin/ui"
        assert _cookie_value(form_res)
        assert form_client.get("/admin/ui").status_code == 200


def test_login_failure_wrong_username_or_password(secured_env):
    with _client() as client:
        bad_pass = client.post(
            "/admin/login",
            json={"username": _FAKE_USER, "password": "wrong-password"},
        )
        assert bad_pass.status_code == 401
        assert auth.ADMIN_SESSION_COOKIE not in bad_pass.cookies

        bad_user = client.post(
            "/admin/login",
            data={"username": "wrong-user", "password": _FAKE_TOKEN},
            follow_redirects=False,
        )
        assert bad_user.status_code == 401
        assert "Invalid username or password." in bad_user.text
        assert auth.ADMIN_SESSION_COOKIE not in bad_user.cookies


def test_cookie_flags_http_and_https(secured_env):
    with _client(base_url="http://testserver") as http_client:
        res = http_client.post(
            "/admin/login",
            json={"username": _FAKE_USER, "password": _FAKE_TOKEN},
        )
        set_cookie = res.headers["set-cookie"]
        lower = set_cookie.lower()
        assert f"{auth.ADMIN_SESSION_COOKIE}=" in set_cookie
        assert "httponly" in lower
        assert "samesite=strict" in lower
        assert "path=/admin" in lower
        assert "secure" not in lower

    with _client(base_url="https://testserver") as https_client:
        res = https_client.post(
            "/admin/login",
            json={"username": _FAKE_USER, "password": _FAKE_TOKEN},
        )
        lower = res.headers["set-cookie"].lower()
        assert "httponly" in lower
        assert "samesite=strict" in lower
        assert "path=/admin" in lower
        assert "secure" in lower

    with _client(base_url="http://testserver") as proxied_client:
        res = proxied_client.post(
            "/admin/login",
            json={"username": _FAKE_USER, "password": _FAKE_TOKEN},
            headers={"X-Forwarded-Proto": "https"},
        )
        assert "secure" in res.headers["set-cookie"].lower()


def test_session_idle_and_absolute_expiry_with_fake_clock(secured_env, fake_clock):
    with _client() as client:
        res = client.post("/admin/login", json={"username": _FAKE_USER, "password": _FAKE_TOKEN})
        assert res.status_code == 200

        # Active at 11h extends idle deadline, so still valid at 22h.
        fake_clock[0] += 11 * 3600
        assert client.get("/admin/session").status_code == 200

        fake_clock[0] += 11 * 3600
        assert client.get("/admin/session").status_code == 200

        # Idle for >= 12h expires the session.
        fake_clock[0] += 12 * 3600
        assert client.get("/admin/session").status_code == 401
        ui_res = client.get("/admin/ui", follow_redirects=False)
        assert ui_res.status_code == 302
        assert ui_res.headers["location"] == "/admin/login?next=/admin/ui"

    # Absolute timeout: 7 days regardless of periodic activity.
    with _client() as abs_client:
        assert abs_client.post("/admin/login", json={"username": _FAKE_USER, "password": _FAKE_TOKEN}).status_code == 200
        for _ in range(16):
            fake_clock[0] += 10 * 3600  # 16 * 10h = 160h (< 168h = 7 days)
            assert abs_client.get("/admin/session").status_code == 200
        fake_clock[0] += 8 * 3600  # 168h = 7 days elapsed since creation
        assert abs_client.get("/admin/session").status_code == 401


def test_logout_deletes_session_and_cookie(secured_env):
    with _client() as client:
        assert client.post("/admin/login", json={"username": _FAKE_USER, "password": _FAKE_TOKEN}).status_code == 200
        assert client.get("/admin/session").status_code == 200

        # Missing CSRF header on logout is rejected and preserves session.
        blocked = client.post("/admin/logout", headers={"Origin": "http://testserver"})
        assert blocked.status_code == 403
        assert client.get("/admin/session").status_code == 200

        out = client.post("/admin/logout", headers=_csrf_headers())
        assert out.status_code == 200
        assert "max-age=0" in out.headers["set-cookie"].lower()

        assert client.get("/admin/session").status_code == 401
        ui_res = client.get("/admin/ui", follow_redirects=False)
        assert ui_res.status_code == 302
        assert ui_res.headers["location"] == "/admin/login?next=/admin/ui"


def test_rate_limit_five_failures_per_ip_per_10_minutes(secured_env, fake_clock):
    with _client() as client:
        for _ in range(5):
            r = client.post("/admin/login", json={"username": _FAKE_USER, "password": "bad"})
            assert r.status_code == 401

        # 6th attempt within window is rate-limited even with valid password.
        blocked = client.post("/admin/login", json={"username": _FAKE_USER, "password": _FAKE_TOKEN})
        assert blocked.status_code == 429

        # Once 10 minutes pass, login works again.
        fake_clock[0] += 10 * 60 + 1
        ok = client.post("/admin/login", json={"username": _FAKE_USER, "password": _FAKE_TOKEN})
        assert ok.status_code == 200


def test_csrf_enforced_for_cookie_auth_and_basic_unaffected(secured_env):
    with _client() as client:
        assert client.post("/admin/login", json={"username": _FAKE_USER, "password": _FAKE_TOKEN}).status_code == 200

        # GET succeeds without CSRF header/origin.
        assert client.get("/admin/config").status_code == 200

        payload = {"model_id": "m1", "preset": {"provider": "claude", "model": "claude-sonnet"}}

        # Missing X-Kotoyose-Admin header -> 403.
        assert client.post("/admin/models", json=payload, headers={"Origin": "http://testserver"}).status_code == 403

        # Wrong X-Kotoyose-Admin header -> 403.
        assert client.post(
            "/admin/models",
            json=payload,
            headers={"X-Kotoyose-Admin": "0", "Origin": "http://testserver"},
        ).status_code == 403

        # Missing both Origin and Referer -> 403.
        assert client.post("/admin/models", json=payload, headers={"X-Kotoyose-Admin": "1"}).status_code == 403

        # Wrong Origin -> 403.
        for bad_origin in ("http://evil.example", "https://testserver", "http://testserver:9999", "null"):
            assert client.post(
                "/admin/models",
                json=payload,
                headers={"X-Kotoyose-Admin": "1", "Origin": bad_origin},
            ).status_code == 403

        # Wrong Referer -> 403.
        assert client.post(
            "/admin/models",
            json=payload,
            headers={"X-Kotoyose-Admin": "1", "Referer": "https://evil.example/attack.html"},
        ).status_code == 403

        # Valid header + matching Origin -> succeeds.
        assert client.post("/admin/models", json=payload, headers=_csrf_headers()).status_code == 200

        # Valid header + matching Referer (without Origin) -> succeeds.
        assert client.delete(
            "/admin/models/m1",
            headers={"X-Kotoyose-Admin": "1", "Referer": "http://testserver/admin/ui#/aliases"},
        ).status_code == 200

    # Basic auth is unaffected by CSRF checks.
    with _client() as api_client:
        res = api_client.post(
            "/admin/models",
            auth=(_FAKE_USER, _FAKE_TOKEN),
            json={"model_id": "m2", "preset": {"provider": "claude", "model": "claude-sonnet"}},
        )
        assert res.status_code == 200
        assert api_client.delete("/admin/models/m2", auth=(_FAKE_USER, _FAKE_TOKEN)).status_code == 200


@pytest.mark.parametrize(
    ("raw_next", "expected_next"),
    [
        ("/admin/ui", "/admin/ui"),
        ("/admin/ui#/routes/demo", "/admin/ui#/routes/demo"),
        ("https://evil.example/phish", "/admin/ui"),
        ("http://evil.example/admin/ui", "/admin/ui"),
        ("//evil.example/admin/ui", "/admin/ui"),
        ("///evil.example", "/admin/ui"),
        ("/\\evil.example", "/admin/ui"),
        ("javascript:alert(1)", "/admin/ui"),
        ("/admin/logout", "/admin/ui"),
    ],
)
def test_redirect_honours_safe_relative_next_only(secured_env, raw_next, expected_next):
    with _client() as client:
        res = client.post(
            "/admin/login",
            data={"username": _FAKE_USER, "password": _FAKE_TOKEN, "next": raw_next},
            follow_redirects=False,
        )
        assert res.status_code == 303
        assert res.headers["location"] == expected_next


def test_dev_mode_without_token_keeps_no_auth_behaviour(dev_env):
    with _client() as client:
        assert client.get("/admin/ui", follow_redirects=False).status_code == 200
        sess = client.get("/admin/session")
        assert sess.status_code == 200
        assert sess.json() == {"user": "auth-disabled", "expires_at": None}

        # State change works without cookie/Basic/CSRF in dev mode.
        res = client.post(
            "/admin/models",
            json={"model_id": "dev-m", "preset": {"provider": "claude", "model": "claude-sonnet"}},
        )
        assert res.status_code == 200
        assert client.delete("/admin/models/dev-m").status_code == 200


def test_console_js_sends_csrf_header_handles_401_and_shows_session_footer():
    api_js = (_CONSOLE_DIR / "js/api.js").read_text(encoding="utf-8")
    assert '"X-Kotoyose-Admin": "1"' in api_js
    assert 'credentials: "same-origin"' in api_js
    assert "response.status === 401" in api_js
    assert "/admin/login" in api_js

    nav_js = (_CONSOLE_DIR / "js/nav.js").read_text(encoding="utf-8")
    assert '"Signed in as "' in nav_js
    assert '"Sign out"' in nav_js

    main_js = (_CONSOLE_DIR / "js/main.js").read_text(encoding="utf-8")
    assert "api.session()" in main_js
    assert "api.logout()" in main_js



def test_failed_login_and_rate_limit_never_log_submitted_password(secured_env, caplog, capsys):
    submitted_secret = "CANARY-SUBMITTED-PASSWORD-98765"
    client = TestClient(app)
    with capture_logs() as cap:
        for _ in range(6):
            client.post(
                "/admin/login",
                json={"username": _FAKE_USER, "password": submitted_secret},
            )
        client.post(
            "/admin/login",
            data={"username": _FAKE_USER, "password": submitted_secret},
        )
        # Also verify successful login does not log real token.
        auth.clear_admin_auth_state()
        client.post(
            "/admin/login",
            json={"username": _FAKE_USER, "password": _FAKE_TOKEN},
        )

    captured = capsys.readouterr()
    combined = repr(cap) + caplog.text + captured.out + captured.err
    assert submitted_secret not in combined
    assert _FAKE_TOKEN not in combined
    assert "auth.login.failed" in combined
    assert "auth.login.rate_limited" in combined
    assert "auth.login.ok" in combined
    assert timedelta(seconds=auth.ADMIN_SESSION_IDLE_SECONDS) == timedelta(hours=12)
    assert timedelta(seconds=auth.ADMIN_SESSION_ABSOLUTE_SECONDS) == timedelta(days=7)
    assert admin_ui is not None


def test_console_requests_get_401_without_basic_challenge(secured_env):
    """console の fetch にはブラウザの Basic ダイアログを出させない (ログイン画面へ戻すため)。"""
    client = TestClient(app)
    plain = client.get("/admin/config")
    assert plain.status_code == 401
    assert plain.headers.get("www-authenticate", "").startswith("Basic")

    console = client.get("/admin/config", headers={"X-Kotoyose-Admin": "1"})
    assert console.status_code == 401
    assert "www-authenticate" not in console.headers

    client.cookies.set(auth.ADMIN_SESSION_COOKIE, "expired-or-unknown", path="/admin")
    stale = client.get("/admin/config")
    assert stale.status_code == 401
    assert "www-authenticate" not in stale.headers
