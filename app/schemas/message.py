"""
通知、交互消息和推送订阅的数据结构。
"""

from dataclasses import asdict as _asdict
from dataclasses import dataclass as _dataclass
from dataclasses import field as _field
from enum import Enum
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, Field, field_validator
from pydantic import PrivateAttr as _PrivateAttr

from app.schemas.common import JsonData
from app.schemas.notification import ChannelCapabilityManager as _ChannelCapabilityManager
from app.schemas.notification import NotificationTextFormat as _NotificationTextFormat
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

    all: int = Field(default=0, description="全部消息清理时间")
    system: int = Field(default=0, description="系统消息清理时间")
    media: int = Field(default=0, description="媒体消息清理时间")


class MessageResponse(BaseModel):
    """
    消息发送响应，包含消息ID等信息用于后续编辑。
    """

    message_id: Optional[Union[str, int]] = Field(default=None, description="消息ID")
    chat_id: Optional[Union[str, int]] = Field(default=None, description="聊天ID")
    channel: Optional[NotificationChannel] = Field(default=None, description="消息渠道")
    source: Optional[str] = Field(default=None, description="消息来源")
    metadata: Optional[Dict[str, JsonData]] = Field(
        default=None,
        description="渠道自定义上下文（如飞书流式卡片 card_id/element_id/sequence）",
    )
    success: bool = Field(default=False, description="是否发送成功")


class MessageHistoryItem(BaseModel):
    """
    通知历史记录。
    """

    id: Optional[int] = Field(default=None, description="消息ID")
    channel: Optional[str] = Field(default=None, description="消息渠道")
    source: Optional[str] = Field(default=None, description="消息来源")
    mtype: Optional[str] = Field(default=None, description="消息类型")
    title: Optional[str] = Field(default=None, description="标题")
    text: Optional[str] = Field(default=None, description="文本内容")
    image: Optional[str] = Field(default=None, description="图片")
    poster: Optional[str] = Field(default=None, description="独立媒体海报，不使用背景图回退")
    link: Optional[str] = Field(default=None, description="链接")
    userid: Optional[str] = Field(default=None, description="用户ID")
    reg_time: Optional[str] = Field(default=None, description="登记时间")
    action: Optional[int] = Field(default=None, description="消息方向：0-接收消息，1-发送消息")
    note: Optional[JsonData] = Field(default=None, description="附件json")


class WebMessageItem(MessageHistoryItem):
    """
    Web 消息历史记录。
    """


class MessageClearData(BaseModel):
    """
    消息中心各范围的清理时间。
    """

    clear_before: MessageClearBefore = Field(description="各范围清理时间")


