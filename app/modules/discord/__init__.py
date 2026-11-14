"""
Discord 通知路由、入站解析、附件下载与输入状态反馈。
"""

import json
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union
from urllib.parse import quote, unquote

from app.adapters.network.http import RequestUtils
from app.application.messaging.channel.admin import (
    matches_channel_admin,
    register_channel_admin_resolver,
    resolve_config_principal_ids,
)
from app.domain.context import Context, MediaInfo
from app.modules._base.notification import _MessageChannelModuleBase
from app.runtime.log import logger
from app.schemas.event import CommandRegisterEventData
from app.schemas.message import IncomingMessage, Message, MessageResponse
from app.schemas.types import ModuleType, NotificationChannel

# SDK 缺失或加载失败时保留其他通知渠道的可用性。
Discord: Any = None
try:
    from app.modules.discord.discord import Discord as _Discord
    Discord = _Discord
except Exception as err:
    logger.error(f"Discord 模块未加载，缺少依赖或初始化错误：{err}")


register_channel_admin_resolver(
    NotificationChannel.Discord,
    lambda config: resolve_config_principal_ids(config, "DISCORD_ADMINS"),
)


class DiscordModule(_MessageChannelModuleBase[Discord]):
    """
    处理 Discord 消息收发、附件和通知路由。
    """
    # 管理员配置键，与渠道管理员解析器保持一致。
    _admin_config_key = "DISCORD_ADMINS"
    _notification_user_target_keys = ("discord_userid",)
    _notification_admin_default_config_keys = ("DISCORD_CHANNEL_ID",)
    _IMAGE_SUFFIXES = (
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".bmp",
        ".tiff",
        ".svg",
    )
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
        if not Discord:
            logger.error("Discord 依赖未就绪（需要安装 discord.py==2.6.4），模块未启动")
            return
        super().init_service(
            service_name=Discord.__name__.lower(), service_type=Discord
        )
        self._channel = NotificationChannel.Discord

    @staticmethod
    def get_name() -> str:
        """
        返回渠道的显示名称。
        """
        return "Discord"

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
        return NotificationChannel.Discord

    @staticmethod
    def get_priority() -> int:
        """
        获取模块优先级，数字越小优先级越高，只有同一接口下优先级才生效。
        """
        return 4

    def stop(self) -> bool:
        """
        停止全部 Discord 实例，并返回资源是否全部收敛。
        """
        return self._stop_service_instances()

    def init_setting(self) -> Optional[Tuple[str, Union[str, bool]]]:
        """
        保留设置初始化钩子，实例配置由通知服务管理。
        """
        return None

    @staticmethod
    def _send_admin_denied(
            client: Optional[Discord],
            userid: Optional[Union[str, int]],
            chat_id: Optional[Union[str, int]] = None,
    ) -> None:
        """
        向 Discord 非管理员用户发送命令拒绝提示。
        """
        if client and userid:
            client.send_msg(
                title="只有管理员才有权限执行此命令",
                userid=str(userid),
                original_chat_id=str(chat_id) if chat_id else None,
            )

    def message_parser(
        self, source: str, body: Any, form: Any, args: Any
    ) -> Optional[IncomingMessage]:
        """
        解析 Discord 入站消息和按钮交互，保留原频道回复信息。

        管理员身份按实际发言人判断；按钮交互保留原消息和频道标识以支持编辑。

        :param source: 通知服务配置名称
        :param body: 客户端编码为 JSON 的消息或交互报文
        :param form: 兼容统一入口的表单参数，当前不使用
        :param args: 兼容统一入口的查询参数，当前不使用
        :return: 统一消息对象；配置缺失、JSON 无效、内容不足或命令权限不足时返回 None
        """
        client_config = self.get_config(source)
        if not client_config:
            return None
        client: Optional[Discord] = self.get_instance(client_config.name)
        try:
            msg_json: dict[str, Any] = json.loads(body)
        except Exception as e:
            logger.debug(f"解析 Discord 消息失败：{str(e)}")
            return None

        if not msg_json:
            return None

        msg_type = msg_json.get("type")
        userid = msg_json.get("userid")
        username = msg_json.get("username")

        if msg_type == "interaction":
            callback_data = msg_json.get("callback_data")
            message_id = msg_json.get("message_id")
            chat_id = msg_json.get("chat_id")
            if callback_data and userid:
                if str(callback_data).strip().startswith("/") and self._should_reject_admin_command(
                        client_config.config, userid
                ):
                    self._send_admin_denied(client, userid, chat_id)
                    return None
                logger.info(
                    f"收到来自 {client_config.name} 的 Discord 按钮回调："
                    f"userid={userid}, username={username}, callback_data={callback_data}"
                )
                return IncomingMessage(
                    channel=NotificationChannel.Discord,
                    source=client_config.name,
                    userid=userid,
                    username=username,
                    is_channel_admin=matches_channel_admin(
                        NotificationChannel.Discord, client_config.config, userid
                    ),
                    text=f"CALLBACK:{callback_data}",
                    is_callback=True,
                    callback_data=callback_data,
                    message_id=message_id,
                    chat_id=str(chat_id) if chat_id else None,
                )
            return None

        if msg_type == "message":
            text = msg_json.get("text")
            chat_id = msg_json.get("chat_id")
            images = self._extract_images(msg_json)
            audio_refs = self._extract_audio_refs(msg_json)
            files = self._extract_files(msg_json)
            if (text or images or audio_refs or files) and userid:
                if text and text.startswith("/") and self._should_reject_admin_command(
                        client_config.config, userid
                ):
                    self._send_admin_denied(client, userid, chat_id)
                    return None
                logger.info(
                    f"收到来自 {client_config.name} 的 Discord 消息："
                    f"userid={userid}, username={username}, text={text}, "
                    f"images={len(images) if images else 0}, audios={len(audio_refs) if audio_refs else 0}, "
                    f"files={len(files) if files else 0}"
                )
                return IncomingMessage(
                    channel=NotificationChannel.Discord,
                    source=client_config.name,
                    userid=userid,
                    username=username,
                    is_channel_admin=matches_channel_admin(
                        NotificationChannel.Discord, client_config.config, userid
                    ),
                    text=text,
                    chat_id=str(chat_id) if chat_id else None,
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
        从Discord消息中提取图片URL。
        """
        attachments = msg_json.get("attachments", [])
        if not attachments:
            return None
        images = []
        for attachment in attachments:
            url = attachment.get("url") or attachment.get("proxy_url")
            if not url:
                continue
            content_type = (attachment.get("content_type") or "").lower()
            filename = (attachment.get("filename") or "").lower()
            if (
                attachment.get("type") == "image"
                or content_type.startswith("image/")
                or filename.endswith(DiscordModule._IMAGE_SUFFIXES)
            ):
                images.append(
                    IncomingMessage.MessageImage(
                        ref=url,
                        name=attachment.get("filename"),
                        mime_type=attachment.get("content_type"),
                        size=attachment.get("size"),
                    )
                )
        return images if images else None

    @classmethod
    def _extract_audio_refs(cls, msg_json: dict[str, Any]) -> Optional[List[str]]:
        """
        从Discord消息中提取音频URL。
        """
        attachments = msg_json.get("attachments", [])
        if not attachments:
            return None
        audio_refs = []
        for attachment in attachments:
            url = attachment.get("url") or attachment.get("proxy_url")
            if not url:
                continue
            content_type = (attachment.get("content_type") or "").lower()
            filename = (attachment.get("filename") or "").lower()
            if content_type.startswith("audio/") or filename.endswith(cls._AUDIO_SUFFIXES):
                audio_refs.append(f"discord://file/{quote(url, safe='')}")
        return audio_refs if audio_refs else None

    @classmethod
    def _extract_files(
        cls, msg_json: dict[str, Any]
    ) -> Optional[List[IncomingMessage.MessageAttachment]]:
        """
        从 Discord 消息中提取非图片/非音频文件。
        """
        attachments = msg_json.get("attachments", [])
        if not attachments:
            return None

        files = []
        for attachment in attachments:
            url = attachment.get("url") or attachment.get("proxy_url")
            if not url:
                continue
            content_type = (attachment.get("content_type") or "").lower()
            filename = (attachment.get("filename") or "").lower()
            is_image = (
                attachment.get("type") == "image"
                or content_type.startswith("image/")
                or filename.endswith(cls._IMAGE_SUFFIXES)
            )
            is_audio = content_type.startswith("audio/") or filename.endswith(
                cls._AUDIO_SUFFIXES
            )
            if is_image or is_audio:
                continue
            files.append(
                IncomingMessage.MessageAttachment(
                    ref=f"discord://file/{quote(url, safe='')}",
                    name=attachment.get("filename"),
                    mime_type=attachment.get("content_type"),
                    size=attachment.get("size"),
                )
            )
        return files or None

    def download_discord_file_bytes(self, file_ref: str, source: str) -> Optional[bytes]:
        """
        下载 Discord 附件引用并返回原始字节。

        :param file_ref: discord://file/ 开头的内部文件引用
        :param source: 所属通知配置名称
        :return: 非空文件内容；引用、配置或下载不可用时返回 None
        """
        if not file_ref or not file_ref.startswith("discord://file/"):
            return None
        if not self.get_config(source):
            return None
        file_url = unquote(file_ref.replace("discord://file/", "", 1))
        resp = RequestUtils(timeout=30).get_res(file_url)
        if resp and resp.content:
            content = resp.content
            return content if isinstance(content, bytes) else None
        return None

    def post_message(self, message: Message, **kwargs: Any) -> None:
        """
        向匹配的 Discord 配置发送普通通知。

        不汇总客户端发送结果；客户端负责记录失败。

        :param message: 提供通知范围、接收人和原消息信息的通知对象
        :param kwargs: 兼容统一接口的扩展参数，当前不使用
        """
        configs = self.get_configs()
        logger.debug(
            f"[Discord] post_message 被调用，message.source={message.source}, "
            f"message.userid={message.userid}, message.channel={message.channel}"
        )
        logger.debug(
            f"[Discord] 当前配置数量: {len(configs)}, 配置名称: {list(configs.keys())}"
        )
        logger.debug(
            f"[Discord] 当前实例数量: {len(self.get_instances())}, 实例名称: {list(self.get_instances().keys())}"
        )

        if not configs:
            logger.debug("[Discord] get_configs() 返回空，没有可用的 Discord 配置")
            return

        for conf in configs.values():
            logger.debug(
                f"[Discord] 检查配置: name={conf.name}, type={conf.type}, enabled={conf.enabled}"
            )
            if not self.check_message(message, conf.name):
                logger.debug(
                    f"[Discord] check_message 返回 False，跳过配置: {conf.name}"
                )
                continue
            logger.debug(f"[Discord] check_message 通过，准备发送到: {conf.name}")
            targets = self.get_notification_targets(message)
            userid = message.userid
            if not userid and targets is not None:
                userid = targets.get("discord_userid")
                if not userid:
                    logger.warn("用户没有指定 Discord 用户ID，消息无法发送")
                    return
            client: Optional[Discord] = self.get_instance(conf.name)
            logger.debug(
                f"[Discord] get_instance('{conf.name}') 返回: {client is not None}"
            )
            if client:
                logger.debug(
                    f"[Discord] 调用 client 发送, userid={userid}, title={message.title[:50] if message.title else None}..."
                )
                if message.file_path:
                    result = client.send_file(
                        file_path=message.file_path,
                        file_name=message.file_name,
                        title=message.title,
                        text=message.text,
                        userid=userid,
                        original_chat_id=message.original_chat_id,
                    )
                else:
                    texts: Sequence[Optional[str]] = message.channel_text_chunks(NotificationChannel.Discord) or [message.text]
                    for text in texts:
                        result = client.send_msg(
                            title=message.title,
                            text=text,
                            image=message.image,
                            userid=userid,
                            link=message.link,
                            buttons=message.buttons,
                            original_message_id=message.original_message_id,
                            original_chat_id=message.original_chat_id,
                            mtype=message.mtype,
                        )
                logger.debug(f"[Discord] send_msg 返回结果: {result}")
            else:
                logger.warning(
                    f"[Discord] 未找到配置 '{conf.name}' 对应的 Discord 客户端实例"
                )

    def post_medias_message(
        self, message: Message, medias: List[MediaInfo]
    ) -> None:
        """
        向匹配的 Discord 配置发送媒体候选列表。

        不汇总客户端发送结果；客户端负责记录失败。

        :param message: 提供通知范围、接收人和原消息信息的通知对象
        :param medias: 待展示的媒体列表
        """
        for conf in self.get_configs().values():
            if not self.check_message(message, conf.name):
                continue
            client: Optional[Discord] = self.get_instance(conf.name)
            if client:
                client.send_medias_msg(
                    title=message.title,
                    medias=medias,
                    userid=self.get_notification_userid(message, "discord_userid"),
                    buttons=message.buttons,
                    original_message_id=message.original_message_id,
                    original_chat_id=message.original_chat_id,
                )

    def post_torrents_message(
        self, message: Message, torrents: List[Context]
    ) -> None:
        """
        向匹配的 Discord 配置发送种子候选列表。

        不汇总客户端发送结果；客户端负责记录失败。

        :param message: 提供通知范围、接收人和原消息信息的通知对象
        :param torrents: 待展示的种子上下文列表
        """
        for conf in self.get_configs().values():
            if not self.check_message(message, conf.name):
                continue
            client: Optional[Discord] = self.get_instance(conf.name)
            if client:
                client.send_torrents_msg(
                    title=message.title,
                    torrents=torrents,
                    userid=self.get_notification_userid(message, "discord_userid"),
                    buttons=message.buttons,
                    original_message_id=message.original_message_id,
                    original_chat_id=message.original_chat_id,
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
        :param message_id: Discord 数字消息标识
        :param chat_id: 聊天ID（频道ID）
        :return: 删除成功返回 True，失败返回 False；非 Discord 渠道返回 None
        """
        if channel != self._channel:
            return None
        success = False
        for conf in self.get_configs().values():
            if source != conf.name:
                continue
            client: Optional[Discord] = self.get_instance(conf.name)
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
        :param metadata: 兼容统一接口的元数据，当前不使用
        :return: 客户端明确成功时为 True；未处理或失败时为 False，非 Discord 渠道为 None
        """
        if channel != self._channel:
            return None
        for conf in self.get_configs().values():
            if source != conf.name:
                continue
            client: Optional[Discord] = self.get_instance(conf.name)
            if client:
                result = client.send_msg(
                    title=title or "",
                    text=text,
                    buttons=buttons,
                    original_message_id=message_id,
                    original_chat_id=str(chat_id),
                )
                if isinstance(result, tuple):
                    if result and result[0]:
                        return True
                elif result:
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
        启动会话正在输入状态。

        :param channel: 目标通知渠道；非 Discord 时不处理
        :param source: 通知配置名称
        :param userid: 用于定位会话的用户标识
        :param message_id: 原消息标识，仅随状态保存，不参与输入状态定位
        :param chat_id: 优先使用的会话频道标识
        :param text: 原消息文本；为空时不启动输入状态
        :return: 成功时返回供结束阶段清理使用的状态字典；不适用或失败时返回 None
        """
        if channel != self._channel:
            return None
        if not text:
            return None
        config = self.get_config(source)
        if not config:
            return None
        client: Optional[Discord] = self.get_instance(config.name)
        if not client:
            return None
        if not client.start_typing(
                userid=str(userid) if userid else None,
                chat_id=str(chat_id) if chat_id else None,
        ):
            return None
        return {
            "channel": channel.value,
            "source": source,
            "userid": userid,
            "message_id": str(message_id) if message_id else None,
            "chat_id": str(chat_id) if chat_id else None,
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
        请求停止会话正在输入状态。

        :param channel: 目标通知渠道；非 Discord 时不处理
        :param source: 通知配置名称
        :param userid: 用于定位会话的用户标识
        :param message_id: 原消息标识，仅随状态保存，不参与输入状态定位
        :param chat_id: 优先使用的会话频道标识
        :param status: 开始阶段返回的状态，优先使用其中的用户与频道
        :return: 找到既有任务并请求停止时返回 True，失败返回 False；非 Discord 渠道返回 None
        """
        if channel != self._channel:
            return None
        target_chat_id = (status or {}).get("chat_id") or chat_id
        target_userid = (status or {}).get("userid") or userid
        config = self.get_config(source)
        if not config:
            return False
        client: Optional[Discord] = self.get_instance(config.name)
        if not client:
            return False
        return bool(client.stop_typing(
            userid=str(target_userid) if target_userid else None,
            chat_id=str(target_chat_id) if target_chat_id else None,
        ))

    def send_direct_message(self, message: Message) -> Optional[MessageResponse]:
        """
        向匹配的 Discord 通知实例直接发送文件或文本。

        :param message: 提供接收对象、文件或文本内容的通知消息
        :return: 首次成功发送的消息响应；无可用接收人或全部失败时返回 None
        """
        for conf in self.get_configs().values():
            if not self.check_message(message, conf.name):
                continue
            targets = self.get_notification_targets(message)
            userid = message.userid
            if not userid and targets is not None:
                userid = targets.get("discord_userid")
                if not userid:
                    logger.warn("用户没有指定 Discord 用户ID，消息无法发送")
                    return None
            client: Optional[Discord] = self.get_instance(conf.name)
            if client:
                if message.file_path:
                    result = client.send_file(
                        file_path=message.file_path,
                        file_name=message.file_name,
                        title=message.title,
                        text=message.text,
                        userid=userid,
                    )
                else:
                    result = client.send_msg(
                        title=message.title or "",
                        text=message.text,
                        userid=userid,
                    )
                if result:
                    success, response_data = (
                        (result[0], result[1])
                        if isinstance(result, tuple)
                        else (result, None)
                    )
                    if success:
                        message_id = None
                        chat_id = None
                        if isinstance(response_data, dict):
                            message_id = response_data.get("message_id")
                            chat_id = response_data.get("chat_id")
                        elif response_data is not None:
                            message_id = str(response_data)
                        return MessageResponse(
                            message_id=str(message_id) if message_id else None,
                            chat_id=str(chat_id) if chat_id else None,
                            channel=NotificationChannel.Discord,
                            source=conf.name,
                            success=True,
                        )
        return None
