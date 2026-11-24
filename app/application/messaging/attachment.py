"""
统一通知图片与附件的投递语义，拆分组合消息并显式说明渠道不具备的能力。
"""

from pathlib import Path

import app.application.messaging.channel as channel_platform
from app.schemas.message import Message
from app.schemas.notification import ChannelCapability, ChannelCapabilityManager
from app.schemas.types import NotificationChannel


def notification_parts(message: Message, channel: NotificationChannel, *, provider: str | None = None) -> list[Message]:
    """
    为单个渠道生成互不污染的图片、附件或降级文本。

    :param message: 插件或宿主的统一消息；原对象及其路由范围保持不变
    :param channel: 当前模块渠道，由宿主查询能力，插件无需判断
    :return: 顺序投递的消息；同时有图片和文件时保留二者且说明文字只发送一次
    """
    if message.image and message.file_path:
        parts = [
            message.model_copy(update={"file_path": None, "file_name": None, "voice_path": None}),
            message.model_copy(update={
                "image": None, "poster": None, "native_image": False, "voice_path": None,
                "title": None, "text": None, "rich_content": None, "rich_message": None,
                "link": None, "buttons": None,
            }),
        ]
    else:
        parts = [message]
    try:
        platform = channel_platform.platform_for_channel(channel.value, provider=provider)
    except ValueError:
        if provider is not None:
            raise
        matches = [entry for entry in channel_platform.platform_catalog().providers() if entry.channel == channel.value]
        if matches:
            raise ValueError("分段需要明确连接的平台 owner")
        platform = None
    if platform is not None:
        parts = platform.split_notification(parts)
    adapted = [_channel_part(part, channel) for part in parts]
    if len(adapted) > 1:
        for index, part in enumerate(adapted):
            part.set_attachment_part(index, final=index == len(adapted) - 1)
    return adapted


def _channel_part(message: Message, channel: NotificationChannel) -> Message:
    """
    将不受支持的附件转换为明确提示，不暴露本地路径或伪造下载链接。
    """
    if message.file_path and not ChannelCapabilityManager.supports_capability(channel, ChannelCapability.FILE_SENDING):
        name = Path(message.file_name or message.file_path).name
        hint = f"[附件未发送：{name}；当前渠道不支持本地文件]"
        return message.model_copy(update={
            "file_path": None, "file_name": None, "rich_content": None,
            "text": "\n".join(part for part in (message.text, hint) if part),
        })
    if message.image and message.native_image and not ChannelCapabilityManager.supports_capability(
        channel, ChannelCapability.NATIVE_IMAGES,
    ):
        if message.image.startswith(("http://", "https://")) and ChannelCapabilityManager.supports_capability(
            channel, ChannelCapability.IMAGES,
        ):
            return message.model_copy(update={
                "native_image": False, "link": None,
                "title": message.title or (None if message.text else "图片"),
            })
        hint = (f"图片：{message.image}" if message.image.startswith(("http://", "https://"))
                else "[图片未发送：当前渠道不支持上传原图]")
        return message.model_copy(update={
            "image": None, "native_image": False, "rich_content": None,
            "text": "\n".join(part for part in (message.text, hint) if part),
        })
    return message
