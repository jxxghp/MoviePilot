"""
QQ 通知渠道路由、入站解析与附件下载入口。

主动发送对象与配额由 QQ 平台控制；后台网关接收入站消息。
"""

import json
from typing import Any, List, Optional, Tuple, Union
from urllib.parse import quote, unquote

from app.adapters.network.http import RequestUtils
from app.application.messaging.channel.admin import (
    matches_channel_admin,
    register_channel_admin_resolver,
    resolve_config_principal_ids,
)
from app.domain.context import Context, MediaInfo
from app.modules._base.notification import _MessageChannelModuleBase
from app.modules.qqbot.qqbot import QQBot
from app.runtime.log import logger
from app.schemas.message import IncomingMessage, Message
from app.schemas.types import ModuleType, NotificationChannel

register_channel_admin_resolver(
    NotificationChannel.QQ,
    lambda config: resolve_config_principal_ids(
        config, "QQBOT_ADMINS", "QQ_OPENID"
    ),
)


class QQBotModule(_MessageChannelModuleBase[QQBot]):
    """
    QQ Bot 通知模块。
    """

    # 管理员配置键，与渠道管理员解析器保持一致
    _admin_config_key = "QQBOT_ADMINS"
    _notification_user_target_keys = (
        "qq_userid",
        "qq_openid",
        "qq_group_openid",
        "qq_group",
    )
    _notification_admin_default_config_keys = ("QQ_OPENID", "QQ_GROUP_OPENID")

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
        初始化 QQ Bot 通知服务并设置渠道标识。
        """
        super().init_service(service_name=QQBot.__name__.lower(), service_type=QQBot)
        self._channel = NotificationChannel.QQ

    @staticmethod
    def get_name() -> str:
        """
        返回渠道的显示名称。
        """
        return "QQ"

    @staticmethod
    def get_type() -> ModuleType:
        """
        声明模块属于通知服务。
        """
        return ModuleType.Notification

    @staticmethod
    def get_subtype() -> NotificationChannel:
        """
        返回 QQ 通知渠道标识。
        """
        return NotificationChannel.QQ

    @staticmethod
    def get_priority() -> int:
        """
        返回通知模块的加载优先级。
        """
        return 10

    def _commands_enabled(self, config: Optional[dict[str, Any]]) -> bool:
        """
        QQ 机器人客户端未提供命令注册/删除 API，跳过命令注册，。
        避免基类默认钩子调用不存在的 client.register_commands。
        """
        return False

    def stop(self) -> bool:
        """
        停止全部 QQ Bot 实例，并返回资源是否全部收敛。
        """
        return self._stop_service_instances()

    def init_setting(self) -> Optional[Tuple[str, Union[str, bool]]]:
        """
        保留设置初始化钩子，实例配置由通知服务管理。
        """
        return None

    @staticmethod
    def _send_admin_denied(
            client: Optional[QQBot], userid: Optional[Union[str, int]]
    ) -> None:
        """
        向 QQ 非管理员用户发送命令拒绝提示。
        """
        if client and userid:
            client.send_msg(title="只有管理员才有权限执行此命令", userid=str(userid))

    def message_parser(
        self, source: str, body: Any, form: Any, args: Any
    ) -> Optional[IncomingMessage]:
        """
        解析网关单聊或群聊消息，校验发言人的管理员权限。

        群聊回复路由使用 group:群标识，管理员身份仍按实际发言人判断。

        :param source: 通知配置名称
        :param body: 网关事件字典或编码为 JSON 的字节
        :param form: 兼容统一入口的表单参数，当前不使用
        :param args: 兼容统一入口的查询参数，当前不使用
        :return: 统一消息对象；配置缺失、内容为空、不支持的事件或权限不足时返回 None
        """
        client_config = self.get_config(source)
        if not client_config:
            return None
        client = self.get_instance(client_config.name)
        try:
            if isinstance(body, bytes):
                msg_body = json.loads(body)
            elif isinstance(body, dict):
                msg_body = body
            else:
                return None
        except (json.JSONDecodeError, TypeError) as err:
            logger.debug(f"解析 QQ 消息失败: {err}")
            return None

        msg_type = msg_body.get("type")
        content = (msg_body.get("content") or "").strip()
        images = self._extract_images(msg_body)
        audio_refs = self._extract_audio_refs(msg_body)
        files = self._extract_files(msg_body)
        if not content and not images and not audio_refs and not files:
            return None

        if msg_type == "C2C_MESSAGE_CREATE":
            author = msg_body.get("author", {})
            user_openid = author.get("user_openid", "")
            if not user_openid:
                return None
            if content.startswith("/") and self._should_reject_admin_command(
                    client_config.config, user_openid
            ):
                self._send_admin_denied(client, user_openid)
                return None
            logger.info(
                f"收到 QQ 私聊消息: userid={user_openid}, "
                f"text={(content or '')[:50]}..., images={len(images) if images else 0}, "
                f"audios={len(audio_refs) if audio_refs else 0}, files={len(files) if files else 0}"
            )
            return IncomingMessage(
                channel=NotificationChannel.QQ,
                source=client_config.name,
                userid=user_openid,
                username=user_openid,
                is_channel_admin=matches_channel_admin(
                    NotificationChannel.QQ,
                    client_config.config,
                    user_openid,
                ),
                text=content,
                images=images,
                audio_refs=audio_refs,
                files=files,
            )
        elif msg_type == "GROUP_AT_MESSAGE_CREATE":
            author = msg_body.get("author", {})
            member_openid = author.get("member_openid", "")
            group_openid = msg_body.get("group_openid", "")
            # 群聊用 group:group_openid 作为 userid，便于回复时识别
            userid = f"group:{group_openid}" if group_openid else member_openid
            if content.startswith("/") and self._should_reject_admin_command(
                    client_config.config, member_openid
            ):
                self._send_admin_denied(client, userid)
                return None
            logger.info(
                f"收到 QQ 群消息: group={group_openid}, userid={member_openid}, "
                f"text={(content or '')[:50]}..., images={len(images) if images else 0}, "
                f"audios={len(audio_refs) if audio_refs else 0}, files={len(files) if files else 0}"
            )
            return IncomingMessage(
                channel=NotificationChannel.QQ,
                source=client_config.name,
                userid=userid,
                username=member_openid or group_openid,
                is_channel_admin=matches_channel_admin(
                    NotificationChannel.QQ,
                    client_config.config,
                    member_openid,
                ),
                text=content,
                images=images,
                audio_refs=audio_refs,
                files=files,
            )
        return None

    @classmethod
    def _extract_images(
        cls, msg_body: dict[str, Any]
    ) -> Optional[List[IncomingMessage.MessageImage]]:
        """
        提取 QQ 图片附件与兼容图片字段，按引用去重。
        """
        images: List[IncomingMessage.MessageImage] = []
        attachments = msg_body.get("attachments") or []
        if isinstance(attachments, list):
            for attachment in attachments:
                if not isinstance(attachment, dict):
                    continue
                url = attachment.get("url") or attachment.get("proxy_url")
                if not url:
                    continue
                content_type = (
                    attachment.get("content_type")
                    or attachment.get("mime_type")
                    or ""
                ).lower()
                filename = (
                    attachment.get("filename")
                    or attachment.get("name")
                    or ""
                ).lower()
                if content_type.startswith("image/") or filename.endswith(cls._IMAGE_SUFFIXES):
                    images.append(
                        IncomingMessage.MessageImage(
                            ref=url,
                            name=attachment.get("filename") or attachment.get("name"),
                            mime_type=attachment.get("content_type")
                            or attachment.get("mime_type"),
                            size=attachment.get("size"),
                        )
                    )

        for key in ("image", "image_url", "pic_url"):
            value = msg_body.get(key)
            if isinstance(value, str) and value.startswith("http"):
                images.append(IncomingMessage.MessageImage(ref=value))

        extra_images = msg_body.get("images")
        if isinstance(extra_images, list):
            for item in extra_images:
                if isinstance(item, str) and item.startswith("http"):
                    images.append(IncomingMessage.MessageImage(ref=item))
                elif isinstance(item, dict):
                    url = item.get("url") or item.get("image_url")
                    if isinstance(url, str) and url.startswith("http"):
                        images.append(
                            IncomingMessage.MessageImage(
                                ref=url,
                                name=item.get("name") or item.get("filename"),
                                mime_type=item.get("content_type")
                                or item.get("mime_type"),
                                size=item.get("size"),
                            )
                        )

        deduped: List[IncomingMessage.MessageImage] = []
        for image in images:
            if image.ref not in [item.ref for item in deduped]:
                deduped.append(image)
        return deduped or None

    @classmethod
    def _extract_audio_refs(cls, msg_body: dict[str, Any]) -> Optional[List[str]]:
        """
        提取音频附件，并生成可回读的 QQ 文件引用。
        """
        audio_refs: List[str] = []
        attachments = msg_body.get("attachments") or []
        if isinstance(attachments, list):
            for attachment in attachments:
                if not isinstance(attachment, dict):
                    continue
                url = attachment.get("url") or attachment.get("proxy_url")
                if not url:
                    continue
                content_type = (
                    attachment.get("content_type")
                    or attachment.get("mime_type")
                    or ""
                ).lower()
                filename = (
                    attachment.get("filename")
                    or attachment.get("name")
                    or ""
                ).lower()
                if content_type.startswith("audio/") or filename.endswith(cls._AUDIO_SUFFIXES):
                    audio_refs.append(f"qq://file/{quote(url, safe='')}")

        deduped = []
        for audio_ref in audio_refs:
            if audio_ref not in deduped:
                deduped.append(audio_ref)
        return deduped or None

    @classmethod
    def _extract_files(
        cls, msg_body: dict[str, Any]
    ) -> Optional[List[IncomingMessage.MessageAttachment]]:
        """
        提取图片和音频以外的文件附件，保留文件元数据。
        """
        files: List[IncomingMessage.MessageAttachment] = []
        attachments = msg_body.get("attachments") or []
        if isinstance(attachments, list):
            for attachment in attachments:
                if not isinstance(attachment, dict):
                    continue
                url = attachment.get("url") or attachment.get("proxy_url")
                if not url:
                    continue
                content_type = (
                    attachment.get("content_type")
                    or attachment.get("mime_type")
                    or ""
                ).lower()
                filename = (
                    attachment.get("filename") or attachment.get("name") or ""
                ).lower()
                is_image = content_type.startswith("image/") or filename.endswith(
                    cls._IMAGE_SUFFIXES
                )
                is_audio = content_type.startswith("audio/") or filename.endswith(
                    cls._AUDIO_SUFFIXES
                )
                if is_image or is_audio:
                    continue
                files.append(
                    IncomingMessage.MessageAttachment(
                        ref=f"qq://file/{quote(url, safe='')}",
                        name=attachment.get("filename") or attachment.get("name"),
                        mime_type=attachment.get("content_type")
                        or attachment.get("mime_type"),
                        size=attachment.get("size"),
                    )
                )
        return files or None

    def download_qq_file_bytes(self, file_ref: str, source: str) -> Optional[bytes]:
        """
        下载 QQ 文件引用中的音频或普通附件。

        :param file_ref: qq://file/ 开头的内部文件引用
        :param source: 所属通知配置名称
        :return: 非空文件字节；引用、配置或下载不可用时返回 None
        """
        if not file_ref or not file_ref.startswith("qq://file/"):
            return None
        if not self.get_config(source):
            return None
        file_url = unquote(file_ref.replace("qq://file/", "", 1))
        resp = RequestUtils(timeout=30).get_res(file_url)
        if resp and isinstance(resp.content, bytes) and resp.content:
            return resp.content
        return None

    def post_message(self, message: Message, **kwargs: Any) -> None:
        """
        按通知路由发送 QQ 消息，并保留原正文作为权限不足时的回退。
        """
        for conf in self.get_configs().values():
            if not self.check_message(message, conf.name):
                continue
            targets = self.get_notification_targets(message)
            userid = message.userid
            if not userid and targets:
                userid = targets.get("qq_userid") or targets.get("qq_openid")
                if not userid:
                    userid = targets.get("qq_group_openid") or targets.get("qq_group")
                    if userid:
                        userid = f"group:{userid}"
            # 无 userid 且无默认配置时，由 client 向曾发过消息的用户/群广播
            client = self.get_instance(conf.name)
            if client:
                client.send_msg(
                    title=message.title or "",
                    text=message.text,
                    image=message.image,
                    link=message.link,
                    userid=str(userid) if userid is not None else None,
                    targets=targets,
                    formatted_texts=message.channel_text_chunks(NotificationChannel.QQ),
                )

    def post_medias_message(self, message: Message, medias: List[MediaInfo]) -> None:
        """
        按实例和用户路由发送 QQ 媒体候选列表。
        """
        for conf in self.get_configs().values():
            if not self.check_message(message, conf.name):
                continue
            targets = self.get_notification_targets(message)
            userid = message.userid
            if not userid and targets:
                userid = targets.get("qq_userid") or targets.get("qq_openid")
                if not userid:
                    g = targets.get("qq_group_openid") or targets.get("qq_group")
                    if g:
                        userid = f"group:{g}"
            client = self.get_instance(conf.name)
            if client:
                client.send_medias_msg(
                    medias=medias,
                    userid=str(userid) if userid is not None else None,
                    title=message.title,
                    link=message.link,
                    targets=targets,
                )

    def post_torrents_message(
        self, message: Message, torrents: List[Context]
    ) -> None:
        """
        按实例和用户路由发送 QQ 种子候选列表。
        """
        for conf in self.get_configs().values():
            if not self.check_message(message, conf.name):
                continue
            targets = self.get_notification_targets(message)
            userid = message.userid
            if not userid and targets:
                userid = targets.get("qq_userid") or targets.get("qq_openid")
                if not userid:
                    g = targets.get("qq_group_openid") or targets.get("qq_group")
                    if g:
                        userid = f"group:{g}"
            client = self.get_instance(conf.name)
            if client:
                client.send_torrents_msg(
                    torrents=torrents,
                    userid=str(userid) if userid is not None else None,
                    title=message.title,
                    link=message.link,
                    targets=targets,
                )
