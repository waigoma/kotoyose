"""
coding preset + per-request cwd 上書き機能のテスト。

対象:
  - ModelPreset の新フィールド (allow_cwd_override / setting_sources)
  - build_options の cwd_override gate と setting_sources フォールバック
  - chat.py の 400 バリデーション (allow_cwd_override=False / cwd 不存在)
  - get_or_resume_client への cwd_override plumbing
"""
import os
import textwrap
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kotoyose.config import ModelPreset, _load_yaml_config, build_options

from tests.fixtures.api_keys import TEST_API_HEADERS

pytestmark = pytest.mark.usefixtures("client_api_key")


# ── ModelPreset フィールドのデフォルト値 ──────────────────────────────────


def test_allow_cwd_override_default_false():
    """allow_cwd_override の既定値は False (安全側 opt-in)。"""
    preset = ModelPreset()
    assert preset.allow_cwd_override is False


def test_setting_sources_default_none():
    """setting_sources の既定値は None (build_options で ['project'] にフォールバック)。"""
    preset = ModelPreset()
    assert preset.setting_sources is None


def test_setting_sources_accepts_empty_list():
    """setting_sources=[] (隔離モード) を受け入れる。"""
    preset = ModelPreset(setting_sources=[])
    assert preset.setting_sources == []


def test_setting_sources_accepts_user_project():
    """setting_sources=['user', 'project'] を受け入れる。"""
    preset = ModelPreset(setting_sources=["user", "project"])
    assert preset.setting_sources == ["user", "project"]


def test_allow_cwd_override_accepts_true():
    """allow_cwd_override=True を受け入れる。cwd_allowed_roots も一緒に指定する。"""
    preset = ModelPreset(allow_cwd_override=True, cwd_allowed_roots=["~/kotoyose-workspace"])
    assert preset.allow_cwd_override is True
    assert preset.cwd_allowed_roots == ["~/kotoyose-workspace"]


# ── YAML からの読み込み ───────────────────────────────────────────────────


def test_coding_preset_loaded_from_yaml(tmp_path):
    """coding preset の新フィールドが YAML から読み込まれる。"""
    yaml_content = textwrap.dedent("""
        models:
          coding:
            bare: false
            allow_cwd_override: true
            cwd_allowed_roots:
              - ~/kotoyose-workspace
            setting_sources: [user, project]
            cwd: ~/kotoyose-workspace
            model: cc/claude-sonnet-5
            deny:
              - "Bash(rm -rf*)"
              - "Write(/etc/*)"
    """)
    yaml_file = tmp_path / "config.yml"
    yaml_file.write_text(yaml_content)
    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        config = _load_yaml_config()
    preset = config.preset_for("coding")
    assert preset.allow_cwd_override is True
    assert preset.cwd_allowed_roots == ["~/kotoyose-workspace"]
    assert preset.setting_sources == ["user", "project"]
    assert preset.bare is False


def test_allow_cwd_override_defaults_to_false_when_omitted(tmp_path):
    """allow_cwd_override を省略した YAML は False になる (後方互換)。"""
    yaml_file = tmp_path / "config.yml"
    yaml_file.write_text("models:\n  any:\n    bare: true\n")
    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        config = _load_yaml_config()
    assert config.preset_for("any").allow_cwd_override is False


def test_setting_sources_none_when_omitted(tmp_path):
    """setting_sources を省略した YAML は None になる。"""
    yaml_file = tmp_path / "config.yml"
    yaml_file.write_text("models:\n  any:\n    bare: true\n")
    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        config = _load_yaml_config()
    assert config.preset_for("any").setting_sources is None


def test_setting_sources_empty_list_from_yaml(tmp_path):
    """setting_sources: [] (隔離モード) が YAML から読み込まれる。"""
    yaml_content = textwrap.dedent("""
        models:
          isolated:
            setting_sources: []
    """)
    yaml_file = tmp_path / "config.yml"
    yaml_file.write_text(yaml_content)
    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        config = _load_yaml_config()
    preset = config.preset_for("isolated")
    assert preset.setting_sources == []


# ── build_options の cwd_override gate ───────────────────────────────────


