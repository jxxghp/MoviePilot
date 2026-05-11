from dataclasses import dataclass
from enum import Enum
from typing import Optional, Union, List, Dict, Set, Any

from pydantic import BaseModel, Field, field_validator

from app.schemas.types import ContentType, NotificationType, MessageChannel


class MessageResponse(BaseModel):
    """
    消息发送响应，包含消息ID等信息用于后续编辑
    """

    # 消息ID
    message_id: Optional[Union[str, int]] = None
    # 聊天ID
    chat_id: Optional[Union[str, int]] = None
    # 消息渠道
    channel: Optional[MessageChannel] = None
    # 消息来源
    source: Optional[str] = None
    # 是否发送成功
    success: bool = False


class CommingMessage(BaseModel):
    """
    外来消息
    """

    class MessageImage(BaseModel):
        """
        外来消息图片
        """

        ref: str
        name: Optional[str] = None
        mime_type: Optional[str] = None
        size: Optional[int] = None

        @classmethod
        def from_value(cls, value: Any) -> Optional["CommingMessage.MessageImage"]:
            if value is None:
                return None
            if isinstance(value, cls):
                return value
            if isinstance(value, str):
                return cls(ref=value)
            if isinstance(value, dict):
                ref = (
                    value.get("ref")
                    or value.get("url")
                    or value.get("image_url")
                    or value.get("file_url")
                )
                if not ref:
                    return None
                size = value.get("size")
                try:
                    size = int(size) if size is not None else None
                except (TypeError, ValueError):
                    size = None
                return cls(
                    ref=ref,
                    name=value.get("name") or value.get("filename"),
                    mime_type=value.get("mime_type") or value.get("content_type"),
                    size=size,
                )
            return None

        @classmethod
        def normalize_list(
            cls, values: Optional[Any]
        ) -> Optional[List["CommingMessage.MessageImage"]]:
            if not values:
                return None
            if not isinstance(values, list):
                values = [values]
            normalized = []
            for value in values:
                item = cls.from_value(value)
                if item:
                    normalized.append(item)
            return normalized or None

    class MessageAttachment(BaseModel):
        """
        外来消息附件（非图片/非语音）
        """

        ref: str
        name: Optional[str] = None
        mime_type: Optional[str] = None
        size: Optional[int] = None

    # 用户ID
    userid: Optional[Union[str, int]] = None
    # 用户名称
    username: Optional[Union[str, int]] = None
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
    message_id: Optional[Union[str, int]] = None
    # 聊天ID（用于回调时定位聊天）
    chat_id: Optional[str] = None
    # 完整的回调查询信息（原始数据）
    callback_query: Optional[Dict] = None
    # 图片列表（图片URL或file_id）
    images: Optional[List[MessageImage]] = None
    # 语音/音频引用列表
    audio_refs: Optional[List[str]] = None
    # 文件附件列表
    files: Optional[List[MessageAttachment]] = None

    @field_validator("images", mode="before")
    @classmethod
    def _normalize_images(
        cls, value: Any
    ) -> Optional[List["CommingMessage.MessageImage"]]:
        return cls.MessageImage.normalize_list(value)

    def to_dict(self):
        """
        转换为字典
        """
        items = self.model_dump()
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
    # 语音文件路径
    voice_path: Optional[str] = None
    # 本地文件路径
    file_path: Optional[str] = None
    # 发送时展示的文件名
    file_name: Optional[str] = None
    # 语音消息附带说明文字
    voice_caption: Optional[str] = None
    # 链接
    link: Optional[str] = None
    # 用户ID
    userid: Optional[Union[str, int]] = None
    # 用户名称
    username: Optional[Union[str, int]] = None
    # 时间
    date: Optional[str] = None
    # 消息方向
    action: Optional[int] = 1
    # 消息目标用户ID字典，未指定用户ID时使用
    targets: Optional[dict] = None
    # 按钮列表，格式：[[{"text": "按钮文本", "callback_data": "回调数据", "url": "链接"}]]
    buttons: Optional[List[List[dict]]] = None
    # 原消息ID，用于编辑消息
    original_message_id: Optional[Union[str, int]] = None
    # 原消息的聊天ID，用于编辑消息
    original_chat_id: Optional[str] = None
    # 是否禁用链接预览（仅Telegram支持）
    disable_web_page_preview: Optional[bool] = None

    def to_dict(self):
        """
        转换为字典
        """
        items = self.model_dump()
        for k, v in items.items():
            if isinstance(v, MessageChannel) or isinstance(v, NotificationType):
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
    # QQ开关
    qq: Optional[bool] = False


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
    # 单条消息最大长度（0 表示不限制），用于流式输出时自动分段
    max_message_length: int = 0
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
                ChannelCapability.MESSAGE_DELETION,
                ChannelCapability.CALLBACK_QUERIES,
                ChannelCapability.MARKDOWN,
                ChannelCapability.RICH_TEXT,
                ChannelCapability.IMAGES,
                ChannelCapability.LINKS,
                ChannelCapability.FILE_SENDING,
            },
            max_buttons_per_row=4,
            max_button_rows=10,
            max_button_text_length=30,
            # Telegram 文本消息限制 4096 字符，预留空间给 MarkdownV2 转义和标题
            max_message_length=3500,
        ),
        MessageChannel.Wechat: ChannelCapabilities(
            channel=MessageChannel.Wechat,
            capabilities={
                ChannelCapability.IMAGES,
                ChannelCapability.LINKS,
                ChannelCapability.MENU_COMMANDS,
            },
            fallback_enabled=True,
        ),
        MessageChannel.WechatClawBot: ChannelCapabilities(
            channel=MessageChannel.WechatClawBot,
            capabilities={
                ChannelCapability.MARKDOWN,
                ChannelCapability.IMAGES,
                ChannelCapability.LINKS,
                ChannelCapability.FILE_SENDING,
            },
            max_message_length=2800,
            fallback_enabled=True,
        ),
        MessageChannel.Slack: ChannelCapabilities(
            channel=MessageChannel.Slack,
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
            },
            max_buttons_per_row=3,
            max_button_rows=8,
            max_button_text_length=25,
            # Slack 消息限制 40000 字符，预留空间给格式化
            max_message_length=39000,
            fallback_enabled=True,
        ),
        MessageChannel.Discord: ChannelCapabilities(
            channel=MessageChannel.Discord,
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
            },
            max_buttons_per_row=5,
            max_button_rows=5,
            max_button_text_length=80,
            # Discord 消息限制 2000 字符
            max_message_length=1800,
            fallback_enabled=True,
        ),
        MessageChannel.SynologyChat: ChannelCapabilities(
            channel=MessageChannel.SynologyChat,
            capabilities={
                ChannelCapability.MARKDOWN,
                ChannelCapability.RICH_TEXT,
                ChannelCapability.IMAGES,
                ChannelCapability.LINKS,
            },
            fallback_enabled=True,
        ),
        MessageChannel.VoceChat: ChannelCapabilities(
            channel=MessageChannel.VoceChat,
            capabilities={
                ChannelCapability.MARKDOWN,
                ChannelCapability.RICH_TEXT,
                ChannelCapability.IMAGES,
                ChannelCapability.LINKS,
            },
            fallback_enabled=True,
        ),
        MessageChannel.WebPush: ChannelCapabilities(
            channel=MessageChannel.WebPush,
            capabilities={ChannelCapability.LINKS},
            fallback_enabled=True,
        ),
        MessageChannel.Web: ChannelCapabilities(
            channel=MessageChannel.Web,
            capabilities={
                ChannelCapability.MARKDOWN,
                ChannelCapability.RICH_TEXT,
                ChannelCapability.IMAGES,
                ChannelCapability.LINKS,
            },
            fallback_enabled=True,
        ),
        MessageChannel.QQ: ChannelCapabilities(
            channel=MessageChannel.QQ,
            capabilities={
                ChannelCapability.RICH_TEXT,
                ChannelCapability.IMAGES,
                ChannelCapability.LINKS,
            },
            fallback_enabled=True,
        ),
    }

    @classmethod
    def get_capabilities(cls, channel: MessageChannel) -> Optional[ChannelCapabilities]:
        """
        获取渠道能力
        """
        return cls._capabilities.get(channel)

    @classmethod
    def supports_capability(
        cls, channel: MessageChannel, capability: ChannelCapability
    ) -> bool:
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
    def supports_editing(cls, channel: MessageChannel) -> bool:
        """
        检查渠道是否支持消息编辑
        """
        return cls.supports_capability(channel, ChannelCapability.MESSAGE_EDITING)

    @classmethod
    def supports_markdown(cls, channel: MessageChannel) -> bool:
        """
        检查渠道是否支持 Markdown。
        """
        return cls.supports_capability(channel, ChannelCapability.MARKDOWN)

    @classmethod
    def supports_deletion(cls, channel: MessageChannel) -> bool:
        """
        检查渠道是否支持消息删除
        """
        return cls.supports_capability(channel, ChannelCapability.MESSAGE_DELETION)

    @classmethod
    def get_max_buttons_per_row(cls, channel: MessageChannel) -> int:
        """
        获取每行最大按钮数
        """
        channel_caps = cls.get_capabilities(channel)
        return channel_caps.max_buttons_per_row if channel_caps else 2

    @classmethod
    def get_max_button_rows(cls, channel: MessageChannel) -> int:
        """
        获取最大按钮行数
        """
        channel_caps = cls.get_capabilities(channel)
        return channel_caps.max_button_rows if channel_caps else 5

    @classmethod
    def get_max_button_text_length(cls, channel: MessageChannel) -> int:
        """
        获取按钮文本最大长度
        """
        channel_caps = cls.get_capabilities(channel)
        return channel_caps.max_button_text_length if channel_caps else 20

    @classmethod
    def get_max_message_length(cls, channel: MessageChannel) -> int:
        """
        获取单条消息最大长度（0 表示不限制）
        """
        channel_caps = cls.get_capabilities(channel)
        return channel_caps.max_message_length if channel_caps else 0

    @classmethod
    def should_use_fallback(cls, channel: MessageChannel) -> bool:
        """
        是否应该使用降级策略
        """
        channel_caps = cls.get_capabilities(channel)
        return channel_caps.fallback_enabled if channel_caps else True
