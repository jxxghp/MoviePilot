"""通知渠道能力与 API 输出模型。"""

from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional, Set

from pydantic import BaseModel, Field

from app.schemas.types import NotificationChannel


class WechatClawBotKnownTarget(BaseModel):
    """微信 ClawBot 已知消息目标。"""

    userid: str
    username: str
    last_active: Optional[int | float] = None


class WechatClawBotData(BaseModel):
    """微信 ClawBot 登录状态或操作结果。"""

    success: bool
    message: Optional[str] = None
    connected: Optional[bool] = None
    account_id: Optional[str] = None
    qrcode: Optional[str] = None
    qrcode_url: Optional[str] = None
    qrcode_status: Optional[str] = None
    qrcode_updated_at: Optional[int | float] = None
    known_targets: list[WechatClawBotKnownTarget] = Field(default_factory=list)
    default_target: Optional[str] = None
    base_url: Optional[str] = None


class ChannelCapability(Enum):
    """
    渠道能力枚举
    """

    # 支持内联按钮
    INLINE_BUTTONS = "inline_buttons"
    # 支持菜单命令
    MENU_COMMANDS = "menu_commands"
    # 支持消息编辑
    MESSAGE_EDITING = "message_editing"
    # 支持消息删除
    MESSAGE_DELETION = "message_deletion"
    # 支持回调查询
    CALLBACK_QUERIES = "callback_queries"
    # 支持富文本
    RICH_TEXT = "rich_text"
    # 支持 Markdown
    MARKDOWN = "markdown"
    # 支持图片
    IMAGES = "images"
    # 支持链接
    LINKS = "links"
    # 支持原生语音输出
    AUDIO_OUTPUT = "audio_output"
    # 支持文件发送
    FILE_SENDING = "file_sending"
    # 支持可收口的消息处理状态提示，如 reaction 或 typing
    PROCESSING_STATUS = "processing_status"


@dataclass
class ChannelCapabilities:
    """
    渠道能力配置
    """

    channel: NotificationChannel
    capabilities: Set[ChannelCapability]
    max_buttons_per_row: int = 5
    max_button_rows: int = 10
    max_button_text_length: int = 30
    # 单条消息最大长度（0 表示不限制），用于流式输出时自动分段
    max_message_length: int = 0
    fallback_enabled: bool = True