def test_build_options_cwd_override_applied_when_allowed(tmp_path):
    """allow_cwd_override=True + cwd_override 指定 → ClaudeAgentOptions.cwd に反映される。"""
    yaml_content = textwrap.dedent("""
        models:
          coding:
            bare: false
            allow_cwd_override: true
            cwd_allowed_roots:
              - ~/kotoyose-workspace
            cwd: ~/kotoyose-workspace
    """)
    yaml_file = tmp_path / "config.yml"
    yaml_file.write_text(yaml_content)
    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        from kotoyose import config as config_module
        config_module._config = config_module._load_yaml_config()
        opts = build_options("coding", cwd_override="/tmp/worktree-42")
    assert opts.cwd == "/tmp/worktree-42"


def test_build_options_cwd_override_expanduser(tmp_path):
    """cwd_override の ~ が os.path.expanduser で展開される。"""
    yaml_content = textwrap.dedent("""
        models:
          coding:
            bare: false
            allow_cwd_override: true
            cwd_allowed_roots:
              - ~/kotoyose-workspace
            cwd: ~/kotoyose-workspace
    """)
    yaml_file = tmp_path / "config.yml"
    yaml_file.write_text(yaml_content)
    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        from kotoyose import config as config_module
        config_module._config = config_module._load_yaml_config()
        opts = build_options("coding", cwd_override="~/some-worktree")
    # ~ が展開されていること
    assert not opts.cwd.startswith("~")
    assert opts.cwd.startswith("/")


def test_build_options_cwd_override_raises_when_not_allowed(tmp_path):
    """allow_cwd_override=False の preset に cwd_override を渡すと ValueError。"""
    yaml_content = textwrap.dedent("""
        models:
          kotoyose-chat:
            bare: true
            allow_cwd_override: false
            cwd: ~/kotoyose-workspace
    """)
    yaml_file = tmp_path / "config.yml"
    yaml_file.write_text(yaml_content)
    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        from kotoyose import config as config_module
        config_module._config = config_module._load_yaml_config()
        with pytest.raises(ValueError, match="allow_cwd_override=False"):
            build_options("kotoyose-chat", cwd_override="/tmp/worktree-42")


def test_build_options_cwd_override_none_uses_preset_cwd(tmp_path):
    """cwd_override=None の場合は preset の cwd が使われる (後方互換)。"""
    yaml_content = textwrap.dedent("""
        models:
          coding:
            bare: false
            allow_cwd_override: true
            cwd_allowed_roots:
              - ~/kotoyose-workspace
            cwd: ~/my-workspace
    """)
    yaml_file = tmp_path / "config.yml"
    yaml_file.write_text(yaml_content)
    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        from kotoyose import config as config_module
        config_module._config = config_module._load_yaml_config()
        opts = build_options("coding", cwd_override=None)
    # cwd はバリデータで expanduser 済み
    assert opts.cwd == os.path.expanduser("~/my-workspace")


# ── build_options の setting_sources フォールバック ───────────────────────


def test_build_options_setting_sources_none_falls_back_to_project(tmp_path):
    """setting_sources=None → ClaudeAgentOptions.setting_sources=['project'] (後方互換)。"""
    yaml_file = tmp_path / "config.yml"
    yaml_file.write_text("models:\n  any:\n    bare: true\n")
    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        from kotoyose import config as config_module
        config_module._config = config_module._load_yaml_config()
        with patch("kotoyose.config._get_oauth_token", return_value=None), \
             patch.dict(os.environ, {"ANTHROPIC_AUTH_TOKEN": "", "ANTHROPIC_API_KEY": ""}, clear=False):
            opts = build_options("any")
    assert opts.setting_sources == ["project"]


def test_build_options_setting_sources_explicit_list_passed_through(tmp_path):
    """setting_sources=['user','project'] → そのまま SDK に渡る。"""
    yaml_content = textwrap.dedent("""
        models:
          coding:
            bare: false
            allow_cwd_override: true
            cwd_allowed_roots:
              - ~/kotoyose-workspace
            setting_sources: [user, project]
    """)
    yaml_file = tmp_path / "config.yml"
    yaml_file.write_text(yaml_content)
    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        from kotoyose import config as config_module
        config_module._config = config_module._load_yaml_config()
        opts = build_options("coding")
    assert opts.setting_sources == ["user", "project"]


