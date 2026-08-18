import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from app.runtime.channels import matches_channel_admin, resolve_config_principal_ids
from app.modules.discord import DiscordModule
from app.modules.feishu.feishu import Feishu
from app.modules.qqbot import QQBotModule
from app.modules.slack import SlackModule
from app.modules.synologychat import SynologyChatModule
from app.modules.telegram import TelegramModule
from app.modules.vocechat import VoceChatModule
from app.modules.wechat import WechatModule
from app.modules.wechat.wechatbot import WeChatBot
from app.modules.wechatclawbot import WechatClawBotModule
from app.schemas.types import NotificationChannel


def _parse_module_message(module, *, config: dict, body, client=None, form=None):
    """使用隔离的渠道配置调用消息解析器。"""
    client = client or SimpleNamespace()
    with patch.object(
        module,
        "get_config",
        return_value=SimpleNamespace(name="channel-test", config=config),
    ), patch.object(module, "get_instance", return_value=client):
        return module.message_parser(
            source="channel-test",
            body=body,
            form=form or {},
            args={},
        )


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ({"ADMINS": " user-1, 42 "}, {"user-1", "42"}),
        ({"ADMINS": ""}, set()),
        ({}, set()),
        (None, set()),
    ],
)
def test_resolve_config_principal_ids_uses_nonempty_stable_values(config, expected):
    """渠道模块声明的配置值应统一转为非空字符串 ID。"""
    assert resolve_config_principal_ids(config, "ADMINS") == expected


@pytest.mark.parametrize(
    ("channel", "config", "principal_ids", "expected"),
    [
        (
            NotificationChannel.Telegram,
            {"TELEGRAM_ADMINS": "other", "TELEGRAM_CHAT_ID": "10001"},
            (10001,),
            True,
        ),
        (
            NotificationChannel.Feishu,
            {"FEISHU_ADMINS": "other", "FEISHU_OPEN_ID": "ou_owner"},
            ("ou_owner",),
            True,
        ),
        (
            NotificationChannel.Wechat,
            {
                "WECHAT_MODE": "bot",
                "WECHAT_ADMINS": "other",
                "WECHAT_BOT_CHAT_ID": "wx_owner",
            },
            ("wx_owner",),
            True,
        ),
        (
            NotificationChannel.Wechat,
            {
                "WECHAT_MODE": "app",
                "WECHAT_ADMINS": "other",
                "WECHAT_BOT_CHAT_ID": "stale_owner",
            },
            ("stale_owner",),
            False,
        ),
        (
            NotificationChannel.WechatClawBot,
            {
                "WECHATCLAWBOT_ADMINS": "other",
                "WECHATCLAWBOT_DEFAULT_TARGET": "wxid_owner",
            },
            ("wxid_owner",),
            True,
        ),
        (
            NotificationChannel.QQ,
            {"QQBOT_ADMINS": "other", "QQ_OPENID": "qq_owner"},
            ("qq_owner",),
            True,
        ),
        (
            NotificationChannel.Telegram,
            {"TELEGRAM_ADMINS": "other", "TELEGRAM_CHAT_ID": "-10001"},
            (10001,),
            False,
        ),
        (
            NotificationChannel.Feishu,
            {"FEISHU_ADMINS": "other", "FEISHU_CHAT_ID": "oc_group"},
            ("ou_user",),
            False,
        ),
        (
            NotificationChannel.QQ,
            {"QQBOT_ADMINS": "other", "QQ_GROUP_OPENID": "qq_group"},
            ("qq_member",),
            False,
        ),
    ],
)
def test_matches_channel_admin_includes_only_primary_user_ids(
    channel, config, principal_ids, expected
):
    """渠道主用户 ID 默认授权，但群组或频道目标不能授权其成员。"""
    assert matches_channel_admin(channel, config, *principal_ids) is expected


def test_matches_channel_admin_rejects_unregistered_channel():
    """未注册管理员解析器的渠道不能获得管理员权限。"""
    assert not matches_channel_admin("unregistered", {"ADMINS": "owner"}, "owner")


@pytest.mark.parametrize("message_kind", ["message", "callback"])
def test_telegram_uses_user_id_not_same_named_username(message_kind):
    module = TelegramModule()
    client = SimpleNamespace(bot_username=None, answer_callback_query=Mock())
    if message_kind == "message":
        payload = {
            "message_id": 10,
            "from": {"id": 10002, "username": "admin"},
            "chat": {"id": 10002},
            "text": "hello",
        }
    else:
        payload = {
            "callback_query": {
                "id": "callback-1",
                "from": {"id": 10002, "username": "admin"},
                "data": "choice:1",
                "message": {"message_id": 10, "chat": {"id": 10002}},
            }
        }

    message = _parse_module_message(
        module,
        config={"TELEGRAM_ADMINS": "admin,10001"},
        body=json.dumps(payload),
        client=client,
    )

    assert message.userid == 10002
    assert message.username == "admin"
    assert message.is_channel_admin is False


