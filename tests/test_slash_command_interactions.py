import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.testing.bootstrap import ensure_optional_stub

ensure_optional_stub("qbittorrentapi", TorrentFilesList=list)
ensure_optional_stub("transmission_rpc", File=object)
ensure_optional_stub("psutil")
ensure_optional_stub("aioshutil")
ensure_optional_stub("pyquery", PyQuery=object)

from app.application.orchestration.message import MessageChain
from app.application.messaging.interaction import InteractionContext
from app.application.orchestration.site import SiteChain
from app.application.messaging.site import site_interaction_manager
from app.application.messaging.skill import skill_interaction_manager
from app.application.orchestration.subscribe import SubscribeChain
from app.application.messaging.subscribe import subscribe_interaction_manager
from app.schemas.types import NotificationChannel


class TestSlashCommandInteractions(unittest.TestCase):
    def tearDown(self):
        skill_interaction_manager.clear()
        site_interaction_manager.clear()
        subscribe_interaction_manager.clear()

    def test_message_routes_text_reply_to_latest_sites_interaction(self):
        chain = MessageChain()
        skill_interaction_manager.create_or_replace(
            user_id="10001",
            channel=NotificationChannel.Wechat,
            source="wechat-test",
            username="tester",
        )
        site_interaction_manager.create_or_replace(
            user_id="10001",
            command="/sites",
            channel=NotificationChannel.Wechat,
            source="wechat-test",
            username="tester",
        )

        with patch.object(chain, "_record_user_message"), patch(
            "app.application.orchestration.message.SiteChain.handle_text_interaction",
            return_value=True,
        ) as handle_site, patch(
            "app.application.orchestration.message.SkillInteractionHandler.handle_text_interaction"
        ) as handle_skills:
            chain.handle_message(
                channel=NotificationChannel.Wechat,
                source="wechat-test",
                userid="10001",
                username="tester",
                text="禁用 1",
            )

        handle_site.assert_called_once()
        handle_skills.assert_not_called()

    def test_message_routes_text_reply_to_latest_subscribes_interaction(self):
        chain = MessageChain()
        site_interaction_manager.create_or_replace(
            user_id="10001",
            command="/sites",
            channel=NotificationChannel.Wechat,
            source="wechat-test",
            username="tester",
        )
        subscribe_interaction_manager.create_or_replace(
            user_id="10001",
            command="/subscribes",
            channel=NotificationChannel.Wechat,
            source="wechat-test",
            username="tester",
        )

        with patch.object(chain, "_record_user_message"), patch(
            "app.application.orchestration.message.SubscribeChain.handle_text_interaction",
            return_value=True,
        ) as handle_subscribes, patch(
            "app.application.orchestration.message.SiteChain.handle_text_interaction"
        ) as handle_sites:
            chain.handle_message(
                channel=NotificationChannel.Wechat,
                source="wechat-test",
                userid="10001",
                username="tester",
                text="搜索 all",
            )

        handle_subscribes.assert_called_once()
        handle_sites.assert_not_called()

    def test_callback_routes_to_sites_chain(self):
        chain = MessageChain()
        request = site_interaction_manager.create_or_replace(
            user_id="10001",
            command="/sites",
            channel=NotificationChannel.Telegram,
            source="telegram-test",
            username="tester",
        )

        with patch(
            "app.application.orchestration.message.SiteChain.handle_callback_interaction",
            return_value=True,
        ) as handle_callback:
            chain._handle_callback(
                callback_data=f"sites:{request.request_id}:refresh",
                context=InteractionContext(
                    channel=NotificationChannel.Telegram,
                    source="telegram-test",
                    user_id="10001",
                    username="tester",
                ),
            )

        handle_callback.assert_called_once()

    def test_callback_routes_to_subscribes_chain(self):
        chain = MessageChain()
        request = subscribe_interaction_manager.create_or_replace(
            user_id="10001",
            command="/subscribes",
            channel=NotificationChannel.Telegram,
            source="telegram-test",
            username="tester",
        )

        with patch(
            "app.application.orchestration.message.SubscribeChain.handle_callback_interaction",
            return_value=True,
        ) as handle_callback:
            chain._handle_callback(
                callback_data=f"subscribes:{request.request_id}:refresh",
                context=InteractionContext(
                    channel=NotificationChannel.Telegram,
                    source="telegram-test",
                    user_id="10001",
                    username="tester",
                ),
            )

        handle_callback.assert_called_once()

    def test_sites_text_exit_skips_notification_history(self):
        chain = SiteChain()
        site_interaction_manager.create_or_replace(
            user_id="10001",
            command="/sites",
            channel=NotificationChannel.Telegram,
            source="telegram-test",
            username="tester",
        )

        with patch.object(chain, "post_message") as post_message:
            handled = chain.handle_text_interaction(
                channel=NotificationChannel.Telegram,
                source="telegram-test",
                userid="10001",
                username="tester",
                text="退出",
            )

        self.assertTrue(handled)
        notification = post_message.call_args.args[0]
        self.assertEqual(notification.title, "站点交互已结束")
        self.assertFalse(notification.save_history)
        self.assertIsNone(site_interaction_manager.get_by_user("10001"))

    def test_subscribes_text_exit_skips_notification_history(self):
        chain = SubscribeChain()
        subscribe_interaction_manager.create_or_replace(
            user_id="10001",
            command="/subscribes",
            channel=NotificationChannel.Telegram,
            source="telegram-test",
            username="tester",
        )

        with patch.object(chain, "post_message") as post_message:
            handled = chain.handle_text_interaction(
                channel=NotificationChannel.Telegram,
                source="telegram-test",
                userid="10001",
                username="tester",
                text="退出",
            )

        self.assertTrue(handled)
        notification = post_message.call_args.args[0]
        self.assertEqual(notification.title, "订阅交互已结束")
        self.assertFalse(notification.save_history)
        self.assertIsNone(subscribe_interaction_manager.get_by_user("10001"))

    def test_sites_renders_markdown_table_when_channel_supports_markdown(self):
        chain = SiteChain()
        fake_sites = [
            SimpleNamespace(
                id=1,
                name="M-Team",
                is_active=True,
                cookie="cookie=value",
                render=1,
                domain="m-team.io",
                url="https://m-team.io/",
            )
        ]

        with patch("app.application.orchestration.site.SiteOper.list", return_value=fake_sites), patch.object(
            chain, "post_message"
        ) as post_message:
            chain.remote_list(channel=NotificationChannel.Web, userid="u1", source="web")

        notification = post_message.call_args[0][0]
        self.assertIn("| ID | 站点 | 状态 | Cookie | 渲染 | 域名 |", notification.text)
        self.assertIn("| 1 | M-Team | 启用 | 已配置 | 是 | m-team.io |", notification.text)

    def test_subscribes_renders_markdown_table_when_channel_supports_markdown(self):
        chain = SubscribeChain()
        fake_subscribes = [
            SimpleNamespace(
                id=12,
                name="Example Show",
                type="电视剧",
                year="2024",
                season=1,
                total_episode=10,
                lack_episode=3,
                state="R",
            )
        ]

        with patch(
            "app.application.orchestration.subscribe.SubscribeOper.list", return_value=fake_subscribes
        ), patch.object(chain, "post_message") as post_message:
            chain.remote_list(channel=NotificationChannel.Web, userid="u1", source="web")

        notification = post_message.call_args[0][0]
        self.assertIn("| ID | 名称 | 类型 | 年份 | 季/进度 | 状态 |", notification.text)
        self.assertIn(
            "| 12 | Example Show | 电视剧 | 2024 | 第1季 [7/10] | 订阅中 |",
            notification.text,
        )


class TestUpdateOrPostMessage(unittest.TestCase):
    def test_fallback_post_keeps_original_message_context(self):
        """编辑失败回退发新消息时，必须保留原消息/会话上下文，供渠道回复到原会话。"""
        from app.application.messaging.interaction import update_or_post_message

        chain = SimpleNamespace(
            edit_message=MagicMock(return_value=False),
            post_message=MagicMock(),
        )

        update_or_post_message(
            chain=chain,
            channel=NotificationChannel.Feishu,
            source="feishu-main",
            userid="ou_user",
            username="tester",
            title="标题",
            text="正文",
            original_message_id="om_origin",
            original_chat_id="oc_group",
        )

        chain.post_message.assert_called_once()
        notification = chain.post_message.call_args[0][0]
        self.assertEqual(notification.original_message_id, "om_origin")
        self.assertEqual(notification.original_chat_id, "oc_group")
