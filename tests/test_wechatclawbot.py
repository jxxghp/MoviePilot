import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.modules.wechatclawbot import WechatClawBotModule
from app.modules.wechatclawbot.wechatclawbot import ILinkClient, WechatClawBot


class WechatClawBotTest(unittest.TestCase):
    def test_ilink_parse_incoming_uses_seq_as_message_id_fallback(self):
        client = ILinkClient(base_url="https://ilinkai.weixin.qq.com")

        message = client._parse_incoming(
            {
                "seq": 123456,
                "from_user_id": "wxid_user_1",
                "item_list": [{"type": 1, "text_item": {"text": "你好"}}],
            }
        )

        self.assertIsNotNone(message)
        self.assertEqual(message.message_id, "123456")
        self.assertEqual(message.text, "你好")

    def test_wechatclawbot_message_parser_deduplicates_message_id(self):
        module = WechatClawBotModule()
        body = json.dumps(
            {
                "__channel__": "wechatclawbot",
                "userid": "wxid_user_1",
                "username": "tester",
                "message_id": "msg-1001",
                "text": "刷新订阅",
            }
        )

        with patch.object(
            module,
            "get_config",
            return_value=SimpleNamespace(name="wechatclawbot-test", config={}),
        ):
            first = module.message_parser(
                source="wechatclawbot-test",
                body=body,
                form={},
                args={},
            )
            second = module.message_parser(
                source="wechatclawbot-test",
                body=body,
                form={},
                args={},
            )

        self.assertIsNotNone(first)
        self.assertEqual(first.message_id, "msg-1001")
        self.assertIsNone(second)

    def test_ilink_extract_updates_keeps_empty_sync_buf(self):
        client = ILinkClient(
            base_url="https://ilinkai.weixin.qq.com",
            bot_token="token",
            sync_buf="cursor-old",
        )

        items, sync_buf = client._extract_updates(
            {
                "ret": 0,
                "get_updates_buf": "",
                "msgs": [
                    {
                        "message_id": "msg-1001",
                        "from_user_id": "wxid_user_1",
                        "item_list": [{"type": 1, "text_item": {"text": "你好"}}],
                    }
                ],
            }
        )

        self.assertEqual(sync_buf, "")
        self.assertEqual(len(items), 1)

    def test_ilink_poll_updates_resets_sync_buf_when_server_returns_empty_cursor(self):
        client = ILinkClient(
            base_url="https://ilinkai.weixin.qq.com",
            bot_token="token",
            sync_buf="cursor-old",
        )
        response = MagicMock()
        response.json.return_value = {
            "ret": 0,
            "get_updates_buf": "",
            "msgs": [
                {
                    "message_id": "msg-1001",
                    "from_user_id": "wxid_user_1",
                    "item_list": [{"type": 1, "text_item": {"text": "你好"}}],
                }
            ],
        }

        with patch("app.modules.wechatclawbot.wechatclawbot.RequestUtils.post", return_value=response):
            messages, sync_buf, result = client.poll_updates()

        self.assertTrue(result["success"])
        self.assertEqual(sync_buf, "")
        self.assertEqual(client.sync_buf, "")
        self.assertEqual(len(messages), 1)

    def test_ilink_poll_updates_accepts_canonical_payload_without_explicit_success_flag(self):
        client = ILinkClient(
            base_url="https://ilinkai.weixin.qq.com",
            bot_token="token",
            sync_buf="cursor-old",
        )
        response = MagicMock()
        response.json.return_value = {
            "sync_buf": "cursor-new",
            "msgs": [
                {
                    "message_id": "msg-1002",
                    "from_user_id": "wxid_user_2",
                    "item_list": [{"type": 1, "text_item": {"text": "收到"}}],
                }
            ],
        }

        with patch("app.modules.wechatclawbot.wechatclawbot.RequestUtils.post", return_value=response):
            messages, sync_buf, result = client.poll_updates()

        self.assertTrue(result["success"])
        self.assertEqual(sync_buf, "cursor-new")
        self.assertEqual(client.sync_buf, "cursor-new")
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].text, "收到")

    def test_ilink_poll_updates_rejects_noncanonical_nested_success_payload(self):
        client = ILinkClient(
            base_url="https://ilinkai.weixin.qq.com",
            bot_token="token",
            sync_buf="cursor-old",
        )
        response = MagicMock()
        response.json.return_value = {
            "ret": 0,
            "data": {
                "get_updates_buf": "cursor-new",
                "messages": [
                    {
                        "message_id": "msg-1001",
                        "from_user_id": "wxid_user_1",
                        "item_list": [{"type": 1, "text_item": {"text": "你好"}}],
                    }
                ],
            },
        }

        with patch("app.modules.wechatclawbot.wechatclawbot.RequestUtils.post", return_value=response):
            messages, sync_buf, result = client.poll_updates()

        self.assertFalse(result["success"])
        self.assertEqual(result["message"], "轮询响应结构非官方，缺少顶层 msgs 字段")
        self.assertEqual(sync_buf, "cursor-old")
        self.assertEqual(client.sync_buf, "cursor-old")
        self.assertEqual(messages, [])

    def test_ilink_poll_updates_rejects_failed_payload_even_if_it_contains_messages(self):
        client = ILinkClient(
            base_url="https://ilinkai.weixin.qq.com",
            bot_token="token",
            sync_buf="cursor-old",
        )
        failed_response = MagicMock()
        failed_response.json.return_value = {
            "ret": -2,
            "errmsg": "cursor invalid",
            "data": {
                "sync_buf": "cursor-old",
                "messages": [
                    {
                        "message_id": "msg-dup-1",
                        "from_user_id": "wxid_user_1",
                        "item_list": [{"type": 1, "text_item": {"text": "旧消息"}}],
                    }
                ],
            },
        }

        with patch(
            "app.modules.wechatclawbot.wechatclawbot.RequestUtils.post",
            return_value=failed_response,
        ) as mock_post:
            messages, sync_buf, result = client.poll_updates()

        self.assertFalse(result["success"])
        self.assertEqual(result["message"], "cursor invalid")
        self.assertEqual(sync_buf, "cursor-old")
        self.assertEqual(client.sync_buf, "cursor-old")
        self.assertEqual(messages, [])
        mock_post.assert_called_once()
        request_body = mock_post.call_args.kwargs["json"]
        self.assertIn("get_updates_buf", request_body)
        self.assertNotIn("sync_buf", request_body)
        self.assertNotIn("syncBuf", request_body)

    def test_ilink_test_connection_accepts_getconfig_ilink_user_id_limitation(self):
        client = ILinkClient(
            base_url="https://ilinkai.weixin.qq.com",
            bot_token="token",
        )
        response = MagicMock()
        response.json.return_value = {
            "ret": -1,
            "errmsg": "ilink_user_id required",
        }

        with patch("app.modules.wechatclawbot.wechatclawbot.RequestUtils.post", return_value=response):
            ok, message = client.test_connection()

        # `ilink_user_id required` 仅表示自检接口缺少额外参数，不代表连接失败：视为连接正常
        self.assertTrue(ok)
        self.assertEqual(message, "连接正常")

    def test_wechatclawbot_send_msg_uses_plain_text_payload(self):
        state = {
            "bot_token": None,
            "account_id": None,
            "sync_buf": None,
            "qrcode": {},
            "known_targets": {},
            "user_context_tokens": {},
            "base_url": "https://ilinkai.weixin.qq.com",
        }
        with patch.object(WechatClawBot, "_load_state", return_value=state):
            bot = WechatClawBot(name="wechatclawbot-test", auto_start_polling=False)

        mock_client = MagicMock()
        mock_client.send_text.return_value = True

        with patch.object(bot, "_build_client", return_value=mock_client):
            result = bot.send_msg(
                title="测试标题",
                text="测试正文",
                userid="wxid_user_1",
                link="https://example.com/detail",
            )

        self.assertTrue(result)
        mock_client.send_text.assert_called_once_with(
            to_user="wxid_user_1",
            text="测试标题\n\n测试正文\n\n查看详情：https://example.com/detail",
            context_token=None,
        )

    def test_wechatclawbot_send_msg_prefers_text_when_text_and_image_coexist(self):
        state = {
            "bot_token": None,
            "account_id": None,
            "sync_buf": None,
            "qrcode": {},
            "known_targets": {},
            "user_context_tokens": {},
            "base_url": "https://ilinkai.weixin.qq.com",
        }
        with patch.object(WechatClawBot, "_load_state", return_value=state):
            bot = WechatClawBot(name="wechatclawbot-test", auto_start_polling=False)

        mock_client = MagicMock()
        mock_client.send_text.return_value = True
        mock_client.send_image_png.return_value = True
        mock_client.send_image_text_png.return_value = True

        with (
            patch.object(bot, "_build_client", return_value=mock_client),
            patch.object(bot, "_load_remote_image", return_value=b"image-bytes") as mock_load_image,
        ):
            result = bot.send_msg(
                title="测试标题",
                text="测试正文",
                image="https://example.com/test.png",
                userid="wxid_user_1",
            )

        self.assertTrue(result)
        mock_load_image.assert_not_called()
        mock_client.send_text.assert_called_once_with(
            to_user="wxid_user_1",
            text="测试标题\n\n测试正文",
            context_token=None,
        )
        mock_client.send_image_png.assert_not_called()
        mock_client.send_image_text_png.assert_not_called()

    def test_wechatclawbot_send_file_prefers_image_when_image_file_has_caption(self):
        state = {
            "bot_token": None,
            "account_id": None,
            "sync_buf": None,
            "qrcode": {},
            "known_targets": {},
            "user_context_tokens": {},
            "base_url": "https://ilinkai.weixin.qq.com",
        }
        with patch.object(WechatClawBot, "_load_state", return_value=state):
            bot = WechatClawBot(name="wechatclawbot-test", auto_start_polling=False)

        mock_client = MagicMock()
        mock_client.send_text.return_value = True
        mock_client.send_image_png.return_value = True

        with tempfile.NamedTemporaryFile(suffix=".png") as image_file:
            image_file.write(b"\x89PNG\r\n\x1a\nfake-png")
            image_file.flush()
            with (
                patch.object(bot, "_build_client", return_value=mock_client),
                patch.object(bot, "_guess_mime_type", return_value="image/png"),
            ):
                result = bot.send_file(
                    file_path=image_file.name,
                    title="图片标题",
                    text="图片说明",
                    userid="wxid_user_1",
                )

        self.assertTrue(result)
        mock_client.send_text.assert_not_called()
        mock_client.send_image_png.assert_called_once_with(
            to_user="wxid_user_1",
            image_bytes=b"\x89PNG\r\n\x1a\nfake-png",
            context_token=None,
        )

    def test_wechatclawbot_send_file_prefers_file_when_generic_file_has_caption(self):
        state = {
            "bot_token": None,
            "account_id": None,
            "sync_buf": None,
            "qrcode": {},
            "known_targets": {},
            "user_context_tokens": {},
            "base_url": "https://ilinkai.weixin.qq.com",
        }
        with patch.object(WechatClawBot, "_load_state", return_value=state):
            bot = WechatClawBot(name="wechatclawbot-test", auto_start_polling=False)

        mock_client = MagicMock()
        mock_client.send_text.return_value = True
        mock_client.send_file_bytes.return_value = True

        with tempfile.NamedTemporaryFile(suffix=".txt") as text_file:
            text_file.write(b"plain-text")
            text_file.flush()
            with (
                patch.object(bot, "_build_client", return_value=mock_client),
                patch.object(bot, "_guess_mime_type", return_value="text/plain"),
            ):
                result = bot.send_file(
                    file_path=text_file.name,
                    file_name="report.txt",
                    title="文件标题",
                    text="文件说明",
                    userid="wxid_user_1",
                )

        self.assertTrue(result)
        mock_client.send_text.assert_not_called()
        mock_client.send_file_bytes.assert_called_once_with(
            to_user="wxid_user_1",
            file_bytes=b"plain-text",
            file_name="report.txt",
            mime_type="text/plain",
            context_token=None,
        )