@pytest.mark.parametrize("message_kind", ["message", "callback"])
def test_telegram_uses_stable_user_id_for_admin(message_kind):
    module = TelegramModule()
    client = SimpleNamespace(bot_username=None, answer_callback_query=Mock())
    if message_kind == "message":
        payload = {
            "message_id": 10,
            "from": {"id": 10001, "username": "renamed-user"},
            "chat": {"id": 10001},
            "text": "hello",
        }
    else:
        payload = {
            "callback_query": {
                "id": "callback-1",
                "from": {"id": 10001, "username": "renamed-user"},
                "data": "choice:1",
                "message": {"message_id": 10, "chat": {"id": 10001}},
            }
        }

    message = _parse_module_message(
        module,
        config={"TELEGRAM_ADMINS": "10001"},
        body=json.dumps(payload),
        client=client,
    )

    assert message.userid == 10001
    assert message.is_channel_admin is True


def test_telegram_primary_user_id_is_admin_without_duplicate_admin_entry():
    """Telegram 主用户 ID 无需重复加入管理员名单。"""
    module = TelegramModule()
    client = SimpleNamespace(bot_username=None, send_msg=Mock())
    message = _parse_module_message(
        module,
        config={
            "TELEGRAM_CHAT_ID": "10001",
            "TELEGRAM_ADMINS": "10002",
        },
        body=json.dumps(
            {
                "message_id": 10,
                "from": {"id": 10001, "username": "owner"},
                "chat": {"id": 10001},
                "text": "/sites",
            }
        ),
        client=client,
    )

    assert message.is_channel_admin is True
    client.send_msg.assert_not_called()


def test_telegram_group_chat_id_does_not_authorize_group_member():
    """Telegram 群组 Chat ID 不能使群内发送者默认成为管理员。"""
    message = _parse_module_message(
        TelegramModule(),
        config={"TELEGRAM_CHAT_ID": "-10001", "TELEGRAM_ADMINS": "10002"},
        body=json.dumps(
            {
                "message_id": 10,
                "from": {"id": 10001, "username": "member"},
                "chat": {"id": -10001},
                "text": "hello",
            }
        ),
        client=SimpleNamespace(bot_username=None),
    )

    assert message.is_channel_admin is False


def test_telegram_slash_does_not_accept_admin_display_username():
    """Telegram 斜杠命令不得把可修改的 username 当作管理员 ID。"""
    module = TelegramModule()
    client = SimpleNamespace(bot_username=None, send_msg=Mock())
    message = _parse_module_message(
        module,
        config={"TELEGRAM_ADMINS": "admin"},
        body=json.dumps(
            {
                "message_id": 10,
                "from": {"id": 10002, "username": "admin"},
                "chat": {"id": 10002},
                "text": "/sites",
            }
        ),
        client=client,
    )

    assert message is None
    client.send_msg.assert_called_once()


def test_telegram_empty_admin_list_keeps_legacy_slash_without_agent_admin():
    """空名单保持传统命令可用，但不能生成 Agent 管理员身份。"""
    module = TelegramModule()
    client = SimpleNamespace(bot_username=None, send_msg=Mock())
    message = _parse_module_message(
        module,
        config={"TELEGRAM_ADMINS": ""},
        body=json.dumps(
            {
                "message_id": 10,
                "from": {"id": 10002, "username": "admin"},
                "chat": {"id": 10002},
                "text": "/sites",
            }
        ),
        client=client,
    )

    assert message.is_channel_admin is False
    client.send_msg.assert_not_called()


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "message", "user": "UADMIN", "text": "hello"},
        {
            "type": "block_actions",
            "user": {"id": "UADMIN", "name": "renamed-user"},
            "actions": [{"value": "choice:1"}],
            "message": {"ts": "1710000000.000100"},
            "container": {"channel_id": "C01"},
        },
    ],
)
def test_slack_message_and_callback_use_stable_user_id(payload):
    message = _parse_module_message(
        SlackModule(),
        config={"SLACK_ADMINS": "UADMIN"},
        body=json.dumps(payload),
    )

    assert message.userid == "UADMIN"
    assert message.is_channel_admin is True


def test_slack_slash_does_not_accept_admin_display_username():
    """Slack 原生斜杠命令只接受稳定 user_id，不接受 user_name。"""
    client = SimpleNamespace(send_msg=Mock())
    message = _parse_module_message(
        SlackModule(),
        config={"SLACK_ADMINS": "admin"},
        body=json.dumps(
            {
                "command": "/sites",
                "user_id": "UUSER",
                "user_name": "admin",
                "channel_id": "C01",
            }
        ),
        client=client,
    )

    assert message is None
    client.send_msg.assert_called_once()


