"""Tests for S5e: Antigravity (agy) quota probe, agy models discovery, and store support."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import textwrap
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
import pytest

from kotoyose import auth as auth_module
from kotoyose import db as db_module
from kotoyose.app import app
from kotoyose.config import Connection, HarnessConfig, reload_config, save_config
from kotoyose.connection_state_store import (
    VALID_QUOTA_SOURCES,
    ConnectionStateStore,
    _constraint_ddl,
    set_connection_state_store,
)
from kotoyose.connections_probe import (
    _CLI_VERSION_CACHE,
    probe_all_connections,
    probe_print_cli_connection,
)
from kotoyose.model_discovery import clear_connection_discovery_cache, discover_models
from kotoyose.print_cli_agy import parse_antigravity_usage_output

SAMPLE_AGY_USAGE_PAYLOAD = {
    "conversation_id": "",
    "status": "SUCCESS",
    "response": "<tsv text>",
    "num_turns": 0,
    "usage": {"total_tokens": 0},
    "command": {
        "name": "usage",
        "data": {
            "description": "Usage summary",
            "groups": [
                {
                    "name": "Gemini Models",
                    "description": "Models within this group: Gemini 4 Hydrogen, Gemini 3.8 Flash",
                    "buckets": [
                        {
                            "id": "gemini-weekly",
                            "name": "Weekly Limit Remaining",
                            "window": "weekly",
                            "remaining_fraction": 0.7188,
                            "reset_time": "2026-11-21T08:24:57Z",
                        },
                        {
                            "id": "gemini-5h",
                            "name": "Five Hour Limit Remaining",
                            "window": "5h",
                            "remaining_fraction": 0.6823,
                            "reset_time": "2026-11-14T18:24:57Z",
                        },
                    ],
                },
                {
                    "name": "Claude and GPT models",
                    "description": "Models within this group: Claude Opus 5.5, Claude Sonnet 5.5, GPT-OSS 120B",
                    "buckets": [
                        {
                            "id": "3p-weekly",
                            "name": "Weekly Limit Remaining",
                            "window": "weekly",
                            "remaining_fraction": 1,
                            "reset_time": "2026-11-21T17:15:12Z",
                        },
                        {
                            "id": "3p-5h",
                            "name": "Five Hour Limit Remaining",
                            "window": "5h",
                            "remaining_fraction": 1,
                            "reset_time": "2026-11-14T22:15:12Z",
                        },
                    ],
                },
                {
                    "name": "Gemini 4 Argon",
                    "description": "Models within this group: Gemini 4 Argon",
                    "buckets": [
                        {
                            "id": "gemini-argon-wtu-weekly",
                            "name": "Gemini 4 Argon Weekly Limit",
                            "window": "weekly",
                            "remaining_fraction": 1,
                            "reset_time": "2026-11-21T17:15:12Z",
                        }
                    ],
                },
            ],
        },
    },
}

SAMPLE_AGY_MODELS_TEXT = (
    "gemini-4-hydrogen-high\tGemini 4 Hydrogen (High)\n"
    "gemini-3.8-flash\tGemini 3.8 Flash\n"
    "claude-opus-5-5\tClaude Opus 5.5\n"
)


def _write_fake_agy(
    bin_dir: Path,
    *,
    mode: str = "ok",
    log_path: Path | None = None,
    sleep_seconds: float = 0.0,
    version_text: str = "1.5.9",
    models_text: str = SAMPLE_AGY_MODELS_TEXT,
    models_exit_code: int = 0,
) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    exe = bin_dir / "agy"
    script = textwrap.dedent(
        f"""\
        #!{sys.executable}
        import json
        import os
        import sys
        import time

        MODE = {mode!r}
        LOG_PATH = {str(log_path) if log_path is not None else ""!r}
        SLEEP_SECONDS = {sleep_seconds!r}
        VERSION_TEXT = {version_text!r}
        MODELS_TEXT = {models_text!r}
        MODELS_EXIT_CODE = {models_exit_code!r}
        USAGE_PAYLOAD = {SAMPLE_AGY_USAGE_PAYLOAD!r}

        def main():
            argv = sys.argv[1:]
            if argv == ["--version"]:
                print(VERSION_TEXT)
                return
            if LOG_PATH:
                with open(LOG_PATH, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({{
                        "argv": argv,
                        "cwd": os.getcwd(),
                        "env": dict(os.environ),
                    }}) + "\\n")
            if SLEEP_SECONDS > 0:
                time.sleep(SLEEP_SECONDS)
            if argv == ["models"]:
                if MODELS_EXIT_CODE != 0:
                    sys.stderr.write("models command failed RAW_MODELS_ERR\\n")
                    sys.exit(MODELS_EXIT_CODE)
                sys.stdout.write(MODELS_TEXT)
                return
            if MODE == "ok":
                print(json.dumps(USAGE_PAYLOAD))
            elif MODE == "sign_in_required":
                print(json.dumps({{
                    "status": "ERROR",
                    "error": "Sign-in is required. Please sign in with Google (RAW_TOKEN_SENTINEL).",
                }}))
                sys.exit(1)
            elif MODE == "sign_in_stderr":
                sys.stderr.write("Not signed in: please sign in to Antigravity\\n")
                sys.exit(1)
            elif MODE == "garbage":
                print("not json at all! RAW_GARBAGE_SENTINEL_AGY")
            else:
                print(json.dumps({{"status": "ERROR", "error": "unexpected " + ("X" * 300)}}))
                sys.exit(1)

        if __name__ == "__main__":
            main()
        """
    )
    exe.write_text(script, encoding="utf-8")
    exe.chmod(0o755)
    return exe


@pytest.fixture
def isolated_s5e_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("KOTOYOSE_CONFIG", str(cfg_path))
    monkeypatch.setattr("kotoyose.app.configure_logging", lambda: None)
    monkeypatch.setattr(auth_module, "API_KEY", None)
    monkeypatch.setattr(auth_module, "BASIC_AUTH_USER", None)
    monkeypatch.setattr(db_module, "_pool", None)
    _CLI_VERSION_CACHE.clear()
    clear_connection_discovery_cache()
    store = ConnectionStateStore(pool=None)
    set_connection_state_store(store)
    reload_config()
    yield tmp_path, store
    set_connection_state_store(None)
    clear_connection_discovery_cache()
    reload_config()


def test_parse_antigravity_usage_output_labels_used_percent_and_resets():
    parsed = parse_antigravity_usage_output(json.dumps(SAMPLE_AGY_USAGE_PAYLOAD))
    assert parsed["auth"] == "ok"
    assert parsed["quota_source"] == "agy_cli_usage"
    assert parsed["last_error"] is None

    quota = parsed["quota"]
    assert [w["window"] for w in quota] == [
        "gemini-weekly",
        "gemini-5h",
        "3p-weekly",
        "3p-5h",
        "gemini-argon-wtu-weekly",
    ]
    assert [w["label"] for w in quota] == [
        "Gemini · Weekly",
        "Gemini · Session (5h)",
        "Claude & GPT · Weekly",
        "Claude & GPT · Session (5h)",
        "Argon · Weekly",
    ]
    assert [w["used_percent"] for w in quota] == [28.1, 31.8, 0.0, 0.0, 0.0]
    assert [w["resets_at"] for w in quota] == [
        "2026-11-21T08:24:57Z",
        "2026-11-14T18:24:57Z",
        "2026-11-21T17:15:12Z",
        "2026-11-14T22:15:12Z",
        "2026-11-21T17:15:12Z",
    ]
    assert all(w["source"] == "agy_cli_usage" for w in quota)

    # Unknown group uses its own name verbatim.
    custom_payload = {
        "status": "SUCCESS",
        "command": {
            "name": "usage",
            "data": {
                "groups": [
                    {
                        "name": "Experimental Tier",
                        "buckets": [
                            {
                                "id": "exp-5h",
                                "window": "5h",
                                "remaining_fraction": 0.25,
                                "reset_time": "2026-11-14T19:00:00Z",
                            }
                        ],
                    }
                ]
            },
        },
    }
    custom_parsed = parse_antigravity_usage_output(json.dumps(custom_payload))
    assert custom_parsed["auth"] == "ok"
    assert custom_parsed["quota"][0]["label"] == "Experimental Tier · Session (5h)"
    assert custom_parsed["quota"][0]["used_percent"] == 75.0


def test_parse_antigravity_usage_output_sign_in_and_error_states():
    sign_in = parse_antigravity_usage_output(
        json.dumps({"status": "ERROR", "error": "Sign-in is required RAW_SECRET_ABC"})
    )
    assert sign_in["auth"] == "logged_out"
    assert sign_in["quota"] == []
    assert sign_in["quota_source"] == "agy_cli_usage"
    assert "RAW_SECRET_ABC" not in json.dumps(sign_in)

    garbage = parse_antigravity_usage_output("not-json RAW_GARBAGE_SECRET")
    assert garbage["auth"] == "unknown"
    assert garbage["quota"] == []
    assert garbage["quota_source"] == "agy_cli_usage"
    assert garbage["last_error"] is not None
    assert len(garbage["last_error"]) <= 200
    assert "RAW_GARBAGE_SECRET" not in garbage["last_error"]


@pytest.mark.asyncio
async def test_agy_quota_source_accepted_by_store_and_constraint_ddl():
    assert "agy_cli_usage" in VALID_QUOTA_SOURCES
    ddl = _constraint_ddl()
    assert "'agy_cli_usage'" in ddl
    assert "connection_state_quota_source_check" in ddl

    store = ConnectionStateStore(pool=None)
    parsed = parse_antigravity_usage_output(json.dumps(SAMPLE_AGY_USAGE_PAYLOAD))
    saved = await store.upsert(
        "agy-main",
        auth="ok",
        quota=parsed["quota"],
        quota_source="agy_cli_usage",
        cli_version="1.5.9",
        checked_at=datetime(2026, 11, 14, 15, 0, tzinfo=timezone.utc),
    )
    assert saved["quota_source"] == "agy_cli_usage"
    assert len(saved["quota"]) == 5
    fetched = await store.get("agy-main")
    assert fetched is not None
    assert fetched["quota_source"] == "agy_cli_usage"
    assert [w["label"] for w in fetched["quota"]] == [
        "Gemini · Weekly",
        "Gemini · Session (5h)",
        "Claude & GPT · Weekly",
        "Claude & GPT · Session (5h)",
        "Argon · Weekly",
    ]


@pytest.mark.asyncio
async def test_probe_antigravity_connection_on_path_uses_minimal_env_temp_cwd_and_no_shell(
    isolated_s5e_env, monkeypatch: pytest.MonkeyPatch
):
    tmp_path, store = isolated_s5e_env
    bin_dir = tmp_path / "bin"
    log_path = tmp_path / "agy-calls.jsonl"
    _write_fake_agy(bin_dir, mode="ok", log_path=log_path)

    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LANG", "C.UTF-8")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret-should-not-leak")
    monkeypatch.setenv("UNRELATED_SECRET", "top-secret-value")

    cfg = HarnessConfig.model_validate(
        {
            "connections": {
                "agy-host": {"provider": "antigravity", "kind": "subscription", "profile": "agy", "data_policy": "no_training"},
            }
        }
    )

    with patch("asyncio.create_subprocess_shell", side_effect=AssertionError("shell must never be used")):
        results = await probe_all_connections(cfg=cfg, execution_service=None, store=store)

    state = results["agy-host"]
    assert state["auth"] == "ok"
    assert state["quota_source"] == "agy_cli_usage"
    assert state["cli_version"] == "1.5.9"
    assert state["last_error"] is None
    assert len(state["quota"]) == 5
    assert [w["window"] for w in state["quota"]] == [
        "gemini-weekly",
        "gemini-5h",
        "3p-weekly",
        "3p-5h",
        "gemini-argon-wtu-weekly",
    ]

    recorded = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert [entry["argv"] for entry in recorded] == [["-p", "/usage", "--output-format", "json"]]
    call = recorded[0]
    assert call["cwd"] != str(Path.cwd())
    assert set(call["env"].keys()) <= {"PATH", "HOME", "LANG", "__PYVENV_LAUNCHER__", "LC_CTYPE"}
    assert "ANTHROPIC_API_KEY" not in call["env"]
    assert "UNRELATED_SECRET" not in call["env"]


@pytest.mark.asyncio
async def test_probe_antigravity_connection_variants_and_driver_override(isolated_s5e_env):
    tmp_path, store = isolated_s5e_env
    conn = Connection(provider="antigravity", kind="subscription", profile="agy", data_policy="no_training")

    for mode, expected_auth, expect_err in (
        ("sign_in_required", "logged_out", False),
        ("sign_in_stderr", "logged_out", False),
        ("garbage", "unknown", True),
        ("other_error", "unknown", True),
    ):
        _CLI_VERSION_CACHE.clear()
        cli_bin = _write_fake_agy(tmp_path / f"bin-{mode}", mode=mode)
        st = await probe_print_cli_connection(f"agy-{mode}", conn, cli_binary=cli_bin, store=store)
        assert st["auth"] == expected_auth
        assert st["quota"] == []
        assert st["quota_source"] == "agy_cli_usage"
        assert st["cli_version"] == "1.5.9"
        if expect_err:
            assert st["last_error"]
            assert len(st["last_error"]) <= 200
        dumped = json.dumps(st)
        assert "RAW_GARBAGE_SENTINEL_AGY" not in dumped
        assert "RAW_TOKEN_SENTINEL" not in dumped

    # Timeout variant
    _CLI_VERSION_CACHE.clear()
    hang_bin = _write_fake_agy(tmp_path / "bin-hang", mode="ok", sleep_seconds=5.0)
    st_timeout = await probe_print_cli_connection(
        "agy-timeout", conn, cli_binary=hang_bin, store=store, timeout_seconds=0.25
    )
    assert st_timeout["auth"] == "unknown"
    assert st_timeout["quota"] == []
    assert st_timeout["last_error"] is not None
    assert "timed out" in st_timeout["last_error"]
    assert len(st_timeout["last_error"]) <= 200


def test_admin_check_now_probes_antigravity_via_driver_install(isolated_s5e_env):
    tmp_path, _store = isolated_s5e_env
    cli_bin = _write_fake_agy(tmp_path / "driver-bin", mode="ok", version_text="1.5.9")

    cfg = HarnessConfig.model_validate(
        {
            "connections": {
                "agy-main": {"provider": "antigravity", "kind": "subscription", "profile": "agy", "data_policy": "no_training"},
            }
        }
    )
    save_config(cfg)

    fake_exec_service = SimpleNamespace(
        config=SimpleNamespace(drivers={"cli.print": SimpleNamespace(profile_binaries={"agy": cli_bin})}),
        close=AsyncMock(),
    )

    with (
        patch("kotoyose.app.start_execution_service", new=AsyncMock(return_value=fake_exec_service)),
        patch("kotoyose.app.connection_probe_loop", new=AsyncMock()),
    ):
        with TestClient(app) as client:
            resp = client.post("/admin/connections/agy-main/probe")
            assert resp.status_code == 200
            body = resp.json()
            assert body["state"]["auth"] == "ok"
            assert body["state"]["quota_source"] == "agy_cli_usage"
            assert body["state"]["cli_version"] == "1.5.9"
            assert len(body["state"]["quota"]) == 5


@pytest.mark.asyncio
async def test_antigravity_connection_model_discovery_and_cache(
    isolated_s5e_env, monkeypatch: pytest.MonkeyPatch
):
    tmp_path, _store = isolated_s5e_env
    bin_dir = tmp_path / "bin-models"
    log_path = tmp_path / "models-calls.jsonl"
    _write_fake_agy(bin_dir, mode="ok", log_path=log_path)

    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    clock = {"now": 1000.0}
    monkeypatch.setattr("kotoyose.model_discovery.time.monotonic", lambda: clock["now"])

    cfg = HarnessConfig.model_validate(
        {
            "connections": {
                "agy-main": {"provider": "antigravity", "kind": "subscription", "profile": "agy", "data_policy": "no_training"},
            }
        }
    )
    save_config(cfg)

    # lifespan の probe loop も偽の agy を呼ぶと、呼び出し回数の数え方が競合する。
    with (
        patch("kotoyose.app.start_execution_service", new=AsyncMock(return_value=None)),
        patch("kotoyose.app.connection_probe_loop", new=AsyncMock()),
        TestClient(app) as client,
    ):
        r1 = client.get("/admin/providers/antigravity/models", params={"connection": "agy-main"})
        assert r1.status_code == 200
        body1 = r1.json()
        assert body1["provider"] == "antigravity"
        assert body1["source"] == "agy_cli_models"
        assert body1["warning"] is None
        assert body1["error_code"] is None
        assert body1["models"] == [
            "gemini-4-hydrogen-high",
            "gemini-3.8-flash",
            "claude-opus-5-5",
        ]
        assert body1["model_details"] == [
            {"id": "gemini-4-hydrogen-high", "name": "Gemini 4 Hydrogen (High)", "label": "Gemini 4 Hydrogen (High)"},
            {"id": "gemini-3.8-flash", "name": "Gemini 3.8 Flash", "label": "Gemini 3.8 Flash"},
            {"id": "claude-opus-5-5", "name": "Claude Opus 5.5", "label": "Claude Opus 5.5"},
        ]

        # Within 10 minutes -> cached (no second agy models call)
        clock["now"] += 599.0
        r2 = client.get("/admin/providers/antigravity/models", params={"connection": "agy-main"})
        assert r2.status_code == 200
        assert r2.json()["models"] == body1["models"]
        assert len(log_path.read_text(encoding="utf-8").splitlines()) == 1

        # After 10 minutes -> refreshes
        clock["now"] += 2.0
        r3 = client.get("/admin/providers/antigravity/models", params={"connection": "agy-main"})
        assert r3.status_code == 200
        assert len(log_path.read_text(encoding="utf-8").splitlines()) == 2

    # Failure falls back to bundled path and is not cached
    clear_connection_discovery_cache()
    fail_bin = tmp_path / "bin-models-fail"
    _write_fake_agy(fail_bin, mode="ok", models_exit_code=1)
    monkeypatch.setenv("PATH", f"{fail_bin}:{os.environ.get('PATH', '')}")
    failed = await discover_models("antigravity", connection="agy-main")
    assert failed["source"] == "bundled_catalog"
    assert failed["models"] == []
    assert "bundled catalog" in failed["warning"]
    assert "RAW_MODELS_ERR" not in failed["warning"]
