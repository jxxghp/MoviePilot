"""
QQ Bot 通知模块
基于 QQ 开放平台，支持主动消息推送和 Gateway 接收消息
注意：用户/群需曾与机器人交互过才能收到主动消息，且每月有配额限制
"""

import json
from urllib.parse import quote, unquote
from typing import Optional, List, Tuple, Union, Any

from app.core.context import MediaInfo, Context
from app.log import logger
from app.modules import _ModuleBase, _MessageBase
from app.modules.qqbot.qqbot import QQBot
from app.schemas import CommingMessage, MessageChannel, Notification
from app.schemas.types import ModuleType
from app.utils.http import RequestUtils


class QQBotModule(_ModuleBase, _MessageBase[QQBot]):
    """QQ Bot 通知模块"""

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
        self.stop()
        super().init_service(service_name=QQBot.__name__.lower(), service_type=QQBot)
        self._channel = MessageChannel.QQ

    @staticmethod
    def get_name() -> str:
        return "QQ"

    @staticmethod
    def get_type() -> ModuleType:
        return ModuleType.Notification

    @staticmethod
    def get_subtype() -> MessageChannel:
        return MessageChannel.QQ

    @staticmethod
    def get_priority() -> int:
        return 10

    def stop(self) -> None:
        for client in self.get_instances().values():
            if hasattr(client, "stop"):
                client.stop()

    def test(self) -> Optional[Tuple[bool, str]]:
        if not self.get_instances():
            return None
        for name, client in self.get_instances().items():
            if not client.get_state():
                return False, f"QQ Bot {name} 未就绪"
        return True, ""

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        pass

    def message_parser(
        self, source: str, body: Any, form: Any, args: Any
    ) -> Optional[CommingMessage]:
        """
        解析 Gateway 转发的 QQ 消息
        body 格式: {"type": "C2C_MESSAGE_CREATE"|"GROUP_AT_MESSAGE_CREATE", "content": "...", "author": {...}, "id": "...", ...}
        """
        client_config = self.get_config(source)
        if not client_config:
            return None
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
        if not content and not images and not audio_refs:
            return None

        if msg_type == "C2C_MESSAGE_CREATE":
            author = msg_body.get("author", {})
            user_openid = author.get("user_openid", "")
            if not user_openid:
                return None
            logger.info(
                f"收到 QQ 私聊消息: userid={user_openid}, "
                f"text={(content or '')[:50]}..., images={len(images) if images else 0}, "
                f"audios={len(audio_refs) if audio_refs else 0}"
            )
            return CommingMessage(
                channel=MessageChannel.QQ,
                source=client_config.name,
                userid=user_openid,
                username=user_openid,
                text=content,
                images=images,
                audio_refs=audio_refs,
            )
        elif msg_type == "GROUP_AT_MESSAGE_CREATE":
            author = msg_body.get("author", {})
            member_openid = author.get("member_openid", "")
            group_openid = msg_body.get("group_openid", "")
            # 群聊用 group:group_openid 作为 userid，便于回复时识别
            userid = f"group:{group_openid}" if group_openid else member_openid
            logger.info(
                f"收到 QQ 群消息: group={group_openid}, userid={member_openid}, "
                f"text={(content or '')[:50]}..., images={len(images) if images else 0}, "
                f"audios={len(audio_refs) if audio_refs else 0}"
            )
            return CommingMessage(
                channel=MessageChannel.QQ,
                source=client_config.name,
                userid=userid,
                username=member_openid or group_openid,
                text=content,
                images=images,
                audio_refs=audio_refs,
            )
        return None

    @classmethod
    def _extract_images(cls, msg_body: dict) -> Optional[List[str]]:
        images: List[str] = []
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
                    images.append(url)

        for key in ("image", "image_url", "pic_url"):
            value = msg_body.get(key)
            if isinstance(value, str) and value.startswith("http"):
                images.append(value)

        extra_images = msg_body.get("images")
        if isinstance(extra_images, list):
            for item in extra_images:
                if isinstance(item, str) and item.startswith("http"):
                    images.append(item)
                elif isinstance(item, dict):
                    url = item.get("url") or item.get("image_url")
                    if isinstance(url, str) and url.startswith("http"):
                        images.append(url)

        deduped = []
        for image in images:
            if image not in deduped:
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

    def post_message(self, message: Notification, **kwargs) -> None:
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

    def post_medias_message(self, message: Notification, medias: List[MediaInfo]) -> None:
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
        self, message: Notification, torrents: List[Context]
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