def test_build_options_setting_sources_empty_list_passed_through(tmp_path):
    """setting_sources=[] (隔離モード) → そのまま SDK に渡る (Noneへのフォールバックなし)。"""
    yaml_content = textwrap.dedent("""
        models:
          isolated:
            bare: true
            setting_sources: []
    """)
    yaml_file = tmp_path / "config.yml"
    yaml_file.write_text(yaml_content)
    with patch.dict(os.environ, {"KOTOYOSE_CONFIG": str(yaml_file)}):
        from kotoyose import config as config_module
        config_module._config = config_module._load_yaml_config()
        with patch("kotoyose.config._get_oauth_token", return_value=None), \
             patch.dict(os.environ, {"ANTHROPIC_AUTH_TOKEN": "", "ANTHROPIC_API_KEY": ""}, clear=False):
            opts = build_options("isolated")
    assert opts.setting_sources == []


# ── chat.py の 400 バリデーション ─────────────────────────────────────────


@pytest.fixture()
def chat_client(tmp_path, monkeypatch):
    """chat ルートテスト用の TestClient。認証無効化 + 独立した config。"""
    from fastapi.testclient import TestClient
    from kotoyose.app import app
    from kotoyose.config import reload_config

    path = tmp_path / "config.yml"
    path.write_text(textwrap.dedent(f"""
        models:
          strict-preset:
            bare: true
            allow_cwd_override: false
            cwd: ~/kotoyose-workspace
          coding:
            bare: false
            allow_cwd_override: true
            cwd_allowed_roots:
              - ~/kotoyose-workspace
              - {tmp_path}
            setting_sources: [user, project]
            cwd: ~/kotoyose-workspace
    """))
    monkeypatch.setenv("KOTOYOSE_CONFIG", str(path))
    monkeypatch.setattr("kotoyose.auth.API_KEY", None)
    reload_config()
    yield TestClient(app, headers=TEST_API_HEADERS)
    reload_config()


def test_chat_400_when_cwd_override_not_allowed(chat_client):
    """allow_cwd_override=False の preset に cwd を送ると 400 が返る。"""
    res = chat_client.post("/v1/chat/completions", json={
        "model": "strict-preset",
        "messages": [{"role": "user", "content": "hello"}],
        "cwd": "/tmp/some-worktree",
    })
    assert res.status_code == 400
    body = res.json()
    assert body["error"]["type"] == "invalid_request_error"
    # preset 名がメッセージに含まれること
    assert "strict-preset" in body["error"]["message"]


def test_chat_400_when_cwd_does_not_exist(chat_client, tmp_path):
    """cwd に存在しないディレクトリを指定すると 400 が返る。"""
    nonexistent = str(tmp_path / "nonexistent-worktree")
    res = chat_client.post("/v1/chat/completions", json={
        "model": "coding",
        "messages": [{"role": "user", "content": "hello"}],
        "cwd": nonexistent,
    })
    assert res.status_code == 400
    body = res.json()
    assert body["error"]["type"] == "invalid_request_error"
    assert "does not exist or is not a directory" in body["error"]["message"]
    assert nonexistent in body["error"]["message"]


def test_chat_passes_cwd_override_to_get_or_resume_client(chat_client, tmp_path):
    """正常系: get_or_resume_client に cwd_override が渡ること (mock で検証)。"""
    worktree = tmp_path / "worktree-42"
    worktree.mkdir()

    mock_client = MagicMock()
    mock_client.connect = AsyncMock()

    captured: dict = {}

    async def mock_get_or_resume(lookup_key, model, cwd_override=None):
        captured["cwd_override"] = cwd_override
        return mock_client, False

    async def mock_ask_claude(client, prompt):
        return "OK", None, None

    with patch("kotoyose.routes.chat.get_or_resume_client", side_effect=mock_get_or_resume), \
         patch("kotoyose.routes.chat.ask_claude", side_effect=mock_ask_claude), \
         patch("kotoyose.routes.chat.record_received", new_callable=AsyncMock, return_value="req-1"), \
         patch("kotoyose.routes.chat.record_completed", new_callable=AsyncMock), \
         patch("kotoyose.chat_session.upsert_session", new_callable=AsyncMock):
        chat_client.post("/v1/chat/completions", json={
            "model": "coding",
            "messages": [{"role": "user", "content": "hello"}],
            "cwd": str(worktree),
        })

    assert captured.get("cwd_override") == str(worktree)


