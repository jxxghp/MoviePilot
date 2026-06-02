import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.modules.feishu.feishu import Feishu
from app.modules.discord import DiscordModule
from app.modules.qqbot import QQBotModule
from app.modules.slack import SlackModule
from app.modules.synologychat import SynologyChatModule
from app.modules.telegram import TelegramModule
from app.modules.vocechat import VoceChatModule
from app.modules.wechatclawbot import WechatClawBotModule


class TestMessageChannelPermissions(unittest.TestCase):
    """消息渠道管理员权限测试。"""

    def test_feishu_command_callback_blocks_non_admin(self):
        """飞书命令型按钮回调应拦截非管理员。"""
        with (
            patch.object(Feishu, "_build_api_client", return_value=Mock()),
            patch.object(Feishu, "_start_ws_client"),
        ):
            client = Feishu(
                FEISHU_APP_ID="app-id",
                FEISHU_APP_SECRET="app-secret",
                FEISHU_ADMINS="ou_admin",
                name="feishu-test",
            )

        with patch.object(client, "send_text", return_value={"success": True}) as send_text:
            message = client.parse_message(
                {
                    "type": "cardAction",
                    "callback_data": "/sites",
                    "message_id": "om_1",
                    "chat_id": "oc_1",
                    "sender": {
                        "open_id": "ou_user",
                        "user_id": "u_user",
                        "name": "tester",
                    },
                }
            )

        self.assertIsNone(message)
        send_text.assert_called_once_with(
            "只有管理员才有权限执行此命令",
            userid="ou_user",
            chat_id="oc_1",
            receive_id_type="open_id",
        )

    def test_telegram_command_callback_blocks_non_admin(self):
        """Telegram 命令型按钮回调应拦截非管理员。"""
        module = TelegramModule()
        client = SimpleNamespace(answer_callback_query=Mock(), bot_username=None)

        with patch.object(
            module,
            "get_config",
            return_value=SimpleNamespace(
                name="telegram-test", config={"TELEGRAM_ADMINS": "10001"}
            ),
        ), patch.object(module, "get_instance", return_value=client):
            message = module.message_parser(
                source="telegram-test",
                body=json.dumps(
                    {
                        "callback_query": {
                            "id": "callback-1",
                            "from": {"id": 10002, "username": "tester"},
                            "data": "/sites",
                            "message": {"message_id": 12, "chat": {"id": "-100"}},
                        }
                    }
                ),
                form={},
                args={},
            )

        self.assertIsNone(message)
        client.answer_callback_query.assert_called_once_with(
            callback_query_id="callback-1",
            text="只有管理员才有权限执行此命令",
            show_alert=True,
        )

    def test_slack_command_callback_blocks_non_admin(self):
        """Slack 命令型按钮回调应拦截非管理员。"""
        module = SlackModule()
        client = SimpleNamespace(send_msg=Mock())

        with patch.object(
            module,
            "get_config",
            return_value=SimpleNamespace(
                name="slack-test", config={"SLACK_ADMINS": "UADMIN"}
            ),
        ), patch.object(module, "get_instance", return_value=client):
            message = module.message_parser(
                source="slack-test",
                body=json.dumps(
                    {
                        "type": "block_actions",
                        "user": {"id": "UUSER", "name": "tester"},
                        "actions": [{"value": "/sites"}],
                        "message": {"ts": "1710000000.000100"},
                        "container": {"channel_id": "C01"},
                    }
                ),
                form={},
                args={},
            )

        self.assertIsNone(message)
        client.send_msg.assert_called_once_with(
            title="只有管理员才有权限执行此命令", userid="UUSER"
        )

    def test_discord_command_interaction_blocks_non_admin(self):
        """Discord 命令型按钮回调应拦截非管理员。"""
        module = DiscordModule()
        client = SimpleNamespace(send_msg=Mock())

        with patch.object(
            module,
            "get_config",
            return_value=SimpleNamespace(
                name="discord-test", config={"DISCORD_ADMINS": "admin-id"}
            ),
        ), patch.object(module, "get_instance", return_value=client):
            message = module.message_parser(
                source="discord-test",
                body=json.dumps(
                    {
                        "type": "interaction",
                        "userid": "user-id",
                        "username": "tester",
                        "callback_data": "/sites",
                        "message_id": "msg-1",
                        "chat_id": "chat-1",
                    }
                ),
                form={},
                args={},
            )

        self.assertIsNone(message)
        client.send_msg.assert_called_once_with(
            title="只有管理员才有权限执行此命令",
            userid="user-id",
            original_chat_id="chat-1",
        )

    def test_non_command_callbacks_allow_non_admin(self):
        """非命令型按钮回调不应套用管理员限制。"""
        with (
            patch.object(Feishu, "_build_api_client", return_value=Mock()),
            patch.object(Feishu, "_start_ws_client"),
        ):
            feishu = Feishu(
                FEISHU_APP_ID="app-id",
                FEISHU_APP_SECRET="app-secret",
                FEISHU_ADMINS="ou_admin",
                name="feishu-test",
            )
        with patch.object(feishu, "send_text") as send_text:
            feishu_message = feishu.parse_message(
                {
                    "type": "cardAction",
                    "callback_data": "sites:req:refresh",
                    "sender": {"open_id": "ou_user", "user_id": "u_user"},
                }
            )
        self.assertIsNotNone(feishu_message)
        send_text.assert_not_called()

        telegram_module = TelegramModule()
        telegram_client = SimpleNamespace(answer_callback_query=Mock(), bot_username=None)
        with patch.object(
            telegram_module,
            "get_config",
            return_value=SimpleNamespace(
                name="telegram-test", config={"TELEGRAM_ADMINS": "10001"}
            ),
        ), patch.object(telegram_module, "get_instance", return_value=telegram_client):
            telegram_message = telegram_module.message_parser(
                source="telegram-test",
                body=json.dumps(
                    {
                        "callback_query": {
                            "id": "callback-1",
                            "from": {"id": 10002, "username": "tester"},
                            "data": "sites:req:refresh",
                        }
                    }
                ),
                form={},
                args={},
            )
        self.assertIsNotNone(telegram_message)
        telegram_client.answer_callback_query.assert_not_called()

        slack_module = SlackModule()
        slack_client = SimpleNamespace(send_msg=Mock())
        with patch.object(
            slack_module,
            "get_config",
            return_value=SimpleNamespace(
                name="slack-test", config={"SLACK_ADMINS": "UADMIN"}
            ),
        ), patch.object(slack_module, "get_instance", return_value=slack_client):
            slack_message = slack_module.message_parser(
                source="slack-test",
                body=json.dumps(
                    {
                        "type": "block_actions",
                        "user": {"id": "UUSER", "name": "tester"},
                        "actions": [{"value": "sites:req:refresh"}],
                        "message": {"ts": "1710000000.000100"},
                        "container": {"channel_id": "C01"},
                    }
                ),
                form={},
                args={},
            )
        self.assertIsNotNone(slack_message)
        slack_client.send_msg.assert_not_called()

        discord_module = DiscordModule()
        discord_client = SimpleNamespace(send_msg=Mock())
        with patch.object(
            discord_module,
            "get_config",
            return_value=SimpleNamespace(
                name="discord-test", config={"DISCORD_ADMINS": "admin-id"}
            ),
        ), patch.object(discord_module, "get_instance", return_value=discord_client):
            discord_message = discord_module.message_parser(
                source="discord-test",
                body=json.dumps(
                    {
                        "type": "interaction",
                        "userid": "user-id",
                        "username": "tester",
                        "callback_data": "sites:req:refresh",
                    }
                ),
                form={},
                args={},
            )
        self.assertIsNotNone(discord_message)
        discord_client.send_msg.assert_not_called()

        clawbot_module = WechatClawBotModule()
        clawbot_client = SimpleNamespace(send_msg=Mock())
        with patch.object(
            clawbot_module,
            "get_config",
            return_value=SimpleNamespace(
                name="wechatclawbot-test",
                config={"WECHATCLAWBOT_ADMINS": "admin-user"},
            ),
        ), patch.object(clawbot_module, "get_instance", return_value=clawbot_client):
            clawbot_message = clawbot_module.message_parser(
                source="wechatclawbot-test",
                body={
                    "__channel__": "wechatclawbot",
                    "userid": "normal-user",
                    "text": "CALLBACK:sites:req:refresh",
                },
                form={},
                args={},
            )
        self.assertIsNotNone(clawbot_message)
        clawbot_client.send_msg.assert_not_called()

    def test_qq_slash_command_blocks_non_admin(self):
        """QQ 斜杠命令应拦截非管理员。"""
        module = QQBotModule()
        client = SimpleNamespace(send_msg=Mock())

        with patch.object(
            module,
            "get_config",
            return_value=SimpleNamespace(
                name="qq-test", config={"QQBOT_ADMINS": "admin-openid"}
            ),
        ), patch.object(module, "get_instance", return_value=client):
            message = module.message_parser(
                source="qq-test",
                body={
                    "type": "C2C_MESSAGE_CREATE",
                    "content": "/sites",
                    "author": {"user_openid": "user-openid"},
                },
                form={},
                args={},
            )

        self.assertIsNone(message)
        client.send_msg.assert_called_once_with(
            title="只有管理员才有权限执行此命令", userid="user-openid"
        )

    def test_vocechat_slash_command_blocks_non_admin(self):
        """VoceChat 斜杠命令应拦截非管理员。"""
        module = VoceChatModule()
        client = SimpleNamespace(send_msg=Mock())

        with patch.object(
            module,
            "get_config",
            return_value=SimpleNamespace(
                name="vocechat-test",
                config={"VOCECHAT_ADMINS": "UID#1", "channel_id": "2"},
            ),
        ), patch.object(module, "get_instance", return_value=client):
            message = module.message_parser(
                source="vocechat-test",
                body=json.dumps(
                    {
                        "detail": {
                            "type": "normal",
                            "content_type": "text/plain",
                            "content": "/sites",
                        },
                        "from_uid": 3,
                        "target": {"uid": 1},
                    }
                ),
                form={},
                args={},
            )

        self.assertIsNone(message)
        client.send_msg.assert_called_once_with(
            title="只有管理员才有权限执行此命令", userid="UID#3"
        )

    def test_synologychat_slash_command_blocks_non_admin(self):
        """Synology Chat 斜杠命令应拦截非管理员。"""
        module = SynologyChatModule()
        client = SimpleNamespace(check_token=Mock(return_value=True), send_msg=Mock())

        with patch.object(
            module,
            "get_config",
            return_value=SimpleNamespace(
                name="synology-test", config={"SYNOLOGYCHAT_ADMINS": "admin"}
            ),
        ), patch.object(module, "get_instance", return_value=client):
            message = module.message_parser(
                source="synology-test",
                body={},
                form={
                    "token": "token",
                    "text": "/sites",
                    "user_id": "42",
                    "username": "tester",
                },
                args={},
            )

        self.assertIsNone(message)
        client.send_msg.assert_called_once_with(
            title="只有管理员才有权限执行此命令", userid="42"
        )

    def test_wechatclawbot_command_callback_blocks_non_admin(self):
        """微信 ClawBot 命令型回调消息应拦截非管理员。"""
        module = WechatClawBotModule()
        client = SimpleNamespace(send_msg=Mock())

        with patch.object(
            module,
            "get_config",
            return_value=SimpleNamespace(
                name="wechatclawbot-test",
                config={"WECHATCLAWBOT_ADMINS": "admin-user"},
            ),
        ), patch.object(module, "get_instance", return_value=client):
            message = module.message_parser(
                source="wechatclawbot-test",
                body={
                    "__channel__": "wechatclawbot",
                    "userid": "normal-user",
                    "text": "CALLBACK:/sites",
                },
                form={},
                args={},
            )

        self.assertIsNone(message)
        client.send_msg.assert_called_once_with(
            title="只有管理员才有权限执行此命令", userid="normal-user"
        )
