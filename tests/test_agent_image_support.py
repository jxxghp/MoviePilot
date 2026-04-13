import base64
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import quote

from telebot import apihelper

from app.agent.tools.impl.send_message import SendMessageInput
from app.agent import MoviePilotAgent, AgentChain
from app.chain.message import MessageChain
from app.core.config import settings
from app.helper.voice import VoiceHelper
from app.modules.discord import DiscordModule
from app.modules.qqbot import QQBotModule
from app.modules.slack import SlackModule
from app.modules.telegram.telegram import Telegram
from app.modules.telegram import TelegramModule
from app.modules.synologychat import SynologyChatModule
from app.modules.vocechat import VoceChatModule
from app.modules.wechat import WechatModule
from app.modules.wechat.wechatbot import WeChatBot
from app.schemas import CommingMessage, Notification
from app.schemas.types import MessageChannel


class AgentImageSupportTest(unittest.TestCase):
    def test_telegram_extract_audio_refs_returns_prefixed_file_ids(self):
        audio_refs = TelegramModule._extract_audio_refs(
            {
                "voice": {"file_id": "voice-1"},
                "audio": {"file_id": "audio-1"},
            }
        )

        self.assertEqual(
            audio_refs,
            ["tg://voice_file_id/voice-1", "tg://audio_file_id/audio-1"],
        )

    def test_telegram_extract_images_returns_prefixed_file_ids(self):
        images = TelegramModule._extract_images(
            {
                "photo": [{"file_id": "small"}, {"file_id": "large"}],
                "document": {"file_id": "doc-image", "mime_type": "image/png"},
            }
        )

        self.assertEqual(
            images,
            ["tg://file_id/large", "tg://file_id/doc-image"],
        )

    def test_telegram_message_parser_accepts_double_encoded_body(self):
        module = TelegramModule()
        body = json.dumps(
            json.dumps(
                {
                    "message": {
                        "from": {"id": 10001, "username": "tester"},
                        "chat": {"id": 10001, "type": "private"},
                        "photo": [{"file_id": "small"}, {"file_id": "large"}],
                    }
                }
            )
        )

        with patch.object(
            module,
            "get_config",
            return_value=SimpleNamespace(name="telegram-test", config={}),
        ), patch.object(
            module,
            "get_instance",
            return_value=SimpleNamespace(bot_username=None),
        ):
            message = module.message_parser(
                source="telegram-test", body=body, form={}, args={}
            )

        self.assertIsNotNone(message)
        self.assertEqual(message.images, ["tg://file_id/large"])

    def test_telegram_forward_payload_uses_dict_not_json_string(self):
        payload = Telegram._serialize_update_payload(
            SimpleNamespace(
                to_dict=lambda: {
                    "text": "hi",
                    "photo": [{"file_id": "image-1"}],
                }
            )
        )

        self.assertEqual(
            payload,
            {"text": "hi", "photo": [{"file_id": "image-1"}]},
        )

    def test_telegram_download_file_uses_configured_file_url(self):
        telegram = Telegram.__new__(Telegram)
        telegram._bot = Mock()
        telegram._telegram_token = "token-123"
        telegram._bot.get_file.return_value = SimpleNamespace(file_path="photos/a.jpg")

        old_file_url = apihelper.FILE_URL
        old_proxy = apihelper.proxy
        apihelper.FILE_URL = "https://tg-proxy.example/file/bot{0}/{1}"
        apihelper.proxy = {"https": "http://127.0.0.1:7890"}

        try:
            with patch(
                "app.modules.telegram.telegram.RequestUtils.get_res",
                return_value=SimpleNamespace(content=b"image-bytes"),
            ) as get_res:
                content = telegram.download_file("file-id-1")
        finally:
            apihelper.FILE_URL = old_file_url
            apihelper.proxy = old_proxy

        self.assertEqual(content, b"image-bytes")
        get_res.assert_called_once_with(
            "https://tg-proxy.example/file/bottoken-123/photos/a.jpg"
        )

    def test_process_allows_image_only_message(self):
        chain = MessageChain()
        message = CommingMessage(
            channel=MessageChannel.Telegram,
            source="telegram-test",
            userid="10001",
            username="tester",
            images=["tg://file_id/image-1"],
        )

        with patch.object(chain, "message_parser", return_value=message), patch.object(
            chain, "handle_message"
        ) as handle_message:
            chain.process(body="{}", form={}, args={"source": "telegram-test"})

        handle_kwargs = handle_message.call_args.kwargs
        self.assertEqual(handle_kwargs["text"], "")
        self.assertEqual(handle_kwargs["images"], ["tg://file_id/image-1"])

    def test_process_allows_audio_only_message(self):
        chain = MessageChain()
        message = CommingMessage(
            channel=MessageChannel.Telegram,
            source="telegram-test",
            userid="10001",
            username="tester",
            audio_refs=["tg://voice_file_id/voice-1"],
        )

        with patch.object(chain, "message_parser", return_value=message), patch.object(
            chain, "handle_message"
        ) as handle_message:
            chain.process(body="{}", form={}, args={"source": "telegram-test"})

        handle_kwargs = handle_message.call_args.kwargs
        self.assertEqual(handle_kwargs["text"], "")
        self.assertEqual(handle_kwargs["audio_refs"], ["tg://voice_file_id/voice-1"])

    def test_image_message_routes_to_agent_even_when_global_agent_is_disabled(self):
        chain = MessageChain()

        with patch.object(chain, "load_cache", return_value={}), patch.object(
            chain.messagehelper, "put"
        ), patch.object(chain.messageoper, "add"), patch.object(
            chain, "_handle_ai_message"
        ) as handle_ai_message, patch.object(
            settings, "AI_AGENT_ENABLE", True
        ), patch.object(
            settings, "AI_AGENT_GLOBAL", False
        ):
            chain.handle_message(
                channel=MessageChannel.Telegram,
                source="telegram-test",
                userid="10001",
                username="tester",
                text="",
                images=["tg://file_id/image-1"],
            )

        handle_ai_message.assert_called_once()

    def test_audio_message_routes_to_agent_with_voice_reply_flag(self):
        chain = MessageChain()

        with patch.object(chain, "load_cache", return_value={}), patch.object(
            chain, "_transcribe_audio_refs", return_value="帮我推荐一部电影"
        ), patch.object(chain.messagehelper, "put"), patch.object(
            chain.messageoper, "add"
        ), patch.object(chain, "_handle_ai_message") as handle_ai_message:
            chain.handle_message(
                channel=MessageChannel.Telegram,
                source="telegram-test",
                userid="10001",
                username="tester",
                text="",
                audio_refs=["tg://voice_file_id/voice-1"],
            )

        handle_ai_message.assert_called_once()
        self.assertEqual(handle_ai_message.call_args.kwargs["text"], "帮我推荐一部电影")
        self.assertTrue(handle_ai_message.call_args.kwargs["reply_with_voice"])

    def test_transcribe_audio_refs_supports_new_channel_refs(self):
        chain = MessageChain()
        audio_refs = [
            "slack://file/" + quote("https://files.slack.com/test.mp3", safe=""),
            "discord://file/" + quote("https://cdn.discordapp.com/voice.ogg", safe=""),
            "qq://file/" + quote("https://example.com/qq-voice.ogg", safe=""),
            "vocechat://file/%2Fuploads%2Fvoice.ogg",
            "synology://file/" + quote("https://example.com/synology-voice.wav", safe=""),
        ]

        with patch.object(VoiceHelper, "is_available", return_value=True), patch.object(
            chain,
            "run_module",
            side_effect=[b"slack", b"discord", b"qq", b"vocechat", b"synology"],
        ) as run_module, patch.object(
            VoiceHelper,
            "transcribe_bytes",
            side_effect=["slack text", "discord text", "qq text", "vocechat text", "synology text"],
        ) as transcribe_bytes:
            result = chain._transcribe_audio_refs(
                audio_refs=audio_refs,
                channel=MessageChannel.Slack,
                source="mixed-source",
            )

        self.assertEqual(
            result,
            "slack text\ndiscord text\nqq text\nvocechat text\nsynology text",
        )
        self.assertEqual(
            [call.args[0] for call in run_module.call_args_list],
            [
                "download_slack_file_bytes",
                "download_discord_file_bytes",
                "download_qq_file_bytes",
                "download_vocechat_file_bytes",
                "download_synologychat_file_bytes",
            ],
        )
        self.assertEqual(
            [call.kwargs["filename"] for call in transcribe_bytes.call_args_list],
            [
                "test.mp3",
                "voice.ogg",
                "qq-voice.ogg",
                "voice.ogg",
                "synology-voice.wav",
            ],
        )

    def test_agent_send_agent_message_does_not_auto_convert_to_voice(self):
        agent = MoviePilotAgent(
            session_id="session-1",
            user_id="user-1",
            channel=MessageChannel.Telegram.value,
            source="telegram-test",
            username="tester",
        )
        agent.reply_with_voice = True

        with patch.object(
            AgentChain, "async_post_message", new_callable=AsyncMock
        ) as async_post_message:
            import asyncio

            asyncio.run(agent.send_agent_message("这是语音回复"))

        notification = async_post_message.await_args.args[0]
        self.assertIsNone(notification.voice_path)
        self.assertEqual(notification.text, "这是语音回复")

    def test_slack_images_use_authenticated_data_url_download(self):
        chain = MessageChain()

        with patch.object(
            chain,
            "run_module",
            return_value="data:image/png;base64,abc123",
        ) as run_module:
            images = chain._download_images_to_base64(
                images=["https://files.slack.com/files-pri/T1-F1/test.png"],
                channel=MessageChannel.Slack,
                source="slack-test",
            )

        self.assertEqual(images, ["data:image/png;base64,abc123"])
        run_module.assert_called_once_with(
            "download_slack_file_to_data_url",
            file_url="https://files.slack.com/files-pri/T1-F1/test.png",
            source="slack-test",
        )

    def test_slack_module_download_file_to_data_url(self):
        module = SlackModule()
        client = Mock()
        client.download_file.return_value = (b"png-binary", "image/png")

        with patch.object(
            module, "get_config", return_value=SimpleNamespace(name="slack-test")
        ), patch.object(module, "get_instance", return_value=client):
            data_url = module.download_slack_file_to_data_url(
                "https://files.slack.com/files-pri/T1-F1/test.png",
                "slack-test",
            )

        self.assertEqual(
            data_url,
            f"data:image/png;base64,{base64.b64encode(b'png-binary').decode()}",
        )

    def test_slack_extract_audio_refs_returns_private_file_refs(self):
        audio_refs = SlackModule._extract_audio_refs(
            {
                "files": [
                    {
                        "type": "audio",
                        "filetype": "mp3",
                        "mimetype": "audio/mpeg",
                        "url_private": "https://files.slack.com/files-pri/T1-F1/test.mp3",
                    }
                ]
            }
        )

        self.assertEqual(
            audio_refs,
            [
                "slack://file/"
                + quote("https://files.slack.com/files-pri/T1-F1/test.mp3", safe="")
            ],
        )

    def test_send_message_input_accepts_image_only_payload(self):
        payload = SendMessageInput(
            explanation="send poster image",
            image_url="https://example.com/poster.png",
        )

        self.assertEqual(payload.image_url, "https://example.com/poster.png")

    def test_discord_extract_images_supports_attachment_content_type(self):
        images = DiscordModule._extract_images(
            {
                "attachments": [
                    {
                        "content_type": "image/png",
                        "url": "https://cdn.discordapp.com/test.png",
                    }
                ]
            }
        )

        self.assertEqual(images, ["https://cdn.discordapp.com/test.png"])

    def test_discord_extract_audio_refs_supports_attachment_content_type(self):
        audio_refs = DiscordModule._extract_audio_refs(
            {
                "attachments": [
                    {
                        "content_type": "audio/ogg",
                        "filename": "voice.ogg",
                        "url": "https://cdn.discordapp.com/voice.ogg",
                    }
                ]
            }
        )

        self.assertEqual(
            audio_refs,
            [
                "discord://file/"
                + quote("https://cdn.discordapp.com/voice.ogg", safe="")
            ],
        )

    def test_discord_send_direct_message_returns_chat_id(self):
        module = DiscordModule()
        client = Mock()
        client.send_msg.return_value = (
            True,
            {"message_id": "discord-msg-1", "chat_id": "discord-chat-1"},
        )

        with patch.object(
            module,
            "get_configs",
            return_value={"discord-test": SimpleNamespace(name="discord-test")},
        ), patch.object(
            module, "check_message", return_value=True
        ), patch.object(
            module, "get_instance", return_value=client
        ):
            response = module.send_direct_message(
                Notification(title="hi", userid="user-1")
            )

        self.assertIsNotNone(response)
        self.assertEqual(response.message_id, "discord-msg-1")
        self.assertEqual(response.chat_id, "discord-chat-1")

    def test_download_images_routes_wechat_refs_to_module_downloader(self):
        chain = MessageChain()

        with patch.object(
            chain,
            "run_module",
            return_value="data:image/png;base64,wechat123",
        ) as run_module:
            images = chain._download_images_to_base64(
                images=["wxwork://media_id/media-1"],
                channel=MessageChannel.Wechat,
                source="wechat-test",
            )

        self.assertEqual(images, ["data:image/png;base64,wechat123"])
        run_module.assert_called_once_with(
            "download_wechat_image_to_data_url",
            image_ref="wxwork://media_id/media-1",
            source="wechat-test",
        )

    def test_wechat_message_parser_extracts_image_media_id(self):
        module = WechatModule()
        xml_message = b"""
        <xml>
          <FromUserName><![CDATA[user-1]]></FromUserName>
          <MsgType><![CDATA[image]]></MsgType>
          <PicUrl><![CDATA[https://example.com/image.png]]></PicUrl>
          <MediaId><![CDATA[media-1]]></MediaId>
        </xml>
        """
        crypt = Mock()
        crypt.DecryptMsg.return_value = (0, xml_message)

        with patch.object(
            module,
            "get_config",
            return_value=SimpleNamespace(
                name="wechat-test",
                config={
                    "WECHAT_TOKEN": "token",
                    "WECHAT_ENCODING_AESKEY": "encoding",
                    "WECHAT_CORPID": "corpid",
                },
            ),
        ), patch.object(
            module, "get_instance", return_value=SimpleNamespace(send_msg=Mock())
        ), patch(
            "app.modules.wechat.WXBizMsgCrypt",
            return_value=crypt,
        ):
            message = module.message_parser(
                source="wechat-test",
                body=b"encrypted",
                form={},
                args={"msg_signature": "sig", "timestamp": "1", "nonce": "n"},
            )

        self.assertIsNotNone(message)
        self.assertEqual(message.images, ["wxwork://media_id/media-1"])

    def test_wechat_bot_parser_accepts_image_only_payload(self):
        module = WechatModule()
        body = json.dumps(
            {
                "body": {
                    "from": {"userid": "wxbot-user"},
                    "msgtype": "image",
                    "image": {
                        "download_url": "https://example.com/encrypted-image",
                        "aeskey": "YWJjZGVmZw",
                    },
                }
            }
        )

        with patch.object(
            module,
            "get_config",
            return_value=SimpleNamespace(
                name="wechat-bot-test", config={"WECHAT_MODE": "bot"}
            ),
        ), patch.object(
            module, "get_instance", return_value=SimpleNamespace(send_msg=Mock())
        ):
            message = module.message_parser(
                source="wechat-bot-test",
                body=body,
                form={},
                args={},
            )

        self.assertIsNotNone(message)
        self.assertTrue(message.images[0].startswith("wxbot://image/"))

    def test_wechat_bot_handles_image_only_callback(self):
        bot = WeChatBot.__new__(WeChatBot)
        bot._config_name = "wechat-bot-test"
        bot._admins = []
        bot.send_msg = Mock()
        bot._remember_target = Mock()
        bot._forward_to_message_chain = Mock()

        payload = {
            "body": {
                "from": {"userid": "wxbot-user"},
                "msgtype": "image",
                "image": {
                    "download_url": "https://example.com/encrypted-image",
                    "aeskey": "YWJjZGVmZw",
                },
            }
        }

        bot._handle_callback_message(payload)

        bot._remember_target.assert_called_once_with("wxbot-user")
        bot._forward_to_message_chain.assert_called_once_with(payload)

    def test_vocechat_message_parser_extracts_image_file_payload(self):
        module = VoceChatModule()
        body = json.dumps(
            {
                "detail": {
                    "type": "normal",
                    "content_type": "vocechat/file",
                    "content": "/uploads/poster.png",
                    "properties": {"content_type": "image/png"},
                },
                "from_uid": 7910,
                "target": {"gid": 2},
            }
        )

        with patch.object(
            module,
            "get_config",
            return_value=SimpleNamespace(
                name="vocechat-test", config={"channel_id": "2"}
            ),
        ):
            message = module.message_parser(
                source="vocechat-test",
                body=body,
                form={},
                args={},
            )

        self.assertIsNotNone(message)
        self.assertEqual(
            message.images,
            ["vocechat://file/%2Fuploads%2Fposter.png"],
        )

    def test_vocechat_message_parser_extracts_audio_file_payload(self):
        module = VoceChatModule()
        body = json.dumps(
            {
                "detail": {
                    "type": "normal",
                    "content_type": "vocechat/file",
                    "content": "/uploads/voice.ogg",
                    "properties": {"content_type": "audio/ogg"},
                },
                "from_uid": 7910,
                "target": {"gid": 2},
            }
        )

        with patch.object(
            module,
            "get_config",
            return_value=SimpleNamespace(
                name="vocechat-test", config={"channel_id": "2"}
            ),
        ):
            message = module.message_parser(
                source="vocechat-test",
                body=body,
                form={},
                args={},
            )

        self.assertIsNotNone(message)
        self.assertEqual(
            message.audio_refs,
            ["vocechat://file/%2Fuploads%2Fvoice.ogg"],
        )

    def test_vocechat_post_message_passes_image_and_correct_target(self):
        module = VoceChatModule()
        client = Mock()

        with patch.object(
            module,
            "get_configs",
            return_value={"vocechat-test": SimpleNamespace(name="vocechat-test")},
        ), patch.object(
            module, "check_message", return_value=True
        ), patch.object(
            module, "get_instance", return_value=client
        ):
            module.post_message(
                Notification(
                    title="poster",
                    image="https://example.com/poster.png",
                    targets={"vocechat_userid": "UID#100"},
                )
            )

        client.send_msg.assert_called_once_with(
            title="poster",
            text=None,
            image="https://example.com/poster.png",
            userid="UID#100",
            link=None,
        )

    def test_qq_message_parser_accepts_image_only_attachment(self):
        module = QQBotModule()

        with patch.object(
            module,
            "get_config",
            return_value=SimpleNamespace(name="qq-test", config={}),
        ):
            message = module.message_parser(
                source="qq-test",
                body={
                    "type": "C2C_MESSAGE_CREATE",
                    "author": {"user_openid": "qq-user"},
                    "attachments": [
                        {
                            "content_type": "image/png",
                            "url": "https://example.com/qq-image.png",
                        }
                    ],
                },
                form={},
                args={},
            )

        self.assertIsNotNone(message)
        self.assertEqual(message.images, ["https://example.com/qq-image.png"])

    def test_qq_message_parser_accepts_audio_only_attachment(self):
        module = QQBotModule()

        with patch.object(
            module,
            "get_config",
            return_value=SimpleNamespace(name="qq-test", config={}),
        ):
            message = module.message_parser(
                source="qq-test",
                body={
                    "type": "C2C_MESSAGE_CREATE",
                    "author": {"user_openid": "qq-user"},
                    "attachments": [
                        {
                            "content_type": "audio/ogg",
                            "filename": "voice.ogg",
                            "url": "https://example.com/qq-voice.ogg",
                        }
                    ],
                },
                form={},
                args={},
            )

        self.assertIsNotNone(message)
        self.assertEqual(
            message.audio_refs,
            ["qq://file/" + quote("https://example.com/qq-voice.ogg", safe="")],
        )

    def test_synology_message_parser_accepts_image_only_form(self):
        module = SynologyChatModule()

        with patch.object(
            module,
            "get_config",
            return_value=SimpleNamespace(name="synology-test", config={}),
        ), patch.object(
            module,
            "get_instance",
            return_value=SimpleNamespace(check_token=lambda token: token == "token-1"),
        ):
            message = module.message_parser(
                source="synology-test",
                body={},
                form={
                    "token": "token-1",
                    "user_id": "42",
                    "username": "tester",
                    "file_url": "https://example.com/image.png",
                },
                args={},
            )

        self.assertIsNotNone(message)
        self.assertEqual(message.images, ["https://example.com/image.png"])

    def test_synology_message_parser_accepts_audio_only_form(self):
        module = SynologyChatModule()

        with patch.object(
            module,
            "get_config",
            return_value=SimpleNamespace(name="synology-test", config={}),
        ), patch.object(
            module,
            "get_instance",
            return_value=SimpleNamespace(check_token=lambda token: token == "token-1"),
        ):
            message = module.message_parser(
                source="synology-test",
                body={},
                form={
                    "token": "token-1",
                    "user_id": "42",
                    "username": "tester",
                    "attachments": json.dumps(
                        [
                            {
                                "url": "https://example.com/voice.ogg",
                                "content_type": "audio/ogg",
                            }
                        ]
                    ),
                },
                args={},
            )

        self.assertIsNotNone(message)
        self.assertEqual(
            message.audio_refs,
            ["synology://file/" + quote("https://example.com/voice.ogg", safe="")],
        )

if __name__ == "__main__":
    unittest.main()