def test_chat_no_cwd_field_still_works(chat_client):
    """cwd フィールドなしの通常リクエストは 400 にならない (後方互換)。"""
    mock_client = MagicMock()

    async def mock_get_or_resume(lookup_key, model, cwd_override=None):
        return mock_client, False

    async def mock_ask_claude(client, prompt):
        return "hello!", None, None

    with patch("kotoyose.routes.chat.get_or_resume_client", side_effect=mock_get_or_resume), \
         patch("kotoyose.routes.chat.ask_claude", side_effect=mock_ask_claude), \
         patch("kotoyose.routes.chat.record_received", new_callable=AsyncMock, return_value="req-1"), \
         patch("kotoyose.routes.chat.record_completed", new_callable=AsyncMock), \
         patch("kotoyose.chat_session.upsert_session", new_callable=AsyncMock):
        res = chat_client.post("/v1/chat/completions", json={
            "model": "strict-preset",
            "messages": [{"role": "user", "content": "hello"}],
        })
    # cwd なしは 400 にならない (SDK 接続失敗は 503 だが mock があるので 200)
    assert res.status_code != 400


# ── cwd_allowed_roots 検証テスト ──────────────────────────────────────────


@pytest.fixture()
def chat_client_with_roots(tmp_path, monkeypatch):
    """cwd_allowed_roots を持つ preset で chat ルートテスト。"""
    from fastapi.testclient import TestClient
    from kotoyose.app import app
    from kotoyose.config import reload_config

    path = tmp_path / "config.yml"
    path.write_text(textwrap.dedent(f"""
        models:
          coding-with-roots:
            bare: false
            allow_cwd_override: true
            cwd_allowed_roots:
              - {tmp_path}/allowed
              - ~/kotoyose-workspace
            cwd: ~/kotoyose-workspace
    """))
    # テスト用の許可ディレクトリを作成
    (tmp_path / "allowed").mkdir(exist_ok=True)
    monkeypatch.setenv("KOTOYOSE_CONFIG", str(path))
    monkeypatch.setattr("kotoyose.auth.API_KEY", None)
    reload_config()
    yield TestClient(app, headers=TEST_API_HEADERS)
    reload_config()


def test_chat_400_when_cwd_outside_allowed_roots(chat_client_with_roots, tmp_path):
    """cwd が cwd_allowed_roots 外のディレクトリの場合は 400 が返る。"""
    outside = tmp_path / "outside-not-allowed"
    outside.mkdir(exist_ok=True)
    res = chat_client_with_roots.post("/v1/chat/completions", json={
        "model": "coding-with-roots",
        "messages": [{"role": "user", "content": "hello"}],
        "cwd": str(outside),
    })
    assert res.status_code == 400
    body = res.json()
    assert body["error"]["type"] == "invalid_request_error"
    # 許可ルート外であることがメッセージに含まれること
    assert "許可ルート外" in body["error"]["message"]


def test_chat_passes_when_cwd_inside_allowed_roots(chat_client_with_roots, tmp_path):
    """cwd が cwd_allowed_roots 内の場合は 400 にならない (mock で通す)。"""
    allowed = tmp_path / "allowed" / "worktree-1"
    allowed.mkdir(exist_ok=True, parents=True)

    mock_client = MagicMock()
    mock_client.connect = AsyncMock()

    async def mock_get_or_resume(lookup_key, model, cwd_override=None):
        return mock_client, False

    async def mock_ask_claude(client, prompt):
        return "OK", None, None

    with patch("kotoyose.routes.chat.get_or_resume_client", side_effect=mock_get_or_resume), \
         patch("kotoyose.routes.chat.ask_claude", side_effect=mock_ask_claude), \
         patch("kotoyose.routes.chat.record_received", new_callable=AsyncMock, return_value="req-1"), \
         patch("kotoyose.routes.chat.record_completed", new_callable=AsyncMock), \
         patch("kotoyose.chat_session.upsert_session", new_callable=AsyncMock):
        res = chat_client_with_roots.post("/v1/chat/completions", json={
            "model": "coding-with-roots",
            "messages": [{"role": "user", "content": "hello"}],
            "cwd": str(allowed),
        })
    # 許可ルート内なら 400 にならない
    assert res.status_code != 400


