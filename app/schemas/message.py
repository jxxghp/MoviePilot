from enum import Enum
from typing import Optional, Union, List, Dict, Any

from pydantic import BaseModel, Field, field_validator

from app.schemas.common import JsonData
from app.schemas.notification import ChannelField
from app.schemas.types import ContentType, MessageType, NotificationChannel


class MessageClearScope(str, Enum):
    """
    通知中心清理范围。
    """

    # 全部消息
    All = "all"
    # 系统消息
    System = "system"
    # 媒体消息
    Media = "media"


class MessageClearBefore(BaseModel):
    """
    通知中心按范围记录的清理时间。
    """

    # 全部消息清理时间
    all: int = 0
    # 系统消息清理时间
    system: int = 0
    # 媒体消息清理时间
    media: int = 0


class MessageResponse(BaseModel):
    """
    消息发送响应，包含消息ID等信息用于后续编辑
    """

    # 消息ID
    message_id: Optional[Union[str, int]] = None
    # 聊天ID
    chat_id: Optional[Union[str, int]] = None
    # 消息渠道
    channel: ChannelField = None
    # 消息来源
    source: Optional[str] = None
    # 渠道自定义上下文（如飞书流式卡片 card_id/element_id/sequence）
    metadata: Optional[Dict[str, JsonData]] = None
    # 是否发送成功
    success: bool = False


class MessageHistoryItem(BaseModel):
    """
    通知历史记录。
    """

    # 消息ID
    id: Optional[int] = None
    # 消息渠道
    channel: Optional[str] = None
    # 消息来源
    source: Optional[str] = None
    # 消息类型
    mtype: Optional[str] = None
    # 标题
    title: Optional[str] = None
    # 文本内容
    text: Optional[str] = None
    # 图片
    image: Optional[str] = None
    # 链接
    link: Optional[str] = None
    # 用户ID
    userid: Optional[str] = None
    # 登记时间
    reg_time: Optional[str] = None
    # 消息方向：0-接收消息，1-发送消息
    action: Optional[int] = None
    # 附件json
    note: Optional[JsonData] = None


class WebMessageItem(MessageHistoryItem):
    """Web 消息历史记录。"""


class MessageClearData(BaseModel):
    """消息中心各范围的清理时间。"""

    clear_before: MessageClearBefore = Field(description="各范围清理时间")


class IncomingMessage(BaseModel):
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
        def from_value(cls, value: Any) -> Optional["IncomingMessage.MessageImage"]:
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
        ) -> Optional[List["IncomingMessage.MessageImage"]]:
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
    # 渠道适配器依据稳定用户 ID、管理员名单及渠道主用户 ID 生成的授权事实
    is_channel_admin: Optional[bool] = None
    # 消息渠道
    channel: ChannelField = None
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
    # 回复目标消息ID（用于 ForceReply 等回复场景）
    reply_to_message_id: Optional[Union[str, int]] = None
    # 完整的回调查询信息（原始数据）
    callback_query: Optional[Dict] = None
    # 图片列表（图片URL或file_id）
    images: Optional[List[MessageImage]] = None
    # 语音/音频引用列表
    audio_refs: Optional[List[str]] = None
    # 文件附件列表
    files: Optional[List[MessageAttachment]] = None
    # 结构化按钮回调数据（优先于 CALLBACK: 文本前缀）
    callback_data: Optional[str] = None

    @field_validator("images", mode="before")
    @classmethod
    def _normalize_images(
            cls, value: Any
    ) -> Optional[List["IncomingMessage.MessageImage"]]:
        return cls.MessageImage.normalize_list(value)

    def to_dict(self):
        """
        转换为字典
        """
        items = self.model_dump()
        for k, v in items.items():
            if isinstance(v, NotificationChannel):
                items[k] = v.value
        return items


class Message(BaseModel):
    """
    消息
    """

    # 消息渠道
    channel: ChannelField = None
    # 消息来源
    source: Optional[str] = None
    # 消息类型
    mtype: Optional[MessageType] = None
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
    # Telegram ForceReply 回复标记
    force_reply: bool = False
    # 原消息ID，用于编辑消息
    original_message_id: Optional[Union[str, int]] = None
    # 原消息的聊天ID，用于编辑消息
    original_chat_id: Optional[str] = None
    # 是否必须按用户身份投递到私聊，禁止回退原会话或最近会话映射
    private_delivery: bool = False
    # 是否禁用链接预览（仅Telegram支持）
    disable_web_page_preview: Optional[bool] = None
    # 消息文本格式；Telegram 支持 MarkdownV2、HTML、plain，飞书直发支持 plain
    parse_mode: Optional[str] = None
    # Telegram Rich Message 完整 Markdown 正文；其他渠道可使用 text 作为回退
    rich_message: Optional[str] = None
    # 是否写入消息历史
    save_history: bool = True

    def to_dict(self):
        """
        转换为字典
        """
        items = self.model_dump()
        for k, v in items.items():
            if isinstance(v, NotificationChannel) or isinstance(v, MessageType):
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
    # 飞书开关
    feishu: Optional[bool] = False
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
    keys: Optional[dict[str, str]] = Field(default_factory=dict)


class SubscriptionMessage(BaseModel):
    """
    客户端订阅消息体
    """

    title: Optional[str] = None
    body: Optional[str] = None
    icon: Optional[str] = None
    url: Optional[str] = None
    data: Optional[dict[str, JsonData]] = Field(default_factory=dict)


class AgentWebChatRequest(BaseModel):
    """
    Web 智能助手对话请求。
    """

    class AgentWebChatFile(BaseModel):
        """
        Web 智能助手输入附件。
        """

        ref: str = Field(..., min_length=1)
        name: Optional[str] = Field(None)
        mime_type: Optional[str] = Field(None)
        size: Optional[int] = Field(None)
        local_path: Optional[str] = Field(None)
        status: Optional[str] = Field(None)

    # 用户本轮输入
    text: str = Field(default="")
    # 展示历史中记录的用户可读文本；为空时使用 text
    display_text: Optional[str] = Field(None)
    # 前端会话标识，相同标识复用同一段 Agent 记忆
    session_id: Optional[str] = Field(None)
    # 图片 URL 或 data URL 列表
    images: Optional[List[str]] = Field(default_factory=list)
    # 语音/音频引用列表
    audio_refs: Optional[List[str]] = Field(default_factory=list)
    # 文件附件列表
    files: Optional[List[AgentWebChatFile]] = Field(default_factory=list)
    # 用户通过按钮选择时的完整选择快照
    choice_selection: Optional[Dict[str, JsonData]] = Field(default=None)
    # WebAgent 按钮回调关联的原消息 ID，用于传统交互原地编辑卡片
    original_message_id: Optional[Union[str, int]] = Field(default=None)
    # WebAgent 按钮回调关联的原聊天 ID，用于传统交互原地编辑卡片
    original_chat_id: Optional[Union[str, int]] = Field(default=None)
    # 是否在展示历史中记录本轮用户消息
    echo_user: bool = Field(default=True)


class AgentWebChoiceRequest(BaseModel):
    """
    Web 智能助手按钮选择请求。
    """

    # 前端会话标识，用于保持与原对话窗口的关联
    session_id: Optional[str] = Field(None)
    # Agent 工具生成的按钮回调数据
    callback_data: str = Field(..., min_length=1)
    # WebAgent 原助手消息 ID，用于传统按钮回调原地编辑
    original_message_id: Optional[Union[str, int]] = Field(default=None)
    # WebAgent 原聊天 ID，用于传统按钮回调原地编辑
    original_chat_id: Optional[Union[str, int]] = Field(default=None)
