import json
import re
from typing import Optional, Union, List, Tuple, Any

from app.domain.context import MediaInfo, Context
from app.application.messaging.agent import (
    matches_channel_admin,
    register_channel_admin_resolver,
    resolve_config_principal_ids,
)
from app.runtime.log import logger
from app.modules._base import _MessageChannelModuleBase
from app.modules.telegram.telegram import Telegram
from app.schemas.notification import NotificationChannel
from app.schemas.message import IncomingMessage
from app.schemas.message import Message
from app.schemas.system import NotificationConf
from app.schemas.message import MessageResponse
from app.schemas.types import ModuleType


register_channel_admin_resolver(
    NotificationChannel.Telegram,
    lambda config: resolve_config_principal_ids(
        config, "TELEGRAM_ADMINS", "TELEGRAM_CHAT_ID"
    ),
)


class TelegramModule(_MessageChannelModuleBase[Telegram]):
    """
    Telegram 通知模块，负责模块生命周期、消息解析和通知发送。
    """

    # 管理员配置键，与渠道 resolver 保持一致
    _admin_config_key = "TELEGRAM_ADMINS"

    def init_module(self) -> None:
        """
        初始化模块
        """
        super().init_service(
            service_name=Telegram.__name__.lower(), service_type=Telegram
        )
        self._channel = NotificationChannel.Telegram

    @staticmethod
    def get_name() -> str:
        """
        获取模块名称
        """
        return "Telegram"

    @staticmethod
    def get_type() -> ModuleType:
        """
        获取模块类型
        """
        return ModuleType.Notification

    @staticmethod
    def get_subtype() -> NotificationChannel:
        """
        获取模块子类型
        """
        return NotificationChannel.Telegram

    @staticmethod
    def get_priority() -> int:
        """
        获取模块优先级，数字越小优先级越高，只有同一接口下优先级才生效
        """
        return 0

    def stop(self) -> None:
        """停止模块"""
        for client in self.get_instances().values():
            try:
                client.stop()
            except Exception as err:
                logger.error(f"停止Telegram模块实例失败：{err}")

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        """
        获取模块初始化配置项。
        """
        pass

    def message_parser(
        self, source: str, body: Any, form: Any, args: Any
    ) -> Optional[IncomingMessage]:
        """
        解析消息内容，返回字典，注意以下约定值：
        userid: 用户ID
        username: 用户名
        text: 内容
        :param source: 消息来源
        :param body: 请求体
        :param form: 表单
        :param args: 参数
        :return: 渠道、消息体
        """
        """
            普通消息格式：
            {
                'update_id': ,
                'message': {
                    'message_id': ,
                    'from': {
                        'id': ,
                        'is_bot': False,
                        'first_name': '',
                        'username': '',
                        'language_code': 'zh-hans'
                    },
                    'chat': {
                        'id': ,
                        'first_name': '',
                        'username': '',
                        'type': 'private'
                    },
                    'date': ,
                    'text': ''
                }
            }

            按钮回调格式：
            {
                'callback_query': {
                    'id': '',
                    'from': {...},
                    'message': {...},
                    'data': 'callback_data'
                }
            }
        """
        # 获取服务配置
        client_config = self.get_config(source)
        if not client_config:
            return None
        client: Telegram = self.get_instance(client_config.name)
        try:
            message = json.loads(body)
            while isinstance(message, str):
                message = json.loads(message)
        except Exception as err:
            logger.debug(f"解析Telegram消息失败：{str(err)}")
            return None

        if not isinstance(message, dict):
            logger.debug(f"Telegram消息格式无效：{type(message)}")
            return None

        # 兼容某些转发链路使用 Telegram Update 外壳
        if "message" in message and isinstance(message.get("message"), dict):
            message = message.get("message")

        if message:
            # 处理按钮回调
            if "callback_query" in message:
                return self._handle_callback_query(message, client_config, client)

            # 处理普通消息
            return self._handle_text_message(message, client_config, client)

        return None

    def _handle_callback_query(
        self, message: dict, client_config: NotificationConf, client: Telegram
    ) -> Optional[IncomingMessage]:
        """
        处理按钮回调查询
        """
        callback_query = message.get("callback_query", {})
        user_info = callback_query.get("from", {})
        callback_data = callback_query.get("data", "")
        user_id = user_info.get("id")
        user_name = user_info.get("username")

        if callback_data and user_id:
            if str(callback_data).strip().startswith("/") and self._should_reject_admin_command(
                    client_config.config, user_id
            ):
                if client:
                    client.answer_callback_query(
                        callback_query_id=callback_query.get("id"),
                        text="只有管理员才有权限执行此命令",
                        show_alert=True,
                    )
                return None

            logger.info(
                f"收到来自 {client_config.name} 的Telegram按钮回调："
                f"userid={user_id}, username={user_name}, callback_data={callback_data}"
            )

            # 将callback_data作为特殊格式的text返回，以便主程序识别这是按钮回调
            callback_text = f"CALLBACK:{callback_data}"

            # 创建包含完整回调信息的CommingMessage
            return IncomingMessage(
                channel=NotificationChannel.Telegram,
                source=client_config.name,
                userid=user_id,
                username=user_name,
                is_channel_admin=matches_channel_admin(
                    NotificationChannel.Telegram,
                    client_config.config,
                    user_id,
                ),
                text=callback_text,
                is_callback=True,
                callback_data=callback_data,
                message_id=callback_query.get("message", {}).get("message_id"),
                chat_id=str(
                    callback_query.get("message", {}).get("chat", {}).get("id", "")
                ),
                callback_query=callback_query,
            )
        return None

    def _handle_text_message(
        self, msg: dict, client_config: NotificationConf, client: Telegram
    ) -> Optional[IncomingMessage]:
        """
        处理普通文本消息
        """
        text = msg.get("text") or msg.get("caption")
        message_id = msg.get("message_id")
        user_id = msg.get("from", {}).get("id")
        user_name = msg.get("from", {}).get("username")
        chat_id = msg.get("chat", {}).get("id")
        reply_to_message_id = (msg.get("reply_to_message") or {}).get("message_id")

        # 将 text_link 实体中的 URL 嵌入到文本中
        if text:
            text = self._embed_entity_links(text, msg.get("entities") or msg.get("caption_entities"))

        # 将 reply_markup 中的 URL 按钮信息追加到文本中
        text = self._append_reply_markup_links(text, msg.get("reply_markup"))

        images = self._extract_images(msg)
        audio_refs = self._extract_audio_refs(msg)
        files = self._extract_files(msg)

        if user_id:
            if not text and not images and not audio_refs and not files:
                logger.debug(
                    f"收到来自 {client_config.name} 的Telegram消息无文本、图片、语音和文件"
                )
                return None

            logger.info(
                f"收到来自 {client_config.name} 的Telegram消息："
                f"userid={user_id}, username={user_name}, chat_id={chat_id}, text={text}, "
                f"images={len(images) if images else 0}, audios={len(audio_refs) if audio_refs else 0}, "
                f"files={len(files) if files else 0}"
            )

            cleaned_text = (
                self._clean_bot_mention(text, client.bot_username if client else None)
                if text
                else None
            )

            user_list = client_config.config.get("TELEGRAM_USERS")

            if cleaned_text and cleaned_text.startswith("/"):
                if self._should_reject_admin_command(client_config.config, user_id):
                    client.send_msg(
                        title="只有管理员才有权限执行此命令", userid=user_id
                    )
                    return None
            else:
                if user_list and str(user_id) not in user_list.split(","):
                    logger.info(f"用户{user_id}不在用户白名单中，无法使用此机器人")
                    client.send_msg(
                        title="你不在用户白名单中，无法使用此机器人", userid=user_id
                    )
                    return None

            return IncomingMessage(
                channel=NotificationChannel.Telegram,
                source=client_config.name,
                userid=user_id,
                username=user_name,
                is_channel_admin=matches_channel_admin(
                    NotificationChannel.Telegram,
                    client_config.config,
                    user_id,
                ),
                text=cleaned_text,
                message_id=message_id,
                chat_id=str(chat_id) if chat_id else None,
                reply_to_message_id=reply_to_message_id,
                images=images if images else None,
                audio_refs=audio_refs if audio_refs else None,
                files=files if files else None,
            )
        return None

    @staticmethod
    def _extract_images(msg: dict) -> Optional[List[IncomingMessage.MessageImage]]:
        """
        从Telegram消息中提取图片file_id
        """
        images = []
        photo = msg.get("photo")
        if photo and isinstance(photo, list):
            largest_photo = photo[-1]
            file_id = largest_photo.get("file_id")
            if file_id:
                images.append(
                    IncomingMessage.MessageImage(
                        ref=f"tg://file_id/{file_id}",
                        mime_type="image/jpeg",
                        size=largest_photo.get("file_size"),
                    )
                )

        document = msg.get("document")
        if document:
            file_id = document.get("file_id")
            mime_type = document.get("mime_type", "")
            if file_id and mime_type.startswith("image/"):
                images.append(
                    IncomingMessage.MessageImage(
                        ref=f"tg://file_id/{file_id}",
                        name=document.get("file_name"),
                        mime_type=document.get("mime_type"),
                        size=document.get("file_size"),
                    )
                )

        return images if images else None

    @staticmethod
    def _extract_audio_refs(msg: dict) -> Optional[List[str]]:
        """
        从Telegram消息中提取语音/音频 file_id。
        """
        audio_refs = []
        voice = msg.get("voice")
        if voice:
            file_id = voice.get("file_id")
            if file_id:
                audio_refs.append(f"tg://voice_file_id/{file_id}")

        audio = msg.get("audio")
        if audio:
            file_id = audio.get("file_id")
            if file_id:
                audio_refs.append(f"tg://audio_file_id/{file_id}")

        return audio_refs if audio_refs else None

    @staticmethod
    def _extract_files(msg: dict) -> Optional[List[IncomingMessage.MessageAttachment]]:
        """
        从 Telegram 消息中提取非图片文件附件。
        """
        document = msg.get("document")
        if not isinstance(document, dict):
            return None

        file_id = document.get("file_id")
        mime_type = (document.get("mime_type") or "").lower()
        if not file_id or mime_type.startswith("image/"):
            return None

        return [
            IncomingMessage.MessageAttachment(
                ref=f"tg://document_file_id/{file_id}",
                name=document.get("file_name"),
                mime_type=document.get("mime_type"),
                size=document.get("file_size"),
            )
        ]

    @staticmethod
    def _embed_entity_links(text: str, entities: Optional[List[dict]]) -> str:
        """
        将 text_link 实体中的 URL 嵌入到文本中

        :param text: 原始文本
        :param entities: 消息实体列表
        :return: 嵌入链接后的文本
        """
        if not entities:
            return text
        text_link_entities = sorted(
            [e for e in entities if e.get("type") == "text_link" and e.get("url")],
            key=lambda e: e.get("offset", 0),
            reverse=True,
        )
        text_utf16 = text.encode("utf-16-le")
        for entity in text_link_entities:
            offset = entity.get("offset", 0)
            length = entity.get("length", 0)
            url = entity["url"]
            char_offset = len(text_utf16[:offset * 2].decode("utf-16-le"))
            char_length = len(text_utf16[offset * 2: (offset + length) * 2].decode("utf-16-le"))
            display_text = text[char_offset: char_offset + char_length]
            text = text[:char_offset] + f"{display_text}({url})" + text[char_offset + char_length:]
            text_utf16 = text.encode("utf-16-le")
        return text

    @staticmethod
    def _append_reply_markup_links(text: Optional[str], reply_markup: Optional[dict]) -> Optional[str]:
        """
        将 reply_markup 中的 URL 按钮信息追加到文本末尾

        :param text: 原始文本
        :param reply_markup: 消息的 reply_markup 字段
        :return: 追加按钮链接后的文本
        """
        if not reply_markup:
            return text
        inline_keyboard = reply_markup.get("inline_keyboard")
        if not inline_keyboard:
            return text
        button_lines = []
        for row in inline_keyboard:
            for button in row:
                btn_text = button.get("text", "")
                btn_url = button.get("url")
                if btn_url:
                    button_lines.append(f"{btn_text}({btn_url})")
        if not button_lines:
            return text
        buttons_text = "\n".join(button_lines)
        if text:
            return f"{text}\n{buttons_text}"
        return buttons_text

    @staticmethod
    def _clean_bot_mention(text: str, bot_username: Optional[str]) -> str:
        """
        清理消息中的@bot部分，确保文本处理一致性
        :param text: 原始消息文本
        :param bot_username: bot用户名
        :return: 清理后的文本
        """
        if not text or not bot_username:
            return text

        # Remove @bot_username from the beginning and any position in text
        cleaned = text
        mention_pattern = f"@{bot_username}"

        # Remove mention at the beginning with optional following space
        if cleaned.startswith(mention_pattern):
            cleaned = cleaned[len(mention_pattern):].lstrip()

        # Remove mention at any other position
        cleaned = cleaned.replace(mention_pattern, "").strip()

        # Clean up multiple spaces
        cleaned = re.sub(r"\s+", " ", cleaned).strip()

        return cleaned

    def post_message(self, message: Message, **kwargs) -> None:
        """
        发送消息
        :param message: 消息体
        :return: 成功或失败
        """
        for conf in self.get_configs().values():
            if not self.check_message(message, conf.name):
                continue
            targets = message.targets
            userid = message.userid
            if not userid and targets is not None:
                userid = targets.get("telegram_userid")
                if not userid:
                    logger.warn(f"用户没有指定 Telegram用户ID，消息无法发送")
                    return
            client: Telegram = self.get_instance(conf.name)
            if client:
                if message.file_path:
                    client.send_file(
                        file_path=message.file_path,
                        file_name=message.file_name,
                        title=message.title,
                        text=message.text,
                        userid=userid,
                        original_chat_id=message.original_chat_id,
                        parse_mode=message.parse_mode,
                    )
                elif message.voice_path:
                    client.send_voice(
                        voice_path=message.voice_path,
                        userid=userid,
                        caption=message.voice_caption,
                        original_chat_id=message.original_chat_id,
                        parse_mode=message.parse_mode,
                    )
                else:
                    # Telegram 的 reply_markup 不能同时承载 InlineKeyboard 和 ForceReply。
                    # 普通通知只清空可编辑消息 ID，仍保留原会话作为新消息目标。
                    has_interaction_context = bool(message.buttons or message.force_reply)
                    original_message_id = (
                        message.original_message_id if has_interaction_context else None
                    )
                    client.send_msg(
                        title=message.title,
                        text=message.text,
                        image=message.image,
                        userid=userid,
                        link=message.link,
                        buttons=message.buttons,
                        force_reply=message.force_reply,
                        original_message_id=original_message_id,
                        original_chat_id=message.original_chat_id,
                        disable_web_page_preview=message.disable_web_page_preview,
                        parse_mode=message.parse_mode,
                    )

    def post_medias_message(
        self, message: Message, medias: List[MediaInfo]
    ) -> None:
        """
        发送媒体信息选择列表
        :param message: 消息体
        :param medias: 媒体列表
        :return: 成功或失败
        """
        for conf in self.get_configs().values():
            if not self.check_message(message, conf.name):
                continue
            client: Telegram = self.get_instance(conf.name)
            if client:
                client.send_medias_msg(
                    title=message.title,
                    medias=medias,
                    userid=message.userid,
                    link=message.link,
                    buttons=message.buttons,
                    original_message_id=message.original_message_id,
                    original_chat_id=message.original_chat_id,
                    parse_mode=message.parse_mode,
                )

    def post_torrents_message(
        self, message: Message, torrents: List[Context]
    ) -> None:
        """
        发送种子信息选择列表
        :param message: 消息体
        :param torrents: 种子列表
        :return: 成功或失败
        """
        for conf in self.get_configs().values():
            if not self.check_message(message, conf.name):
                continue
            client: Telegram = self.get_instance(conf.name)
            if client:
                client.send_torrents_msg(
                    title=message.title,
                    torrents=torrents,
                    userid=message.userid,
                    link=message.link,
                    buttons=message.buttons,
                    original_message_id=message.original_message_id,
                    original_chat_id=message.original_chat_id,
                    parse_mode=message.parse_mode,
                )

    def delete_message(
        self,
        channel: NotificationChannel,
        source: str,
        message_id: int,
        chat_id: Optional[int] = None,
    ) -> Optional[bool]:
        """
        删除消息
        :param channel: 消息渠道
        :param source: 指定的消息源
        :param message_id: 消息ID
        :param chat_id: 聊天ID
        :return: 删除是否成功
        """
        if channel != self._channel:
            return None
        success = False
        for conf in self.get_configs().values():
            if source != conf.name:
                continue
            client: Telegram = self.get_instance(conf.name)
            if client:
                result = client.delete_msg(message_id=message_id, chat_id=chat_id)
                if result:
                    success = True
        return success

    def edit_message(
        self,
        channel: NotificationChannel,
        source: str,
        message_id: Union[str, int],
        chat_id: Union[str, int],
        text: str,
        title: Optional[str] = None,
        buttons: Optional[List[List[dict]]] = None,
        metadata: Optional[dict] = None,
        parse_mode: Optional[str] = None,
    ) -> Optional[bool]:
        """
        编辑消息
        :param channel: 消息渠道
        :param source: 指定的消息源
        :param message_id: 消息ID
        :param chat_id: 聊天ID
        :param text: 新的消息内容
        :param title: 消息标题
        :param buttons: 新的按钮列表
        :param metadata: 其他元信息
        :param parse_mode: Telegram 消息格式类型，默认 MarkdownV2，可传 HTML
        :return: 编辑是否成功
        """
        if channel != self._channel:
            return None
        for conf in self.get_configs().values():
            if source != conf.name:
                continue
            client: Telegram = self.get_instance(conf.name)
            if client:
                result = client.edit_msg(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                    title=title,
                    buttons=buttons,
                    parse_mode=parse_mode,
                )
                if result:
                    return True
        return False

    def mark_message_processing_started(
            self,
            channel: NotificationChannel,
            source: str,
            userid: Optional[Union[str, int]] = None,
            message_id: Optional[Union[str, int]] = None,
            chat_id: Optional[Union[str, int]] = None,
            text: Optional[str] = None,
    ) -> Optional[dict]:
        """
        标记 Telegram 消息正在处理。
        Telegram typing 需要周期性续发，因此在模块接口中启动保活任务。
        """
        if channel != self._channel:
            return None
        client_config = self.get_config(source)
        if not client_config:
            return None
        client: Telegram = self.get_instance(client_config.name)
        if not client:
            return None
        started = client.start_typing(chat_id=chat_id, userid=userid)
        if not started:
            return None
        return {
            "channel": channel.value,
            "source": source,
            "userid": userid,
            "message_id": message_id,
            "chat_id": chat_id,
            "metadata": {"kind": "typing"},
        }

    def mark_message_processing_finished(
            self,
            channel: NotificationChannel,
            source: str,
            userid: Optional[Union[str, int]] = None,
            message_id: Optional[Union[str, int]] = None,
            chat_id: Optional[Union[str, int]] = None,
            status: Optional[dict] = None,
    ) -> Optional[bool]:
        """
        结束 Telegram typing 状态。
        """
        if channel != self._channel:
            return None
        if status:
            chat_id = status.get("chat_id") or chat_id
            userid = status.get("userid") or userid
        client_config = self.get_config(source)
        if not client_config:
            return False
        client: Telegram = self.get_instance(client_config.name)
        if not client:
            return False
        return client.stop_typing(chat_id=chat_id, userid=userid)

    def send_direct_message(self, message: Message) -> Optional[MessageResponse]:
        """
        直接发送消息并返回消息ID等信息
        :param message: 消息体
        :return: 消息响应（包含message_id, chat_id等）
        """
        for conf in self.get_configs().values():
            if not self.check_message(message, conf.name):
                continue
            targets = message.targets
            userid = message.userid
            if not userid and targets is not None:
                userid = targets.get("telegram_userid")
                if not userid:
                    logger.warn("用户没有指定 Telegram用户ID，消息无法发送")
                    return None
            client: Telegram = self.get_instance(conf.name)
            if client:
                if message.voice_path:
                    result = client.send_voice(
                        voice_path=message.voice_path,
                        userid=userid,
                        caption=message.voice_caption,
                        original_chat_id=message.original_chat_id,
                        parse_mode=message.parse_mode,
                    )
                else:
                    # direct message 只禁用编辑旧消息；仅 ForceReply 使用 original_chat_id
                    # 发回原会话，并保留 original_message_id 让 client reply_to 原消息。
                    original_chat_id = message.original_chat_id if message.force_reply else None
                    original_message_id = message.original_message_id if message.force_reply else None
                    result = client.send_msg(
                        title=message.title,
                        text=message.text,
                        image=message.image,
                        userid=userid,
                        link=message.link,
                        force_reply=message.force_reply,
                        original_message_id=original_message_id,
                        original_chat_id=original_chat_id,
                        disable_web_page_preview=message.disable_web_page_preview,
                        parse_mode=message.parse_mode,
                        private_delivery=message.private_delivery,
                    )
                if result and result.get("success"):
                    return MessageResponse(
                        message_id=result.get("message_id"),
                        chat_id=result.get("chat_id"),
                        channel=NotificationChannel.Telegram,
                        source=conf.name,
                        success=True,
                    )
        return None

    def download_telegram_file_to_base64(self, file_id: str, source: str) -> Optional[str]:
        """
        下载Telegram文件并转为base64
        :param file_id: Telegram文件ID
        :param source: 来源名称
        :return: base64编码的图片数据
        """
        config = self.get_config(source)
        if not config:
            return None
        client = self.get_instance(config.name)
        if not client:
            return None
        file_content = client.download_file(file_id)
        if file_content:
            import base64

            return base64.b64encode(file_content).decode()
        return None

    def download_telegram_file_bytes(self, file_id: str, source: str) -> Optional[bytes]:
        """
        下载Telegram文件并返回原始字节。
        """
        config = self.get_config(source)
        if not config:
            return None
        client = self.get_instance(config.name)
        if not client:
            return None
        return client.download_file(file_id)
