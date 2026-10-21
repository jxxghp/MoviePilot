"""
SynologyChat 通知渠道的消息解析、管理员校验与附件下载入口。
"""

import json
from collections.abc import Buffer, Mapping
from typing import Any, List, Optional, Sequence, SupportsIndex, SupportsInt, Union
from urllib.parse import quote, unquote

from app.adapters.network.http import RequestUtils
from app.application.messaging.channel.admin import (
    matches_channel_admin,
    register_channel_admin_resolver,
    resolve_config_principal_ids,
)
from app.domain.context import Context, MediaInfo
from app.modules._base.notification import _MessageChannelModuleBase
from app.modules.synologychat.synologychat import SynologyChat
from app.runtime.log import logger
from app.schemas.message import IncomingMessage, Message
from app.schemas.types import ModuleType, NotificationChannel

register_channel_admin_resolver(
    NotificationChannel.SynologyChat,
    lambda config: resolve_config_principal_ids(config, "SYNOLOGYCHAT_ADMINS"),
)


class SynologyChatModule(_MessageChannelModuleBase[SynologyChat]):
    """
    负责 Synology Chat 消息收发、附件解析和管理员命令校验。
    """

    # 管理员配置键，与渠道管理员解析器保持一致
    _admin_config_key = "SYNOLOGYCHAT_ADMINS"
    _notification_user_target_keys = ("synologychat_userid",)
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
        super().init_service(service_name=SynologyChat.__name__.lower(),
                             service_type=SynologyChat)
        self._channel = NotificationChannel.SynologyChat

    @staticmethod
    def get_name() -> str:
        """
        获取模块显示名称。
        """
        return "Synology Chat"

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
        return NotificationChannel.SynologyChat

    @staticmethod
    def get_priority() -> int:
        """
        获取模块优先级，数字越小优先级越高，只有同一接口下优先级才生效。
        """
        return 5

    def _commands_enabled(self, config: Optional[dict[str, Any]]) -> bool:
        """
        客户端不提供命令菜单注册或删除 API，跳过基类的菜单注册流程。
        """
        return False

    def stop(self) -> None:
        """
        客户端没有需要在模块停止时释放的后台资源。
        """
        pass

    def init_setting(self) -> None:
        """
        实例由通知服务配置管理，不额外声明环境开关。
        """
        pass

    @staticmethod
    def _send_admin_denied(
            client: Optional[SynologyChat], userid: Optional[Union[str, int]]
    ) -> None:
        """
        向 Synology Chat 非管理员用户发送命令拒绝提示。
        """
        if client and userid:
            client.send_msg(title="只有管理员才有权限执行此命令", userid=str(userid))

    def message_parser(self, source: str, body: Any, form: Any,
                       args: Any) -> Optional[IncomingMessage]:
        """
        将 SynologyChat 入站消息转换为统一消息，并校验管理员命令权限。

        群晖回调令牌必须与配置一致；文本、图片、音频和普通附件均可构成有效消息。

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
            if not client:
                return None
            message: Mapping[str, object] = form
            if not message:
                return None
            token = message.get("token")
            if not isinstance(token, str) or not token or not client.check_token(token):
                return None
            raw_text = message.get("text")
            if raw_text and not isinstance(raw_text, str):
                return None
            text = raw_text if isinstance(raw_text, str) else None
            # 保留 int 原有的字符串、缓冲区与数值转换协议。
            raw_user_id = message.get("user_id")
            if not isinstance(raw_user_id, (str, Buffer, SupportsInt, SupportsIndex)):
                return None
            user_id = int(raw_user_id)
            user_name = message.get("username")
            images = self._extract_images(message)
            audio_refs = self._extract_audio_refs(message)
            files = self._extract_files(message)
            if (text or images or audio_refs or files) and user_id:
                if text and text.startswith("/") and self._should_reject_admin_command(
                        client_config.config, user_id
                ):
                    self._send_admin_denied(client, user_id)
                    return None
                logger.info(
                    f"收到来自 {client_config.name} 的SynologyChat消息："
                    f"userid={user_id}, username={user_name}, text={text}, "
                    f"images={len(images) if images else 0}, audios={len(audio_refs) if audio_refs else 0}, "
                    f"files={len(files) if files else 0}"
                )
                return IncomingMessage(channel=NotificationChannel.SynologyChat, source=client_config.name,
                                       userid=user_id,
                                       username=str(user_name) if user_name is not None else None,
                                       is_channel_admin=matches_channel_admin(
                                           NotificationChannel.SynologyChat,
                                           client_config.config,
                                           user_id,
                                       ), text=text or "",
                                       images=images, audio_refs=audio_refs, files=files)
        except Exception as err:
            logger.debug(f"解析SynologyChat消息失败：{str(err)}")
        return None

    @classmethod
    def _extract_images(
        cls, message: Mapping[str, object]
    ) -> Optional[List[IncomingMessage.MessageImage]]:
        """
        从消息字段和附件中提取并去重图片引用。
        """
        images = []
        for key in ("file_url", "image_url", "pic_url"):
            value = message.get(key)
            if isinstance(value, str) and cls._looks_like_image(value):
                images.append(IncomingMessage.MessageImage(ref=value))

        for key in ("attachments", "files"):
            raw_value = message.get(key)
            if not raw_value:
                continue
            try:
                parsed = json.loads(raw_value) if isinstance(raw_value, str) else raw_value
            except Exception:
                parsed = raw_value
            items = parsed if isinstance(parsed, list) else [parsed]
            for item in items:
                if isinstance(item, str) and cls._looks_like_image(item):
                    images.append(IncomingMessage.MessageImage(ref=item))
                elif isinstance(item, dict):
                    url = item.get("url") or item.get("file_url") or item.get("image_url")
                    if isinstance(url, str) and cls._looks_like_image(url):
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
    def _extract_audio_refs(cls, message: Mapping[str, object]) -> Optional[List[str]]:
        """
        从消息字段和附件中提取并去重音频下载引用。
        """
        audio_refs = []
        for key in ("audio_url", "voice_url", "file_url"):
            value = message.get(key)
            if isinstance(value, str) and cls._looks_like_audio(value):
                audio_refs.append(f"synology://file/{quote(value, safe='')}")

        for key in ("attachments", "files"):
            raw_value = message.get(key)
            if not raw_value:
                continue
            try:
                parsed = json.loads(raw_value) if isinstance(raw_value, str) else raw_value
            except Exception:
                parsed = raw_value
            items = parsed if isinstance(parsed, list) else [parsed]
            for item in items:
                if isinstance(item, str) and cls._looks_like_audio(item):
                    audio_refs.append(f"synology://file/{quote(item, safe='')}")
                elif isinstance(item, dict):
                    url = item.get("url") or item.get("file_url") or item.get("audio_url")
                    if not isinstance(url, str):
                        continue
                    content_type = (
                        item.get("content_type")
                        or item.get("mime_type")
                        or ""
                    ).lower()
                    name = (
                        item.get("name")
                        or item.get("filename")
                        or ""
                    ).lower()
                    if content_type.startswith("audio/") or cls._looks_like_audio(url) or name.endswith(cls._AUDIO_SUFFIXES):
                        audio_refs.append(f"synology://file/{quote(url, safe='')}")

        deduped = []
        for audio_ref in audio_refs:
            if audio_ref not in deduped:
                deduped.append(audio_ref)
        return deduped or None

    @classmethod
    def _looks_like_image(cls, value: str) -> bool:
        """
        判断 HTTP 地址是否包含支持的图片扩展名。
        """
        if not value or not isinstance(value, str):
            return False
        lowered = value.lower()
        return lowered.startswith("http") and any(
            suffix in lowered for suffix in cls._IMAGE_SUFFIXES
        )

    @classmethod
    def _looks_like_audio(cls, value: str) -> bool:
        """
        判断 HTTP 地址是否包含支持的音频扩展名。
        """
        if not value or not isinstance(value, str):
            return False
        lowered = value.lower()
        return lowered.startswith("http") and any(
            suffix in lowered for suffix in cls._AUDIO_SUFFIXES
        )

    @classmethod
    def _extract_files(
        cls, message: Mapping[str, object]
    ) -> Optional[List[IncomingMessage.MessageAttachment]]:
        """
        提取普通附件下载引用，排除已归类的图片和音频。
        """
        files = []
        for key in ("attachments", "files"):
            raw_value = message.get(key)
            if not raw_value:
                continue
            try:
                parsed = json.loads(raw_value) if isinstance(raw_value, str) else raw_value
            except Exception:
                parsed = raw_value
            items = parsed if isinstance(parsed, list) else [parsed]
            for item in items:
                if not isinstance(item, dict):
                    continue
                url = item.get("url") or item.get("file_url") or item.get("download_url")
                if not isinstance(url, str) or not url.startswith("http"):
                    continue
                content_type = (
                    item.get("content_type") or item.get("mime_type") or ""
                ).lower()
                name = (item.get("name") or item.get("filename") or "").lower()
                is_image = content_type.startswith("image/") or name.endswith(
                    cls._IMAGE_SUFFIXES
                ) or cls._looks_like_image(url)
                is_audio = content_type.startswith("audio/") or name.endswith(
                    cls._AUDIO_SUFFIXES
                ) or cls._looks_like_audio(url)
                if is_image or is_audio:
                    continue
                files.append(
                    IncomingMessage.MessageAttachment(
                        ref=f"synology://file/{quote(url, safe='')}",
                        name=item.get("name") or item.get("filename"),
                        mime_type=item.get("content_type") or item.get("mime_type"),
                        size=item.get("size"),
                    )
                )

        deduped = []
        seen_refs = set()
        for file_item in files:
            if file_item.ref in seen_refs:
                continue
            seen_refs.add(file_item.ref)
            deduped.append(file_item)
        return deduped or None

    def download_synologychat_file_bytes(self, file_ref: str, source: str) -> Optional[bytes]:
        """
        下载 SynologyChat 文件引用并返回原始字节。

        :param file_ref: synologychat://file/ 开头的内部文件引用
        :param source: 所属通知服务配置名称
        :return: 非空文件内容；引用、配置或响应不可用时返回 None
        """
        if not file_ref or not file_ref.startswith("synology://file/"):
            return None
        if not self.get_config(source):
            return None
        file_url = unquote(file_ref.replace("synology://file/", "", 1))
        resp = RequestUtils(timeout=30).get_res(file_url)
        if resp and resp.content:
            content: bytes = resp.content
            return content
        return None

    def post_message(self, message: Message, **kwargs: object) -> None:
        """
        向通过渠道范围筛选的 SynologyChat 配置发送普通通知。

        不汇总客户端发送结果；客户端负责记录发送失败。

        :param message: 提供渠道范围、接收人和标题等信息的通知消息
        :param kwargs: 兼容统一通知入口的扩展参数，当前不使用
        """
        for conf in self.get_configs().values():
            if not self.check_message(message, conf.name):
                continue
            client = self.get_instance(conf.name)
            if client:
                texts: Sequence[Optional[str]] = message.channel_text_chunks(NotificationChannel.SynologyChat) or [message.text]
                for text in texts:
                    client.send_msg(title=message.title, text=text,
                                    image=message.image,
                                    userid=self.get_notification_userid(
                                        message, "synologychat_userid"
                                    ),
                                    link=message.link)

    def post_medias_message(self, message: Message, medias: List[MediaInfo]) -> None:
        """
        向通过渠道范围筛选的 SynologyChat 配置发送媒体候选列表。

        不汇总客户端发送结果；客户端负责记录发送失败。

        :param message: 提供渠道范围、接收人和标题等信息的通知消息
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
                    userid=self.get_notification_userid(
                        message, "synologychat_userid"
                    ),
                )

    def post_torrents_message(self, message: Message, torrents: List[Context]) -> None:
        """
        向通过渠道范围筛选的 SynologyChat 配置发送种子候选列表。

        不汇总客户端发送结果；客户端负责记录发送失败。

        :param message: 提供渠道范围、接收人和标题等信息的通知消息
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
                    userid=self.get_notification_userid(
                        message, "synologychat_userid"
                    ),
                    link=message.link,
                )
