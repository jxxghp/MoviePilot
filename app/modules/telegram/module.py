"""
Telegram 通知模块的权限检查、入站解析和实例投递路由。
"""

import json
import re
from typing import Any, List, Optional, Tuple, Union
from urllib.parse import quote

from app.application.messaging.channel.admin import (
    matches_channel_admin,
    register_channel_admin_resolver,
    resolve_config_principal_ids,
)
from app.domain.context import Context, MediaInfo
from app.modules._base.notification import _MessageChannelModuleBase
from app.modules.telegram.telegram import Telegram
from app.runtime.log import logger
from app.schemas.message import IncomingMessage, Message, MessageResponse
from app.schemas.system import NotificationConf
from app.schemas.types import ModuleType, NotificationChannel

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

    # 管理员配置键，与渠道管理员解析器保持一致
    _admin_config_key = "TELEGRAM_ADMINS"
    _notification_user_target_keys = ("telegram_userid",)
    _notification_admin_default_config_keys = ("TELEGRAM_CHAT_ID",)

    def init_module(self) -> None:
        """
        加载 Telegram 通知实例并绑定渠道类型。
        """
        super().init_service(
            service_name=Telegram.__name__.lower(), service_type=Telegram
        )
        self._channel = NotificationChannel.Telegram

    @staticmethod
    def get_name() -> str:
        """
        返回配置界面使用的渠道名称。

        :return: 固定模块名称 Telegram
        """
        return "Telegram"

    @staticmethod
    def get_type() -> ModuleType:
        """
        声明模块属于通知服务。

        :return: 通知模块类型
        """
        return ModuleType.Notification

    @staticmethod
    def get_subtype() -> NotificationChannel:
        """
        声明模块使用 Telegram 通知渠道。

        :return: 固定模块名称 Telegram 渠道枚举
        """
        return NotificationChannel.Telegram

    @staticmethod
    def get_priority() -> int:
        """
        返回同一接口的模块调度优先级。

        :return: 固定为 0；数值越小越先调用
        """
        return 0

    def stop(self) -> bool:
        """
        停止全部 Telegram 实例并收尾轮询分发和输入状态任务。

        :return: 所有实例均报告停止成功时返回 True，否则返回 False
        """
        return self._stop_service_instances()

    def init_setting(self) -> Optional[Tuple[str, Union[str, bool]]]:
        """
        保留设置初始化钩子，实例配置由通知服务管理。
        """
        pass

    def message_parser(
        self, source: str, body: Any, form: Any, args: Any
    ) -> Optional[IncomingMessage]:
        """
        解析 Telegram 消息或按钮回调，并检查访问权限。

        :param source: 通知实例名称
        :param body: JSON 文本或字节；兼容再次编码的字符串及 Update 消息外壳
        :param form: 兼容其他渠道的表单参数，当前不使用
        :param args: 兼容其他渠道的查询参数，当前不使用
        :return: 含发送者和附件的统一消息；来源缺失、报文无效或不满足接收条件时返回 None
        """
        client_config = self.get_config(source)
        if not client_config:
            return None
        client = self.get_instance(client_config.name)
        if client is None:
            return None
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
        nested_message = message.get("message")
        if isinstance(nested_message, dict):
            message = nested_message

        if message:
            if "callback_query" in message:
                return self._handle_callback_query(message, client_config, client)

            return self._handle_text_message(message, client_config, client)

        return None

    def _handle_callback_query(
        self, message: dict[str, Any], client_config: NotificationConf, client: Telegram
    ) -> Optional[IncomingMessage]:
        """
        提取按钮回调并校验命令型回调的管理员身份。

        :param message: 含 callback_query 的更新报文
        :param client_config: 当前通知实例和管理员配置
        :param client: 机器人客户端，用于回显权限不足提示
        :return: 含 CALLBACK: 正文和原回调的统一消息；内容或用户缺失及命令未授权时返回 None
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
        self, msg: dict[str, Any], client_config: NotificationConf, client: Telegram
    ) -> Optional[IncomingMessage]:
        """
        提取文本与附件，去除机器人提及并应用访问规则。

        斜杠命令检查管理员规则；其他消息检查普通用户白名单。链接实体和按钮链接会合入正文。

        :param msg: Bot API Message 字典
        :param client_config: 当前实例、管理员和普通用户白名单配置
        :param client: 提供机器人用户名并发送权限提示的客户端
        :return: 通过检查的统一消息；无发送者、无内容或访问被拒绝时返回 None
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

            user_list = (client_config.config or {}).get("TELEGRAM_USERS")

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
    def _extract_images(msg: dict[str, Any]) -> Optional[List[IncomingMessage.MessageImage]]:
        """
        从Telegram消息中提取图片file_id。
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
    def _extract_audio_refs(msg: dict[str, Any]) -> Optional[List[str]]:
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
    def _extract_files(msg: dict[str, Any]) -> Optional[List[IncomingMessage.MessageAttachment]]:
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
    def _embed_entity_links(text: str, entities: Optional[List[dict[str, Any]]]) -> str:
        """
        将 text_link 实体中的 URL 嵌入到文本中。

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
    def _append_reply_markup_links(text: Optional[str], reply_markup: Optional[dict[str, Any]]) -> Optional[str]:
        """
        将 reply_markup 中的 URL 按钮信息追加到文本末尾。

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
        移除机器人提及，并把剩余空白折叠为单个空格。

        :param text: 原消息正文
        :param bot_username: 机器人用户名，不带 @；为空时保留原文本
        :return: 清理后的文本
        """
        if not text or not bot_username:
            return text

        cleaned = text
        mention_pattern = f"@{bot_username}"

        if cleaned.startswith(mention_pattern):
            cleaned = cleaned[len(mention_pattern):].lstrip()

        cleaned = cleaned.replace(mention_pattern, "").strip()

        cleaned = re.sub(r"\s+", " ", cleaned).strip()

        return cleaned

    @staticmethod
    def _notification_rich_message(message: Message) -> Optional[str]:
        """
        渠道正文附上宿主详情入口；原有 Rich Message 调用仍保持完整正文语义。
        """
        rich_text = message.channel_text(NotificationChannel.Telegram)
        if not rich_text:
            return message.rich_message
        if message.link:
            # 输入仍是 Markdown，HTML 标签会被 richify 当作普通文本转义。
            link = quote(message.link, safe=":/?#&=%")
            rich_text = f'{rich_text}\n\n[查看详情](<{link}>)'
        return rich_text

    def post_message(self, message: Message, **kwargs: object) -> None:
        """
        向符合通知规则的实例发送文件、语音或文本图文。

        未确认发送的实例统一抛错，供持久派发保留重试意图。
        普通通知保留原会话但不编辑原消息，带按钮或强制回复上下文时由客户端决定编辑或回复。

        :param message: 通知内容及目标；文件优先于语音，其次为普通消息
        :param kwargs: 兼容模块分发的附加参数，当前不使用
        :raises RuntimeError: 一个或多个匹配实例缺失客户端或未确认发送成功
        """
        failures = []
        for conf in self.get_configs().values():
            if not self.check_message(message, conf.name):
                continue
            targets = self.get_notification_targets(message)
            userid = message.userid
            if not userid and targets is not None:
                userid = targets.get("telegram_userid")
                if not userid:
                    logger.warn("用户没有指定 Telegram用户ID，消息无法发送")
                    return
            client = self.get_instance(conf.name)
            result = None
            if client:
                if message.file_path:
                    result = client.send_file(
                        file_path=message.file_path,
                        file_name=message.file_name,
                        title=message.title,
                        text=message.text,
                        userid=str(userid) if userid is not None else None,
                        original_chat_id=message.original_chat_id,
                        parse_mode=message.parse_mode,
                    )
                elif message.voice_path:
                    result = client.send_voice(
                        voice_path=message.voice_path,
                        userid=str(userid) if userid is not None else None,
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
                    result = client.send_msg(
                        title=message.title,
                        text=message.text,
                        image=message.image,
                        userid=str(userid) if userid is not None else None,
                        link=message.link,
                        buttons=message.buttons,
                        force_reply=message.force_reply,
                        original_message_id=int(original_message_id) if original_message_id else None,
                        original_chat_id=message.original_chat_id,
                        disable_web_page_preview=message.disable_web_page_preview,
                        parse_mode=message.parse_mode,
                        rich_message=self._notification_rich_message(message),
                    )
            if not result or not result.get("success"):
                failures.append(conf.name or "Telegram")
        if failures:
            raise RuntimeError(f"Telegram 通知发送未确认：{', '.join(failures)}")

    def post_medias_message(
        self, message: Message, medias: List[MediaInfo]
    ) -> None:
        """
        向符合通知规则的实例发送或编辑媒体候选列表。

        :param message: 标题、按钮、接收目标及原消息上下文
        :param medias: 按原顺序展示的媒体候选项
        """
        for conf in self.get_configs().values():
            if not self.check_message(message, conf.name):
                continue
            client = self.get_instance(conf.name)
            if client:
                client.send_medias_msg(
                    title=message.title,
                    medias=medias,
                    userid=self.get_notification_userid(message, "telegram_userid"),
                    link=message.link,
                    buttons=message.buttons,
                    original_message_id=int(message.original_message_id) if message.original_message_id else None,
                    original_chat_id=message.original_chat_id,
                    parse_mode=message.parse_mode,
                )

    def post_torrents_message(
        self, message: Message, torrents: List[Context]
    ) -> None:
        """
        向符合通知规则的实例发送或编辑种子候选列表。

        :param message: 标题、按钮、接收目标及原消息上下文
        :param torrents: 含种子和媒体信息的候选上下文
        """
        for conf in self.get_configs().values():
            if not self.check_message(message, conf.name):
                continue
            client = self.get_instance(conf.name)
            if client:
                client.send_torrents_msg(
                    title=message.title,
                    torrents=torrents,
                    userid=self.get_notification_userid(message, "telegram_userid"),
                    link=message.link,
                    buttons=message.buttons,
                    original_message_id=int(message.original_message_id) if message.original_message_id else None,
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
        通过指定通知实例删除消息。

        :param channel: 目标渠道；非 Telegram 时忽略
        :param source: 通知实例名称
        :param message_id: 目标消息标识
        :param chat_id: 目标会话；为空时使用实例默认会话
        :return: 删除成功返回 True，实例缺失或删除失败返回 False；渠道不匹配返回 None
        """
        if channel != self._channel:
            return None
        success = False
        for conf in self.get_configs().values():
            if source != conf.name:
                continue
            client = self.get_instance(conf.name)
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
        buttons: Optional[List[List[dict[str, Any]]]] = None,
        metadata: Optional[dict[str, Any]] = None,
        parse_mode: Optional[str] = None,
    ) -> Optional[bool]:
        """
        编辑消息。

        :param channel: 消息渠道
        :param source: 指定的消息源
        :param message_id: 消息ID
        :param chat_id: 聊天ID
        :param text: 新的消息内容
        :param title: 消息标题
        :param buttons: 新的按钮列表
        :param metadata: telegram_rich_message 为流式富文本；telegram_wait_for_edit_interval 表示等待编辑间隔收尾
        :param parse_mode: Telegram 消息格式类型，默认 MarkdownV2，可传 HTML
        :return: 编辑是否成功
        """
        if channel != self._channel:
            return None
        for conf in self.get_configs().values():
            if source != conf.name:
                continue
            client = self.get_instance(conf.name)
            if client:
                result = client.edit_msg(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                    title=title,
                    buttons=buttons,
                    parse_mode=parse_mode,
                    rich_message=(metadata or {}).get("telegram_rich_message"),
                    wait_for_interval=bool((metadata or {}).get("telegram_wait_for_edit_interval")),
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
    ) -> Optional[dict[str, Any]]:
        """
        启动定期续发的“正在输入”状态任务。

        :param channel: 来源渠道；非 Telegram 时忽略
        :param source: 来源通知实例名称
        :param userid: 无会话时用于解析最近会话或私聊目标的用户标识
        :param message_id: 原消息标识，保存在状态中
        :param chat_id: 显式原会话标识，优先于用户映射
        :param text: 兼容统一接口的正文，当前不使用
        :return: 含输入状态类型的处理状态；不适用、实例缺失或启动失败时返回 None
        """
        if channel != self._channel:
            return None
        client_config = self.get_config(source)
        if not client_config:
            return None
        client = self.get_instance(client_config.name)
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
            status: Optional[dict[str, Any]] = None,
    ) -> Optional[bool]:
        """
        请求停止此前启动的输入状态任务。

        :param channel: 来源渠道；非 Telegram 时忽略
        :param source: 来源通知实例名称
        :param userid: 状态中无用户时使用的用户标识
        :param message_id: 兼容统一接口的原消息标识，当前不使用
        :param chat_id: 状态中无会话时使用的会话标识
        :param status: 可选起始处理状态，优先使用其中的会话和用户
        :return: 找到目标并请求停止返回 True；实例或目标缺失返回 False；渠道不匹配返回 None
        """
        if channel != self._channel:
            return None
        if status:
            chat_id = status.get("chat_id") or chat_id
            userid = status.get("userid") or userid
        client_config = self.get_config(source)
        if not client_config:
            return False
        client = self.get_instance(client_config.name)
        if not client:
            return False
        return client.stop_typing(chat_id=chat_id, userid=userid)

    def send_direct_message(self, message: Message) -> Optional[MessageResponse]:
        """
        发送新消息并返回首个成功实例的消息引用。

        :param message: 待发送通知；语音单独处理，其他内容发送文本或图文，强制回复保留原消息关系
        :return: 成功消息的标识和来源；没有成功结果或指定用户无 Telegram 目标时返回 None
        """
        for conf in self.get_configs().values():
            if not self.check_message(message, conf.name):
                continue
            targets = self.get_notification_targets(message)
            userid = message.userid
            if not userid and targets is not None:
                userid = targets.get("telegram_userid")
                if not userid:
                    logger.warn("用户没有指定 Telegram用户ID，消息无法发送")
                    return None
            client = self.get_instance(conf.name)
            if client:
                if message.voice_path:
                    result = client.send_voice(
                        voice_path=message.voice_path,
                        userid=str(userid) if userid is not None else None,
                        caption=message.voice_caption,
                        original_chat_id=message.original_chat_id,
                        parse_mode=message.parse_mode,
                    )
                else:
                    # 直接发送不编辑旧消息；仅 ForceReply 使用 original_chat_id
                    # 发回原会话，并保留 original_message_id 建立回复关系。
                    original_chat_id = message.original_chat_id if message.force_reply else None
                    original_message_id = message.original_message_id if message.force_reply else None
                    result = client.send_msg(
                        title=message.title,
                        text=message.text,
                        image=message.image,
                        userid=str(userid) if userid is not None else None,
                        link=message.link,
                        force_reply=message.force_reply,
                        original_message_id=int(original_message_id) if original_message_id else None,
                        original_chat_id=original_chat_id,
                        disable_web_page_preview=message.disable_web_page_preview,
                        parse_mode=message.parse_mode,
                        rich_message=self._notification_rich_message(message),
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
        通过指定实例下载文件并编码为 Base64 文本。

        :param file_id: Telegram 文件标识
        :param source: 通知实例名称
        :return: 不含数据地址前缀的 Base64 文本；实例缺失或未下载到内容返回 None
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
        通过指定实例下载文件的原始内容。

        :param file_id: Telegram 文件标识
        :param source: 通知实例名称
        :return: 文件字节；实例缺失或下载失败返回 None
        """
        config = self.get_config(source)
        if not config:
            return None
        client = self.get_instance(config.name)
        if not client:
            return None
        return client.download_file(file_id)
