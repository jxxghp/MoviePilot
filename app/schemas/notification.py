"""
通知渠道能力与 API 输出模型。
"""

from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional, Set

from pydantic import BaseModel, Field

from app.schemas.types import NotificationChannel


class WechatClawBotKnownTarget(BaseModel):
    """
    微信 ClawBot 已知消息目标。
    """

    userid: str = Field(description="用户 ID")
    username: str = Field(description="微信消息目标的显示名称")
    last_active: Optional[int | float] = Field(default=None, description="消息目标最近活跃时间")


class WechatClawBotData(BaseModel):
    """
    微信 ClawBot 登录状态或操作结果。
    """

    success: bool = Field(description="本次微信渠道操作是否成功")
    message: Optional[str] = Field(default=None, description="面向调用方的结果说明")
    connected: Optional[bool] = Field(default=None, description="是否已建立有效授权或连接")
    account_id: Optional[str] = Field(default=None, description="微信机器人账号标识")
    qrcode: Optional[str] = Field(default=None, description="登录二维码内容")
    qrcode_url: Optional[str] = Field(default=None, description="登录二维码图片地址")
    qrcode_status: Optional[str] = Field(default=None, description="登录二维码当前状态")
    qrcode_updated_at: Optional[int | float] = Field(default=None, description="登录二维码更新时间")
    known_targets: list[WechatClawBotKnownTarget] = Field(default_factory=list, description="已知的消息发送目标")
    default_target: Optional[str] = Field(default=None, description="默认消息发送目标")
    base_url: Optional[str] = Field(default=None, description="服务接口的基础地址")


