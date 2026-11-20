"""試験全体の共通設定。"""

import pytest
import structlog


@pytest.fixture(autouse=True)
def _structlog_without_logger_cache(monkeypatch):
    """lifespan に入る試験が structlog の logger を固定しないようにする。

    本番の configure_logging は cache_logger_on_first_use=True。試験で lifespan に
    入るとその後に初めて使われた module の logger が JSON 出力のまま固定され、後続の
    capture_logs を使う試験が何も捕まえられなくなる (試験の順番で落ちる)。本番の
    設定はそのまま使い、cache だけ切る。
    """
    import kotoyose.app as app_module

    original = app_module.configure_logging

    def configure_without_cache():
        original()
        structlog.configure(cache_logger_on_first_use=False)

    monkeypatch.setattr(app_module, "configure_logging", configure_without_cache)


@pytest.fixture(autouse=True)
def _reset_process_env():
    """試験の中の scrub が os.environ から退避した値を、試験の前の状態に戻す。

    試験の前から在った値 (試験 DB の URL など) だけを戻す。試験が monkeypatch で一時的に
    入れた値まで戻すと、後続の試験へ漏れる (CLAUDE_CODE_OAUTH_TOKEN などで順番依存に落ちた)。
    """
    import os

    from kotoyose import process_env

    process_env._reset_for_tests(restore_environ=False)
    before = dict(os.environ)
    yield
    for key in list(process_env._SCRUBBED):
        if key in before:
            os.environ[key] = before[key]
    process_env._reset_for_tests(restore_environ=False)


@pytest.fixture(autouse=True)
def _isolated_xdg_config_home(tmp_path_factory, monkeypatch):
    """XDG の設定ディレクトリを試験ごとの空ディレクトリへ向ける。

    開発機には本物の ~/.config/kotoyose/execution.json があり、KOTOYOSE_EXECUTION_CONFIG を
    外した試験が既定パス経由でそれを読んでしまう。試験が自分で XDG を設定する場合はそちらが勝つ。
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path_factory.mktemp("xdg-config")))



@pytest.fixture
def configured_chat_alias(monkeypatch):
    """Use an explicit model alias in tests that exercise the chat HTTP route."""
    from kotoyose.config import HarnessConfig

    monkeypatch.setattr("kotoyose.config._config", HarnessConfig(models={"kotoyose-chat": {}}))


@pytest.fixture
def client_api_key(monkeypatch):
    """Authenticate chat/contract tests using a wildcard test client."""
    from kotoyose import config
    from kotoyose.app import app
    from kotoyose.config import Client
    from tests.fixtures.api_keys import sync_fixture_key_store

    clients = config.get_clients
    monkeypatch.setattr(config, "get_clients", lambda cfg=None: {
        **clients(cfg), "test-client": Client(models=["*"], places=["host", "isolated"], allow_full=True),
    })
    store = sync_fixture_key_store(**{"test-client": "test-api-key"})
    monkeypatch.setattr(app.state, "api_key_store", store)
    monkeypatch.setattr("kotoyose.app.ApiKeyStore", lambda pool=None: store)
