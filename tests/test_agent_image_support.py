import asyncio
import base64
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import quote

from telebot import apihelper

from app.agent.tools.impl.send_message import SendMessageInput, SendMessageTool
from app.agent.tools.impl.send_local_file import SendLocalFileInput
from app.agent import MoviePilotAgent, AgentChain
from app.agent.llm import AgentCapabilityManager
from app.chain.message import MessageChain
from app.core.config import settings
from app.agent.llm import LLMHelper
from app.modules.discord import DiscordModule
from app.modules.qqbot import QQBotModule
from app.modules.qqbot.qqbot import QQBot
from app.modules.slack import SlackModule
from app.modules.telegram.telegram import Telegram
from app.modules.telegram import TelegramModule
from app.modules.synologychat import SynologyChatModule
from app.modules.vocechat import VoceChatModule
from app.modules.wechat import WechatModule
from app.modules.wechat.wechatbot import WeChatBot
from app.schemas import CommingMessage, Notification
from app.schemas.types import MessageChannel, NotificationType


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
                "document": {
                    "file_id": "doc-image",
                    "mime_type": "image/png",
                    "file_name": "poster.png",
                },
            }
        )

        self.assertEqual([image.ref for image in images], ["tg://file_id/large", "tg://file_id/doc-image"])
        self.assertEqual(images[0].mime_type, "image/jpeg")
        self.assertEqual(images[1].mime_type, "image/png")
        self.assertEqual(images[1].name, "poster.png")

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
        self.assertEqual([image.ref for image in message.images], ["tg://file_id/large"])

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
        self.assertEqual([image.ref for image in handle_kwargs["images"]], ["tg://file_id/image-1"])

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

    def test_process_allows_file_only_message(self):
        chain = MessageChain()
        message = CommingMessage(
            channel=MessageChannel.Telegram,
            source="telegram-test",
            userid="10001",
            username="tester",
            files=[
                CommingMessage.MessageAttachment(
                    ref="tg://document_file_id/doc-1",
                    name="note.txt",
                    mime_type="text/plain",
                    size=12,
                )
            ],
        )

        with patch.object(chain, "message_parser", return_value=message), patch.object(
            chain, "handle_message"
        ) as handle_message:
            chain.process(body="{}", form={}, args={"source": "telegram-test"})

        handle_kwargs = handle_message.call_args.kwargs
        self.assertEqual(handle_kwargs["text"], "")
        self.assertEqual(handle_kwargs["files"][0].ref, "tg://document_file_id/doc-1")

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

    def test_audio_message_routes_to_agent_without_forcing_voice_reply(self):
        chain = MessageChain()

        with patch.object(chain, "load_cache", return_value={}), patch.object(
            chain, "_transcribe_audio_refs", return_value="帮我推荐一部电影"
        ), patch.object(chain.messagehelper, "put"), patch.object(
            chain.messageoper, "add"
        ), patch.object(chain, "_handle_ai_message") as handle_ai_message:
            with patch.object(settings, "AI_AGENT_ENABLE", True), patch.object(
                settings, "AI_AGENT_GLOBAL", False
            ):
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
        self.assertTrue(handle_ai_message.call_args.kwargs["has_audio_input"])
        self.assertNotIn("reply_with_voice", handle_ai_message.call_args.kwargs)

    def test_file_message_routes_to_agent_even_when_global_agent_is_disabled(self):
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
                files=[
                    CommingMessage.MessageAttachment(
                        ref="tg://document_file_id/doc-1",
                        name="report.txt",
                        mime_type="text/plain",
                    )
                ],
            )

        handle_ai_message.assert_called_once()
        self.assertEqual(handle_ai_message.call_args.kwargs["files"][0].name, "report.txt")

    def test_transcribe_audio_refs_supports_new_channel_refs(self):
        chain = MessageChain()
        audio_refs = [
            "slack://file/" + quote("https://files.slack.com/test.mp3", safe=""),
            "discord://file/" + quote("https://cdn.discordapp.com/voice.ogg", safe=""),
            "qq://file/" + quote("https://example.com/qq-voice.ogg", safe=""),
            "vocechat://file/%2Fuploads%2Fvoice.ogg",
            "synology://file/" + quote("https://example.com/synology-voice.wav", safe=""),
            "feishu://file/om_audio/file_audio/voice.opus",
        ]

        with patch.object(
            AgentCapabilityManager, "is_audio_input_available", return_value=True
        ), patch.object(
            chain,
            "run_module",
            side_effect=[b"slack", b"discord", b"qq", b"vocechat", b"synology", b"feishu"],
        ) as run_module, patch.object(
            AgentCapabilityManager,
            "transcribe_audio",
            side_effect=[
                "slack text",
                "discord text",
                "qq text",
                "vocechat text",
                "synology text",
                "feishu text",
            ],
        ) as transcribe_bytes:
            result = chain._transcribe_audio_refs(
                audio_refs=audio_refs,
                channel=MessageChannel.Slack,
                source="mixed-source",
            )

        self.assertEqual(
            result,
            "slack text\ndiscord text\nqq text\nvocechat text\nsynology text\nfeishu text",
        )
        self.assertEqual(
            [call.args[0] for call in run_module.call_args_list],
            [
                "download_slack_file_bytes",
                "download_discord_file_bytes",
                "download_qq_file_bytes",
                "download_vocechat_file_bytes",
                "download_synologychat_file_bytes",
                "download_feishu_file_bytes",
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
                "voice.opus",
            ],
        )

    def test_agent_send_agent_message_keeps_default_text_reply(self):
        agent = MoviePilotAgent(
            session_id="session-1",
            user_id="user-1",
            channel=MessageChannel.Telegram.value,
            source="telegram-test",
            username="tester",
        )

        with patch.object(
            AgentChain, "async_post_message", new_callable=AsyncMock
        ) as async_post_message:
            import asyncio

            asyncio.run(agent.send_agent_message("这是语音回复"))

        notification = async_post_message.await_args.args[0]
        self.assertIsNone(notification.voice_path)
        self.assertIsNone(notification.voice_caption)
        self.assertEqual(notification.text, "这是语音回复")

    def test_agent_process_wraps_request_as_structured_json(self):
        agent = MoviePilotAgent(
            session_id="session-1",
            user_id="user-1",
            channel=MessageChannel.Telegram.value,
            source="telegram-test",
            username="tester",
        )

        with patch(
            "app.agent.memory.memory_manager.get_agent_messages", return_value=[]
        ), patch.object(agent, "_execute_agent", new_callable=AsyncMock) as execute_agent:
            import asyncio

            asyncio.run(
                agent.process(
                    "帮我总结这个文件",
                    files=[
                        {
                            "name": "report.txt",
                            "local_path": "/tmp/report.txt",
                            "status": "ready",
                        }
                    ],
                )
            )

        messages = execute_agent.await_args.args[0]
        human_message = messages[-1]
        content = human_message.content
        self.assertIsInstance(content, list)
        payload = json.loads(content[0]["text"])
        self.assertEqual(payload["message"], "帮我总结这个文件")
        self.assertEqual(payload["input"]["mode"], "text")
        self.assertFalse(payload["input"]["transcribed"])
        self.assertEqual(payload["files"][0]["local_path"], "/tmp/report.txt")

    def test_agent_process_marks_voice_input_in_structured_json(self):
        """语音输入应在结构化消息中标记为转写来源。"""
        agent = MoviePilotAgent(
            session_id="session-1",
            user_id="user-1",
            channel=MessageChannel.Telegram.value,
            source="telegram-test",
            username="tester",
        )

        with patch(
            "app.agent.memory.memory_manager.get_agent_messages", return_value=[]
        ), patch.object(agent, "_execute_agent", new_callable=AsyncMock) as execute_agent:
            asyncio.run(
                agent.process(
                    "帮我推荐一部电影",
                    has_audio_input=True,
                )
            )

        messages = execute_agent.await_args.args[0]
        payload = json.loads(messages[-1].content[0]["text"])
        self.assertEqual(payload["message"], "帮我推荐一部电影")
        self.assertEqual(payload["input"]["mode"], "voice")
        self.assertTrue(payload["input"]["transcribed"])

    def test_llm_supports_image_input_respects_explicit_override(self):
        with patch.object(settings, "LLM_SUPPORT_IMAGE_INPUT", False):
            self.assertFalse(LLMHelper.supports_image_input())

    def test_llm_supports_image_input_uses_boolean_setting(self):
        with patch.object(settings, "LLM_SUPPORT_IMAGE_INPUT", True):
            self.assertTrue(LLMHelper.supports_image_input())

        with patch.object(settings, "LLM_SUPPORT_IMAGE_INPUT", False):
            self.assertFalse(LLMHelper.supports_image_input())

    def test_handle_ai_message_routes_images_to_files_when_image_input_disabled(self):
        chain = MessageChain()

        with patch.object(settings, "AI_AGENT_ENABLE", True), patch.object(
            settings, "LLM_SUPPORT_IMAGE_INPUT", False
        ), patch.object(chain, "_get_or_create_session_id", return_value="session-1"), patch.object(
            chain, "_download_attachments_to_data_urls"
        ) as download_images, patch.object(
            chain,
            "_prepare_agent_files",
            return_value=[
                {
                    "name": "image_1.jpg",
                    "mime_type": "image/jpeg",
                    "local_path": "/tmp/image_1.jpg",
                    "status": "ready",
                }
            ],
        ) as prepare_files, patch(
            "app.chain.message.agent_manager.process_message", new_callable=AsyncMock
        ) as process_message, patch(
            "app.chain.message.asyncio.run_coroutine_threadsafe"
        ) as run_coroutine_threadsafe:
            chain._handle_ai_message(
                text="/ai 帮我看看这张图",
                channel=MessageChannel.Telegram,
                source="telegram-test",
                userid="10001",
                username="tester",
                images=["tg://file_id/image-1"],
            )

        download_images.assert_not_called()
        prepare_files.assert_called_once()
        attachments = prepare_files.call_args.kwargs["files"]
        self.assertEqual(attachments[0].ref, "tg://file_id/image-1")
        self.assertEqual(attachments[0].mime_type, "image/jpeg")
        run_coroutine_threadsafe.assert_called_once()
        self.assertEqual(process_message.call_args.kwargs["images"], None)
        self.assertEqual(
            process_message.call_args.kwargs["files"][0]["local_path"],
            "/tmp/image_1.jpg",
        )

    def test_handle_ai_message_forwards_voice_input_to_agent_manager(self):
        """AI消息入队时应保留语音输入标记。"""
        chain = MessageChain()

        with patch.object(settings, "AI_AGENT_ENABLE", True), patch.object(
            chain, "_get_or_create_session_id", return_value="session-1"
        ), patch(
            "app.chain.message.agent_manager.process_message", new_callable=AsyncMock
        ) as process_message, patch(
            "app.chain.message.asyncio.run_coroutine_threadsafe",
            side_effect=lambda coro, _loop: (coro.close(), Mock())[1],
        ):
            chain._handle_ai_message(
                text="帮我推荐一部电影",
                channel=MessageChannel.Telegram,
                source="telegram-test",
                userid="10001",
                username="tester",
                has_audio_input=True,
            )

        self.assertTrue(process_message.call_args.kwargs["has_audio_input"])

    def test_slack_images_use_authenticated_data_url_download(self):
        chain = MessageChain()

        with patch.object(
            chain,
            "run_module",
            return_value="data:image/png;base64,abc123",
        ) as run_module:
            images = chain._download_attachments_to_data_urls(
                attachments=["https://files.slack.com/files-pri/T1-F1/test.png"],
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

    def test_send_message_tool_uses_agent_notification_type(self):
        """发送消息工具应固定使用智能体消息类型。"""

        async def _run():
            tool = SendMessageTool(session_id="session-1", user_id="10001")
            tool.set_message_attr(
                channel=MessageChannel.Telegram.value,
                source="telegram-test",
                username="tester",
            )

            with patch(
                "app.agent.tools.base.ToolChain.async_post_message",
                new_callable=AsyncMock,
            ) as async_post_message:
                result = await tool.run(
                    message="处理完成",
                    title="智能体通知",
                    image_url="https://example.com/poster.png",
                )
            return result, async_post_message

        result, async_post_message = asyncio.run(_run())
        notification = async_post_message.await_args.args[0]

        self.assertEqual(result, "消息已发送")
        self.assertEqual(notification.mtype, NotificationType.Agent)
        self.assertEqual(notification.channel, MessageChannel.Telegram)
        self.assertEqual(notification.source, "telegram-test")
        self.assertEqual(notification.title, "智能体通知")
        self.assertEqual(notification.text, "处理完成")
        self.assertEqual(notification.image, "https://example.com/poster.png")

    def test_send_local_file_input_accepts_file_payload(self):
        payload = SendLocalFileInput(
            explanation="send generated report",
            file_path="/tmp/report.txt",
            message="请下载查看",
        )

        self.assertEqual(payload.file_path, "/tmp/report.txt")

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

        self.assertEqual([image.ref for image in images], ["https://cdn.discordapp.com/test.png"])
        self.assertEqual(images[0].mime_type, "image/png")

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

    def test_discord_extract_files_supports_non_media_attachment(self):
        files = DiscordModule._extract_files(
            {
                "attachments": [
                    {
                        "content_type": "application/pdf",
                        "filename": "guide.pdf",
                        "url": "https://cdn.discordapp.com/guide.pdf",
                        "size": 1024,
                    }
                ]
            }
        )

        self.assertEqual(files[0].name, "guide.pdf")
        self.assertEqual(
            files[0].ref,
            "discord://file/" + quote("https://cdn.discordapp.com/guide.pdf", safe=""),
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
            images = chain._download_attachments_to_data_urls(
                attachments=["wxwork://media_id/media-1"],
                channel=MessageChannel.Wechat,
                source="wechat-test",
            )

        self.assertEqual(images, ["data:image/png;base64,wechat123"])
        run_module.assert_called_once_with(
            "download_wechat_image_to_data_url",
            image_ref="wxwork://media_id/media-1",
            source="wechat-test",
        )

    def test_download_images_routes_feishu_refs_to_module_downloader(self):
        chain = MessageChain()

        with patch.object(
            chain,
            "run_module",
            return_value="data:image/png;base64,feishu123",
        ) as run_module:
            data_urls = chain._download_attachments_to_data_urls(
                attachments=[
                    CommingMessage.MessageImage(
                        ref="feishu://image/img_v2_xxx",
                        mime_type="image/png",
                    )
                ],
                channel=MessageChannel.Feishu,
                source="feishu-test",
            )

        self.assertEqual(data_urls, ["data:image/png;base64,feishu123"])
        run_module.assert_called_once_with(
            "download_feishu_image_to_data_url",
            image_ref="feishu://image/img_v2_xxx",
            source="feishu-test",
        )

    def test_download_message_file_bytes_supports_feishu_refs(self):
        chain = MessageChain()

        with patch.object(chain, "run_module", return_value=b"feishu-file") as run_module:
            content = chain._download_message_file_bytes(
                file_ref="feishu://file/file_xxx/report.pdf",
                channel=MessageChannel.Feishu,
                source="feishu-test",
            )

        self.assertEqual(content, b"feishu-file")
        run_module.assert_called_once_with(
            "download_feishu_file_bytes",
            file_ref="feishu://file/file_xxx/report.pdf",
            source="feishu-test",
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
        self.assertEqual([image.ref for image in message.images], ["wxwork://media_id/media-1"])

    def test_wechat_message_parser_extracts_file_media_id(self):
        module = WechatModule()
        xml_message = b"""
        <xml>
          <FromUserName><![CDATA[user-1]]></FromUserName>
          <MsgType><![CDATA[file]]></MsgType>
          <MediaId><![CDATA[file-media-1]]></MediaId>
          <FileName><![CDATA[manual.pdf]]></FileName>
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
        self.assertEqual(message.files[0].ref, "wxwork://file_media_id/file-media-1")

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
        self.assertTrue(message.images[0].ref.startswith("wxbot://image/"))

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
            [image.ref for image in message.images],
            ["vocechat://file/%2Fuploads%2Fposter.png"],
        )
        self.assertEqual(message.images[0].mime_type, "image/png")

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

    def test_vocechat_message_parser_extracts_generic_file_payload(self):
        module = VoceChatModule()
        body = json.dumps(
            {
                "detail": {
                    "type": "normal",
                    "content_type": "vocechat/file",
                    "content": "/uploads/manual.pdf",
                    "properties": {"content_type": "application/pdf"},
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
        self.assertEqual(message.files[0].ref, "vocechat://file/%2Fuploads%2Fmanual.pdf")

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

    def test_slack_post_message_passes_local_file(self):
        module = SlackModule()
        client = Mock()

        with tempfile.TemporaryDirectory() as tempdir:
            file_path = Path(tempdir) / "guide.pdf"
            file_path.write_bytes(b"pdf")

            with patch.object(
                module,
                "get_configs",
                return_value={"slack-test": SimpleNamespace(name="slack-test")},
            ), patch.object(
                module, "check_message", return_value=True
            ), patch.object(
                module, "get_instance", return_value=client
            ):
                module.post_message(
                    Notification(
                        title="手册",
                        text="请下载",
                        file_path=str(file_path),
                        file_name="guide.pdf",
                        userid="U123",
                    )
                )

        client.send_file.assert_called_once()

    def test_discord_post_message_passes_local_file(self):
        module = DiscordModule()
        client = Mock()

        with tempfile.TemporaryDirectory() as tempdir:
            file_path = Path(tempdir) / "guide.pdf"
            file_path.write_bytes(b"pdf")

            with patch.object(
                module,
                "get_configs",
                return_value={
                    "discord-test": SimpleNamespace(
                        name="discord-test",
                        type="discord",
                        enabled=True,
                    )
                },
            ), patch.object(
                module, "check_message", return_value=True
            ), patch.object(
                module, "get_instance", return_value=client
            ):
                module.post_message(
                    Notification(
                        title="手册",
                        text="请下载",
                        file_path=str(file_path),
                        file_name="guide.pdf",
                        userid="user-1",
                    )
                )

        client.send_file.assert_called_once()

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
        self.assertEqual([image.ref for image in message.images], ["https://example.com/qq-image.png"])
        self.assertEqual(message.images[0].mime_type, "image/png")

    def test_qq_markdown_image_size_preserves_poster_ratio(self):
        with patch.object(QQBot, "_get_image_size", return_value=(1000, 1500)):
            content, use_markdown = QQBot._format_message_markdown(
                title="poster",
                image="https://example.com/poster.jpg",
            )

        self.assertTrue(use_markdown)
        self.assertIn("![image #341px #512px](https://example.com/poster.jpg)", content)

    def test_qq_markdown_image_uses_poster_ratio_fallback(self):
        with patch.object(QQBot, "_get_image_size", return_value=None):
            content, use_markdown = QQBot._format_message_markdown(
                image="https://example.com/poster.jpg",
            )

        self.assertTrue(use_markdown)
        self.assertEqual(content, "![image #208px #320px](https://example.com/poster.jpg)")

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
        self.assertEqual([image.ref for image in message.images], ["https://example.com/image.png"])

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

    def test_synology_message_parser_accepts_generic_file_attachment(self):
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
                                "url": "https://example.com/manual.pdf",
                                "content_type": "application/pdf",
                                "filename": "manual.pdf",
                            }
                        ]
                    ),
                },
                args={},
            )

        self.assertIsNotNone(message)
        self.assertEqual(
            message.files[0].ref,
            "synology://file/" + quote("https://example.com/manual.pdf", safe=""),
        )

    def test_prepare_agent_files_saves_local_file(self):
        chain = MessageChain()
        with tempfile.TemporaryDirectory() as tempdir, patch(
            "app.chain.message.settings",
            SimpleNamespace(TEMP_PATH=Path(tempdir)),
        ), patch.object(
            chain,
            "_download_message_file_bytes",
            return_value="你好，MoviePilot".encode("utf-8"),
        ):
            prepared = chain._prepare_agent_files(
                session_id="session-1",
                files=[
                    CommingMessage.MessageAttachment(
                        ref="tg://document_file_id/doc-1",
                        name="note.txt",
                        mime_type="text/plain",
                    )
                ],
                channel=MessageChannel.Telegram,
                source="telegram-test",
            )

            self.assertEqual(prepared[0]["status"], "ready")
            self.assertTrue(Path(prepared[0]["local_path"]).exists())

    def test_telegram_post_message_passes_file_to_client(self):
        module = TelegramModule()
        client = Mock()

        with tempfile.TemporaryDirectory() as tempdir:
            file_path = Path(tempdir) / "report.txt"
            file_path.write_text("hello", encoding="utf-8")

            with patch.object(
                module,
                "get_configs",
                return_value={"telegram-test": SimpleNamespace(name="telegram-test")},
            ), patch.object(
                module, "check_message", return_value=True
            ), patch.object(
                module, "get_instance", return_value=client
            ):
                module.post_message(
                    Notification(
                        title="报告",
                        text="请下载",
                        file_path=str(file_path),
                        file_name="report.txt",
                        userid="user-1",
                    )
                )

        client.send_file.assert_called_once()