def test_slack_empty_admin_list_keeps_legacy_slash_without_agent_admin():
    """Slack 空名单不预拦截命令，但 Agent 管理员事实仍为否。"""
    client = SimpleNamespace(send_msg=Mock())
    message = _parse_module_message(
        SlackModule(),
        config={"SLACK_ADMINS": ""},
        body=json.dumps(
            {
                "command": "/sites",
                "user_id": "UUSER",
                "user_name": "admin",
                "channel_id": "C01",
            }
        ),
        client=client,
    )

    assert message.is_channel_admin is False
    client.send_msg.assert_not_called()


@pytest.mark.parametrize(
    "payload",
    [
        {
            "type": "message",
            "userid": "discord-admin-id",
            "username": "renamed-user",
            "text": "hello",
        },
        {
            "type": "interaction",
            "userid": "discord-admin-id",
            "username": "renamed-user",
            "callback_data": "choice:1",
        },
    ],
)
def test_discord_message_and_callback_use_stable_user_id(payload):
    message = _parse_module_message(
        DiscordModule(),
        config={"DISCORD_ADMINS": "discord-admin-id"},
        body=json.dumps(payload),
    )

    assert message.userid == "discord-admin-id"
    assert message.is_channel_admin is True


@pytest.mark.parametrize(
    ("payload", "admins"),
    [
        (
            {
                "text": "hello",
                "sender": {
                    "open_id": "ou_admin",
                    "user_id": "u_other",
                    "name": "renamed-user",
                },
            },
            "ou_admin",
        ),
        (
            {
                "type": "cardAction",
                "callback_data": "choice:1",
                "sender": {
                    "open_id": "ou_other",
                    "user_id": "u_admin",
                    "name": "renamed-user",
                },
            },
            "u_admin",
        ),
    ],
)
def test_feishu_message_and_card_callback_accept_open_id_or_user_id(payload, admins):
    with patch.object(Feishu, "_build_api_client", return_value=Mock()), patch.object(
        Feishu, "_start_ws_client"
    ), patch("app.modules.feishu.feishu.UserOper") as user_oper:
        user_oper.return_value.get_name.return_value = None
        client = Feishu(
            FEISHU_APP_ID="app-id",
            FEISHU_APP_SECRET="app-secret",
            FEISHU_ADMINS=admins,
            name="feishu-test",
        )
        message = client.parse_message(payload)

    assert message.userid == payload["sender"]["open_id"]
    assert message.is_channel_admin is True


def test_feishu_default_open_id_is_admin_without_duplicate_admin_entry():
    """飞书默认用户 Open ID 无需重复加入管理员名单。"""
    with patch.object(Feishu, "_build_api_client", return_value=Mock()), patch.object(
        Feishu, "_start_ws_client"
    ), patch("app.modules.feishu.feishu.UserOper") as user_oper:
        user_oper.return_value.get_name.return_value = None
        client = Feishu(
            FEISHU_APP_ID="app-id",
            FEISHU_APP_SECRET="app-secret",
            FEISHU_OPEN_ID="ou_owner",
            FEISHU_ADMINS="ou_other",
            name="feishu-test",
        )
        message = client.parse_message(
            {
                "text": "/sites",
                "sender": {"open_id": "ou_owner", "name": "owner"},
            }
        )

    assert message.is_channel_admin is True


@pytest.mark.parametrize(
    ("user_id", "username", "expected"),
    [("wxid_admin", "renamed-user", True), ("wxid_user", "admin-name", False)],
)
def test_wechatclawbot_uses_channel_user_id_not_username(user_id, username, expected):
    message = _parse_module_message(
        WechatClawBotModule(),
        config={"WECHATCLAWBOT_ADMINS": "admin-name,wxid_admin"},
        body={
            "__channel__": "wechatclawbot",
            "userid": user_id,
            "username": username,
            "text": "hello",
        },
    )

    assert message.userid == user_id
    assert message.is_channel_admin is expected


def test_wechatclawbot_primary_user_id_is_admin_without_duplicate_admin_entry():
    """微信 ClawBot 默认用户 ID 无需重复加入管理员名单。"""
    message = _parse_module_message(
        WechatClawBotModule(),
        config={
            "WECHATCLAWBOT_DEFAULT_TARGET": "wxid_owner",
            "WECHATCLAWBOT_ADMINS": "wxid_other",
        },
        body={
            "__channel__": "wechatclawbot",
            "userid": "wxid_owner",
            "username": "owner",
            "text": "/sites",
        },
    )

    assert message.is_channel_admin is True


