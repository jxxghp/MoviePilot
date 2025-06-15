from dataclasses import dataclass
from enum import Enum
from typing import Optional, Union, List, Dict, Set

from pydantic import BaseModel, Field

from app.schemas.types import ContentType, NotificationType, MessageChannel


class CommingMessage(BaseModel):
    """
    外来消息
    """
    # 用户ID
    userid: Optional[Union[str, int]] = None
    # 用户名称
    username: Optional[str] = None
    # 消息渠道
    channel: Optional[MessageChannel] = None
    # 来源（渠道名称）
    source: Optional[str] = None
    # 消息体
    text: Optional[str] = None
    # 时间
    date: Optional[str] = None
    # 消息方向
    action: Optional[int] = 0
    # 是否为回调消息
    is_callback: Optional[bool] = False
    # 回调数据
    callback_data: Optional[str] = None
    # 消息ID（用于回调时定位原消息）
    message_id: Optional[int] = None
    # 聊天ID（用于回调时定位聊天）
    chat_id: Optional[str] = None
    # 完整的回调查询信息（原始数据）
    callback_query: Optional[Dict] = None

    def to_dict(self):
        """
        转换为字典
        """
        items = self.dict()
        for k, v in items.items():
            if isinstance(v, MessageChannel):
                items[k] = v.value
        return items


class Notification(BaseModel):
    """
    消息
    """
    # 消息渠道
    channel: Optional[MessageChannel] = None
    # 消息来源
    source: Optional[str] = None
    # 消息类型
    mtype: Optional[NotificationType] = None
    # 内容类型
    ctype: Optional[ContentType] = None
    # 标题
    title: Optional[str] = None
    # 文本内容
    text: Optional[str] = None
    # 图片
    image: Optional[str] = None
    # 链接
    link: Optional[str] = None
    # 用户ID
    userid: Optional[Union[str, int]] = None
    # 用户名称
    username: Optional[str] = None
    # 时间
    date: Optional[str] = None
    # 消息方向
    action: Optional[int] = 1
    # 消息目标用户ID字典，未指定用户ID时使用
    targets: Optional[dict] = None
    # 按钮列表，格式：[[{"text": "按钮文本", "callback_data": "回调数据", "url": "链接"}]]
    buttons: Optional[List[List[dict]]] = None

    def to_dict(self):
        """
        转换为字典
        """
        items = self.dict()
        for k, v in items.items():
            if isinstance(v, MessageChannel) \
                    or isinstance(v, NotificationType):
                items[k] = v.value
        return items


class NotificationSwitch(BaseModel):
    """
    消息开关
    """
    # 消息类型
    mtype: Optional[str] = None
    # 微信开关
    wechat: Optional[bool] = False
    # TG开关
    telegram: Optional[bool] = False
    # Slack开关
    slack: Optional[bool] = False
    # SynologyChat开关
    synologychat: Optional[bool] = False
    # VoceChat开关
    vocechat: Optional[bool] = False
    # WebPush开关
    webpush: Optional[bool] = False


class Subscription(BaseModel):
    """
    客户端消息订阅
    """
    endpoint: Optional[str] = None
    keys: Optional[dict] = Field(default_factory=dict)


class SubscriptionMessage(BaseModel):
    """
    客户端订阅消息体
    """
    title: Optional[str] = None
    body: Optional[str] = None
    icon: Optional[str] = None
    url: Optional[str] = None
    data: Optional[dict] = Field(default_factory=dict)


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
    # 支持回调查询
    CALLBACK_QUERIES = "callback_queries"
    # 支持富文本
    RICH_TEXT = "rich_text"
    # 支持图片
    IMAGES = "images"
    # 支持链接
    LINKS = "links"
    # 支持文件发送
    FILE_SENDING = "file_sending"


@dataclass
class ChannelCapabilities:
    """
    渠道能力配置
    """
    channel: MessageChannel
    capabilities: Set[ChannelCapability]
    max_buttons_per_row: int = 5
    max_button_rows: int = 10
    max_button_text_length: int = 30
    fallback_enabled: bool = True