def test_chat_400_when_cwd_escapes_via_symlink(chat_client_with_roots, tmp_path):
    """symlink で allowed 外に逃げようとしても resolve() が潰す → 400。"""
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside-not-allowed"
    outside.mkdir(exist_ok=True)

    # allowed 配下に outside へのシンボリックリンクを作成
    symlink = allowed / "bad-link"
    try:
        symlink.symlink_to(outside)
    except OSError:
        # Windows などで symlink が作れない場合はスキップ
        pytest.skip("Symlinks not supported on this platform")

    res = chat_client_with_roots.post("/v1/chat/completions", json={
        "model": "coding-with-roots",
        "messages": [{"role": "user", "content": "hello"}],
        "cwd": str(symlink),
    })
    assert res.status_code == 400
    body = res.json()
    assert body["error"]["type"] == "invalid_request_error"


def test_chat_400_when_cwd_escapes_via_dotdot(chat_client_with_roots, tmp_path):
    """.. を使って allowed 外に逃げようとしても resolve() が潰す → 400。"""
    allowed = tmp_path / "allowed" / "subdir"
    allowed.mkdir(exist_ok=True, parents=True)

    escape_attempt = str(allowed / ".." / "..")
    res = chat_client_with_roots.post("/v1/chat/completions", json={
        "model": "coding-with-roots",
        "messages": [{"role": "user", "content": "hello"}],
        "cwd": escape_attempt,
    })
    assert res.status_code == 400
    body = res.json()
    assert body["error"]["type"] == "invalid_request_error"


# ── Fail-closed テスト (validator 迂回対策) ────────────────────────────────

def test_chat_fails_closed_when_cwd_allowed_roots_none(tmp_path, monkeypatch):
    """
    cwd_allowed_roots が None の場合、model_construct で validator を迂回されても
    chat.py が fail-closed (拒否) すること。
    """
    from fastapi.testclient import TestClient
    from kotoyose.app import app
    from kotoyose.config import ModelPreset, reload_config
    from unittest.mock import patch

    # テスト用 config ファイル (cwd_allowed_roots なしで作成)
    config_path = tmp_path / "config.yml"
    config_path.write_text(textwrap.dedent("""
        models:
          test-no-roots:
            bare: false
            model: claude-sonnet-5
            cwd: ~/test-workspace
            allow_cwd_override: false  # ここは false (validator を通す)
    """))

    monkeypatch.setenv("KOTOYOSE_CONFIG", str(config_path))
    monkeypatch.setattr("kotoyose.auth.API_KEY", None)
    reload_config()

    # model_construct で validator を迂回し、allow_cwd_override=True, cwd_allowed_roots=None を作成
    dangerous_preset = ModelPreset.model_construct(
        provider="claude",
        model="claude-sonnet-5",
        bare=False,
        cwd="~/kotoyose-workspace",
        allow_cwd_override=True,
        cwd_allowed_roots=None,  # validator が通常は禁止するが、model_construct で迂回
        deny=[],
        mcp_servers={},
        memory_enabled=False,
        memory_force=False,
        memory_source=None,
        memory_channel=None,
        memory_sender=None,
        fixed_conversation_key=None,
        thinking=None,
        thinking_budget_tokens=None,
        effort=None,
    )

    # config に動的に追加
    import kotoyose.config as config_module

    original_config = config_module.get_config()
    patched_config = original_config.model_copy(deep=True)
    patched_config.models["dangerous"] = dangerous_preset

    # ディレクトリ作成
    worktree = tmp_path / "workspace"
    worktree.mkdir(exist_ok=True)

    # config とget_config をパッチ
    with patch.object(config_module, "_config", patched_config):
        client = TestClient(app)
        res = client.post("/v1/chat/completions", headers=TEST_API_HEADERS, json={
            "model": "dangerous",
            "messages": [{"role": "user", "content": "hello"}],
            "cwd": str(worktree),
        })

        # cwd_allowed_roots が None でも fail-closed で 400 が返る
        assert res.status_code == 400, f"Expected 400, got {res.status_code}: {res.json()}"
        body = res.json()
        assert body["error"]["type"] == "invalid_request_error"
        # メッセージに「許可ルート外」と表示されるはず
        assert "許可ルート外" in body["error"]["message"]
