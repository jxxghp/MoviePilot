"""
强制私聊的普通/附件发送、账号关联会话证明与审批回执边界。
"""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.application.security import authorization as authorization_module
from app.chain.message import MessageChain
from app.modules.apppush import AppPushModule
from app.modules.dingtalk import DingTalkModule
from app.modules.discord import DiscordModule
from app.modules.feishu.module import FeishuModule
from app.modules.qqbot.module import QQBotModule
from app.modules.slack import SlackModule
from app.modules.synologychat import SynologyChatModule
from app.modules.telegram.module import TelegramModule
from app.modules.vocechat import VoceChatModule
from app.modules.webpush import WebPushModule
from app.modules.wechat import WechatModule
from app.modules.wechat.wechatbot import WeChatBot
from app.modules.wechatclawbot import WechatClawBotModule
from app.schemas.message import Message
from app.schemas.system import NotificationConf
from app.schemas.types import NotificationChannel


@pytest.fixture
def telegram_delivery(monkeypatch):
    """
    装配实际 Telegram 发送模块和仅记录调用的客户端。

    :param monkeypatch: 测试依赖恢复工具
    :return: 模块与确认成功的客户端
    """
    module = TelegramModule()
    monkeypatch.setattr(module, "_channel", NotificationChannel.Telegram)
    config = NotificationConf(id="private-tg", name="private-tg", type="telegram", enabled=True)
    client = Mock()
    for method in ("send_msg", "send_file", "send_voice"):
        getattr(client, method).return_value = {"success": True, "chat_id": "42", "message_id": 1}
    monkeypatch.setattr(module, "get_configs", lambda: {config.name: config})
    monkeypatch.setattr(module, "get_instance", lambda _: client)
    return module, client


@pytest.mark.parametrize("kind", ["text", "file", "voice"])
@pytest.mark.parametrize("direct", [False, True])
def test_telegram_private_constraint_reaches_client(telegram_delivery, kind, direct):
    """
    普通和直接发送的文本、文件、语音都禁止使用原群或最近群会话。
    """
    module, client = telegram_delivery
    fields = {"file_path": "synthetic.bin"} if kind == "file" else {"voice_path": "synthetic.ogg"} if kind == "voice" else {}
    message = Message(channel=NotificationChannel.Telegram, source="private-tg", userid="42", title="私信",
                      original_chat_id="-100-room", original_message_id=99, private_delivery=True, **fields)
    if direct:
        module.send_direct_message(message)
    else:
        module.post_message(message)
    method = "send_file" if kind == "file" else "send_voice" if kind == "voice" else "send_msg"
    assert getattr(client, method).call_count == 1
    kwargs = getattr(client, method).call_args.kwargs
    assert kwargs.get("private_delivery") is True
    assert kwargs.get("original_chat_id") is None and kwargs.get("original_message_id") is None
    assert message.original_chat_id == "-100-room"


def test_private_receipt_rechecks_connection_binding_before_send(telegram_delivery, monkeypatch):
    """
    解析回执后连接同名换代时，实际发送层拒绝使用替换后的客户端。
    """
    module, client = telegram_delivery
    message = Message(channel=NotificationChannel.Telegram, source="private-tg", userid="42",
                      title="授权状态", private_delivery=True, delivery_kind="authorization_receipt")
    message.bind_delivery_connection("private-tg", "private-tg")
    replacement = NotificationConf(id="another", name="private-tg", type="telegram", enabled=True)
    monkeypatch.setattr(module, "get_config", lambda _: replacement)
    module.post_message(message.model_copy(deep=True))
    client.send_msg.assert_not_called()


@pytest.mark.parametrize("module_type,channel,actor", [
    (TelegramModule, NotificationChannel.Telegram, "-100-room"),
    (QQBotModule, NotificationChannel.QQ, "group:room"),
    (VoceChatModule, NotificationChannel.VoceChat, "GID#2"),
    (SlackModule, NotificationChannel.Slack, "CROOM"),
    (WechatModule, NotificationChannel.Wechat, "group:room"),
    (WechatClawBotModule, NotificationChannel.WechatClawBot, "room@chatroom"),
    (FeishuModule, NotificationChannel.Feishu, "oc_room"),
])
def test_private_message_rejects_group_identity_before_sending(monkeypatch, module_type, channel, actor):
    """
    可识别的群地址不能借强制私聊字段进入平台发送接口。
    """
    module = module_type()
    client = Mock()
    config = NotificationConf(id="private", name="private", enabled=True)
    monkeypatch.setattr(module, "get_configs", lambda: {"private": config})
    monkeypatch.setattr(module, "get_instance", lambda _: client)
    with pytest.raises(PermissionError):
        module.post_message(Message(channel=channel, source="private", userid=actor, title="秘密", private_delivery=True))
    assert not client.mock_calls