class ChannelCapabilityManager:
    """
    渠道能力管理器
    """

    _capabilities: Dict[MessageChannel, ChannelCapabilities] = {
        MessageChannel.Telegram: ChannelCapabilities(
            channel=MessageChannel.Telegram,
            capabilities={
                ChannelCapability.INLINE_BUTTONS,
                ChannelCapability.MENU_COMMANDS,
                ChannelCapability.MESSAGE_EDITING,
                ChannelCapability.CALLBACK_QUERIES,
                ChannelCapability.RICH_TEXT,
                ChannelCapability.IMAGES,
                ChannelCapability.LINKS,
                ChannelCapability.FILE_SENDING
            },
            max_buttons_per_row=2,
            max_button_rows=10,
            max_button_text_length=30
        ),
        MessageChannel.Wechat: ChannelCapabilities(
            channel=MessageChannel.Wechat,
            capabilities={
                ChannelCapability.IMAGES,
                ChannelCapability.LINKS,
                ChannelCapability.MENU_COMMANDS
            },
            fallback_enabled=True
        ),
        MessageChannel.Slack: ChannelCapabilities(
            channel=MessageChannel.Slack,
            capabilities={
                ChannelCapability.INLINE_BUTTONS,
                ChannelCapability.CALLBACK_QUERIES,
                ChannelCapability.RICH_TEXT,
                ChannelCapability.IMAGES,
                ChannelCapability.LINKS,
                ChannelCapability.MENU_COMMANDS
            },
            max_buttons_per_row=3,
            max_button_rows=8,
            max_button_text_length=25,
            fallback_enabled=True
        ),
        MessageChannel.SynologyChat: ChannelCapabilities(
            channel=MessageChannel.SynologyChat,
            capabilities={
                ChannelCapability.RICH_TEXT,
                ChannelCapability.IMAGES,
                ChannelCapability.LINKS
            },
            fallback_enabled=True
        ),
        MessageChannel.VoceChat: ChannelCapabilities(
            channel=MessageChannel.VoceChat,
            capabilities={
                ChannelCapability.RICH_TEXT,
                ChannelCapability.IMAGES,
                ChannelCapability.LINKS
            },
            fallback_enabled=True
        ),
        MessageChannel.WebPush: ChannelCapabilities(
            channel=MessageChannel.WebPush,
            capabilities={
                ChannelCapability.LINKS
            },
            fallback_enabled=True
        ),
        MessageChannel.Web: ChannelCapabilities(
            channel=MessageChannel.Web,
            capabilities={
                ChannelCapability.RICH_TEXT,
                ChannelCapability.IMAGES,
                ChannelCapability.LINKS
            },
            fallback_enabled=True
        )
    }

    @classmethod
    def get_capabilities(cls, channel: MessageChannel) -> Optional[ChannelCapabilities]:
        """
        获取渠道能力
        """
        return cls._capabilities.get(channel)

    @classmethod
    def supports_capability(cls, channel: MessageChannel, capability: ChannelCapability) -> bool:
        """
        检查渠道是否支持某项能力
        """
        channel_caps = cls.get_capabilities(channel)
        if not channel_caps:
            return False
        return capability in channel_caps.capabilities

    @classmethod
    def supports_buttons(cls, channel: MessageChannel) -> bool:
        """
        检查渠道是否支持按钮
        """
        return cls.supports_capability(channel, ChannelCapability.INLINE_BUTTONS)

    @classmethod
    def supports_callbacks(cls, channel: MessageChannel) -> bool:
        """
        检查渠道是否支持回调
        """
        return cls.supports_capability(channel, ChannelCapability.CALLBACK_QUERIES)

    @classmethod
    def get_max_buttons_per_row(cls, channel: MessageChannel) -> int:
        """
        获取每行最大按钮数
        """
        channel_caps = cls.get_capabilities(channel)
        return channel_caps.max_buttons_per_row if channel_caps else 5

    @classmethod
    def get_max_button_rows(cls, channel: MessageChannel) -> int:
        """
        获取最大按钮行数
        """
        channel_caps = cls.get_capabilities(channel)
        return channel_caps.max_button_rows if channel_caps else 10

    @classmethod
    def get_max_button_text_length(cls, channel: MessageChannel) -> int:
        """
        获取按钮文本最大长度
        """
        channel_caps = cls.get_capabilities(channel)
        return channel_caps.max_button_text_length if channel_caps else 20

    @classmethod
    def should_use_fallback(cls, channel: MessageChannel) -> bool:
        """
        是否应该使用降级策略
        """
        channel_caps = cls.get_capabilities(channel)
        return channel_caps.fallback_enabled if channel_caps else True
