"""订阅搜索 Agent 工具的数据访问边界。"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

from app.agent.tools.impl import search_subscribe as search_subscribe_module
from app.agent.tools.impl.search_subscribe import SearchSubscribeTool


def _subscribe(*, state: str = "R") -> SimpleNamespace:
    """构造搜索工具需要的最小订阅记录。"""
    return SimpleNamespace(
        id=7,
        name="Example",
        year="2026",
        type="TV",
        season=1,
        state=state,
        total_episode=12,
        lack_episode=2,
        media_source="tmdb",
        media_id="123",
        music_type=None,
        total_tracks=None,
        description=None,
        last_update="2026-08-22 00:00:00",
        filter_groups=[],
    )


def test_search_subscribe_uses_async_data_port(monkeypatch) -> None:
    """订阅搜索不能在 async 工具中调用同步 DB 端口。"""
    record = _subscribe()
    updated = _subscribe()

    class _AsyncSubscribePort:
        def __init__(self) -> None:
            self.async_get = AsyncMock(side_effect=[record, updated])
            self.async_update = AsyncMock()

        def get(self, _subscribe_id):
            raise AssertionError("async 工具不应调用同步订阅查询")

        def update(self, _subscribe_id, _payload):
            raise AssertionError("async 工具不应调用同步订阅更新")

    port = _AsyncSubscribePort()
    monkeypatch.setattr(search_subscribe_module, "SubscribeOper", lambda: port)

    async def _run_blocking(*_args, **_kwargs):
        await asyncio.sleep(0)

    monkeypatch.setattr(SearchSubscribeTool, "run_blocking", _run_blocking)

    result = asyncio.run(
        SearchSubscribeTool(session_id="test", user_id="1").run(
            subscribe_id=record.id,
            filter_groups=["default"],
        )
    )

    payload = json.loads(result)
    assert payload["success"] is True
    assert port.async_get.await_args_list == [call(record.id), call(record.id)]
    port.async_update.assert_awaited_once_with(
        record.id,
        {"filter_groups": ["default"]},
    )


def test_search_subscribe_rejects_paused_subscription_without_search(monkeypatch) -> None:
    """暂停订阅仍应在异步读取后立即返回，不提交搜索任务。"""
    record = _subscribe(state="S")

    class _AsyncSubscribePort:
        async_get = AsyncMock(return_value=record)
        async_update = AsyncMock()

    port = _AsyncSubscribePort()
    monkeypatch.setattr(search_subscribe_module, "SubscribeOper", lambda: port)
    run_blocking = AsyncMock()
    monkeypatch.setattr(SearchSubscribeTool, "run_blocking", run_blocking)

    result = asyncio.run(
        SearchSubscribeTool(session_id="test", user_id="1").run(
            subscribe_id=record.id,
        )
    )

    payload = json.loads(result)
    assert payload["success"] is False
    run_blocking.assert_not_awaited()
