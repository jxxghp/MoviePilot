import asyncio
from types import SimpleNamespace
from typing import Optional, Union
from unittest.mock import AsyncMock, Mock

import pytest

from app.chain._messaging import MessageProcessingMixin, NotificationMixin
from app.modules.telegram.module import TelegramModule
from app.modules.wechat import WechatModule
from app.schemas.message import Message
from app.schemas.system import NotificationConf
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


def test_untyped_notification_defaults_to_admin_sync_async(monkeypatch) -> None:
    """未设置消息类型的通知应同步、异步都只查询管理员并按其设置投递。"""
    monkeypatch.setattr(
        "app.chain._messaging.get_notification_switch",
        lambda _mtype: None,
    )
    settings = {"admin": {"wechat_userid": "admin-1"}}
    sync_repository = _NotificationSettingsRepository(settings)
    async_repository = _NotificationSettingsRepository(settings)
    sync_chain = _NotificationHarness(sync_repository)
    async_chain = _NotificationHarness(async_repository)
    message = Message(title="工作流结果", text="执行完成")

    sync_chain.post_message(message.model_copy(deep=True))
    asyncio.run(async_chain.async_post_message(message.model_copy(deep=True)))

    assert sync_repository.sync_calls == ["admin"]
    assert async_repository.async_calls == ["admin"]
    expected_targets = [{"wechat_userid": "admin-1"}]
    assert [
        item[0]["targets"]
        for item in _delivery_snapshot(sync_chain.messagequeue.send_message)
    ] == expected_targets
    assert [
        item[0]["targets"]
        for item in _delivery_snapshot(async_chain.messagequeue.async_send_message)
    ] == expected_targets


def test_user_route_without_username_falls_back_to_admin(monkeypatch) -> None:
    """用户范围通知缺少责任人时应回退管理员，不得恢复为全体广播。"""
    monkeypatch.setattr(
        "app.chain._messaging.get_notification_switch",
        lambda _mtype: "user,all",
    )
    repository = _NotificationSettingsRepository(
        {"admin": {"telegram_userid": "admin-1"}}
    )
    chain = _NotificationHarness(repository)

    chain.post_message(Message(mtype=MessageType.Download, title="下载完成"))

    assert repository.sync_calls == ["admin"]
    deliveries = _delivery_snapshot(chain.messagequeue.send_message)
    assert [item[0]["targets"] for item in deliveries] == [
        {"telegram_userid": "admin-1"}
    ]
    assert [item[1] for item in deliveries] == [False]


def test_user_route_without_channel_binding_does_not_send(monkeypatch) -> None:
    """已识别操作用户但没有渠道绑定时，仅操作用户范围不得产生投递。"""
    monkeypatch.setattr(
        "app.chain._messaging.get_notification_switch",
        lambda _mtype: "user",
    )
    repository = _NotificationSettingsRepository({"alice": {}})
    chain = _NotificationHarness(repository)

    chain.post_message(
        Message(mtype=MessageType.Download, title="下载完成", username="alice")
    )

    assert repository.sync_calls == ["alice"]
    chain.messagequeue.send_message.assert_not_called()
    chain.eventmanager.send_event.assert_not_called()


def test_admin_route_keeps_default_target_scope_when_personal_binding_is_empty(
    monkeypatch,
) -> None:
    """管理员没有个人绑定时仍产出管理员范围消息，交由渠道使用默认目标。"""
    monkeypatch.setattr(
        "app.chain._messaging.get_notification_switch",
        lambda _mtype: "admin",
    )
    repository = _NotificationSettingsRepository({"admin": {}})
    chain = _NotificationHarness(repository)

    chain.post_message(Message(mtype=MessageType.Download, title="下载完成"))

    delivery = chain.messagequeue.send_message.call_args.kwargs["message"]
    assert delivery.targets == {}
    assert delivery.notification_route_scope == "admin"


def test_notification_channel_default_is_only_an_admin_target() -> None:
    """渠道默认 ID 只允许管理员范围使用，用户范围必须有个人绑定。"""
    module = TelegramModule()
    config = NotificationConf(
        name="telegram-test",
        type="telegram",
        enabled=True,
        switchs=[MessageType.Download.value],
        config={"TELEGRAM_CHAT_ID": "admin-default"},
    )
    module._configs = {config.name: config}
    client = Mock()
    module.get_configs = Mock(return_value={config.name: config})
    module.get_instance = Mock(return_value=client)

    admin_message = Message(mtype=MessageType.Download, title="管理员通知")
    admin_message.set_notification_route_scope("admin")
    user_message = Message(mtype=MessageType.Download, title="用户通知", targets={})
    user_message.set_notification_route_scope("user")
    bound_user_message = Message(
        mtype=MessageType.Download,
        targets={"telegram_userid": "user-id"},
    )
    bound_user_message.set_notification_route_scope("user")

    assert module.check_message(admin_message, config.name)
    assert not module.check_message(user_message, config.name)
    assert module.check_message(bound_user_message, config.name)

    module.post_message(admin_message)
    client.send_msg.assert_called_once()
    assert client.send_msg.call_args.kwargs["userid"] is None

    client.reset_mock()
    module.post_message(user_message)
    client.send_msg.assert_not_called()

    module.post_medias_message(bound_user_message, [])
    assert client.send_medias_msg.call_args.kwargs["userid"] == "user-id"


def test_scoped_notification_still_honors_channel_switch() -> None:
    """隔离路由只改变目标解析，不应绕过渠道消息类型开关。"""
    module = TelegramModule()
    config = NotificationConf(
        name="telegram-test",
        type="telegram",
        enabled=True,
        switchs=[],
        config={"TELEGRAM_CHAT_ID": "admin-default"},
    )
    module._configs = {config.name: config}

    admin_message = Message(mtype=MessageType.Download, title="管理员通知")
    admin_message.set_notification_route_scope("admin")
    user_message = Message(
        mtype=MessageType.Download,
        title="用户通知",
        targets={"telegram_userid": "user-id"},
    )
    user_message.set_notification_route_scope("user")

    assert not module.check_message(admin_message, config.name)
    assert not module.check_message(user_message, config.name)


def test_wechat_rejects_untyped_broadcast_without_explicit_targets(monkeypatch) -> None:
    """企业微信渠道不得把无类型且无目标的消息交给客户端默认广播。"""
    module = WechatModule()
    config = NotificationConf(name="wechat-test", type="wechat", enabled=True)
    module._configs = {config.name: config}
    client = Mock()
    monkeypatch.setattr(module, "get_configs", lambda: {config.name: config})
    monkeypatch.setattr(module, "get_instance", lambda _name: client)

    module.post_message(
        Message(source=config.name, title="工作流结果", text="执行完成")
    )
    client.send_msg.assert_not_called()

    module.post_message(
        Message(
            source=config.name,
            title="工作流结果",
            text="执行完成",
            targets={"wechat_userid": "admin-1"},
        )
    )
    client.send_msg.assert_called_once()
    assert client.send_msg.call_args.kwargs["userid"] == "admin-1"