class ChannelCapabilityManager:
    """
    渠道能力管理器
    """

    _capabilities: Dict[NotificationChannel, ChannelCapabilities] = {
        NotificationChannel.Telegram: ChannelCapabilities(
            channel=NotificationChannel.Telegram,
            capabilities={
                ChannelCapability.INLINE_BUTTONS,
                ChannelCapability.MENU_COMMANDS,
                ChannelCapability.MESSAGE_EDITING,
                ChannelCapability.MESSAGE_DELETION,
                ChannelCapability.CALLBACK_QUERIES,
                ChannelCapability.MARKDOWN,
                ChannelCapability.RICH_TEXT,
                ChannelCapability.IMAGES,
                ChannelCapability.LINKS,
                ChannelCapability.AUDIO_OUTPUT,
                ChannelCapability.FILE_SENDING,
                ChannelCapability.PROCESSING_STATUS,
            },
            max_buttons_per_row=4,
            max_button_rows=10,
            max_button_text_length=30,
            # Telegram 文本消息限制 4096 字符，预留空间给 MarkdownV2 转义和标题
            max_message_length=3500,
        ),
        NotificationChannel.Wechat: ChannelCapabilities(
            channel=NotificationChannel.Wechat,
            capabilities={
                ChannelCapability.IMAGES,
                ChannelCapability.LINKS,
                ChannelCapability.AUDIO_OUTPUT,
                ChannelCapability.MENU_COMMANDS,
            },
            fallback_enabled=True,
        ),
        NotificationChannel.Feishu: ChannelCapabilities(
            channel=NotificationChannel.Feishu,
            capabilities={
                ChannelCapability.INLINE_BUTTONS,
                ChannelCapability.MESSAGE_EDITING,
                ChannelCapability.CALLBACK_QUERIES,
                ChannelCapability.MARKDOWN,
                ChannelCapability.RICH_TEXT,
                ChannelCapability.IMAGES,
                ChannelCapability.LINKS,
                ChannelCapability.AUDIO_OUTPUT,
                ChannelCapability.FILE_SENDING,
                ChannelCapability.PROCESSING_STATUS,
            },
            max_buttons_per_row=3,
            max_button_rows=8,
            max_button_text_length=20,
            max_message_length=30000,
            fallback_enabled=True,
        ),
        NotificationChannel.WechatClawBot: ChannelCapabilities(
            channel=NotificationChannel.WechatClawBot,
            capabilities={
                ChannelCapability.MARKDOWN,
                ChannelCapability.IMAGES,
                ChannelCapability.LINKS,
                ChannelCapability.FILE_SENDING,
            },
            max_message_length=2800,
            fallback_enabled=True,
        ),
        NotificationChannel.Slack: ChannelCapabilities(
            channel=NotificationChannel.Slack,
            capabilities={
                ChannelCapability.INLINE_BUTTONS,
                ChannelCapability.MESSAGE_EDITING,
                ChannelCapability.MESSAGE_DELETION,
                ChannelCapability.CALLBACK_QUERIES,
                ChannelCapability.MARKDOWN,
                ChannelCapability.RICH_TEXT,
                ChannelCapability.IMAGES,
                ChannelCapability.LINKS,
                ChannelCapability.MENU_COMMANDS,
                ChannelCapability.FILE_SENDING,
                ChannelCapability.PROCESSING_STATUS,
            },
            max_buttons_per_row=3,
            max_button_rows=8,
            max_button_text_length=25,
            # Slack 消息限制 40000 字符，预留空间给格式化
            max_message_length=39000,
            fallback_enabled=True,
        ),
        NotificationChannel.Discord: ChannelCapabilities(
            channel=NotificationChannel.Discord,
            capabilities={
                ChannelCapability.INLINE_BUTTONS,
                ChannelCapability.MESSAGE_EDITING,
                ChannelCapability.MESSAGE_DELETION,
                ChannelCapability.CALLBACK_QUERIES,
                ChannelCapability.MARKDOWN,
                ChannelCapability.RICH_TEXT,
                ChannelCapability.IMAGES,
                ChannelCapability.LINKS,
                ChannelCapability.FILE_SENDING,
                ChannelCapability.PROCESSING_STATUS,
            },
            max_buttons_per_row=5,
            max_button_rows=5,
            max_button_text_length=80,
            # Discord 消息限制 2000 字符
            max_message_length=1800,
            fallback_enabled=True,
        ),
        NotificationChannel.DingTalk: ChannelCapabilities(
            channel=NotificationChannel.DingTalk,
            capabilities={
                ChannelCapability.MARKDOWN,
                ChannelCapability.RICH_TEXT,
                ChannelCapability.IMAGES,
                ChannelCapability.LINKS,
            },
            # 自定义机器人 Markdown 文本上限为 20000 字节，预留标题和图片链接空间。
            max_message_length=18000,
            fallback_enabled=True,
        ),
        NotificationChannel.SynologyChat: ChannelCapabilities(
            channel=NotificationChannel.SynologyChat,
            capabilities={
                ChannelCapability.MARKDOWN,
                ChannelCapability.RICH_TEXT,
                ChannelCapability.IMAGES,
                ChannelCapability.LINKS,
            },
            fallback_enabled=True,
        ),
        NotificationChannel.VoceChat: ChannelCapabilities(
            channel=NotificationChannel.VoceChat,
            capabilities={
                ChannelCapability.MARKDOWN,
                ChannelCapability.RICH_TEXT,
                ChannelCapability.IMAGES,
                ChannelCapability.LINKS,
            },
            fallback_enabled=True,
        ),
        NotificationChannel.WebPush: ChannelCapabilities(
            channel=NotificationChannel.WebPush,
            capabilities={ChannelCapability.LINKS},
            fallback_enabled=True,
        ),
        NotificationChannel.Web: ChannelCapabilities(
            channel=NotificationChannel.Web,
            capabilities={
                ChannelCapability.MARKDOWN,
                ChannelCapability.RICH_TEXT,
                ChannelCapability.IMAGES,
                ChannelCapability.LINKS,
            },
            fallback_enabled=True,
        ),
        NotificationChannel.WebAgent: ChannelCapabilities(
            channel=NotificationChannel.WebAgent,
            capabilities={
                ChannelCapability.INLINE_BUTTONS,
                ChannelCapability.CALLBACK_QUERIES,
                ChannelCapability.MESSAGE_EDITING,
                ChannelCapability.MARKDOWN,
                ChannelCapability.RICH_TEXT,
                ChannelCapability.IMAGES,
                ChannelCapability.LINKS,
                ChannelCapability.AUDIO_OUTPUT,
                ChannelCapability.FILE_SENDING,
            },
            fallback_enabled=False,
        ),
        NotificationChannel.QQ: ChannelCapabilities(
            channel=NotificationChannel.QQ,
            capabilities={
                ChannelCapability.RICH_TEXT,
                ChannelCapability.IMAGES,
                ChannelCapability.LINKS,
                ChannelCapability.INLINE_BUTTONS,
                ChannelCapability.CALLBACK_QUERIES,
            },
            max_buttons_per_row=5,
            max_button_rows=5,
            max_button_text_length=30,
            fallback_enabled=True,
        ),
    }

    @classmethod
    def get_capabilities(cls, channel: NotificationChannel) -> Optional[ChannelCapabilities]:
        """
        获取渠道能力
        """
        return cls._capabilities.get(channel)

    @classmethod
    def supports_capability(
        cls, channel: NotificationChannel, capability: ChannelCapability
    ) -> bool:
        """
        检查渠道是否支持某项能力
        """
        channel_caps = cls.get_capabilities(channel)
        if not channel_caps:
            return False
        return capability in channel_caps.capabilities

    @classmethod
    def supports_buttons(cls, channel: NotificationChannel) -> bool:
        """
        检查渠道是否支持按钮
        """
        return cls.supports_capability(channel, ChannelCapability.INLINE_BUTTONS)

    @classmethod
    def supports_callbacks(cls, channel: NotificationChannel) -> bool:
        """
        检查渠道是否支持回调
        """
        return cls.supports_capability(channel, ChannelCapability.CALLBACK_QUERIES)

    @classmethod
    def supports_editing(cls, channel: NotificationChannel) -> bool:
        """
        检查渠道是否支持消息编辑
        """
        return cls.supports_capability(channel, ChannelCapability.MESSAGE_EDITING)

    @classmethod
    def supports_markdown(cls, channel: NotificationChannel) -> bool:
        """
        检查渠道是否支持 Markdown。
        """
        return cls.supports_capability(channel, ChannelCapability.MARKDOWN)

    @classmethod
    def supports_deletion(cls, channel: NotificationChannel) -> bool:
        """
        检查渠道是否支持消息删除
        """
        return cls.supports_capability(channel, ChannelCapability.MESSAGE_DELETION)

    @classmethod
    def get_max_buttons_per_row(cls, channel: NotificationChannel) -> int:
        """
        获取每行最大按钮数
        """
        channel_caps = cls.get_capabilities(channel)
        return channel_caps.max_buttons_per_row if channel_caps else 2

    @classmethod
    def get_max_button_rows(cls, channel: NotificationChannel) -> int:
        """
        获取最大按钮行数
        """
        channel_caps = cls.get_capabilities(channel)
        return channel_caps.max_button_rows if channel_caps else 5

    @classmethod
    def get_max_button_text_length(cls, channel: NotificationChannel) -> int:
        """
        获取按钮文本最大长度
        """
        channel_caps = cls.get_capabilities(channel)
        return channel_caps.max_button_text_length if channel_caps else 20

    @classmethod
    def get_max_message_length(cls, channel: NotificationChannel) -> int:
        """
        获取单条消息最大长度（0 表示不限制）
        """
        channel_caps = cls.get_capabilities(channel)
        return channel_caps.max_message_length if channel_caps else 0

    @classmethod
    def should_use_fallback(cls, channel: NotificationChannel) -> bool:
        """
        是否应该使用降级策略
        """
        channel_caps = cls.get_capabilities(channel)
        return channel_caps.fallback_enabled if channel_caps else True
