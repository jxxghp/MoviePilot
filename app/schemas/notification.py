"""通知渠道能力与 API 输出模型。"""

from dataclasses import dataclass
from enum import Enum
from typing import Annotated, Dict, Iterable, Optional, Set, Union

from pydantic import BaseModel, BeforeValidator, Field

from app.schemas.types import NotificationChannel


# 渠道在接口、配置与插件之间以枚举对象、枚举取值和枚举成员名三种形式流通，
# 这里按取值优先、成员名兜底建立索引，把三者收敛到同一个内建枚举成员。
_BUILTIN_CHANNELS: Dict[str, NotificationChannel] = {
    item.value: item for item in NotificationChannel
}
for _item in NotificationChannel:
    _BUILTIN_CHANNELS.setdefault(_item.name, _item)

# 渠道取值的联合类型：内建渠道为枚举成员，扩展渠道为其自行声明的标识字符串
ChannelRef = Union[NotificationChannel, str]


def resolve_channel(channel: Optional[ChannelRef]) -> Optional[ChannelRef]:
    """把渠道取值收敛为内建枚举成员，非内建取值保留为渠道标识字符串。

    :param channel: 渠道枚举成员、枚举取值、枚举成员名或扩展渠道标识
    :return: 命中内建渠道时为枚举成员，否则为去空白后的标识字符串；空取值为 ``None``
    """
    if channel is None:
        return None
    if isinstance(channel, NotificationChannel):
        return channel
    identity = str(channel).strip()
    if not identity:
        return None
    return _BUILTIN_CHANNELS.get(identity, identity)


def channel_identity(channel: Optional[ChannelRef]) -> Optional[str]:
    """取渠道的稳定标识，作为落库、能力查表与管理员解析的统一键。

    :param channel: 渠道枚举成员、枚举取值、枚举成员名或扩展渠道标识
    :return: 内建渠道为枚举取值，扩展渠道为其标识字符串；空取值为 ``None``
    """
    resolved = resolve_channel(channel)
    if resolved is None:
        return None
    return resolved.value if isinstance(resolved, NotificationChannel) else resolved


# 传输模型的渠道字段类型：入模型前先把三种表示收敛为统一取值，
# 内建渠道得到枚举成员，扩展渠道保留标识字符串
ChannelField = Annotated[Optional[ChannelRef], BeforeValidator(resolve_channel)]


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

    channel: ChannelRef
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

    # 扩展登记的渠道能力，按渠道标识索引；内建静态表未命中时查此表
    _extension_capabilities: Dict[str, ChannelCapabilities] = {}
    # 渠道标识到登记方的映射，用于按登记方整体撤销其登记
    _extension_owners: Dict[str, str] = {}

    @classmethod
    def register_extension_capabilities(
        cls, owner: str, capabilities: Iterable[ChannelCapabilities]
    ) -> None:
        """按登记方整体替换其登记的渠道能力。

        同一登记方重复调用即为覆盖，传入空集合即为撤销，扩展重载与停用
        走同一入口，不会残留上一次登记。

        :param owner: 登记方标识，通常为扩展 ID
        :param capabilities: 该登记方声明的渠道能力集合
        :return: 无
        """
        for identity, registered_owner in list(cls._extension_owners.items()):
            if registered_owner == owner:
                cls._extension_owners.pop(identity, None)
                cls._extension_capabilities.pop(identity, None)
        for item in capabilities or ():
            identity = channel_identity(item.channel)
            if not identity:
                continue
            cls._extension_capabilities[identity] = item
            cls._extension_owners[identity] = owner

    @classmethod
    def get_capabilities(cls, channel: Optional[ChannelRef]) -> Optional[ChannelCapabilities]:
        """
        获取渠道能力，内建渠道优先，未命中时查扩展登记
        """
        resolved = resolve_channel(channel)
        if resolved is None:
            return None
        if isinstance(resolved, NotificationChannel):
            builtin = cls._capabilities.get(resolved)
            if builtin:
                return builtin
        return cls._extension_capabilities.get(channel_identity(resolved))

    @classmethod
    def supports_capability(
        cls, channel: Optional[ChannelRef], capability: ChannelCapability
    ) -> bool:
        """
        检查渠道是否支持某项能力
        """
        channel_caps = cls.get_capabilities(channel)
        if not channel_caps:
            return False
        return capability in channel_caps.capabilities

    @classmethod
    def supports_buttons(cls, channel: Optional[ChannelRef]) -> bool:
        """
        检查渠道是否支持按钮
        """
        return cls.supports_capability(channel, ChannelCapability.INLINE_BUTTONS)

    @classmethod
    def supports_callbacks(cls, channel: Optional[ChannelRef]) -> bool:
        """
        检查渠道是否支持回调
        """
        return cls.supports_capability(channel, ChannelCapability.CALLBACK_QUERIES)

    @classmethod
    def supports_editing(cls, channel: Optional[ChannelRef]) -> bool:
        """
        检查渠道是否支持消息编辑
        """
        return cls.supports_capability(channel, ChannelCapability.MESSAGE_EDITING)

    @classmethod
    def supports_markdown(cls, channel: Optional[ChannelRef]) -> bool:
        """
        检查渠道是否支持 Markdown。
        """
        return cls.supports_capability(channel, ChannelCapability.MARKDOWN)

    @classmethod
    def supports_deletion(cls, channel: Optional[ChannelRef]) -> bool:
        """
        检查渠道是否支持消息删除
        """
        return cls.supports_capability(channel, ChannelCapability.MESSAGE_DELETION)

    @classmethod
    def get_max_buttons_per_row(cls, channel: Optional[ChannelRef]) -> int:
        """
        获取每行最大按钮数
        """
        channel_caps = cls.get_capabilities(channel)
        return channel_caps.max_buttons_per_row if channel_caps else 2

    @classmethod
    def get_max_button_rows(cls, channel: Optional[ChannelRef]) -> int:
        """
        获取最大按钮行数
        """
        channel_caps = cls.get_capabilities(channel)
        return channel_caps.max_button_rows if channel_caps else 5

    @classmethod
    def get_max_button_text_length(cls, channel: Optional[ChannelRef]) -> int:
        """
        获取按钮文本最大长度
        """
        channel_caps = cls.get_capabilities(channel)
        return channel_caps.max_button_text_length if channel_caps else 20

    @classmethod
    def get_max_message_length(cls, channel: Optional[ChannelRef]) -> int:
        """
        获取单条消息最大长度（0 表示不限制）
        """
        channel_caps = cls.get_capabilities(channel)
        return channel_caps.max_message_length if channel_caps else 0

    @classmethod
    def should_use_fallback(cls, channel: Optional[ChannelRef]) -> bool:
        """
        是否应该使用降级策略
        """
        channel_caps = cls.get_capabilities(channel)
        return channel_caps.fallback_enabled if channel_caps else True