class IncomingMessage(BaseModel):
    """
    外来消息。
    """

    class MessageImage(BaseModel):
        """
        外来消息图片。
        """

        ref: str = Field(description="附件的访问地址或渠道文件引用")
        name: Optional[str] = Field(default=None, description="附件文件名")
        mime_type: Optional[str] = Field(default=None, description="附件的 MIME 类型")
        size: Optional[int] = Field(default=None, description="附件大小，单位为字节")

        @classmethod
        def from_value(cls, value: Any) -> Optional["IncomingMessage.MessageImage"]:
            """
            将字符串或字典图片数据归一化为图片模型。

            :param value: 图片引用字符串、图片字典或现有图片对象
            :return: 规范图片对象；没有有效引用或输入类型不支持时返回 None
            """
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
            """
            归一化单项或列表图片输入，忽略无有效引用的条目。

            :param values: 单个图片输入或图片输入列表
            :return: 有效图片列表；输入为空或没有有效条目时返回 None
            """
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
        外来消息附件（非图片/非语音）。
        """

        ref: str = Field(description="附件的访问地址或渠道文件引用")
        name: Optional[str] = Field(default=None, description="附件文件名")
        mime_type: Optional[str] = Field(default=None, description="附件的 MIME 类型")
        size: Optional[int] = Field(default=None, description="附件大小，单位为字节")

    userid: Optional[Union[str, int]] = Field(default=None, description="用户ID")
    username: Optional[Union[str, int]] = Field(default=None, description="用户名称")
    is_channel_admin: Optional[bool] = Field(default=None, description="渠道适配器依据稳定用户 ID、管理员名单及渠道主用户 ID 生成的授权事实")
    channel: Optional[NotificationChannel] = Field(default=None, description="消息渠道")
    source: Optional[str] = Field(default=None, description="来源（渠道名称）")
    text: Optional[str] = Field(default=None, description="消息体")
    date: Optional[str] = Field(default=None, description="时间")
    action: Optional[int] = Field(default=0, description="消息方向")
    is_callback: Optional[bool] = Field(default=False, description="是否为回调消息")
    message_id: Optional[Union[str, int]] = Field(default=None, description="消息ID（用于回调时定位原消息）")
    chat_id: Optional[str] = Field(default=None, description="聊天ID（用于回调时定位聊天）")
    reply_to_message_id: Optional[Union[str, int]] = Field(default=None, description="回复目标消息ID（用于 ForceReply 等回复场景）")
    callback_query: Optional[Dict[str, Any]] = Field(default=None, description="完整的回调查询信息（原始数据）")
    images: Optional[List[MessageImage]] = Field(default=None, description="图片列表（图片URL或file_id）")
    audio_refs: Optional[List[str]] = Field(default=None, description="语音/音频引用列表")
    files: Optional[List[MessageAttachment]] = Field(default=None, description="文件附件列表")
    callback_data: Optional[str] = Field(default=None, description="结构化按钮回调数据（优先于 CALLBACK: 文本前缀）")

    @field_validator("images", mode="before")
    @classmethod
    def _normalize_images(
            cls, value: Any
    ) -> Optional[List["IncomingMessage.MessageImage"]]:
        """
        在模型校验前归一化外来消息的图片字段。
        """
        return cls.MessageImage.normalize_list(value)

    def to_dict(self) -> dict[str, Any]:
        """
        转换为字典。
        """
        items = self.model_dump()
        for k, v in items.items():
            if isinstance(v, NotificationChannel):
                items[k] = v.value
        return items


@_dataclass
class NotificationBlock:
    """
    通知段落，内容均为普通文本。

    单独标题开启分节，节中的标题和正文对表示一个条目。

    :param title: 段落或分节标题，省略时只显示正文
    :param text: 段落正文，省略时只显示标题
    """

    title: Optional[str] = None
    text: Optional[str] = None


@_dataclass
class NotificationContent:
    """
    通用结构化通知，由宿主按渠道能力渲染。

    此结构只表达标题与段落，不包含签到等业务规则。

    :param title: 整条通知的标题
    :param blocks: 按展示顺序排列的通知段落，默认空列表
    """

    title: Optional[str] = None
    blocks: List[NotificationBlock] = _field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """
        返回普通的标题和段落数据，不包含渠道配置或格式标记。

        :return: 递归转换后的标题和段落字典
        """
        return _asdict(self)

    def render(self, text_format: _NotificationTextFormat) -> str:
        """
        按声明的语法生成正文，渠道标题、卡片和按钮仍由各适配器添加。

        :param text_format: 渠道声明支持的正文语法
        :return: 已按目标语法转义并排版的通知正文
        """
        # 按需加载文本转换，保持普通消息模型导入轻量。
        from app.foundation.text import render_notification as _render_notification

        return _render_notification(self.title, [_asdict(block) for block in self.blocks], text_format.value)


class Message(BaseModel):
    """
    消息。

    :ivar _notification_route_scope: 仅在运行时使用的通知隔离路由范围，不写入消息数据
    """

    channel: Optional[NotificationChannel] = Field(default=None, description="消息渠道")
    source: Optional[str] = Field(default=None, description="消息来源")
    mtype: Optional[MessageType] = Field(default=None, description="消息类型")
    ctype: Optional[ContentType] = Field(default=None, description="内容类型")
    title: Optional[str] = Field(default=None, description="标题")
    text: Optional[str] = Field(default=None, description="文本内容")
    image: Optional[str] = Field(default=None, description="图片")
    poster: Optional[str] = Field(default=None, description="独立媒体海报，不使用背景图回退")
    voice_path: Optional[str] = Field(default=None, description="语音文件路径")
    file_path: Optional[str] = Field(default=None, description="本地文件路径")
    file_name: Optional[str] = Field(default=None, description="发送时展示的文件名")
    voice_caption: Optional[str] = Field(default=None, description="语音消息附带说明文字")
    link: Optional[str] = Field(default=None, description="链接")
    userid: Optional[Union[str, int]] = Field(default=None, description="用户ID")
    username: Optional[Union[str, int]] = Field(default=None, description="用户名称")
    date: Optional[str] = Field(default=None, description="时间")
    action: Optional[int] = Field(default=1, description="消息方向")
    targets: Optional[dict[str, Any]] = Field(default=None, description="消息目标用户ID字典，未指定用户ID时使用")
    buttons: Optional[List[List[dict[str, Any]]]] = Field(
        default=None,
        description="按钮列表，格式：[[{\"text\": \"按钮文本\", \"callback_data\": \"回调数据\", \"url\": \"链接\"}]]",
    )
    force_reply: bool = Field(default=False, description="Telegram ForceReply 回复标记")
    original_message_id: Optional[Union[str, int]] = Field(default=None, description="原消息ID，用于编辑消息")
    original_chat_id: Optional[str] = Field(default=None, description="原消息的聊天ID，用于编辑消息")
    private_delivery: bool = Field(default=False, description="是否必须按用户身份投递到私聊，禁止回退原会话或最近会话映射")
    disable_web_page_preview: Optional[bool] = Field(default=None, description="是否禁用链接预览（仅Telegram支持）")
    parse_mode: Optional[str] = Field(default=None, description="消息文本格式；Telegram 支持 MarkdownV2、HTML、plain，飞书直发支持 plain")
    rich_message: Optional[str] = Field(
        default=None,
        description="Telegram Rich Message 完整 Markdown 正文；其他渠道可使用 text 作为回退",
    )
    rich_content: Optional[NotificationContent] = Field(default=None, description="渠道无关的结构化通知，宿主按能力渲染；text 始终保留为纯文本回退")
    save_history: bool = Field(default=True, description="是否写入消息历史")

    # 通知隔离路由范围，仅在运行时传递给通知模块，不写入消息数据
    _notification_route_scope: Optional[str] = _PrivateAttr(default=None)

    def channel_text(self, channel: NotificationChannel, config: Optional[dict[str, object]] = None) -> Optional[str]:
        """
        仅返回能力声明和实例模式允许的专用正文；缺少能力时保留原始通知。
        """
        text_format = _ChannelCapabilityManager.get_notification_text_format(channel, config)
        if text_format is None or self.rich_content is None:
            return None
        return self.rich_content.render(text_format)

    def channel_text_chunks(self, channel: NotificationChannel, max_bytes: Optional[int] = None) -> List[str]:
        """
        按段落读取有界渠道正文；缺失或单段超限时由渠道继续使用原 text。
        """
        text = self.channel_text(channel)
        if not text:
            return []
        if max_bytes is None:
            capability = _ChannelCapabilityManager.get_capabilities(channel)
            max_bytes = capability.notification_text_bytes if capability else 0
        if max_bytes <= 0:
            return [text]
        paragraphs = text.split("\n\n")
        if any(len(part.encode("utf-8")) > max_bytes for part in paragraphs):
            return []
        chunks = []
        current: List[str] = []
        for part in paragraphs:
            candidate = "\n\n".join(current + [part])
            if current and len(candidate.encode("utf-8")) > max_bytes:
                chunks.append("\n\n".join(current))
                current = []
            current.append(part)
        chunks.append("\n\n".join(current))
        return chunks

    def set_notification_route_scope(self, scope: Optional[str]) -> None:
        """
        设置运行时通知路由范围，支持管理员默认目标和用户个人目标的区分。
        """
        self._notification_route_scope = scope

    @property
    def notification_route_scope(self) -> Optional[str]:
        """
        返回运行时通知路由范围，不影响消息序列化和历史记录。
        """
        return self._notification_route_scope

    def to_dict(self) -> dict[str, Any]:
        """
        转换为字典。
        """
        items = self.model_dump()
        for k, v in items.items():
            if isinstance(v, NotificationChannel) or isinstance(v, MessageType):
                items[k] = v.value
        return items


class NotificationSwitch(BaseModel):
    """
    消息开关。
    """

    mtype: Optional[str] = Field(default=None, description="消息类型")
    wechat: Optional[bool] = Field(default=False, description="微信开关")
    feishu: Optional[bool] = Field(default=False, description="飞书开关")
    telegram: Optional[bool] = Field(default=False, description="TG开关")
    slack: Optional[bool] = Field(default=False, description="Slack开关")
    synologychat: Optional[bool] = Field(default=False, description="SynologyChat开关")
    vocechat: Optional[bool] = Field(default=False, description="VoceChat开关")
    webpush: Optional[bool] = Field(default=False, description="WebPush开关")
    qq: Optional[bool] = Field(default=False, description="QQ开关")


class Subscription(BaseModel):
    """
    客户端消息订阅。
    """

    endpoint: Optional[str] = Field(default=None, description="请求端点地址")
    keys: Optional[dict[str, str]] = Field(default_factory=dict, description="推送订阅的加密公钥与认证密钥")


class SubscriptionMessage(BaseModel):
    """
    客户端订阅消息体。
    """

    title: Optional[str] = Field(default=None, description="标题")
    body: Optional[str] = Field(default=None, description="正文内容")
    icon: Optional[str] = Field(default=None, description="Vuetify 存储图标")
    url: Optional[str] = Field(default=None, description="访问地址")
    data: Optional[dict[str, JsonData]] = Field(default_factory=dict, description="返回的结构化数据")


class AgentWebChatRequest(BaseModel):
    """
    Web 智能助手对话请求。
    """

    class AgentWebChatFile(BaseModel):
        """
        Web 智能助手输入附件。
        """

        ref: str = Field(..., min_length=1, description="附件的访问地址或渠道文件引用")
        name: Optional[str] = Field(None, description="附件文件名")
        mime_type: Optional[str] = Field(None, description="附件的 MIME 类型")
        size: Optional[int] = Field(None, description="附件大小，单位为字节")
        local_path: Optional[str] = Field(None, description="附件在服务端的本地路径")
        status: Optional[str] = Field(None, description="附件处理状态")

    text: str = Field(default="", description="用户本轮输入")
    display_text: Optional[str] = Field(None, description="展示历史中记录的用户可读文本；为空时使用 text")
    session_id: Optional[str] = Field(None, description="前端会话标识，相同标识复用同一段 Agent 记忆")
    images: Optional[List[str]] = Field(default_factory=list, description="图片 URL 或 data URL 列表")
    audio_refs: Optional[List[str]] = Field(default_factory=list, description="语音/音频引用列表")
    files: Optional[List[AgentWebChatFile]] = Field(default_factory=list, description="文件附件列表")
    choice_selection: Optional[Dict[str, JsonData]] = Field(default=None, description="用户通过按钮选择时的完整选择快照")
    original_message_id: Optional[Union[str, int]] = Field(
        default=None,
        description="WebAgent 按钮回调关联的原消息 ID，用于传统交互原地编辑卡片",
    )
    original_chat_id: Optional[Union[str, int]] = Field(default=None, description="WebAgent 按钮回调关联的原聊天 ID，用于传统交互原地编辑卡片")
    echo_user: bool = Field(default=True, description="是否在展示历史中记录本轮用户消息")


class AgentWebChoiceRequest(BaseModel):
    """
    Web 智能助手按钮选择请求。
    """

    session_id: Optional[str] = Field(None, description="前端会话标识，用于保持与原对话窗口的关联")
    callback_data: str = Field(..., min_length=1, description="Agent 工具生成的按钮回调数据")
    original_message_id: Optional[Union[str, int]] = Field(default=None, description="WebAgent 原助手消息 ID，用于传统按钮回调原地编辑")
    original_chat_id: Optional[Union[str, int]] = Field(default=None, description="WebAgent 原聊天 ID，用于传统按钮回调原地编辑")
