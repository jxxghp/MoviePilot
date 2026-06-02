import unittest
from unittest.mock import patch

from app.chain.media import MediaChain
from app.chain.message import MediaInteractionChain, MessageChain
from app.core.context import MediaInfo
from app.core.meta import MetaBase
from app.helper.interaction import media_interaction_manager
from app.schemas.types import MessageChannel


class TestMediaInteraction(unittest.TestCase):
    def tearDown(self):
        media_interaction_manager.clear()

    @staticmethod
    def _build_meta(name: str) -> MetaBase:
        meta = MetaBase(name)
        meta.name = name
        meta.begin_season = 1
        return meta

    def test_message_routes_text_reply_to_media_interaction_before_ai(self):
        chain = MessageChain()
        request = media_interaction_manager.create_or_replace(
            user_id="10001",
            channel=MessageChannel.Wechat,
            source="wechat-test",
            username="tester",
            action="Search",
            keyword="星际穿越",
            title="星际穿越",
            meta=self._build_meta("星际穿越"),
            items=[MediaInfo(title="星际穿越", year="2014")],
        )
        self.assertIsNotNone(request)

        with patch.object(chain, "_record_user_message"), patch(
            "app.chain.message.MediaInteractionChain.handle_text_interaction",
            return_value=True,
        ) as handle_text, patch.object(chain, "_handle_ai_message") as handle_ai:
            chain.handle_message(
                channel=MessageChannel.Wechat,
                source="wechat-test",
                userid="10001",
                username="tester",
                text="1",
            )

        handle_text.assert_called_once()
        handle_ai.assert_not_called()

    def test_callback_routes_to_media_interaction_chain(self):
        chain = MessageChain()
        request = media_interaction_manager.create_or_replace(
            user_id="10001",
            channel=MessageChannel.Telegram,
            source="telegram-test",
            username="tester",
            action="Search",
            keyword="星际穿越",
            title="星际穿越",
            meta=self._build_meta("星际穿越"),
            items=[MediaInfo(title="星际穿越", year="2014")],
        )

        with patch(
            "app.chain.message.MediaInteractionChain.handle_callback_interaction",
            return_value=True,
        ) as handle_callback:
            chain._handle_callback(
                text=f"CALLBACK:media:{request.request_id}:page-next",
                channel=MessageChannel.Telegram,
                source="telegram-test",
                userid="10001",
                username="tester",
            )

        handle_callback.assert_called_once()

    def test_media_interaction_starts_search_and_posts_media_list(self):
        chain = MediaInteractionChain()
        meta = self._build_meta("星际穿越")
        medias = [
            MediaInfo(title="星际穿越", year="2014"),
            MediaInfo(title="Interstellar", year="2014"),
        ]

        with patch(
            "app.chain.media.MediaChain.search",
            return_value=(meta, medias),
        ), patch.object(chain, "post_medias_message") as post_medias_message:
            handled = chain.handle_text_interaction(
                channel=MessageChannel.Telegram,
                source="telegram-test",
                userid="10001",
                username="tester",
                text="星际穿越",
            )

        self.assertTrue(handled)
        post_medias_message.assert_called_once()
        notification = post_medias_message.call_args.args[0]
        self.assertTrue(notification.buttons)
        self.assertTrue(
            notification.buttons[0][0]["callback_data"].startswith("media:")
        )

        request = media_interaction_manager.get_by_user("10001")
        self.assertIsNotNone(request)
        self.assertEqual(request.action, "Search")
        self.assertEqual(len(request.items), 2)

    def test_media_interaction_legacy_page_callback_updates_existing_request(self):
        chain = MediaInteractionChain()
        request = media_interaction_manager.create_or_replace(
            user_id="10001",
            channel=MessageChannel.Telegram,
            source="telegram-test",
            username="tester",
            action="Search",
            keyword="星际穿越",
            title="星际穿越",
            meta=self._build_meta("星际穿越"),
            items=[
                MediaInfo(title=f"资源 {index}", year="2024")
                for index in range(1, 11)
            ],
        )

        with patch.object(chain, "post_medias_message") as post_medias_message:
            handled = chain.handle_callback_interaction(
                callback_data="page_n",
                channel=MessageChannel.Telegram,
                source="telegram-test",
                userid="10001",
                username="tester",
                original_message_id=123,
                original_chat_id="456",
            )

        self.assertTrue(handled)
        self.assertEqual(request.page, 1)
        post_medias_message.assert_called_once()
        notification = post_medias_message.call_args.args[0]
        self.assertEqual(notification.original_message_id, 123)
        self.assertEqual(notification.original_chat_id, "456")
