"""
群晖 Chat 空配置、令牌校验与消息负载兼容契约。
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import unquote

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.modules.synologychat.synologychat as client_module
from app.application.outbox import AGENT_TRANSFER_NOTIFICATION_TOPIC, OutboxDispatcher
from app.db.adapters.chain import TransactionalChainDurableEventWriter
from app.db.adapters.outbox import SqlAlchemyOutboxDispatchStore
from app.db.models.outbox import OutboxMessage
from app.modules.synologychat import SynologyChatDeliveryError, SynologyChatModule
from app.modules.synologychat.synologychat import SynologyChat
from app.schemas.message import Message, MessageNotificationContext


@pytest.mark.parametrize(
    "webhook,token",
    [(None, None), ("https://chat.invalid/hook", None), (None, "secret"),
     ("", "secret"), ("https://chat.invalid/hook", "")],
)
def test_incomplete_configuration_stays_disabled(monkeypatch, webhook, token):
    """
    配置缺项时所有公开状态和发送入口均安全返回，且任何令牌都不能通过。
    """
    transport = Mock()
    monkeypatch.setattr(client_module, "RequestUtils", transport)
    client = SynologyChat(SYNOLOGYCHAT_WEBHOOK=webhook, SYNOLOGYCHAT_TOKEN=token)
    assert client.get_state() is False
    assert client.check_token(None) is False
    assert client.check_token("secret") is False
    assert client.send_msg("title") is False
    assert client.send_medias_msg([object()], title="title") is False
    assert client.send_torrents_msg([object()], title="title") is None
    transport.assert_not_called()


@pytest.fixture
def configured_client(monkeypatch):
    """
    使用本地响应替身建立已配置客户端，禁止真实网络请求。
    """
    transport = Mock()
    transport.post_res.return_value = SimpleNamespace(
        status_code=200, json=lambda: {"success": True}, text="", reason="OK",
    )
    transport.get_res.return_value = SimpleNamespace(
        status_code=200, json=lambda: {"data": {"users": [{"user_id": 7, "deleted": False}]}},
    )
    monkeypatch.setattr(client_module, "RequestUtils", Mock(return_value=transport))
    client = SynologyChat(
        SYNOLOGYCHAT_WEBHOOK="https://chat.invalid/hook", SYNOLOGYCHAT_TOKEN="secret",
    )
    return client, transport


@pytest.fixture
def notification_module(
        configured_client: tuple[SynologyChat, Mock], monkeypatch: pytest.MonkeyPatch,
) -> SynologyChatModule:
    """
    将真实通知模块接到隔离客户端，不查询运行实例的通知配置或用户。

    :param configured_client: 使用内存 HTTP 响应的真实客户端
    :param monkeypatch: 自动恢复模块配置、客户端及接收人边界
    :return: 仅包含一个已选中测试配置的通知模块
    """
    client, _transport = configured_client
    module = SynologyChatModule()
    monkeypatch.setattr(module, "get_configs", Mock(return_value={"chat": SimpleNamespace(name="chat")}))
    monkeypatch.setattr(module, "check_message", Mock(return_value=True))
    monkeypatch.setattr(module, "get_instance", Mock(return_value=client))
    monkeypatch.setattr(module, "get_notification_userid", Mock(return_value="17"))
    return module


@pytest.mark.parametrize("failure", ["http-503", "business-error", "empty-json", "no-response", "connection-error"])
def test_module_propagates_unconfirmed_delivery(
        configured_client: tuple[SynologyChat, Mock], notification_module: SynologyChatModule,
        failure: str,
) -> None:
    """
    真实客户端识别的 HTTP、业务或传输失败必须传到严格投递边界。

    :param configured_client: 真实客户端与不会联网的传输替身
    :param notification_module: 保留真实发送循环的通知模块
    :param failure: 模拟未获确认的外部响应种类
    """
    _client, transport = configured_client
    if failure == "http-503":
        transport.post_res.return_value.status_code = 503
    elif failure == "business-error":
        transport.post_res.return_value.json = lambda: {"error": {"code": 105}}
    elif failure == "empty-json":
        transport.post_res.return_value.json = lambda: {}
    elif failure == "no-response":
        transport.post_res.return_value = None
    else:
        transport.post_res.side_effect = ConnectionError("本地测试连接失败")
    with pytest.raises(SynologyChatDeliveryError, match="SynologyChat"):
        notification_module.post_message(Message(title="隔离通知", text="正文"))


def test_module_keeps_successful_none_contract(
        notification_module: SynologyChatModule,
) -> None:
    """
    已确认发送不改变模块协议，成功仍返回 None 而不是布尔值。

    :param notification_module: 使用成功响应的真实通知模块
    """
    assert notification_module.post_message(Message(title="隔离通知")) is None


def test_module_does_not_fail_filtered_configuration(
        notification_module: SynologyChatModule,
) -> None:
    """
    不在渠道或类型范围内的配置属于正常跳过，不能产生重试。

    :param notification_module: 配置与客户端均为本地替身的通知模块
    """
    notification_module.check_message.return_value = False
    assert notification_module.post_message(Message(title="不发送")) is None
    notification_module.get_instance.assert_not_called()


@pytest.mark.parametrize("client", [None, SimpleNamespace(send_msg=Mock(return_value=None))])
def test_module_rejects_missing_client_or_receipt(
        notification_module: SynologyChatModule, client: object,
) -> None:
    """
    已选中的客户端缺失或没有成功回执时，不能报告投递完成。

    :param notification_module: 选中了测试配置的通知模块
    :param client: 缺失实例或不返回成功回执的客户端
    """
    notification_module.get_instance.return_value = client
    with pytest.raises(SynologyChatDeliveryError, match="SynologyChat"):
        notification_module.post_message(Message(title="隔离通知"))


@pytest.mark.parametrize("receipt", [False, None])
def test_partial_failure_still_attempts_other_chunks_and_configurations(
        notification_module: SynologyChatModule, monkeypatch: pytest.MonkeyPatch,
        receipt: object,
) -> None:
    """
    单个配置或分段失败不阻断本模块其余发送，全部尝试后再报告失败。

    :param notification_module: 保留真实模块发送循环的测试实例
    :param monkeypatch: 隔离正文分段边界
    :param receipt: 首个分段未确认成功的客户端返回值
    """
    first = SimpleNamespace(send_msg=Mock(side_effect=[receipt, True]))
    second = SimpleNamespace(send_msg=Mock(return_value=True))
    notification_module.get_configs.return_value = {
        "first": SimpleNamespace(name="first"), "second": SimpleNamespace(name="second"),
    }
    notification_module.get_instance.side_effect = [first, second]
    monkeypatch.setattr(Message, "channel_text_chunks", lambda _message, _channel: ["第一段", "第二段"])
    with pytest.raises(SynologyChatDeliveryError, match="SynologyChat"):
        notification_module.post_message(Message(title="隔离通知"))
    assert first.send_msg.call_count == second.send_msg.call_count == 2
    assert [call.kwargs["text"] for call in second.send_msg.call_args_list] == ["第一段", "第二段"]


def test_http_failure_remains_pending_and_real_outbox_retry_completes(
        tmp_path: Path, configured_client: tuple[SynologyChat, Mock],
        notification_module: SynologyChatModule,
) -> None:
    """
    HTTP 503 必须留下真实 SQLite 待重试记录，恢复成功后才能结算完成。

    :param tmp_path: 不包含正式数据的临时数据库目录
    :param configured_client: 真实渠道客户端与受控 HTTP 响应
    :param notification_module: 运行真实发送代码的通知模块
    """
    _client, transport = configured_client
    transport.post_res.return_value.status_code = 503
    engine = create_engine(f"sqlite:///{tmp_path / 'synology-outbox.db'}")
    OutboxMessage.__table__.create(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    message = Message(title="隔离通知", text="正文", notification_context=MessageNotificationContext(
        scenario="agent.transfer_takeover", trigger="automatic", stage="attention",
        task_id="synology-retry", settlement_revision=1, history_id=1, summary="需要关注",
    ))
    try:
        writer = TransactionalChainDurableEventWriter(factory)
        assert writer.agent_transfer_notification(
            message=message, publish=lambda _key: notification_module.post_message(message),
        )
        with factory() as session:
            row = session.scalars(select(OutboxMessage)).one()
            assert row.status == "pending"
            event_key = row.event_key
        transport.post_res.return_value.status_code = 200
        dispatcher = OutboxDispatcher(
            SqlAlchemyOutboxDispatchStore(factory),
            {AGENT_TRANSFER_NOTIFICATION_TOPIC: lambda _event: notification_module.post_message(message)},
            clock=lambda: datetime.now(timezone.utc) + timedelta(days=1),
        )
        assert dispatcher.dispatch_one()
        with factory() as session:
            row = session.scalars(select(OutboxMessage)).one()
            assert row.event_key == event_key and row.status == "completed"
        assert transport.post_res.call_count == 2
        assert not dispatcher.dispatch_one()
    finally:
        engine.dispose()


@pytest.mark.parametrize("token,expected", [(None, False), ("wrong", False), ("secret", True)])
def test_only_configured_token_is_accepted(configured_client, token, expected):
    """
    保留精确令牌匹配，不接受缺失或错误令牌。
    """
    client, transport = configured_client
    assert client.check_token(token) is expected
    transport.post_res.assert_not_called()
    transport.get_res.assert_not_called()


@pytest.mark.parametrize(
    "title,text,caption",
    [("head", None, "head"), (None, "body", "*None*\nbody"),
     ("head\nbody", None, "*head*\nbody"), ("head", "body\n\nline", "*head*\nbody\nline")],
)
def test_text_format_and_explicit_recipient_are_preserved(configured_client, title, text, caption):
    """
    可空标题和多行正文保持原格式，显式用户 ID 仍按整数编码。
    """
    client, transport = configured_client
    assert client.send_msg(title, text=text, userid="17") is True
    payload = json.loads(transport.post_res.call_args.kwargs["data"].removeprefix("payload="))
    assert unquote(payload["text"]) == caption
    assert payload["user_ids"] == [17]
    transport.get_res.assert_not_called()


def test_empty_message_does_not_send(configured_client):
    """
    标题正文均为空时仍直接拒绝发送。
    """
    client, transport = configured_client
    assert client.send_msg(None) is False
    transport.get_res.assert_not_called()
    transport.post_res.assert_not_called()


def test_bot_user_values_and_deleted_filter_are_preserved(configured_client):
    """
    可见用户的原始 ID 保持原值，只有 deleted 严格为 False 的条目被选中。
    """
    client, transport = configured_client
    transport.get_res.return_value.json = lambda: {
        "data": {"users": [
            {"user_id": 7, "deleted": False},
            {"user_id": "8", "deleted": False},
            {"deleted": False},
            {"user_id": 9, "deleted": True},
            {"user_id": 10, "deleted": 0},
        ]},
    }
    assert client.get_state() is True
    assert client.send_msg("title") is True
    payload = json.loads(transport.post_res.call_args.kwargs["data"].removeprefix("payload="))
    assert payload["user_ids"] == [7, "8", None]


class _IntUserId:
    """
    保留旧插件可能传入的整数转换协议。
    """

    def __int__(self):
        """
        返回用于入站消息路由的用户 ID。
        """
        return 17


class _IndexUserId:
    """
    覆盖 int 支持的索引转换协议。
    """

    def __index__(self):
        """
        返回用于入站消息路由的用户 ID。
        """
        return 17


@pytest.fixture
def incoming_parser(configured_client, monkeypatch):
    """
    把入站解析器连接到内存配置和已配置的本地客户端。
    """
    client, _ = configured_client
    module = SynologyChatModule()
    conf = SimpleNamespace(name="chat", config={})
    monkeypatch.setattr(module, "get_config", lambda _name: conf)
    monkeypatch.setattr(module, "get_instance", lambda _name: client)

    def parse(**fields):
        """
        用正常消息默认值构造表单，允许覆盖待验证字段。
        """
        form = {"token": "secret", "user_id": "17", "text": "hello", **fields}
        return module.message_parser("chat", None, form, None)

    return parse


@pytest.mark.parametrize(
    "value,expected",
    [(17, 17), ("17", 17), (b"17", 17), (bytearray(b"17"), 17),
     (memoryview(b"17"), 17), (17.9, 17), (True, 1), (_IntUserId(), 17), (_IndexUserId(), 17)],
)
def test_user_id_conversion_protocols_remain_supported(incoming_parser, value, expected):
    """
    保留 int 原先接受的字符串、缓冲区和数值协议，不因类型检查缩窄正常输入。
    """
    result = incoming_parser(user_id=value)
    assert result is not None
    assert result.userid == expected
    assert result.text == "hello"


@pytest.mark.parametrize("value", [None, object(), "not-a-number", float("inf")])
def test_invalid_user_ids_still_drop_message(incoming_parser, value):
    """
    不支持或不能完成整数转换的 ID 仍不能生成入站消息。
    """
    assert incoming_parser(user_id=value) is None


@pytest.mark.parametrize("token", [None, b"secret", "wrong", []])
def test_invalid_incoming_tokens_still_drop_message(incoming_parser, token):
    """
    解析入口拒绝缺失、非字符串或错误令牌。
    """
    assert incoming_parser(token=token) is None


def test_falsy_text_preserves_attachment_message(incoming_parser):
    """
    原先视为空文本的值仍允许有效附件作为消息内容。
    """
    result = incoming_parser(text=0, file_url="https://chat.invalid/image.png")
    assert result is not None
    assert result.text == ""
    assert result.images[0].ref == "https://chat.invalid/image.png"


def test_truthy_non_text_still_drops_message(incoming_parser):
    """
    缺少字符串操作的非空文本值仍被拒绝。
    """
    assert incoming_parser(text=["bad"]) is None
