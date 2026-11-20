"""soft通知で進捗保存を要求し、モデル実行を中断しない。"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

from kotoyose.execution_service import ExecutionService


async def test_soft_request_persists_progress_request_and_continues_without_stop():
    service = object.__new__(ExecutionService)
    service.store = SimpleNamespace(append_event=AsyncMock(), finish=AsyncMock())
    await service._warn('fixture-attempt', 0)
    assert service.store.append_event.await_args_list[-1].args == (
        'fixture-attempt', 'progress_save_requested', {'checkpoint': 'deferred_until_stopped', 'action': 'continue'})
    service.store.finish.assert_not_awaited()