@pytest.mark.parametrize("module_type,channel", [
    (DingTalkModule, NotificationChannel.DingTalk),
    (WebPushModule, NotificationChannel.WebPush),
    (AppPushModule, NotificationChannel.AppPush),
])
def test_noninteractive_channel_does_not_fake_private_delivery(monkeypatch, module_type, channel):
    """
    无机器人个人寻址合同的渠道明确拒绝，不能默默回退默认目标。
    """
    module = module_type()
    config = NotificationConf(id="private", name="private", enabled=True)
    monkeypatch.setattr(module, "get_configs", lambda: {"private": config})
    with pytest.raises(PermissionError):
        module.post_message(Message(channel=channel, source="private", userid="42", title="秘密", private_delivery=True))


@pytest.mark.parametrize("kind", ["group", "unknown"])
def test_account_binding_without_private_proof_does_not_consume_code(monkeypatch, kind):
    """
    群消息和未证明私聊的兼容调用不消费账号关联码，也不记录申请副作用。
    """
    service = Mock()
    service.find.return_value = None
    monkeypatch.setattr(authorization_module, "_configured_service", service)
    chain = MessageChain()
    monkeypatch.setattr(chain, "post_message", Mock())
    assert chain._handle_channel_authorization(
        NotificationChannel.Telegram, "bot", "42", "成员", "绑定账号 synthetic-code", None,
        original_chat_id="-100-room", conversation_type=kind,
    )
    service.bind.assert_not_called()
    service.request.assert_not_called()


def test_account_binding_accepts_verified_private_context(monkeypatch):
    """
    同一条指令在受信适配器证明的私聊中才交给关联用例。
    """
    service = Mock()
    service.find.return_value = None
    service.bind.return_value = SimpleNamespace(status="pending")
    monkeypatch.setattr(authorization_module, "_configured_service", service)
    chain = MessageChain()
    monkeypatch.setattr(chain, "post_message", Mock())
    assert chain._handle_channel_authorization(
        NotificationChannel.Telegram, "bot", "42", "成员", "绑定账号 synthetic-code", None,
        original_chat_id="42", conversation_type="private",
    )
    service.bind.assert_called_once()


def test_approval_response_waits_for_commit_and_leaves_receipt_to_outbox(monkeypatch):
    """
    管理端只报告审批保存及后台回执，不把异步投递当成已送达。
    """
    from app.api.endpoints import authorization as endpoint
    from app.schemas.authorization import ChannelMemberUpdate

    service = Mock()
    service.update.return_value = SimpleNamespace(id="member")
    monkeypatch.setattr(endpoint, "_service", lambda: service)
    monkeypatch.setattr(endpoint, "_project", lambda _: None)
    response = endpoint.update_authorization(
        "member", ChannelMemberUpdate(status="approved"), user=SimpleNamespace(id=1),
    )
    assert response.success and "后台" in response.message and "回执" in response.message
    service.update.assert_called_once_with(
        "member", status="approved", role="member", scopes=("search", "subscribe", "download"),
        reviewer_id=1, queue_receipt=True,
    )


@pytest.mark.parametrize("module_type,channel,actor", [
    (WechatModule, NotificationChannel.Wechat, "@all"),
    (WechatModule, NotificationChannel.Wechat, "alice|bob"),
    (WechatModule, NotificationChannel.Wechat, "alice\nbob"),
    (WechatModule, NotificationChannel.Wechat, "user:"),
    (WechatModule, NotificationChannel.Wechat, "user:user:"),
    (SlackModule, NotificationChannel.Slack, "#general"),
    (SlackModule, NotificationChannel.Slack, "DROOM"),
])
def test_private_message_rejects_broadcast_and_multiple_users(monkeypatch, module_type, channel, actor):
    """
    平台可接受的全体、多人及频道别名不能进入个人私聊发送路径。
    """
    module = module_type()
    monkeypatch.setattr(module, "_channel", channel)
    client = Mock()
    monkeypatch.setattr(module, "get_configs", lambda: {"private": NotificationConf(name="private", enabled=True)})
    monkeypatch.setattr(module, "get_instance", lambda _: client)
    with pytest.raises(PermissionError):
        module.post_message(Message(channel=channel, source="private", userid=actor, title="秘密", private_delivery=True))
    assert not client.mock_calls


def test_private_delivery_requires_explicit_platform(telegram_delivery):
    """
    同名实例不构成跨平台个人地址证明，缺少平台时拒绝发送。
    """
    module, client = telegram_delivery
    with pytest.raises(PermissionError):
        module.post_message(Message(source="private-tg", userid="42", title="秘密", private_delivery=True))
    client.send_msg.assert_not_called()