@pytest.mark.parametrize(
    ("sender", "admins", "expected"),
    [("wechat-admin", "wechat-admin", True), ("wechat-user", "display-admin", False)],
)
def test_wechat_bot_uses_sender_userid(sender, admins, expected):
    message = _parse_module_message(
        WechatModule(),
        config={"WECHAT_MODE": "bot", "WECHAT_ADMINS": admins},
        body=json.dumps(
            {
                "body": {
                    "from": {"userid": sender},
                    "msgtype": "text",
                    "text": {"content": "hello"},
                }
            }
        ),
    )

    assert message.userid == sender
    assert message.is_channel_admin is expected


def test_wechat_bot_primary_user_id_is_admin_without_duplicate_admin_entry():
    """企业微信机器人默认用户 ID 无需重复加入管理员名单。"""
    message = _parse_module_message(
        WechatModule(),
        config={
            "WECHAT_MODE": "bot",
            "WECHAT_BOT_CHAT_ID": "wechat-owner",
            "WECHAT_ADMINS": "wechat-other",
        },
        body=json.dumps(
            {
                "body": {
                    "from": {"userid": "wechat-owner"},
                    "msgtype": "text",
                    "text": {"content": "/sites"},
                }
            }
        ),
    )

    assert message.is_channel_admin is True


def test_wechat_bot_client_allows_primary_user_command():
    """企业微信机器人客户端不得在转发前拦截主用户 ID。"""
    bot = WeChatBot.__new__(WeChatBot)
    bot._config_name = "wechat-bot-test"
    bot._admins = ["wechat-other"]
    bot._default_chat_id = "wechat-owner"
    bot.send_msg = Mock()
    bot._remember_target = Mock()
    bot._forward_to_message_chain = Mock()
    payload = {
        "body": {
            "from": {"userid": "wechat-owner"},
            "msgtype": "text",
            "text": {"content": "/sites"},
        }
    }

    bot._handle_callback_message(payload)

    bot.send_msg.assert_not_called()
    bot._forward_to_message_chain.assert_called_once_with(payload)


def test_qq_c2c_uses_user_openid_for_admin():
    message = _parse_module_message(
        QQBotModule(),
        config={"QQBOT_ADMINS": "qq-admin"},
        body={
            "type": "C2C_MESSAGE_CREATE",
            "content": "hello",
            "author": {"user_openid": "qq-admin"},
        },
    )

    assert message.userid == "qq-admin"
    assert message.is_channel_admin is True


def test_qq_primary_user_openid_is_admin_without_duplicate_admin_entry():
    """QQ 默认用户 OpenID 无需重复加入管理员名单。"""
    message = _parse_module_message(
        QQBotModule(),
        config={"QQ_OPENID": "qq-owner", "QQBOT_ADMINS": "qq-other"},
        body={
            "type": "C2C_MESSAGE_CREATE",
            "content": "/sites",
            "author": {"user_openid": "qq-owner"},
        },
    )

    assert message.is_channel_admin is True


@pytest.mark.parametrize(
    ("admins", "expected"),
    [("member-admin", True), ("group:group-admin", False), ("group-admin", False)],
)
def test_qq_group_uses_only_member_openid_for_admin(admins, expected):
    message = _parse_module_message(
        QQBotModule(),
        config={"QQBOT_ADMINS": admins},
        body={
            "type": "GROUP_AT_MESSAGE_CREATE",
            "content": "hello",
            "author": {"member_openid": "member-admin"},
            "group_openid": "group-admin",
        },
    )

    assert message.userid == "group:group-admin"
    assert message.username == "member-admin"
    assert message.is_channel_admin is expected


@pytest.mark.parametrize(
    ("admins", "expected"),
    [("7", True), ("UID#7", True), ("GID#2", False)],
)
def test_vocechat_group_uses_only_sender_uid_for_admin(admins, expected):
    message = _parse_module_message(
        VoceChatModule(),
        config={"VOCECHAT_ADMINS": admins, "channel_id": "2"},
        body=json.dumps(
            {
                "detail": {
                    "type": "normal",
                    "content_type": "text/plain",
                    "content": "hello",
                },
                "from_uid": 7,
                "target": {"gid": 2},
            }
        ),
    )

    assert message.userid == "GID#2"
    assert message.username == "GID#2"
    assert message.is_channel_admin is expected


@pytest.mark.parametrize(
    ("admins", "expected"),
    [("42", True), ("display-admin", False)],
)
def test_synology_chat_uses_numeric_user_id_not_username(admins, expected):
    client = SimpleNamespace(check_token=Mock(return_value=True))
    message = _parse_module_message(
        SynologyChatModule(),
        config={"SYNOLOGYCHAT_ADMINS": admins},
        body={},
        form={
            "token": "token",
            "text": "hello",
            "user_id": "42",
            "username": "display-admin",
        },
        client=client,
    )

    assert message.userid == 42
    assert message.username == "display-admin"
    assert message.is_channel_admin is expected
