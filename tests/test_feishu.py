import sys
import asyncio
import json
import tempfile
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import ANY, MagicMock, patch


sys.modules.setdefault("psutil", ModuleType("psutil"))
sys.modules.setdefault("cn2an", ModuleType("cn2an"))
sys.modules.setdefault("dateparser", ModuleType("dateparser"))
sys.modules.setdefault("zhconv", ModuleType("zhconv"))

if "Pinyin2Hanzi" not in sys.modules:
    pinyin_module = ModuleType("Pinyin2Hanzi")
    setattr(pinyin_module, "is_pinyin", lambda value: False)
    sys.modules["Pinyin2Hanzi"] = pinyin_module

from app.modules.feishu import FeishuModule
from app.modules.feishu.feishu import Feishu
from app.schemas import Notification
from app.schemas.message import (
    ChannelCapability,
    ChannelCapabilityManager,
    MessageResponse,
)
from app.schemas.types import MessageChannel, NotificationType


class TestFeishu(unittest.TestCase):
    @staticmethod
    def _build_client(**kwargs) -> Feishu:
        with (
            patch.object(Feishu, "_build_api_client", return_value=MagicMock()),
            patch.object(Feishu, "_start_ws_client"),
        ):
            return Feishu(
                FEISHU_APP_ID="cli_test_app_id",
                FEISHU_APP_SECRET="cli_test_app_secret",
                name="feishu-test",
                **kwargs,
            )

    @staticmethod
    def _success_response(message_id="om_test", chat_id="oc_test"):
        response = MagicMock()
        response.success.return_value = True
        response.data = SimpleNamespace(
            message_id=message_id,
            chat_id=chat_id,
            msg_type="interactive",
        )
        return response

    @staticmethod
    def _reaction_success_response(reaction_id="reaction_test"):
        response = MagicMock()
        response.success.return_value = True
        response.data = SimpleNamespace(reaction_id=reaction_id)
        return response

    @staticmethod
    def _card_create_success_response(card_id="card_test"):
        response = MagicMock()
        response.success.return_value = True
        response.data = SimpleNamespace(card_id=card_id)
        return response

    @staticmethod
    def _build_message_api(
        create_response=None,
        patch_response=None,
        reply_response=None,
        reaction_create_response=None,
        reaction_delete_response=None,
        card_create_response=None,
        card_settings_response=None,
        card_content_response=None,
        image_create_response=None,
        file_create_response=None,
        image_get_response=None,
        file_get_response=None,
        message_resource_response=None,
    ):
        message_api = SimpleNamespace(
            create=MagicMock(return_value=create_response),
            patch=MagicMock(return_value=patch_response),
            reply=MagicMock(return_value=reply_response),
            update=MagicMock(),
        )
        message_reaction_api = SimpleNamespace(
            create=MagicMock(return_value=reaction_create_response),
            delete=MagicMock(return_value=reaction_delete_response),
        )
        image_api = SimpleNamespace(
            create=MagicMock(return_value=image_create_response),
            get=MagicMock(return_value=image_get_response),
        )
        file_api = SimpleNamespace(
            create=MagicMock(return_value=file_create_response),
            get=MagicMock(return_value=file_get_response),
        )
        message_resource_api = SimpleNamespace(
            get=MagicMock(return_value=message_resource_response),
        )
        api_client = SimpleNamespace(
            im=SimpleNamespace(
                v1=SimpleNamespace(
                    message=message_api,
                    message_reaction=message_reaction_api,
                    image=image_api,
                    file=file_api,
                    message_resource=message_resource_api,
                )
            ),
            cardkit=SimpleNamespace(
                v1=SimpleNamespace(
                    card=SimpleNamespace(
                        create=MagicMock(return_value=card_create_response),
                        settings=MagicMock(return_value=card_settings_response),
                    ),
                    card_element=SimpleNamespace(
                        content=MagicMock(return_value=card_content_response),
                    ),
                )
            ),
        )
        return api_client, message_api

    @staticmethod
    def _resource_response(
        content: bytes,
        file_name: str = "resource.bin",
        content_type: str = "application/octet-stream",
    ):
        response = MagicMock()
        response.code = 0
        response.file = MagicMock()
        response.file.read.return_value = content
        response.file_name = file_name
        response.raw = SimpleNamespace(headers={"Content-Type": content_type})
        return response

    def test_parse_message_returns_callback_message(self):
        client = self._build_client()

        with patch("app.modules.feishu.feishu.UserOper.get_name", return_value=None):
            result = client.parse_message(
                {
                    "type": "cardAction",
                    "callback_data": "approve",
                    "message_id": "om_123",
                    "chat_id": "oc_123",
                    "sender": {
                        "open_id": "ou_user_1",
                        "user_id": "u_user_1",
                        "name": "tester",
                    },
                }
            )

        self.assertIsNotNone(result)
        self.assertEqual(result.channel, MessageChannel.Feishu)
        self.assertEqual(result.userid, "ou_user_1")
        self.assertEqual(result.text, "CALLBACK:approve")
        self.assertTrue(result.is_callback)
        self.assertEqual(result.chat_id, "oc_123")

    def test_extract_card_callback_data_supports_new_and_legacy_values(self):
        self.assertEqual(
            Feishu._extract_card_callback_data({"callback_data": "approve"}),
            "approve",
        )
        self.assertEqual(
            Feishu._extract_card_callback_data({"value": "legacy"}),
            "legacy",
        )
        self.assertEqual(
            Feishu._extract_card_callback_data("direct"),
            "direct",
        )
        self.assertEqual(
            Feishu._extract_card_callback_data({}, name="fallback"),
            "fallback",
        )

    def test_build_event_handler_registers_common_im_events(self):
        registered = []

        class _Builder:
            def __getattr__(self, name):
                if name.startswith("register_"):

                    def _register(handler):
                        registered.append(name)
                        return self

                    return _register
                raise AttributeError(name)

            def build(self):
                return "handler"

        client = self._build_client()
        fake_builder = _Builder()

        with patch(
            "app.modules.feishu.feishu.lark.EventDispatcherHandler.builder",
            return_value=fake_builder,
        ):
            handler = client._build_event_handler()

        self.assertEqual(handler, "handler")
        self.assertIn("register_p2_im_message_receive_v1", registered)
        self.assertIn("register_p2_im_message_message_read_v1", registered)
        self.assertIn("register_p2_im_message_reaction_created_v1", registered)
        self.assertIn("register_p2_im_message_reaction_deleted_v1", registered)
        self.assertIn("register_p2_im_message_recalled_v1", registered)
        self.assertIn(
            "register_p2_im_chat_access_event_bot_p2p_chat_entered_v1", registered
        )
        self.assertIn("register_p2_card_action_trigger", registered)

    def test_parse_message_blocks_non_admin_command(self):
        client = self._build_client(FEISHU_ADMINS="ou_admin")

        with (
            patch("app.modules.feishu.feishu.UserOper.get_name", return_value=None),
            patch.object(
                client, "send_text", return_value={"success": True}
            ) as send_text,
        ):
            result = client.parse_message(
                {
                    "type": "message",
                    "text": "/help",
                    "chat_id": "oc_chat_1",
                    "sender": {
                        "open_id": "ou_user_2",
                        "user_id": "u_user_2",
                        "name": "tester",
                    },
                }
            )

        self.assertIsNone(result)
        send_text.assert_called_once_with(
            "只有管理员才有权限执行此命令",
            userid="ou_user_2",
            chat_id="oc_chat_1",
            receive_id_type="open_id",
        )

    def test_parse_message_maps_feishu_ids_to_moviepilot_username(self):
        client = self._build_client()

        with patch(
            "app.modules.feishu.feishu.UserOper.get_name",
            return_value="moviepilot-user",
        ) as get_name:
            result = client.parse_message(
                {
                    "type": "message",
                    "text": "/ai 添加黑客帝国订阅",
                    "sender": {
                        "open_id": "ou_bound_user",
                        "user_id": "u_bound_user",
                        "name": "ou_bound_user",
                    },
                }
            )

        self.assertIsNotNone(result)
        self.assertEqual(result.userid, "ou_bound_user")
        self.assertEqual(result.username, "moviepilot-user")
        get_name.assert_called_once_with(
            feishu_openid="ou_bound_user",
            feishu_userid="u_bound_user",
        )

    def test_send_notification_uses_direct_card_content(self):
        client = self._build_client()
        client._api_client, message_api = self._build_message_api(
            create_response=self._success_response()
        )

        result = client.send_notification(
            Notification(
                title="测试标题",
                text="测试正文",
                buttons=[[{"text": "确认", "callback_data": "confirm"}]],
            ),
            userid="ou_user_3",
        )

        self.assertTrue(result["success"])
        request = message_api.create.call_args.args[0]
        self.assertEqual(request.receive_id_type, "open_id")
        self.assertEqual(request.request_body.msg_type, "interactive")

        content = json.loads(request.request_body.content)
        self.assertNotIn("card", content)
        self.assertEqual(content["schema"], "2.0")
        self.assertTrue(content["config"]["update_multi"])
        self.assertEqual(content["body"]["padding"], "12px 12px 12px 12px")
        self.assertEqual(content["body"]["elements"][0]["text_size"], "heading")
        self.assertEqual(content["body"]["elements"][0]["tag"], "markdown")
        button = content["body"]["elements"][-1]["columns"][0]["elements"][0]
        self.assertEqual(button["tag"], "button")
        self.assertNotIn("value", button)
        self.assertEqual(
            button["behaviors"],
            [{"type": "callback", "value": {"callback_data": "confirm"}}],
        )

    def test_send_notification_embeds_remote_image_in_card(self):
        client = self._build_client()
        image_upload_response = MagicMock()
        image_upload_response.success.return_value = True
        image_upload_response.data = SimpleNamespace(image_key="img_v2_remote")
        client._api_client, message_api = self._build_message_api(
            create_response=self._success_response(),
            image_create_response=image_upload_response,
        )

        response = MagicMock()
        response.content = b"png-bytes"
        response.headers = {"Content-Type": "image/png"}

        with patch("app.modules.feishu.feishu.RequestUtils") as request_utils:
            request_utils.return_value.get_res.return_value = response
            result = client.send_notification(
                Notification(
                    title="测试标题",
                    text="测试正文",
                    image="https://example.com/poster.png",
                    buttons=[[{"text": "确认", "callback_data": "confirm"}]],
                ),
                userid="ou_user_img",
            )

        self.assertTrue(result["success"])
        response.close.assert_called_once()
        client._api_client.im.v1.image.create.assert_called_once()
        request = message_api.create.call_args.args[0]
        self.assertEqual(request.request_body.msg_type, "interactive")
        content = json.loads(request.request_body.content)
        self.assertEqual(content["body"]["padding"], "0px 0px 0px 0px")
        image_element = content["body"]["elements"][0]
        self.assertEqual(image_element["tag"], "img")
        self.assertEqual(image_element["img_key"], "img_v2_remote")
        self.assertEqual(content["body"]["elements"][1]["margin"], "12px 12px 0px 12px")
        self.assertEqual(content["body"]["elements"][2]["margin"], "4px 12px 12px 12px")
        self.assertEqual(
            content["body"]["elements"][-1]["margin"], "0px 12px 12px 12px"
        )
        self.assertEqual(content["body"]["elements"][-1]["tag"], "column_set")

    def test_send_notification_supports_user_id_target(self):
        client = self._build_client()
        client._api_client, message_api = self._build_message_api(
            create_response=self._success_response()
        )

        client.send_notification(
            Notification(title="测试标题", text="测试正文"),
            userid="u_user_4",
            receive_id_type="user_id",
        )

        request = message_api.create.call_args.args[0]
        self.assertEqual(request.receive_id_type, "user_id")

    def test_edit_message_uses_patch_api_for_cards(self):
        client = self._build_client()
        client._api_client, message_api = self._build_message_api(
            patch_response=self._success_response()
        )

        success = client.edit_message(
            message_id="om_456",
            title="测试标题",
            text="测试正文",
            buttons=[[{"text": "确认", "callback_data": "confirm"}]],
        )

        self.assertTrue(success)
        message_api.patch.assert_called_once()
        message_api.update.assert_not_called()

        request = message_api.patch.call_args.args[0]
        self.assertEqual(request.message_id, "om_456")
        content = json.loads(request.request_body.content)
        self.assertNotIn("card", content)
        self.assertEqual(content["schema"], "2.0")
        self.assertTrue(content["config"]["update_multi"])
        self.assertEqual(content["body"]["elements"][0]["tag"], "markdown")
        button = content["body"]["elements"][-1]["columns"][0]["elements"][0]
        self.assertEqual(
            button["behaviors"],
            [{"type": "callback", "value": {"callback_data": "confirm"}}],
        )

    def test_send_notification_replies_when_original_message_id_is_present(self):
        client = self._build_client()
        client._api_client, message_api = self._build_message_api(
            reply_response=self._success_response(message_id="om_reply")
        )

        result = client.send_notification(
            Notification(title="回复标题", text="回复正文"),
            userid="ou_user_9",
            original_message_id="om_origin",
        )

        self.assertTrue(result["success"])
        message_api.reply.assert_called_once()
        request = message_api.reply.call_args.args[0]
        self.assertEqual(request.message_id, "om_origin")
        self.assertEqual(request.request_body.msg_type, "interactive")

    def test_message_reaction_create_and_delete_use_official_api(self):
        client = self._build_client()
        client._api_client, _ = self._build_message_api(
            reaction_create_response=self._reaction_success_response("reaction_1"),
            reaction_delete_response=self._success_response(),
        )

        reaction_id = client.add_message_reaction(
            "om_origin", Feishu.PROCESSING_REACTION_EMOJI
        )
        deleted = client.delete_message_reaction("om_origin", "reaction_1")

        self.assertEqual(reaction_id, "reaction_1")
        self.assertTrue(deleted)
        create_request = (
            client._api_client.im.v1.message_reaction.create.call_args.args[0]
        )
        self.assertEqual(create_request.message_id, "om_origin")
        self.assertEqual(
            create_request.request_body.reaction_type.emoji_type,
            Feishu.PROCESSING_REACTION_EMOJI,
        )
        delete_request = (
            client._api_client.im.v1.message_reaction.delete.call_args.args[0]
        )
        self.assertEqual(delete_request.message_id, "om_origin")
        self.assertEqual(delete_request.reaction_id, "reaction_1")

    def test_send_notification_uses_streaming_card_for_agent_text(self):
        client = self._build_client()
        client._api_client, message_api = self._build_message_api(
            create_response=self._success_response(
                message_id="om_stream", chat_id="oc_stream"
            ),
            card_create_response=self._card_create_success_response("card_stream"),
        )

        result = client.send_notification(
            Notification(
                mtype=NotificationType.Agent,
                title="MoviePilot助手",
                text="第一帧内容",
            ),
            userid="ou_user_stream",
        )

        self.assertTrue(result["success"])
        self.assertEqual(
            result["metadata"]["feishu_streaming"]["card_id"], "card_stream"
        )
        self.assertEqual(result["metadata"]["feishu_streaming"]["sequence"], 0)
        card_request = client._api_client.cardkit.v1.card.create.call_args.args[0]
        self.assertEqual(card_request.request_body.type, "card_json")
        card_payload = json.loads(card_request.request_body.data)
        self.assertTrue(card_payload["config"]["streaming_mode"])
        self.assertEqual(
            card_payload["body"]["elements"][-1]["element_id"],
            Feishu.STREAM_CARD_BODY_ELEMENT_ID,
        )
        message_request = message_api.create.call_args.args[0]
        self.assertEqual(message_request.request_body.msg_type, "interactive")
        self.assertEqual(
            json.loads(message_request.request_body.content)["data"]["card_id"],
            "card_stream",
        )

    def test_send_notification_replies_with_streaming_card_for_agent_text(self):
        client = self._build_client()
        client._api_client, message_api = self._build_message_api(
            reply_response=self._success_response(
                message_id="om_reply", chat_id="oc_stream"
            ),
            card_create_response=self._card_create_success_response("card_stream"),
        )

        result = client.send_notification(
            Notification(
                mtype=NotificationType.Agent,
                title="MoviePilot助手",
                text="第一帧内容",
            ),
            userid="ou_user_stream",
            original_message_id="om_origin",
        )

        self.assertTrue(result["success"])
        message_api.create.assert_not_called()
        reply_request = message_api.reply.call_args.args[0]
        self.assertEqual(reply_request.message_id, "om_origin")
        self.assertEqual(reply_request.request_body.msg_type, "interactive")
        self.assertEqual(
            json.loads(reply_request.request_body.content)["data"]["card_id"],
            "card_stream",
        )
        self.assertEqual(result["metadata"]["feishu_streaming"]["sequence"], 0)

    def test_edit_replied_streaming_card_uses_first_increment_sequence(self):
        client = self._build_client()
        client._api_client, message_api = self._build_message_api(
            patch_response=self._success_response(),
            card_content_response=self._success_response(),
        )

        success = client.edit_message(
            message_id="om_reply",
            text="补充内容",
            metadata={
                "feishu_streaming": {
                    "card_id": "card_stream",
                    "element_id": Feishu.STREAM_CARD_BODY_ELEMENT_ID,
                    "sequence": 0,
                }
            },
        )

        self.assertTrue(success)
        message_api.patch.assert_not_called()
        content_request = (
            client._api_client.cardkit.v1.card_element.content.call_args.args[0]
        )
        self.assertEqual(content_request.request_body.sequence, 1)

    def test_edit_message_uses_cardkit_content_for_streaming_card(self):
        client = self._build_client()
        client._api_client, message_api = self._build_message_api(
            patch_response=self._success_response(),
            card_content_response=self._success_response(),
        )

        success = client.edit_message(
            message_id="om_stream",
            text="第二帧内容",
            metadata={
                "feishu_streaming": {
                    "card_id": "card_stream",
                    "element_id": Feishu.STREAM_CARD_BODY_ELEMENT_ID,
                    "sequence": 0,
                }
            },
        )

        self.assertTrue(success)
        client._api_client.cardkit.v1.card_element.content.assert_called_once()
        message_api.patch.assert_not_called()
        content_request = (
            client._api_client.cardkit.v1.card_element.content.call_args.args[0]
        )
        self.assertEqual(content_request.card_id, "card_stream")
        self.assertEqual(content_request.element_id, Feishu.STREAM_CARD_BODY_ELEMENT_ID)
        self.assertEqual(content_request.request_body.sequence, 1)

    def test_close_streaming_card_updates_card_settings(self):
        client = self._build_client()
        client._api_client, _ = self._build_message_api(
            card_settings_response=self._success_response(),
        )

        success = client.close_streaming_card(card_id="card_stream", sequence=3)

        self.assertTrue(success)
        settings_request = client._api_client.cardkit.v1.card.settings.call_args.args[0]
        self.assertEqual(settings_request.card_id, "card_stream")
        settings_payload = json.loads(settings_request.request_body.settings)
        self.assertFalse(settings_payload["config"]["streaming_mode"])

    def test_parse_message_supports_image_and_file_payloads(self):
        client = self._build_client()

        with patch("app.modules.feishu.feishu.UserOper.get_name", return_value=None):
            image_message = client.parse_message(
                {
                    "type": "message",
                    "text": "",
                    "images": [{"ref": "feishu://image/img_v2_test"}],
                    "message_id": "om_img",
                    "chat_id": "oc_chat",
                    "sender": {
                        "open_id": "ou_user_5",
                        "name": "tester",
                    },
                }
            )

            file_message = client.parse_message(
                {
                    "type": "message",
                    "text": "",
                    "files": [
                        {
                            "ref": "feishu://file/file_key/report.pdf",
                            "name": "report.pdf",
                        }
                    ],
                    "message_id": "om_file",
                    "chat_id": "oc_chat",
                    "sender": {
                        "open_id": "ou_user_6",
                        "name": "tester",
                    },
                }
            )

        self.assertEqual(image_message.images[0].ref, "feishu://image/img_v2_test")
        self.assertEqual(file_message.files[0].ref, "feishu://file/file_key/report.pdf")

    def test_on_message_wraps_feishu_image_ref_with_message_id(self):
        client = self._build_client()
        message = SimpleNamespace(
            message_id="om_img_evt",
            chat_id="oc_chat_evt",
            chat_type="p2p",
            message_type="image",
            content=json.dumps({"image_key": "img_v2_evt"}),
        )
        sender = SimpleNamespace(
            sender_id=SimpleNamespace(open_id="ou_user_evt", user_id=None)
        )
        event = SimpleNamespace(sender=sender, message=message)

        with patch.object(client, "_forward_to_message_chain") as forward:
            client._on_message(SimpleNamespace(event=event))

        payload = forward.call_args.args[0]
        self.assertEqual(
            payload["images"][0]["ref"], "feishu://image/om_img_evt/img_v2_evt"
        )

    def test_on_message_wraps_feishu_audio_ref_with_message_id(self):
        client = self._build_client()
        message = SimpleNamespace(
            message_id="om_audio_evt",
            chat_id="oc_chat_evt",
            chat_type="p2p",
            message_type="audio",
            content=json.dumps(
                {"file_key": "file_audio_evt", "file_name": "voice.opus"}
            ),
        )
        sender = SimpleNamespace(
            sender_id=SimpleNamespace(open_id="ou_user_evt", user_id=None)
        )
        event = SimpleNamespace(sender=sender, message=message)

        with patch.object(client, "_forward_to_message_chain") as forward:
            client._on_message(SimpleNamespace(event=event))

        payload = forward.call_args.args[0]
        self.assertEqual(
            payload["audio_refs"],
            ["feishu://file/om_audio_evt/file_audio_evt/voice.opus"],
        )

    def test_feishu_channel_capabilities_enable_images_and_files(self):
        self.assertTrue(
            ChannelCapabilityManager.supports_capability(
                MessageChannel.Feishu,
                ChannelCapability.IMAGES,
            )
        )
        self.assertTrue(
            ChannelCapabilityManager.supports_capability(
                MessageChannel.Feishu,
                ChannelCapability.FILE_SENDING,
            )
        )

    def test_send_file_uploads_image_then_sends_mixed_card(self):
        client = self._build_client()
        image_upload_response = MagicMock()
        image_upload_response.success.return_value = True
        image_upload_response.data = SimpleNamespace(image_key="img_v2_uploaded")
        client._api_client, message_api = self._build_message_api(
            create_response=self._success_response(message_id="om_image"),
            image_create_response=image_upload_response,
        )

        with tempfile.NamedTemporaryFile(suffix=".png") as fp:
            fp.write(b"png-bytes")
            fp.flush()
            result = client.send_file(
                file_path=fp.name,
                userid="ou_user_7",
                title="图片标题",
                text="图片说明",
            )

        self.assertTrue(result["success"])
        client._api_client.im.v1.image.create.assert_called_once()
        request = message_api.create.call_args.args[0]
        self.assertEqual(request.request_body.msg_type, "interactive")
        content = json.loads(request.request_body.content)
        self.assertEqual(content["body"]["padding"], "0px 0px 0px 0px")
        self.assertEqual(content["body"]["elements"][0]["img_key"], "img_v2_uploaded")
        self.assertEqual(content["body"]["elements"][1]["content"], "图片标题")
        self.assertEqual(content["body"]["elements"][1]["margin"], "12px 12px 0px 12px")
        self.assertEqual(content["body"]["elements"][2]["content"], "图片说明")
        self.assertEqual(content["body"]["elements"][2]["margin"], "4px 12px 12px 12px")

    def test_send_file_keeps_non_image_file_message_and_caption(self):
        client = self._build_client()
        file_upload_response = MagicMock()
        file_upload_response.success.return_value = True
        file_upload_response.data = SimpleNamespace(file_key="file_doc")
        client._api_client, message_api = self._build_message_api(
            create_response=self._success_response(message_id="om_file"),
            file_create_response=file_upload_response,
        )

        with (
            tempfile.NamedTemporaryFile(suffix=".txt") as fp,
            patch.object(
                client, "send_text", return_value={"success": True}
            ) as send_text,
        ):
            fp.write(b"text-bytes")
            fp.flush()
            result = client.send_file(
                file_path=fp.name,
                userid="ou_user_7",
                title="文件标题",
                text="文件说明",
            )

        self.assertTrue(result["success"])
        client._api_client.im.v1.file.create.assert_called_once()
        request = message_api.create.call_args.args[0]
        self.assertEqual(request.request_body.msg_type, "file")
        self.assertEqual(
            json.loads(request.request_body.content)["file_key"], "file_doc"
        )
        send_text.assert_called_once()

    def test_send_voice_uploads_audio_file_and_optionally_sends_caption(self):
        client = self._build_client()
        file_upload_response = MagicMock()
        file_upload_response.success.return_value = True
        file_upload_response.data = SimpleNamespace(file_key="file_audio")
        client._api_client, message_api = self._build_message_api(
            create_response=self._success_response(message_id="om_audio"),
            file_create_response=file_upload_response,
        )

        with tempfile.NamedTemporaryFile(suffix=".opus") as fp:
            fp.write(b"opus-bytes")
            fp.flush()
            with patch.object(
                client, "send_text", return_value={"success": True}
            ) as send_text:
                result = client.send_voice(
                    voice_path=fp.name,
                    userid="ou_user_8",
                    caption="这是说明",
                )

        self.assertTrue(result["success"])
        request = message_api.create.call_args.args[0]
        self.assertEqual(request.request_body.msg_type, "audio")
        self.assertEqual(
            json.loads(request.request_body.content)["file_key"], "file_audio"
        )
        send_text.assert_called_once()

    def test_download_helpers_return_bytes_and_data_url(self):
        client = self._build_client()
        client._api_client, _ = self._build_message_api(
            image_get_response=self._resource_response(
                b"image-bytes", file_name="poster.png", content_type="image/png"
            ),
            file_get_response=self._resource_response(
                b"file-bytes", file_name="report.txt", content_type="text/plain"
            ),
            message_resource_response=self._resource_response(
                b"resource-bytes", file_name="voice.opus", content_type="audio/ogg"
            ),
        )

        image_download = client.download_image_bytes("img_v2_test")
        file_download = client.download_file_bytes("file_test")
        resource_download = client.download_message_resource_bytes(
            "om_test", "file_test", "audio"
        )

        self.assertEqual(image_download[0], b"image-bytes")
        self.assertEqual(file_download[0], b"file-bytes")
        self.assertEqual(resource_download[0], b"resource-bytes")

    def test_module_send_direct_message_prefers_open_id_target(self):
        module = FeishuModule()
        module._channel = MessageChannel.Feishu
        conf = SimpleNamespace(name="feishu-main")
        client = MagicMock()
        client.send_notification.return_value = {
            "success": True,
            "message_id": "om_789",
            "chat_id": "oc_789",
        }

        with (
            patch.object(module, "get_configs", return_value={"feishu-main": conf}),
            patch.object(module, "check_message", return_value=True),
            patch.object(module, "get_instance", return_value=client),
        ):
            response = module.send_direct_message(
                Notification(
                    targets={
                        "feishu_userid": "u_target",
                        "feishu_openid": "ou_target",
                    }
                )
            )

        client.send_notification.assert_called_once_with(
            message=ANY,
            userid="ou_target",
            chat_id=None,
            receive_id_type="open_id",
            original_message_id=None,
        )
        self.assertTrue(response.success)
        self.assertEqual(response.message_id, "om_789")
        self.assertEqual(response.chat_id, "oc_789")

    def test_run_ws_client_binds_thread_local_event_loop(self):
        client = self._build_client()
        original_loop = object()
        fake_ws_client = MagicMock()
        created_loops = []
        real_new_event_loop = asyncio.new_event_loop

        def _new_loop():
            loop = real_new_event_loop()
            created_loops.append(loop)
            return loop

        with (
            patch(
                "app.modules.feishu.feishu.lark_ws_client_module.loop", original_loop
            ),
            patch(
                "app.modules.feishu.feishu.lark_ws_client_module._select",
                new=MagicMock(return_value=None),
            ),
            patch(
                "app.modules.feishu.feishu.asyncio.new_event_loop",
                side_effect=_new_loop,
            ),
            patch(
                "app.modules.feishu.feishu.lark.ws.Client", return_value=fake_ws_client
            ),
            patch.object(
                fake_ws_client, "start", side_effect=lambda: None
            ) as mock_start,
        ):
            client._run_ws_client()

        self.assertIsNone(client._ws_loop)
        mock_start.assert_called_once()
        self.assertEqual(len(created_loops), 1)
        self.assertTrue(created_loops[0].is_closed())

    def test_stop_disconnects_ws_client_via_threadsafe_loop(self):
        client = self._build_client()
        stop_loop = MagicMock()
        stop_loop.is_running.return_value = True
        client._ws_loop = stop_loop
        client._ws_client = MagicMock()
        client._ws_thread = MagicMock()
        client._ws_thread.is_alive.return_value = False

        future = MagicMock()
        future.result.return_value = None

        with patch(
            "app.modules.feishu.feishu.asyncio.run_coroutine_threadsafe",
            return_value=future,
        ) as runner:
            client.stop()

        runner.assert_called_once()
        future.result.assert_called_once_with(timeout=5)

    def test_module_download_helpers_delegate_to_client(self):
        module = FeishuModule()
        client = MagicMock()
        client.download_image_bytes.return_value = (b"image", "poster.png", "image/png")
        client.download_file_bytes.return_value = (b"file", "note.txt", "text/plain")
        client.download_message_resource_bytes.return_value = (
            b"image",
            "poster.png",
            "image/png",
        )

        with (
            patch.object(
                module, "get_config", return_value=SimpleNamespace(name="feishu-main")
            ),
            patch.object(module, "get_instance", return_value=client),
        ):
            data_url = module.download_feishu_image_to_data_url(
                "feishu://image/om_msg/img_v2_xxx", "feishu-main"
            )
            file_bytes = module.download_feishu_file_bytes(
                "feishu://file/file_xxx/note.txt", "feishu-main"
            )
            audio_bytes = module.download_feishu_file_bytes(
                "feishu://file/om_audio/file_audio/voice.opus",
                "feishu-main",
            )

        self.assertTrue(data_url.startswith("data:image/png;base64,"))
        self.assertEqual(file_bytes, b"file")
        self.assertEqual(audio_bytes, b"image")
        client.download_message_resource_bytes.assert_any_call(
            message_id="om_msg",
            file_key="img_v2_xxx",
            resource_type="image",
        )
        client.download_message_resource_bytes.assert_any_call(
            message_id="om_audio",
            file_key="file_audio",
            resource_type="audio",
        )

    def test_module_message_reaction_helpers_delegate_to_client(self):
        module = FeishuModule()
        client = MagicMock()
        client.add_message_reaction.return_value = "reaction_2"
        client.delete_message_reaction.return_value = True

        with (
            patch.object(
                module, "get_config", return_value=SimpleNamespace(name="feishu-main")
            ),
            patch.object(module, "get_instance", return_value=client),
        ):
            reaction_id = module.add_feishu_message_reaction(
                "om_x", "GLANCE", "feishu-main"
            )
            deleted = module.delete_feishu_message_reaction(
                "om_x", "reaction_2", "feishu-main"
            )

        self.assertEqual(reaction_id, "reaction_2")
        self.assertTrue(deleted)

    def test_module_finalize_message_closes_streaming_card(self):
        module = FeishuModule()
        module._channel = MessageChannel.Feishu
        client = MagicMock()
        client.close_streaming_card.return_value = True

        with (
            patch.object(
                module, "get_config", return_value=SimpleNamespace(name="feishu-main")
            ),
            patch.object(module, "get_instance", return_value=client),
        ):
            success = module.finalize_message(
                MessageResponse(
                    message_id="om_stream",
                    chat_id="oc_stream",
                    channel=MessageChannel.Feishu,
                    source="feishu-main",
                    metadata={
                        "feishu_streaming": {
                            "card_id": "card_stream",
                            "sequence": 2,
                        }
                    },
                    success=True,
                )
            )

        self.assertTrue(success)
        client.close_streaming_card.assert_called_once_with(
            card_id="card_stream", sequence=3
        )

    def test_module_post_message_prefers_file_and_voice_paths(self):
        module = FeishuModule()
        conf = SimpleNamespace(name="feishu-main")
        client = MagicMock()

        with (
            patch.object(module, "get_configs", return_value={"feishu-main": conf}),
            patch.object(module, "check_message", return_value=True),
            patch.object(module, "get_instance", return_value=client),
        ):
            module.post_message(
                Notification(
                    file_path="/tmp/demo.txt",
                    text="说明",
                    title="标题",
                    userid="ou_user",
                )
            )
            module.post_message(
                Notification(
                    voice_path="/tmp/demo.opus",
                    voice_caption="语音说明",
                    userid="ou_user",
                )
            )

        client.send_file.assert_called_once()
        client.send_voice.assert_called_once()

    def test_module_post_message_sends_image_card_before_file_attachment(self):
        module = FeishuModule()
        conf = SimpleNamespace(name="feishu-main")
        client = MagicMock()

        with (
            patch.object(module, "get_configs", return_value={"feishu-main": conf}),
            patch.object(module, "check_message", return_value=True),
            patch.object(module, "get_instance", return_value=client),
        ):
            module.post_message(
                Notification(
                    file_path="/tmp/demo.txt",
                    file_name="demo.txt",
                    image="https://example.com/poster.png",
                    text="说明",
                    title="标题",
                    userid="ou_user",
                )
            )

        client.send_notification.assert_called_once()
        sent_message = client.send_notification.call_args.kwargs["message"]
        self.assertEqual(sent_message.image, "https://example.com/poster.png")
        self.assertIsNone(sent_message.file_path)
        client.send_file.assert_called_once()
        self.assertIsNone(client.send_file.call_args.kwargs.get("title"))
        self.assertIsNone(client.send_file.call_args.kwargs.get("text"))

    def test_module_send_direct_message_sends_image_card_before_file_attachment(self):
        module = FeishuModule()
        module._channel = MessageChannel.Feishu
        conf = SimpleNamespace(name="feishu-main")
        client = MagicMock()
        client.send_notification.return_value = {
            "success": True,
            "message_id": "om_card",
            "chat_id": "oc_card",
        }
        client.send_file.return_value = {"success": True, "message_id": "om_file"}

        with (
            patch.object(module, "get_configs", return_value={"feishu-main": conf}),
            patch.object(module, "check_message", return_value=True),
            patch.object(module, "get_instance", return_value=client),
        ):
            response = module.send_direct_message(
                Notification(
                    channel=MessageChannel.Feishu,
                    source="feishu-main",
                    file_path="/tmp/demo.txt",
                    file_name="demo.txt",
                    image="https://example.com/poster.png",
                    text="说明",
                    title="标题",
                    userid="ou_user",
                )
            )

        self.assertTrue(response.success)
        self.assertEqual(response.message_id, "om_card")
        client.send_notification.assert_called_once()
        client.send_file.assert_called_once()

    def test_module_post_message_passes_original_message_id_for_reply(self):
        module = FeishuModule()
        conf = SimpleNamespace(name="feishu-main")
        client = MagicMock()

        with (
            patch.object(module, "get_configs", return_value={"feishu-main": conf}),
            patch.object(module, "check_message", return_value=True),
            patch.object(module, "get_instance", return_value=client),
        ):
            module.post_message(
                Notification(
                    title="标题",
                    text="正文",
                    userid="ou_user",
                    original_message_id="om_source",
                    original_chat_id="oc_source",
                )
            )

        client.send_notification.assert_called_once()
        self.assertEqual(
            client.send_notification.call_args.kwargs["original_message_id"],
            "om_source",
        )


if __name__ == "__main__":
    unittest.main()