@pytest.mark.parametrize("channel_type,expected", [("dm", "private"), ("guild", "group"), (None, "unknown")])
def test_discord_parser_preserves_actual_conversation_type(monkeypatch, channel_type, expected):
    """
    Discord 使用 SDK 归一化的会话类型，不用个人 userid 推断私聊。
    """
    module = DiscordModule()
    config = NotificationConf(name="private-discord", type="discord", enabled=True)
    monkeypatch.setattr(module, "get_config", lambda _: config)
    monkeypatch.setattr(module, "get_instance", lambda _: None)
    message = module.message_parser("private-discord", json.dumps({
        "type": "message", "userid": "42", "chat_id": "17", "text": "绑定账号 synthetic-code",
        "channel_type": channel_type,
    }), {}, {})
    assert message.conversation_type == expected


@pytest.mark.parametrize("target,expected", [({"uid": 1}, "private"), ({"gid": 2}, "group"), ({}, "unknown")])
def test_voce_parser_requires_explicit_private_target(monkeypatch, target, expected):
    """
    VoceChat 缺少群字段不等于私聊，必须有明确的个人目标。
    """
    module = VoceChatModule()
    config = NotificationConf(name="private-voce", type="vocechat", enabled=True)
    monkeypatch.setattr(module, "get_config", lambda _: config)
    monkeypatch.setattr(module, "get_instance", lambda _: None)
    message = module.message_parser("private-voce", json.dumps({
        "from_uid": 7, "target": target,
        "detail": {"type": "normal", "content_type": "text/plain", "content": "绑定账号 synthetic-code"},
    }), {}, {})
    assert message.conversation_type == expected


@pytest.mark.parametrize("module_type,channel,kind,actor", [
    (TelegramModule, NotificationChannel.Telegram, "telegram", "42"),
    (WechatModule, NotificationChannel.Wechat, "wechat", "alice"),
    (WechatClawBotModule, NotificationChannel.WechatClawBot, "wechatclawbot", "alice@im.wechat"),
    (FeishuModule, NotificationChannel.Feishu, "feishu", "ou_alice"),
    (QQBotModule, NotificationChannel.QQ, "qqbot", "alice"),
    (DiscordModule, NotificationChannel.Discord, "discord", "42"),
    (SlackModule, NotificationChannel.Slack, "slack", "U42"),
    (VoceChatModule, NotificationChannel.VoceChat, "vocechat", "UID#7"),
    (SynologyChatModule, NotificationChannel.SynologyChat, "synologychat", "7"),
])
@pytest.mark.parametrize("confirmed", [False, True])
def test_all_interactive_providers_confirm_only_actual_private_send(
    monkeypatch, module_type, channel, kind, actor, confirmed,
):
    """
    九类交互渠道必须按个人地址发送并上报确定确认，平台失败不能伪装成功。
    """
    module = module_type()
    monkeypatch.setattr(module, "_channel", channel)
    config = NotificationConf(id="private", name="private", type=kind, enabled=True)
    client = Mock()
    for method in ("send_msg", "send_notification"):
        result = {"success": confirmed} if kind == "telegram" else confirmed
        getattr(client, method).return_value = result
    monkeypatch.setattr(module, "get_configs", lambda: {config.name: config})
    monkeypatch.setattr(module, "get_instance", lambda _: client)
    message = Message(channel=channel, source="private", userid=actor, title="秘密", private_delivery=True,
                      original_chat_id="group:room", original_message_id=99)
    if not confirmed:
        with pytest.raises(RuntimeError):
            module.post_message(message)
    else:
        assert module.post_message(message) is True
    method = client.send_notification if kind == "feishu" else client.send_msg
    assert method.call_args.kwargs["userid"] == actor
    assert method.call_args.kwargs.get("original_chat_id") is None
    assert method.call_args.kwargs.get("original_message_id") is None


def test_strict_private_delivery_without_provider_is_not_success(monkeypatch):
    """
    空的模块目录或未处理的定向消息不能使审批回执被报告为确认成功。
    """
    chain = MessageChain()
    monkeypatch.setattr(chain, "run_module_strict", Mock(return_value=None))
    with pytest.raises(RuntimeError, match="没有提供方确认"):
        chain.post_message(Message(
            channel=NotificationChannel.Telegram, source="missing", userid="42", title="秘密",
            private_delivery=True, save_history=False,
        ), _strict_delivery=True)


@pytest.mark.parametrize("userid", ["user:", "user:   "])
def test_wechat_explicit_empty_target_never_falls_back_to_known_users(userid):
    """
    低层收到显式但无法寻址的个人前缀时不回退到历史用户广播。
    """
    client = WeChatBot.__new__(WeChatBot)
    client._default_chat_id = None
    client._known_targets = {"alice", "bob"}
    assert client._resolve_targets(userid) == []
    assert client._resolve_targets(None) == [("alice", 1), ("bob", 1)]
