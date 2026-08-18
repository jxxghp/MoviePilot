"""
QQ Bot 通知模块
基于 QQ 开放平台，支持主动消息推送和 Gateway 接收消息
注意：用户/群需曾与机器人交互过才能收到主动消息，且每月有配额限制
"""

import json
from urllib.parse import quote, unquote
from typing import Optional, List, Tuple, Union, Any

from app.domain.context import MediaInfo, Context
from app.runtime.channels import (
    matches_channel_admin,
    register_channel_admin_resolver,
    resolve_config_principal_ids,
)
from app.runtime.log import logger
from app.modules._base import _MessageChannelModuleBase
from app.modules.qqbot.qqbot import QQBot
from app.schemas.message import IncomingMessage
from app.schemas.notification import NotificationChannel
from app.schemas.message import Message
from app.adapters.network.http import RequestUtils


register_channel_admin_resolver(
    NotificationChannel.QQ,
    lambda config: resolve_config_principal_ids(
        config, "QQBOT_ADMINS", "QQ_OPENID"
    ),
)


class QQBotModule(_MessageChannelModuleBase[QQBot]):
    """QQ Bot 通知模块"""

    # 管理员配置键，与渠道 resolver 保持一致
    _admin_config_key = "QQBOT_ADMINS"

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
        super().init_service(service_name=QQBot.__name__.lower(), service_type=QQBot)
        self._channel = NotificationChannel.QQ

    @staticmethod
    def get_name() -> str:
        return "QQ"

    @staticmethod
    def get_subtype() -> NotificationChannel:
        return NotificationChannel.QQ

    @staticmethod
    def get_priority() -> int:
        return 10

    def _commands_enabled(self, config: Optional[dict]) -> bool:
        """
        QQ 机器人客户端未提供命令注册/删除 API，跳过命令注册，
        避免基类默认钩子调用不存在的 client.register_commands。
        """
        return False

    def stop(self) -> None:
        """停止模块"""
        for client in self.get_instances().values():
            try:
                client.stop()
            except Exception as err:
                logger.error(f"停止QQ Bot模块实例失败：{err}")

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        pass

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
        解析 Gateway 转发的 QQ 消息
        body 格式: {"type": "C2C_MESSAGE_CREATE"|"GROUP_AT_MESSAGE_CREATE", "content": "...", "author": {...}, "id": "...", ...}
        """
        client_config = self.get_config(source)
        if not client_config:
            return None
        client: QQBot = self.get_instance(client_config.name)
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
        cls, msg_body: dict
    ) -> Optional[List[IncomingMessage.MessageImage]]:
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

        deduped = []
        for image in images:
            if image.ref not in [item.ref for item in deduped]:
                deduped.append(image)
        return deduped or None

    @classmethod
    def _extract_audio_refs(cls, msg_body: dict) -> Optional[List[str]]:
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
        cls, msg_body: dict
    ) -> Optional[List[IncomingMessage.MessageAttachment]]:
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
        下载QQ音频附件并返回原始字节
        """
        if not file_ref or not file_ref.startswith("qq://file/"):
            return None
        if not self.get_config(source):
            return None
        file_url = unquote(file_ref.replace("qq://file/", "", 1))
        resp = RequestUtils(timeout=30).get_res(file_url)
        if resp and resp.content:
            return resp.content
        return None

    def post_message(self, message: Message, **kwargs) -> None:
        for conf in self.get_configs().values():
            if not self.check_message(message, conf.name):
                continue
            targets = message.targets
            userid = message.userid
            if not userid and targets:
                userid = targets.get("qq_userid") or targets.get("qq_openid")
                if not userid:
                    userid = targets.get("qq_group_openid") or targets.get("qq_group")
                    if userid:
                        userid = f"group:{userid}"
            # 无 userid 且无默认配置时，由 client 向曾发过消息的用户/群广播
            client: QQBot = self.get_instance(conf.name)
            if client:
                client.send_msg(
                    title=message.title,
                    text=message.text,
                    image=message.image,
                    link=message.link,
                    userid=userid,
                    targets=targets,
                )

    def post_medias_message(self, message: Message, medias: List[MediaInfo]) -> None:
        for conf in self.get_configs().values():
            if not self.check_message(message, conf.name):
                continue
            targets = message.targets
            userid = message.userid
            if not userid and targets:
                userid = targets.get("qq_userid") or targets.get("qq_openid")
                if not userid:
                    g = targets.get("qq_group_openid") or targets.get("qq_group")
                    if g:
                        userid = f"group:{g}"
            client: QQBot = self.get_instance(conf.name)
            if client:
                client.send_medias_msg(
                    medias=medias,
                    userid=userid,
                    title=message.title,
                    link=message.link,
                    targets=targets,
                )

    def post_torrents_message(
        self, message: Message, torrents: List[Context]
    ) -> None:
        for conf in self.get_configs().values():
            if not self.check_message(message, conf.name):
                continue
            targets = message.targets
            userid = message.userid
            if not userid and targets:
                userid = targets.get("qq_userid") or targets.get("qq_openid")
                if not userid:
                    g = targets.get("qq_group_openid") or targets.get("qq_group")
                    if g:
                        userid = f"group:{g}"
            client: QQBot = self.get_instance(conf.name)
            if client:
                client.send_torrents_msg(
                    torrents=torrents,
                    userid=userid,
                    title=message.title,
                    link=message.link,
                    targets=targets,
                )
