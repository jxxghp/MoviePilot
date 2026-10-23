"""
Slack 通知路由、入站消息解析、附件下载与处理状态反馈。
"""

import json
import re
from typing import Any, Dict, List, Optional, Tuple, Union, cast
from urllib.parse import quote, unquote

from app.application.messaging.channel.admin import (
    matches_channel_admin,
    register_channel_admin_resolver,
    resolve_config_principal_ids,
)
from app.domain.context import Context, MediaInfo
from app.modules._base.notification import _MessageChannelModuleBase
from app.modules.slack.slack import Slack
from app.runtime.log import logger
from app.schemas.event import CommandRegisterEventData
from app.schemas.message import IncomingMessage, Message, MessageResponse
from app.schemas.types import ModuleType, NotificationChannel

register_channel_admin_resolver(
    NotificationChannel.Slack,
    lambda config: resolve_config_principal_ids(config, "SLACK_ADMINS"),
)


class SlackModule(_MessageChannelModuleBase[Slack]):
    """
    处理 Slack 消息收发及通知实例路由。
    """
    # 管理员配置键，与渠道管理员解析器保持一致。
    _admin_config_key = "SLACK_ADMINS"
    _notification_user_target_keys = ("slack_userid",)
    _notification_admin_default_config_keys = ("SLACK_CHANNEL",)
    # 使用“眼睛”表情反馈消息正在处理。
    PROCESSING_REACTION = "eyes"
    _AUDIO_SUFFIXES = (
        ".mp3",
        ".m4a",
        ".wav",
        ".ogg",
        ".oga",
        ".opus",
        ".aac",
        ".amr",
        ".flac",
        ".mpga",
        ".mpeg",
        ".webm",
    )

    def init_module(self) -> None:
        """
        初始化模块。
        """
        super().init_service(service_name=Slack.__name__.lower(), service_type=Slack)
        self._channel = NotificationChannel.Slack

    @staticmethod
    def get_name() -> str:
        """
        返回渠道的显示名称。
        """
        return "Slack"

    @staticmethod
    def get_type() -> ModuleType:
        """
        获取模块类型。
        """
        return ModuleType.Notification

    @staticmethod
    def get_subtype() -> NotificationChannel:
        """
        获取模块子类型。
        """
        return NotificationChannel.Slack

    @staticmethod
    def get_priority() -> int:
        """
        获取模块优先级，数字越小优先级越高，只有同一接口下优先级才生效。
        """
        return 3

    def stop(self) -> bool:
        """
        停止全部 Slack 实例，并返回资源是否全部收敛。
        """
        return self._stop_service_instances()

    def init_setting(self) -> Optional[Tuple[str, Union[str, bool]]]:
        """
        保留设置初始化钩子，实例配置由通知服务管理。
        """
        pass

    @staticmethod
    def _send_admin_denied(client: Optional[Slack], userid: Optional[Union[str, int]]) -> None:
        """
        向 Slack 非管理员用户发送命令拒绝提示。
        """
        if client and userid:
            client.send_msg(title="只有管理员才有权限执行此命令", userid=str(userid))

    def message_parser(
        self, source: str, body: Any, form: Any, args: Any
    ) -> Optional[IncomingMessage]:
        """
        解析 Slack 普通消息、提及、快捷方式、命令和按钮回调。

        命令按实际发言人校验管理员身份。按钮回调保留原消息时间戳和频道以支持编辑。

        :param source: 通知服务配置名称
        :param body: JSON 编码的事件报文，兼容被重复编码为字符串的报文
        :param form: 兼容统一入口的表单参数，当前不使用
        :param args: 兼容统一入口的查询参数，当前不使用
        :return: 统一消息对象；配置缺失、JSON 无效、事件不支持或权限不足时返回 None
        """
        client_config = self.get_config(source)
        if not client_config:
            return None
        client = self.get_instance(client_config.name)
        try:
            msg_json = json.loads(body)
            while isinstance(msg_json, str):
                msg_json = json.loads(msg_json)
        except Exception as err:
            logger.debug(f"解析Slack消息失败：{str(err)}")
            return None
        if not isinstance(msg_json, dict):
            logger.debug(f"Slack消息格式无效：{type(msg_json)}")
            return None
        if msg_json:
            images = None
            audio_refs = None
            files = None
            message_id = None
            chat_id = None
            if msg_json.get("type") == "message":
                userid = msg_json.get("user")
                text = msg_json.get("text")
                username = msg_json.get("user")
                if text and text.startswith("/") and self._should_reject_admin_command(
                        client_config.config, userid
                ):
                    self._send_admin_denied(client, userid)
                    return None
                message_id = msg_json.get("ts")
                chat_id = msg_json.get("channel")
                images = self._extract_images(msg_json)
                audio_refs = self._extract_audio_refs(msg_json)
                files = self._extract_files(msg_json)
            elif msg_json.get("type") == "block_actions":
                user_data = cast(dict[str, Any], msg_json.get("user", {}))
                actions = cast(list[dict[str, Any]], msg_json.get("actions", []))
                userid = user_data.get("id")
                callback_data = actions[0].get("value")
                text = f"CALLBACK:{callback_data}"
                username = user_data.get("name")
                if str(callback_data).strip().startswith("/") and self._should_reject_admin_command(
                        client_config.config, userid
                ):
                    self._send_admin_denied(client, userid)
                    return None

                message_info = msg_json.get("message", {})
                # Slack消息的时间戳作为消息ID
                message_ts = message_info.get("ts")
                channel_data = cast(dict[str, Any], msg_json.get("channel", {}))
                container_data = cast(dict[str, Any], msg_json.get("container", {}))
                channel_id = channel_data.get("id") or container_data.get("channel_id")

                logger.info(
                    f"收到来自 {client_config.name} 的Slack按钮回调："
                    f"userid={userid}, username={username}, callback_data={callback_data}"
                )

                return IncomingMessage(
                    channel=NotificationChannel.Slack,
                    source=client_config.name,
                    userid=userid,
                    username=username,
                    is_channel_admin=matches_channel_admin(
                        NotificationChannel.Slack, client_config.config, userid
                    ),
                    text=text,
                    is_callback=True,
                    callback_data=callback_data,
                    message_id=message_ts,
                    chat_id=channel_id,
                )
            elif msg_json.get("type") == "event_callback":
                userid = msg_json.get("event", {}).get("user")
                text = re.sub(
                    r"<@[0-9A-Z]+>",
                    "",
                    msg_json.get("event", {}).get("text"),
                    flags=re.IGNORECASE,
                ).strip()
                username = ""
                if text and text.startswith("/") and self._should_reject_admin_command(
                        client_config.config, userid
                ):
                    self._send_admin_denied(client, userid)
                    return None
                message_id = msg_json.get("event", {}).get("ts")
                chat_id = msg_json.get("event", {}).get("channel")
                images = self._extract_images(msg_json.get("event", {}))
                audio_refs = self._extract_audio_refs(msg_json.get("event", {}))
                files = self._extract_files(msg_json.get("event", {}))
            elif msg_json.get("type") == "shortcut":
                userid = msg_json.get("user", {}).get("id")
                text = msg_json.get("callback_id")
                username = msg_json.get("user", {}).get("username")
                if text and text.startswith("/") and self._should_reject_admin_command(
                        client_config.config, userid
                ):
                    self._send_admin_denied(client, userid)
                    return None
            elif msg_json.get("command"):
                userid = msg_json.get("user_id")
                text = " ".join(part for part in [msg_json.get("command"), msg_json.get("text")] if part).strip()
                username = msg_json.get("user_name")
                chat_id = msg_json.get("channel_id")
                if self._should_reject_admin_command(client_config.config, userid):
                    self._send_admin_denied(client, userid)
                    return None
            else:
                return None
            logger.info(
                f"收到来自 {client_config.name} 的Slack消息：userid={userid}, username={username}, "
                f"text={text}, images={len(images) if images else 0}, audios={len(audio_refs) if audio_refs else 0}, "
                f"files={len(files) if files else 0}"
            )
            return IncomingMessage(
                channel=NotificationChannel.Slack,
                source=client_config.name,
                userid=userid,
                username=username,
                is_channel_admin=matches_channel_admin(
                    NotificationChannel.Slack, client_config.config, userid
                ),
                text=text,
                message_id=message_id,
                chat_id=chat_id,
                images=images,
                audio_refs=audio_refs,
                files=files,
            )
        return None

    @staticmethod
    def _extract_images(
        msg_json: dict[str, Any],
    ) -> Optional[List[IncomingMessage.MessageImage]]:
        """
        从Slack消息中提取图片URL。
        """
        files = msg_json.get("files", [])
        if not files:
            return None
        images = []
        for file in files:
            file_type = str(file.get("type", "")).lower()
            file_ext = str(file.get("filetype", "")).lower()
            mime_type = str(file.get("mimetype", "")).lower()
            if (
                file_type == "image"
                or file_ext in ("jpg", "jpeg", "png", "gif", "webp", "bmp")
                or mime_type.startswith("image/")
            ):
                url = file.get("url_private") or file.get("url_private_download")
                if url:
                    images.append(
                        IncomingMessage.MessageImage(
                            ref=url,
                            name=file.get("name") or file.get("title"),
                            mime_type=file.get("mimetype"),
                            size=file.get("size"),
                        )
                    )
        return images if images else None

    @classmethod
    def _extract_audio_refs(cls, msg_json: dict[str, Any]) -> Optional[List[str]]:
        """
        从Slack消息中提取音频文件引用。
        """
        files = msg_json.get("files", [])
        if not files:
            return None
        audio_refs = []
        for file in files:
            file_type = str(file.get("type", "")).lower()
            file_ext = f".{str(file.get('filetype', '')).lower().lstrip('.')}"
            mime_type = str(file.get("mimetype", "")).lower()
            if (
                file_type == "audio"
                or mime_type.startswith("audio/")
                or file_ext in cls._AUDIO_SUFFIXES
            ):
                url = file.get("url_private_download") or file.get("url_private")
                if url:
                    audio_refs.append(f"slack://file/{quote(url, safe='')}")
        return audio_refs if audio_refs else None

    @classmethod
    def _extract_files(
        cls, msg_json: dict[str, Any]
    ) -> Optional[List[IncomingMessage.MessageAttachment]]:
        """
        从 Slack 消息中提取非图片/非音频文件。
        """
        files = msg_json.get("files", [])
        if not files:
            return None

        attachments = []
        for file in files:
            file_type = str(file.get("type", "")).lower()
            file_ext = f".{str(file.get('filetype', '')).lower().lstrip('.')}"
            mime_type = str(file.get("mimetype", "")).lower()
            is_image = (
                file_type == "image"
                or file_ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")
                or mime_type.startswith("image/")
            )
            is_audio = (
                file_type == "audio"
                or mime_type.startswith("audio/")
                or file_ext in cls._AUDIO_SUFFIXES
            )
            if is_image or is_audio:
                continue

            url = file.get("url_private_download") or file.get("url_private")
            if not url:
                continue
            attachments.append(
                IncomingMessage.MessageAttachment(
                    ref=f"slack://file/{quote(url, safe='')}",
                    name=file.get("name") or file.get("title"),
                    mime_type=file.get("mimetype"),
                    size=file.get("size"),
                )
            )
        return attachments or None

    def download_slack_file_to_data_url(self, file_url: str, source: str) -> Optional[str]:
        """
        下载 Slack 私有图片并转换为 data URL。

        :param file_url: Slack 私有文件下载地址
        :param source: 所属通知配置名称
        :return: 带 MIME 类型的 Base64 地址；配置、客户端或下载不可用时返回 None
        """
        config = self.get_config(source)
        if not config:
            return None
        client = self.get_instance(config.name)
        if not client:
            return None
        file_data = client.download_file(file_url)
        if file_data:
            import base64

            content, mime_type = file_data
            return f"data:{mime_type};base64,{base64.b64encode(content).decode()}"
        return None

    def download_slack_file_bytes(self, file_ref: str, source: str) -> Optional[bytes]:
        """
        下载 Slack 文件引用并返回原始字节。

        :param file_ref: slack://file/ 开头的内部文件引用
        :param source: 所属通知配置名称
        :return: 非空文件内容；配置、客户端或下载不可用时返回 None
        """
        if not file_ref or not file_ref.startswith("slack://file/"):
            return None
        config = self.get_config(source)
        if not config:
            return None
        client = self.get_instance(config.name)
        if not client:
            return None
        file_url = unquote(file_ref.replace("slack://file/", "", 1))
        file_data = client.download_file(file_url)
        if file_data:
            content, _ = file_data
            return content
        return None

    def post_message(self, message: Message, **kwargs: object) -> None:
        """
        向通过渠道范围筛选的 Slack 配置发送普通通知。

        不汇总客户端发送结果；客户端负责记录发送失败。

        :param message: 提供通知范围、接收人、标题和原消息标识等信息的消息
        :param kwargs: 兼容统一接口的扩展参数，当前不使用
        """
        for conf in self.get_configs().values():
            if not self.check_message(message, conf.name):
                continue
            targets = self.get_notification_targets(message)
            userid = message.userid
            if not userid and targets is not None:
                userid = targets.get("slack_userid")
                if not userid:
                    logger.warn("用户没有指定 Slack用户ID，消息无法发送")
                    return
            client = self.get_instance(conf.name)
            if client:
                userid_text = str(userid) if userid is not None else None
                if message.file_path:
                    client.send_file(
                        file_path=message.file_path,
                        file_name=message.file_name,
                        title=message.title,
                        text=message.text,
                        userid=userid_text,
                    )
                else:
                    client.send_msg(
                        title=message.title,
                        text=message.text,
                        image=message.image,
                        userid=userid_text,
                        link=message.link,
                        buttons=message.buttons,
                        original_message_id=(
                            str(message.original_message_id)
                            if message.original_message_id is not None
                            else None
                        ),
                        original_chat_id=(
                            str(message.original_chat_id)
                            if message.original_chat_id is not None
                            else None
                        ),
                        formatted_text=message.channel_text(NotificationChannel.Slack),
                    )

    def post_medias_message(
        self, message: Message, medias: List[MediaInfo]
    ) -> None:
        """
        向通过渠道范围筛选的 Slack 配置发送媒体候选列表。

        不汇总客户端发送结果；客户端负责记录发送失败。

        :param message: 提供通知范围、接收人、标题和原消息标识等信息的消息
        :param medias: 待展示的媒体列表
        """
        for conf in self.get_configs().values():
            if not self.check_message(message, conf.name):
                continue
            client = self.get_instance(conf.name)
            if client:
                client.send_medias_msg(
                    title=message.title,
                    medias=medias,
                    userid=self.get_notification_userid(message, "slack_userid"),
                    buttons=message.buttons,
                    original_message_id=(
                        str(message.original_message_id)
                        if message.original_message_id is not None
                        else None
                    ),
                    original_chat_id=(
                        str(message.original_chat_id)
                        if message.original_chat_id is not None
                        else None
                    ),
                )

    def post_torrents_message(
        self, message: Message, torrents: List[Context]
    ) -> None:
        """
        向通过渠道范围筛选的 Slack 配置发送种子候选列表。

        不汇总客户端发送结果；客户端负责记录发送失败。

        :param message: 提供通知范围、接收人、标题和原消息标识等信息的消息
        :param torrents: 待展示的种子上下文列表
        """
        for conf in self.get_configs().values():
            if not self.check_message(message, conf.name):
                continue
            client = self.get_instance(conf.name)
            if client:
                client.send_torrents_msg(
                    title=message.title,
                    torrents=torrents,
                    userid=self.get_notification_userid(message, "slack_userid"),
                    buttons=message.buttons,
                    original_message_id=(
                        str(message.original_message_id)
                        if message.original_message_id is not None
                        else None
                    ),
                    original_chat_id=(
                        str(message.original_chat_id)
                        if message.original_chat_id is not None
                        else None
                    ),
                )

    def delete_message(
        self,
        channel: NotificationChannel,
        source: str,
        message_id: str,
        chat_id: Optional[str] = None,
    ) -> Optional[bool]:
        """
        删除消息。

        :param channel: 消息渠道
        :param source: 指定的消息源
        :param message_id: 消息ID（Slack中为时间戳）
        :param chat_id: 聊天ID（频道ID）
        :return: 删除成功返回 True，失败返回 False；非 Slack 渠道返回 None
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
        :param metadata: 兼容统一编辑接口的元数据，当前不使用
        :return: 编辑成功返回 True，失败返回 False；非 Slack 渠道返回 None
        """
        if channel != self._channel:
            return None
        for conf in self.get_configs().values():
            if source != conf.name:
                continue
            client = self.get_instance(conf.name)
            if client:
                result = client.send_msg(
                    title=title or "",
                    text=text,
                    buttons=buttons,
                    original_message_id=str(message_id),
                    original_chat_id=str(chat_id),
                )
                if result and result[0]:
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
        给入站消息添加正在处理的表情反应。

        :param channel: 目标通知渠道；非 Slack 时不处理
        :param source: 通知配置名称
        :param userid: 兼容统一接口的用户标识；开始时随状态保存，不参与频道定位
        :param message_id: Slack 消息时间戳
        :param chat_id: 消息所在频道标识
        :param text: 原始文本；为空或以 CALLBACK: 开头时不添加标记
        :return: 成功时返回可供结束清理使用的状态字典；不适用或失败时返回 None
        """
        if channel != self._channel:
            return None
        if not message_id or not chat_id or not text or str(text).startswith("CALLBACK:"):
            return None
        config = self.get_config(source)
        if not config:
            return None
        client = self.get_instance(config.name)
        if not client:
            return None
        if not client.add_reaction(
                channel=str(chat_id),
                timestamp=str(message_id),
                emoji=self.PROCESSING_REACTION,
        ):
            return None
        return {
            "channel": channel.value,
            "source": source,
            "userid": userid,
            "message_id": str(message_id),
            "chat_id": str(chat_id),
            "metadata": {
                "kind": "reaction",
                "emoji": self.PROCESSING_REACTION,
            },
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
        清除入站消息的正在处理表情反应。

        :param channel: 目标通知渠道；非 Slack 时不处理
        :param source: 通知配置名称
        :param userid: 兼容统一接口的用户标识；开始时随状态保存，不参与频道定位
        :param message_id: Slack 消息时间戳
        :param chat_id: 消息所在频道标识
        :param status: 开始时返回的状态，优先使用其中的消息、频道和表情信息
        :return: 清除成功时返回 True，失败返回 False；非 Slack 渠道返回 None
        """
        if channel != self._channel:
            return None
        metadata = (status or {}).get("metadata") or {}
        target_message_id = (status or {}).get("message_id") or message_id
        target_chat_id = (status or {}).get("chat_id") or chat_id
        emoji = metadata.get("emoji") or self.PROCESSING_REACTION
        if not target_message_id or not target_chat_id:
            return False
        config = self.get_config(source)
        if not config:
            return False
        client = self.get_instance(config.name)
        if not client:
            return False
        return client.remove_reaction(
            channel=str(target_chat_id),
            timestamp=str(target_message_id),
            emoji=str(emoji),
        )

    def send_direct_message(self, message: Message) -> Optional[MessageResponse]:
        """
        向首个成功匹配的 Slack 实例直接发送文件或文本。

        文本使用 Slack 时间戳作为消息标识；文件上传可能仅返回文件标识，频道标识可能为空。

        :param message: 包含接收人、文件或文本内容的通知消息
        :return: 首次发送成功的消息响应；没有可用目标或全部失败时返回 None
        """
        for conf in self.get_configs().values():
            if not self.check_message(message, conf.name):
                continue
            targets = self.get_notification_targets(message)
            userid = message.userid
            if not userid and targets is not None:
                userid = targets.get("slack_userid")
                if not userid:
                    logger.warn("用户没有指定 Slack 用户ID，消息无法发送")
                    return None
            client = self.get_instance(conf.name)
            if client:
                userid_text = str(userid) if userid is not None else None
                if message.file_path:
                    result = client.send_file(
                        file_path=message.file_path,
                        file_name=message.file_name,
                        title=message.title,
                        text=message.text,
                        userid=userid_text,
                    )
                else:
                    result = client.send_msg(
                        title=message.title or "",
                        text=message.text,
                        userid=userid_text,
                        formatted_text=message.channel_text(NotificationChannel.Slack),
                    )
                if result and result[0]:
                    # Slack 使用时间戳作为 message_id，chat_id 是频道ID
                    response_data = result[1]
                    message_id = None
                    channel_id = None
                    if hasattr(response_data, "get"):
                        message_id = response_data.get("ts")
                        channel_id = response_data.get("channel")
                    if not message_id and hasattr(response_data, "data"):
                        files = (response_data.data or {}).get("files") or []
                        if files:
                            message_id = files[0].get("id")
                            shares = (
                                files[0].get("shares", {})
                                .get("private", {})
                            )
                            if shares:
                                channel_id = next(iter(shares.keys()), None)
                    return MessageResponse(
                        message_id=message_id,
                        chat_id=channel_id,
                        channel=NotificationChannel.Slack,
                        source=conf.name,
                        success=True,
                    )
        return None
