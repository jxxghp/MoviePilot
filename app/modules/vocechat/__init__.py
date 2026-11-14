"""
VoceChat 通知渠道的消息解析、管理员校验与附件下载入口。
"""

import json
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union
from urllib.parse import quote, unquote

from app.application.messaging.channel.admin import (
    matches_channel_admin,
    register_channel_admin_resolver,
    resolve_config_principal_ids,
)
from app.domain.context import Context, MediaInfo
from app.modules._base.notification import _MessageChannelModuleBase
from app.modules.vocechat.vocechat import VoceChat
from app.runtime.log import logger
from app.schemas.message import IncomingMessage, Message
from app.schemas.types import ModuleType, NotificationChannel

register_channel_admin_resolver(
    NotificationChannel.VoceChat,
    lambda config: resolve_config_principal_ids(config, "VOCECHAT_ADMINS"),
)


class VoceChatModule(_MessageChannelModuleBase[VoceChat]):
    """
    解析 VoceChat 入站消息并发送渠道通知。
    """

    # 管理员配置键，与渠道管理员解析器保持一致
    _admin_config_key = "VOCECHAT_ADMINS"
    _notification_user_target_keys = ("vocechat_userid",)
    _notification_admin_default_config_keys = ("VOCECHAT_CHANNEL_ID",)
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
        super().init_service(service_name=VoceChat.__name__.lower(),
                             service_type=VoceChat)
        self._channel = NotificationChannel.VoceChat

    @staticmethod
    def get_name() -> str:
        """
        获取模块名称。
        """
        return "VoceChat"

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
        return NotificationChannel.VoceChat

    @staticmethod
    def get_priority() -> int:
        """
        获取模块优先级，数字越小优先级越高，只有同一接口下优先级才生效。
        """
        return 4

    def stop(self) -> None:
        """
        VoceChat 客户端无需额外停止操作。
        """
        pass

    def init_setting(self) -> Optional[Tuple[str, Union[str, bool]]]:
        """
        VoceChat 不提供额外的模块初始化设置。
        """
        return None

    @staticmethod
    def _send_admin_denied(
            client: Optional[VoceChat], userid: Optional[Union[str, int]]
    ) -> None:
        """
        向 VoceChat 非管理员用户发送命令拒绝提示。
        """
        if client and userid:
            client.send_msg(title="只有管理员才有权限执行此命令", userid=str(userid))

    def message_parser(self, source: str, body: Any, form: Any,
                       args: Any) -> Optional[IncomingMessage]:
        """
        将 VoceChat 入站消息转换为统一消息，并校验管理员命令权限。

        接收人标识用于回复路由，管理员身份按实际发送人匹配。

        :param source: 通知服务配置名称
        :param body: 原始 JSON 请求体；Synology Chat 使用 form 字段
        :param form: 群晖回调表单；VoceChat 不使用此参数
        :param args: 兼容统一入口的查询参数，当前不使用
        :return: 有效文本或附件消息；配置缺失、格式无效、被拒绝或解析失败时返回 None
        """
        try:
            client_config = self.get_config(source)
            if not client_config:
                return None
            client = self.get_instance(client_config.name)
            msg_body = json.loads(body)
            msg_type = msg_body.get("detail", {}).get("type")
            if msg_type not in ("normal", "reply"):
                return None
            logger.debug(f"收到VoceChat请求：{msg_body}")
            detail = msg_body.get("detail", {}) or {}
            content_type = detail.get("content_type") or ""
            content = detail.get("content")
            images = self._extract_images(detail)
            audio_refs = self._extract_audio_refs(detail)
            files = self._extract_files(detail)
            text = None
            if content_type in ("text/plain", "text/markdown") and isinstance(content, str):
                text = content
            gid = msg_body.get("target", {}).get("gid")
            from_uid = msg_body.get("from_uid")
            if from_uid is None:
                return None
            actor_userid = f"UID#{from_uid}"
            channel_id = (client_config.config or {}).get("VOCECHAT_CHANNEL_ID")
            if gid and str(gid) == str(channel_id):
                userid = f"GID#{gid}"
            else:
                userid = f"UID#{msg_body.get('from_uid')}"

            if (text or images or audio_refs or files) and userid:
                if text and text.startswith("/") and self._should_reject_admin_command(
                        client_config.config, from_uid, actor_userid
                ):
                    self._send_admin_denied(client, userid)
                    return None
                logger.info(
                    f"收到来自 {client_config.name} 的VoceChat消息："
                    f"userid={userid}, text={text}, images={len(images) if images else 0}, "
                    f"audios={len(audio_refs) if audio_refs else 0}, files={len(files) if files else 0}"
                )
                return IncomingMessage(channel=NotificationChannel.VoceChat, source=client_config.name,
                                       userid=userid, username=userid,
                                       is_channel_admin=matches_channel_admin(
                                           NotificationChannel.VoceChat, client_config.config,
                                           from_uid, actor_userid,
                                       ), text=text or "",
                                       images=images, audio_refs=audio_refs, files=files)
        except Exception as err:
            logger.error(f"VoceChat消息处理发生错误：{str(err)}")
        return None

    @classmethod
    def _extract_images(
        cls, detail: dict[str, Any]
    ) -> Optional[List[IncomingMessage.MessageImage]]:
        """
        从 VoceChat 文件消息中提取图片引用。
        """
        content_type = detail.get("content_type") or ""
        if content_type != "vocechat/file":
            return None
        properties = detail.get("properties") or {}
        mime_type = (
            properties.get("content_type")
            or properties.get("mime_type")
            or properties.get("contentType")
            or ""
        ).lower()
        file_path = (
            properties.get("path")
            or properties.get("file_path")
            or properties.get("storage_path")
            or detail.get("content")
        )
        direct_url = (
            properties.get("url")
            or properties.get("download_url")
            or properties.get("file_url")
        )
        file_name = (
            properties.get("name")
            or properties.get("filename")
            or (str(file_path).rsplit("/", 1)[-1] if file_path else "")
        ).lower()

        is_image = mime_type.startswith("image/") or file_name.endswith(cls._IMAGE_SUFFIXES)
        if not is_image:
            return None
        if isinstance(direct_url, str) and direct_url.startswith("http"):
            return [
                IncomingMessage.MessageImage(
                    ref=direct_url,
                    name=properties.get("name") or properties.get("filename"),
                    mime_type=mime_type or None,
                    size=properties.get("size"),
                )
            ]
        if isinstance(file_path, str) and file_path:
            return [
                IncomingMessage.MessageImage(
                    ref=f"vocechat://file/{quote(file_path, safe='')}",
                    name=properties.get("name") or properties.get("filename"),
                    mime_type=mime_type or None,
                    size=properties.get("size"),
                )
            ]
        return None

    @classmethod
    def _extract_audio_refs(cls, detail: dict[str, Any]) -> Optional[List[str]]:
        """
        从 VoceChat 文件消息中提取音频引用。
        """
        content_type = detail.get("content_type") or ""
        if content_type != "vocechat/file":
            return None
        properties = detail.get("properties") or {}
        mime_type = (
            properties.get("content_type")
            or properties.get("mime_type")
            or properties.get("contentType")
            or ""
        ).lower()
        file_path = (
            properties.get("path")
            or properties.get("file_path")
            or properties.get("storage_path")
            or detail.get("content")
        )
        file_name = (
            properties.get("name")
            or properties.get("filename")
            or (str(file_path).rsplit("/", 1)[-1] if file_path else "")
        ).lower()

        is_audio = mime_type.startswith("audio/") or file_name.endswith(cls._AUDIO_SUFFIXES)
        if not is_audio:
            return None
        if isinstance(file_path, str) and file_path:
            return [f"vocechat://file/{quote(file_path, safe='')}"]
        return None

    @classmethod
    def _extract_files(
        cls, detail: dict[str, Any]
    ) -> Optional[List[IncomingMessage.MessageAttachment]]:
        """
        从 VoceChat 文件消息中提取普通附件。
        """
        content_type = detail.get("content_type") or ""
        if content_type != "vocechat/file":
            return None
        properties = detail.get("properties") or {}
        mime_type = (
            properties.get("content_type")
            or properties.get("mime_type")
            or properties.get("contentType")
            or ""
        ).lower()
        file_path = (
            properties.get("path")
            or properties.get("file_path")
            or properties.get("storage_path")
            or detail.get("content")
        )
        file_name = (
            properties.get("name")
            or properties.get("filename")
            or (str(file_path).rsplit("/", 1)[-1] if file_path else "")
        )
        lowered_name = str(file_name).lower()
        is_image = mime_type.startswith("image/") or lowered_name.endswith(
            cls._IMAGE_SUFFIXES
        )
        is_audio = mime_type.startswith("audio/") or lowered_name.endswith(
            cls._AUDIO_SUFFIXES
        )
        if is_image or is_audio or not isinstance(file_path, str) or not file_path:
            return None
        return [
            IncomingMessage.MessageAttachment(
                ref=f"vocechat://file/{quote(file_path, safe='')}",
                name=file_name,
                mime_type=properties.get("content_type")
                or properties.get("mime_type")
                or properties.get("contentType"),
                size=properties.get("size"),
            )
        ]

    def post_message(self, message: Message, **kwargs: Any) -> None:
        """
        向通过渠道范围筛选的 VoceChat 配置发送普通通知。

        不汇总客户端发送结果；客户端负责记录发送失败。

        :param message: 提供渠道范围、接收人和标题等信息的通知消息
        :param kwargs: 兼容统一通知入口的扩展参数，当前不使用
        """
        for conf in self.get_configs().values():
            if not self.check_message(message, conf.name):
                continue
            userid = self.get_notification_userid(message, "vocechat_userid")
            client = self.get_instance(conf.name)
            if client:
                texts: Sequence[Optional[str]] = message.channel_text_chunks(NotificationChannel.VoceChat) or [message.text]
                for text in texts:
                    client.send_msg(title=message.title, text=text,
                                    image=message.image, userid=userid, link=message.link)

    def post_medias_message(self, message: Message, medias: List[MediaInfo]) -> None:
        """
        向通过渠道范围筛选的 VoceChat 配置发送媒体候选列表。

        不汇总客户端发送结果；客户端负责记录发送失败。

        :param message: 提供渠道范围、接收人和标题等信息的通知消息
        :param medias: 待展示的媒体列表
        """
        for conf in self.get_configs().values():
            if not self.check_message(message, conf.name):
                continue
            client = self.get_instance(conf.name)
            if client:
                userid = self.get_notification_userid(message, "vocechat_userid")
                client.send_msg(title=message.title, userid=userid)
                client.send_medias_msg(title=message.title, medias=medias,
                                       userid=userid, link=message.link)

    def post_torrents_message(self, message: Message, torrents: List[Context]) -> None:
        """
        向通过渠道范围筛选的 VoceChat 配置发送种子候选列表。

        不汇总客户端发送结果；客户端负责记录发送失败。

        :param message: 提供渠道范围、接收人和标题等信息的通知消息
        :param torrents: 待展示的种子上下文列表
        """
        for conf in self.get_configs().values():
            if not self.check_message(message, conf.name):
                continue
            userid = self.get_notification_userid(message, "vocechat_userid")
            client = self.get_instance(conf.name)
            if client:
                client.send_torrents_msg(title=message.title, torrents=torrents,
                                         userid=userid, link=message.link)

    def register_commands(self, commands: Dict[str, dict[str, Any]]) -> None:
        """
        VoceChat 不支持注册命令菜单。
        """
        return None

    def download_vocechat_image_to_data_url(self, image_ref: str, source: str) -> Optional[str]:
        """
        将 VoceChat 图片引用下载并编码为 data URL。

        :param image_ref: vocechat://file/ 开头的内部文件引用
        :param source: 所属通知服务配置名称
        :return: 包含 MIME 类型和 Base64 内容的地址；无法获取文件时返回 None
        """
        if not image_ref or not image_ref.startswith("vocechat://file/"):
            return None
        client_config = self.get_config(source)
        if not client_config:
            return None
        client = self.get_instance(client_config.name)
        if not client:
            return None
        file_path = unquote(image_ref.replace("vocechat://file/", "", 1))
        return client.download_file_to_data_url(file_path)

    def download_vocechat_file_bytes(self, file_ref: str, source: str) -> Optional[bytes]:
        """
        下载 VoceChat 文件引用并返回原始字节。

        :param file_ref: vocechat://file/ 开头的内部文件引用
        :param source: 所属通知服务配置名称
        :return: 非空文件内容；引用、配置或响应不可用时返回 None
        """
        if not file_ref or not file_ref.startswith("vocechat://file/"):
            return None
        client_config = self.get_config(source)
        if not client_config:
            return None
        client = self.get_instance(client_config.name)
        if not client:
            return None
        file_path = unquote(file_ref.replace("vocechat://file/", "", 1))
        file_data = client.download_file(file_path)
        if file_data:
            content, _ = file_data
            return content
        return None
