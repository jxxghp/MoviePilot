import sys
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

sys.modules.setdefault("qbittorrentapi", ModuleType("qbittorrentapi"))
setattr(sys.modules["qbittorrentapi"], "TorrentFilesList", list)
sys.modules.setdefault("transmission_rpc", ModuleType("transmission_rpc"))
setattr(sys.modules["transmission_rpc"], "File", object)
sys.modules.setdefault("psutil", ModuleType("psutil"))
sys.modules.setdefault("aioshutil", ModuleType("aioshutil"))
sys.modules.setdefault("pyquery", ModuleType("pyquery"))
setattr(sys.modules["pyquery"], "PyQuery", object)
sys.modules.setdefault("dateparser", ModuleType("dateparser"))
setattr(sys.modules["dateparser"], "parse", lambda *args, **kwargs: None)
sys.modules.setdefault("dateutil", ModuleType("dateutil"))
dateutil_parser = ModuleType("dateutil.parser")
setattr(dateutil_parser, "parse", lambda *args, **kwargs: None)
sys.modules.setdefault("dateutil.parser", dateutil_parser)
setattr(sys.modules["dateutil"], "parser", dateutil_parser)

from app.chain.message import MessageChain
from app.chain.site import SiteChain, site_interaction_manager
from app.chain.skills import skills_interaction_manager
from app.chain.subscribe import SubscribeChain, subscribe_interaction_manager
from app.schemas.types import MessageChannel


class TestSlashCommandInteractions(unittest.TestCase):
    def tearDown(self):
        skills_interaction_manager.clear()
        site_interaction_manager.clear()
        subscribe_interaction_manager.clear()

    def test_message_routes_text_reply_to_latest_sites_interaction(self):
        chain = MessageChain()
        skills_interaction_manager.create_or_replace(
            user_id="10001",
            channel=MessageChannel.Wechat,
            source="wechat-test",
            username="tester",
        )
        site_interaction_manager.create_or_replace(
            user_id="10001",
            command="/sites",
            channel=MessageChannel.Wechat,
            source="wechat-test",
            username="tester",
        )

        with patch.object(chain, "_record_user_message"), patch(
            "app.chain.message.SiteChain.handle_text_interaction",
            return_value=True,
        ) as handle_site, patch(
            "app.chain.message.SkillsChain.handle_text_interaction"
        ) as handle_skills:
            chain.handle_message(
                channel=MessageChannel.Wechat,
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
            channel=MessageChannel.Wechat,
            source="wechat-test",
            username="tester",
        )
        subscribe_interaction_manager.create_or_replace(
            user_id="10001",
            command="/subscribes",
            channel=MessageChannel.Wechat,
            source="wechat-test",
            username="tester",
        )

        with patch.object(chain, "_record_user_message"), patch(
            "app.chain.message.SubscribeChain.handle_text_interaction",
            return_value=True,
        ) as handle_subscribes, patch(
            "app.chain.message.SiteChain.handle_text_interaction"
        ) as handle_sites:
            chain.handle_message(
                channel=MessageChannel.Wechat,
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
            channel=MessageChannel.Telegram,
            source="telegram-test",
            username="tester",
        )

        with patch(
            "app.chain.message.SiteChain.handle_callback_interaction",
            return_value=True,
        ) as handle_callback:
            chain._handle_callback(
                text=f"CALLBACK:sites:{request.request_id}:refresh",
                channel=MessageChannel.Telegram,
                source="telegram-test",
                userid="10001",
                username="tester",
            )

        handle_callback.assert_called_once()

    def test_callback_routes_to_subscribes_chain(self):
        chain = MessageChain()
        request = subscribe_interaction_manager.create_or_replace(
            user_id="10001",
            command="/subscribes",
            channel=MessageChannel.Telegram,
            source="telegram-test",
            username="tester",
        )

        with patch(
            "app.chain.message.SubscribeChain.handle_callback_interaction",
            return_value=True,
        ) as handle_callback:
            chain._handle_callback(
                text=f"CALLBACK:subscribes:{request.request_id}:refresh",
                channel=MessageChannel.Telegram,
                source="telegram-test",
                userid="10001",
                username="tester",
            )

        handle_callback.assert_called_once()

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

        with patch("app.chain.site.SiteOper.list", return_value=fake_sites), patch.object(
            chain, "post_message"
        ) as post_message:
            chain.remote_list(channel=MessageChannel.Web, userid="u1", source="web")

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
            "app.chain.subscribe.SubscribeOper.list", return_value=fake_subscribes
        ), patch.object(chain, "post_message") as post_message:
            chain.remote_list(channel=MessageChannel.Web, userid="u1", source="web")

        notification = post_message.call_args[0][0]
        self.assertIn("| ID | 名称 | 类型 | 年份 | 季/进度 | 状态 |", notification.text)
        self.assertIn(
            "| 12 | Example Show | 电视剧 | 2024 | 第1季 [7/10] | 订阅中 |",
            notification.text,
        )