class ChannelCapability(Enum):
    """
    渠道能力枚举。
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


class NotificationTextFormat(Enum):
    """
    通知投递链路实际支持的正文语法，独立于平台其他交互能力。
    """

    # 基础 Markdown 正文
    MARKDOWN = "markdown"
    # 支持富文本扩展的 Markdown 正文
    RICH_MARKDOWN = "rich_markdown"
    # 卡片内部使用的 Markdown 正文
    CARD_MARKDOWN = "card_markdown"
    # Slack 风格的 mrkdwn 正文
    MRKDWN = "mrkdwn"


@dataclass
class ChannelCapabilities:
    """
    通知渠道的交互能力与消息长度限制。

    :param channel: 适用的通知渠道
    :param capabilities: 渠道支持的交互能力集合
    :param max_buttons_per_row: 每行允许的最大按钮数，默认 5
    :param max_button_rows: 允许的最大按钮行数，默认 10
    :param max_button_text_length: 单个按钮允许的最大字符数，默认 30
    :param max_message_length: 单条消息最大字符数；为 0 时不限制，用于流式输出分段
    :param fallback_enabled: 不支持所需能力时是否允许降级，默认允许
    :param notification_text_format: 普通通知使用的富文本语法；为空表示不支持专用富文本
    :param notification_text_bytes: 通知正文的 UTF-8 字节预算；为 0 时由渠道自身控制分段
    :param notification_mode_key: 限制富文本能力的配置键；为空表示不按配置模式限制
    :param notification_mode_value: 配置键必须匹配的值；调用方未传配置时只返回声明的潜在能力
    """

    channel: NotificationChannel
    capabilities: Set[ChannelCapability]
    max_buttons_per_row: int = 5
    max_button_rows: int = 10
    max_button_text_length: int = 30
    max_message_length: int = 0
    fallback_enabled: bool = True
    notification_text_format: Optional[NotificationTextFormat] = None
    notification_text_bytes: int = 0
    notification_mode_key: Optional[str] = None
    notification_mode_value: Optional[str] = None


class ChannelCapabilityManager:
    """
    渠道能力管理器。
    """

    _capabilities: Dict[NotificationChannel, ChannelCapabilities] = {
        NotificationChannel.Telegram: ChannelCapabilities(
            channel=NotificationChannel.Telegram,
            notification_text_format=NotificationTextFormat.RICH_MARKDOWN,
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
            notification_text_format=NotificationTextFormat.MARKDOWN,
            notification_text_bytes=3000,
            notification_mode_key="WECHAT_MODE",
            notification_mode_value="bot",
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
            notification_text_format=NotificationTextFormat.CARD_MARKDOWN,
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
            notification_text_format=NotificationTextFormat.MRKDWN,
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
            notification_text_format=NotificationTextFormat.MARKDOWN,
            notification_text_bytes=3000,
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
            notification_text_format=NotificationTextFormat.MARKDOWN,
            notification_text_bytes=12000,
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
            notification_text_format=NotificationTextFormat.MRKDWN,
            notification_text_bytes=3000,
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
            notification_text_format=NotificationTextFormat.MARKDOWN,
            notification_text_bytes=6000,
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
            notification_text_format=NotificationTextFormat.MARKDOWN,
            notification_text_bytes=1500,
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
        获取渠道能力。

        :param channel: 要查询的通知渠道
        :return: 渠道能力声明；未登记的渠道返回 None
        """
        return cls._capabilities.get(channel)

    @classmethod
    def get_notification_text_format(
        cls, channel: NotificationChannel, config: Optional[dict[str, object]] = None,
    ) -> Optional[NotificationTextFormat]:
        """
        读取通知正文能力；提供实例配置时校验模式，实际服务端权限由发送渠道确认。

        :param channel: 要查询的通知渠道
        :param config: 渠道实例配置；省略时不校验配置模式
        :return: 支持的正文语法；未知渠道或实例模式不匹配时返回 None
        """
        capability = cls.get_capabilities(channel)
        if not capability:
            return None
        if (config is not None and capability.notification_mode_key
                and config.get(capability.notification_mode_key) != capability.notification_mode_value):
            return None
        return capability.notification_text_format

    @classmethod
    def supports_capability(
        cls, channel: NotificationChannel, capability: ChannelCapability
    ) -> bool:
        """
        检查渠道是否支持某项能力。

        :param channel: 要查询的通知渠道
        :param capability: 待检查的能力
        :return: 已登记且声明支持此能力时返回 True，否则返回 False
        """
        channel_caps = cls.get_capabilities(channel)
        if not channel_caps:
            return False
        return capability in channel_caps.capabilities

    @classmethod
    def supports_buttons(cls, channel: NotificationChannel) -> bool:
        """
        检查渠道是否支持按钮。

        :param channel: 要查询的通知渠道
        :return: 渠道是否声明支持该能力，未知渠道返回 False
        """
        return cls.supports_capability(channel, ChannelCapability.INLINE_BUTTONS)

    @classmethod
    def supports_callbacks(cls, channel: NotificationChannel) -> bool:
        """
        检查渠道是否支持回调。

        :param channel: 要查询的通知渠道
        :return: 渠道是否声明支持该能力，未知渠道返回 False
        """
        return cls.supports_capability(channel, ChannelCapability.CALLBACK_QUERIES)

    @classmethod
    def supports_editing(cls, channel: NotificationChannel) -> bool:
        """
        检查渠道是否支持消息编辑。

        :param channel: 要查询的通知渠道
        :return: 渠道是否声明支持该能力，未知渠道返回 False
        """
        return cls.supports_capability(channel, ChannelCapability.MESSAGE_EDITING)

    @classmethod
    def supports_markdown(cls, channel: NotificationChannel) -> bool:
        """
        检查渠道是否支持 Markdown。

        :param channel: 要查询的通知渠道
        :return: 渠道是否声明支持该能力，未知渠道返回 False
        """
        return cls.supports_capability(channel, ChannelCapability.MARKDOWN)

    @classmethod
    def supports_deletion(cls, channel: NotificationChannel) -> bool:
        """
        检查渠道是否支持消息删除。

        :param channel: 要查询的通知渠道
        :return: 渠道是否声明支持该能力，未知渠道返回 False
        """
        return cls.supports_capability(channel, ChannelCapability.MESSAGE_DELETION)

    @classmethod
    def get_max_buttons_per_row(cls, channel: NotificationChannel) -> int:
        """
        获取每行最大按钮数。

        :param channel: 要查询的通知渠道
        :return: 每行最大按钮数；未知渠道回退为 2
        """
        channel_caps = cls.get_capabilities(channel)
        return channel_caps.max_buttons_per_row if channel_caps else 2

    @classmethod
    def get_max_button_rows(cls, channel: NotificationChannel) -> int:
        """
        获取最大按钮行数。

        :param channel: 要查询的通知渠道
        :return: 最大按钮行数；未知渠道回退为 5
        """
        channel_caps = cls.get_capabilities(channel)
        return channel_caps.max_button_rows if channel_caps else 5

    @classmethod
    def get_max_button_text_length(cls, channel: NotificationChannel) -> int:
        """
        获取按钮文本最大长度。

        :param channel: 要查询的通知渠道
        :return: 按钮文本最大字符数；未知渠道回退为 20
        """
        channel_caps = cls.get_capabilities(channel)
        return channel_caps.max_button_text_length if channel_caps else 20

    @classmethod
    def get_max_message_length(cls, channel: NotificationChannel) -> int:
        """
        获取单条消息最大长度（0 表示不限制）。

        :param channel: 要查询的通知渠道
        :return: 单条消息最大字符数；未知渠道或不限制时返回 0
        """
        channel_caps = cls.get_capabilities(channel)
        return channel_caps.max_message_length if channel_caps else 0

    @classmethod
    def should_use_fallback(cls, channel: NotificationChannel) -> bool:
        """
        是否应该使用降级策略。

        :param channel: 要查询的通知渠道
        :return: 是否允许能力降级；未知渠道默认允许
        """
        channel_caps = cls.get_capabilities(channel)
        return channel_caps.fallback_enabled if channel_caps else True
