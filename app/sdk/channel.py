"""
渠道扩展第一版类型门面；契约供宿主清单扩展使用，不提供插件可信入站或装配能力。
"""

from app.application.messaging.channel.platform import ChannelPlatform
from app.domain.authorization import ChannelActor
from app.schemas.channel import (
    CHANNEL_EXTENSION_API_VERSION,
    ChannelConnection,
    ChannelConnectionInfo,
    ChannelPlatformCapabilities,
    InteractionPolicy,
    NotificationRoute,
    NotificationRule,
)
from app.schemas.message import IncomingMessage, Message, MessageAttachmentRead
from app.schemas.types import NotificationChannel

__all__ = [
    "CHANNEL_EXTENSION_API_VERSION",
    "ChannelActor",
    "ChannelConnection",
    "ChannelConnectionInfo",
    "ChannelPlatform",
    "ChannelPlatformCapabilities",
    "IncomingMessage",
    "InteractionPolicy",
    "Message",
    "MessageAttachmentRead",
    "NotificationChannel",
    "NotificationRoute",
    "NotificationRule",
]
