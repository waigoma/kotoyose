"""並行リクエスト時の get_or_resume_client 挙動を documenting test で固定する。

現状の実装は cache-check → DB-lookup → SDK-create の周辺に lock を持たないため、
同一 lookup_key に対する並行 2 リクエストは双方とも cache miss → SDK Client 二重生成
となる。最終的に sessions[lookup_key] には一方の client が残り、もう一方は捨てられる
(connect 済みのため resource leak の可能性あり)。

これは現状の意図的な (というより未対処の) 挙動であり、本テストは今後 lock を導入する
際のリグレッション基準・現状認識ドキュメントとして機能する。

並行性が問題になる運用 (multiple users, シェルスクリプトの並列起動など) では、
アプリ層で per-conversation の排他を入れることが推奨される。
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import kotoyose.session as session_module
from kotoyose.config import HarnessConfig

from kotoyose.session import get_or_resume_client


@pytest.fixture(autouse=True)
def reset_session_state(monkeypatch):
    monkeypatch.setattr("kotoyose.config._config", HarnessConfig(models={"cc/claude-sonnet-5": {}}))
    monkeypatch.setattr("kotoyose.config._get_oauth_token", lambda: None)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    session_module.sessions.clear()
    session_module.session_last_used.clear()
    yield
    session_module.sessions.clear()
    session_module.session_last_used.clear()


@pytest.mark.asyncio
async def test_concurrent_same_key_creates_two_clients_documented_race():
    """同一 lookup_key に対する並行 2 リクエストは SDK Client が 2 回生成される (現状挙動)。

    将来 per-key lock を導入したらこのテストの assert 値を変えて、Client が 1 回だけ
    生成されることを保証するよう更新する。
    """
    ctor_calls: list = []

    def fake_ctor(options=None):
        ctor_calls.append(options)
        c = MagicMock()
        # connect で少し yield して並行が確実に交差するようにする
        async def _connect():
            await asyncio.sleep(0)
        c.connect = _connect
        return c

    # lookup_session も少し yield してから返すことで、ふたつの呼び出しが
    # cache-check 通過 → lookup_session 完了 → SDK 生成 の race window に揃う
    async def fake_lookup(_, **_kwargs):
        await asyncio.sleep(0)
        return None

    with (
        patch("kotoyose.session.lookup_session", new=fake_lookup),
        patch("kotoyose.session.ClaudeSDKClient", side_effect=fake_ctor),
    ):
        results = await asyncio.gather(
            get_or_resume_client("conv:race", "cc/claude-sonnet-5"),
            get_or_resume_client("conv:race", "cc/claude-sonnet-5"),
        )

    # 二重生成: 現状の race の結果として SDK Client は 2 つ作られる
    assert len(ctor_calls) == 2, (
        "documenting current behavior: concurrent same-key requests create 2 SDK clients. "
        "If you added a per-key lock, this assertion must change to == 1."
    )

    # 双方とも tuple を返し、各 client は MagicMock 由来 (None ではない)
    assert all(isinstance(r, tuple) and r[0] is not None for r in results)

    # 最終的に sessions[lookup_key] にはひとつだけ残る (どちらかの client)
    assert "conv:race" in session_module.sessions


@pytest.mark.asyncio
async def test_concurrent_after_first_lands_subsequent_is_cache_hit():
    """1 件目の終了後に 2 件目が来た場合は cache hit になる (race window 外なら問題なし)。"""
    fake_client = MagicMock()
    fake_client.connect = AsyncMock()

    with (
        patch("kotoyose.session.lookup_session", new=AsyncMock(return_value=None)),
        patch("kotoyose.session.ClaudeSDKClient", return_value=fake_client),
    ):
        # 1 件目 — sessions に登録される
        c1, _ = await get_or_resume_client("conv:sequential", "cc/claude-sonnet-5")
        # 2 件目 — cache hit
        c2, was_resumed = await get_or_resume_client("conv:sequential", "cc/claude-sonnet-5")

    assert c1 is c2
    assert was_resumed is False  # cache hit
