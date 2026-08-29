import asyncio
from types import SimpleNamespace
from typing import Optional, Union
from unittest.mock import AsyncMock, Mock

import pytest

from app.chain._messaging import MessageProcessingMixin, NotificationMixin
from app.schemas.message import Message
from app.schemas.types import MessageType


class _NotificationSettingsRepository:
    """记录同步和异步通知设置查询，并返回同一份测试快照。"""

    def __init__(
        self,
        settings: dict[str, Optional[dict[str, object]]],
        *,
        failure: Optional[str] = None,
    ) -> None:
        """保存测试设置、失败用户名和两种查询调用记录。"""
        self.settings = settings
        self.failure = failure
        self.sync_calls: list[str] = []
        self.async_calls: list[str] = []

    def get_notification_settings(
        self, username: str
    ) -> Optional[dict[str, object]]:
        """同步返回指定用户的通知设置。"""
        self.sync_calls.append(username)
        if username == self.failure:
            raise RuntimeError(f"无法读取 {username} 的通知设置")
        return self.settings.get(username)

    async def async_get_notification_settings(
        self, username: str
    ) -> Optional[dict[str, object]]:
        """异步返回指定用户的通知设置。"""
        self.async_calls.append(username)
        if username == self.failure:
            raise RuntimeError(f"无法读取 {username} 的通知设置")
        return self.settings.get(username)


class _NotificationHarness(MessageProcessingMixin, NotificationMixin):
    """提供通知 mixin 所需的最小可观测同步和异步边界。"""

    def __init__(self, repository: _NotificationSettingsRepository) -> None:
        """注入用户设置仓库及可断言的历史、事件和队列替身。"""
        self.user_repository = repository
        self.runtime_config = SimpleNamespace(superuser="admin")
        self.messageoper = SimpleNamespace(
            add=Mock(),
            async_add=AsyncMock(),
            exists_by_source=Mock(return_value=False),
        )
        self.eventmanager = SimpleNamespace(
            send_event=Mock(),
            async_send_event=AsyncMock(),
        )
        self.messagequeue = SimpleNamespace(
            send_message=Mock(),
            async_send_message=AsyncMock(),
        )


def _delivery_snapshot(mock: Union[Mock, AsyncMock]) -> list[tuple[dict, bool]]:
    """把队列调用投影为与同步方式无关的消息和立即投递标志。"""
    return [
        (
            call.kwargs["message"].model_dump(mode="json"),
            bool(call.kwargs.get("immediately", False)),
        )
        for call in mock.call_args_list
    ]


def _event_snapshot(mock: Union[Mock, AsyncMock]) -> list[dict]:
    """返回通知事件调用中的稳定数据载荷。"""
    return [call.kwargs["data"] for call in mock.call_args_list]


@pytest.mark.parametrize(
    ("action", "settings", "expected_users", "expected_targets"),
    [
        (
            "user,all",
            {"alice": {"telegram": "1001"}},
            ["alice"],
            [{"telegram": "1001"}, None],
        ),
        (
            "user,admin",
            {"alice": None, "admin": {"telegram": "9001"}},
            ["alice", "admin"],
            [{"telegram": "9001"}],
        ),
        (
            "admin,user",
            {"alice": None, "admin": {"telegram": "9001"}},
            ["admin", "alice"],
            [{"telegram": "9001"}],
        ),
    ],
)
def test_notification_routing_sync_async_parity(
    monkeypatch,
    action: str,
    settings: dict[str, Optional[dict[str, object]]],
    expected_users: list[str],
    expected_targets: list[Optional[dict[str, object]]],
) -> None:
    """同步与异步入口应共享用户路由、管理员回退和原消息投递决策。"""
    monkeypatch.setattr(
        "app.chain._messaging.get_notification_switch",
        lambda _mtype: action,
    )
    sync_repository = _NotificationSettingsRepository(settings)
    async_repository = _NotificationSettingsRepository(settings)
    sync_chain = _NotificationHarness(sync_repository)
    async_chain = _NotificationHarness(async_repository)
    message = Message(
        mtype=MessageType.Download,
        title="下载完成",
        text="影片已加入下载器",
        username="alice",
    )

    sync_chain.post_message(message.model_copy(deep=True))
    asyncio.run(async_chain.async_post_message(message.model_copy(deep=True)))

    sync_deliveries = _delivery_snapshot(sync_chain.messagequeue.send_message)
    async_deliveries = _delivery_snapshot(
        async_chain.messagequeue.async_send_message
    )
    assert sync_repository.sync_calls == expected_users
    assert async_repository.async_calls == expected_users
    assert [item[0]["targets"] for item in sync_deliveries] == expected_targets
    assert sync_deliveries == async_deliveries
    assert _event_snapshot(sync_chain.eventmanager.send_event) == _event_snapshot(
        async_chain.eventmanager.async_send_event
    )
    assert sync_chain.messageoper.add.call_count == 1
    assert async_chain.messageoper.async_add.await_count == 1


def test_notification_routing_lookup_failure_sync_async_parity(monkeypatch) -> None:
    """用户设置查询失败时两种入口都应传播错误且不得产生错误路由投递。"""
    monkeypatch.setattr(
        "app.chain._messaging.get_notification_switch",
        lambda _mtype: "user",
    )
    sync_chain = _NotificationHarness(
        _NotificationSettingsRepository({}, failure="alice")
    )
    async_chain = _NotificationHarness(
        _NotificationSettingsRepository({}, failure="alice")
    )
    message = Message(
        mtype=MessageType.Download,
        title="下载完成",
        username="alice",
    )

    with pytest.raises(RuntimeError, match="无法读取 alice"):
        sync_chain.post_message(message.model_copy(deep=True))
    with pytest.raises(RuntimeError, match="无法读取 alice"):
        asyncio.run(async_chain.async_post_message(message.model_copy(deep=True)))

    sync_chain.eventmanager.send_event.assert_not_called()
    async_chain.eventmanager.async_send_event.assert_not_awaited()
    sync_chain.messagequeue.send_message.assert_not_called()
    async_chain.messagequeue.async_send_message.assert_not_awaited()
    assert sync_chain.messageoper.add.call_count == 1
    assert async_chain.messageoper.async_add.await_count == 1
