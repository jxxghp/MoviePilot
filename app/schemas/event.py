from collections.abc import Mapping as _Mapping
from enum import Enum as _Enum
from pathlib import Path
from typing import Annotated as _Annotated
from typing import Any, Callable, Dict, Iterable, List, Optional, Set
from typing import Union as _Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic import BeforeValidator as _BeforeValidator

from app.schemas.category import ClassificationFieldDefinition
from app.schemas.common import JsonData
from app.schemas.context import Context as _ContextSnapshotBase
from app.schemas.context import MediaInfo as _MediaInfoSnapshot
from app.schemas.context import MetaInfo as _MetaInfoSnapshot
from app.schemas.file import FileItem
from app.schemas.media import OptionalMediaIdentityMixin, RequiredMediaIdentityMixin
from app.schemas.music import MusicInfo as _MusicInfoSnapshot
from app.schemas.music import MusicMeta as _MusicMetaSnapshot
from app.schemas.subscribe import Subscribe as _SubscribeSnapshot
from app.schemas.transfer import TransferInfo
from app.schemas.types import MediaSource, MediaType, NotificationChannel

JsonValue = JsonData
"""事件扩展字段允许的 JSON 值类型。"""


def _coerce_event_snapshot(value: Any) -> Any:
    """把旧运行时对象转换为只用于契约校验的可读取快照。"""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="python")
    elif hasattr(value, "to_dict") and callable(value.to_dict):
        value = value.to_dict()
    elif isinstance(value, _Enum):
        return value.value
    elif isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, _Mapping):
        return {str(key): _coerce_event_snapshot(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_coerce_event_snapshot(item) for item in value]
    return value


EventJsonValue = _Annotated[JsonValue, _BeforeValidator(_coerce_event_snapshot)]
MediaSnapshot = _Annotated[
    _Union[_MusicInfoSnapshot, _MediaInfoSnapshot],
    _BeforeValidator(_coerce_event_snapshot),
]
MetaSnapshot = _Annotated[
    _Union[_MusicMetaSnapshot, _MetaInfoSnapshot],
    _BeforeValidator(_coerce_event_snapshot),
]


class ContextSnapshot(_ContextSnapshotBase):
    """资源上下文的稳定事件快照，不替换链路中的运行时 Context。"""

    allowed_episodes: Optional[Set[int]] = None

    model_config = ConfigDict(
        extra="allow",
        from_attributes=True,
        arbitrary_types_allowed=True,
    )

    @model_validator(mode="before")  # type: ignore[misc]
    @classmethod
    def coerce_runtime_context(cls, value: Any) -> Any:
        """允许旧 Context/dataclass 进入校验，同时保持原对象继续投递。"""
        return _coerce_event_snapshot(value)


class FileContextSnapshot(BaseModel):  # type: ignore[misc]
    """音乐批次中单个文件的元数据上下文快照。"""

    path: str
    meta: Optional[MetaSnapshot] = None
    mediainfo: Optional[MediaSnapshot] = None

    model_config = ConfigDict(extra="allow", from_attributes=True)

    @model_validator(mode="before")  # type: ignore[misc]
    @classmethod
    def coerce_runtime_context(cls, value: Any) -> Any:
        """把旧文件上下文对象转换成可验证的字典。"""
        return _coerce_event_snapshot(value)


class Event(BaseModel):
    """
    事件模型
    """

    event_type: str = Field(..., description="事件类型")
    event_data: Optional[dict] = Field(default={}, description="事件数据")
    priority: Optional[int] = Field(0, description="事件优先级")


class BaseEventData(BaseModel):
    """
    事件数据的基类，所有具体事件数据类应继承自此类
    """

    model_config = ConfigDict(from_attributes=True, arbitrary_types_allowed=True)


class ExtensibleEventData(BaseEventData):
    """允许第三方插件附加字段的类型化事件载荷基类。"""

    model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True)


class EmptyEventData(ExtensibleEventData):
    """当前没有固定字段、但仍需进入 typed registry 的事件载荷。"""


class PluginReloadEventData(ExtensibleEventData):
    """插件重载广播事件载荷。"""

    plugin_id: str = Field(description="重载的插件 ID")


class PluginActionEventData(ExtensibleEventData):
    """插件命令动作载荷；具体动作参数由目标插件扩展。"""

    plugin_id: Optional[str] = Field(default=None, description="目标插件 ID")
    action: Optional[str] = Field(default=None, description="插件动作名称")
    channel: Optional[str] = Field(default=None, description="消息渠道")
    source: Optional[str] = Field(default=None, description="消息来源")
    user: Optional[Any] = Field(default=None, description="发起用户")


class PluginTriggeredEventData(ExtensibleEventData):
    """插件主动发布的跨插件事件载荷。"""

    plugin_id: str = Field(description="发布事件的插件 ID")
    event_name: str = Field(description="插件定义的稳定事件名")
    data: Any = Field(default=None, description="插件定义的事件数据")


class CommandExecuteEventData(ExtensibleEventData):
    """斜杠命令执行事件载荷。"""

    cmd: str = Field(description="包含参数的完整命令文本")
    user: Optional[Any] = Field(default=None, description="发起用户")
    channel: Optional[str] = Field(default=None, description="消息渠道")
    source: Optional[str] = Field(default=None, description="消息来源")
    processing_status: Optional[Any] = Field(default=None, description="交互处理状态")


class SiteEventData(ExtensibleEventData):
    """站点新增、更新、删除或数据刷新事件载荷。"""

    site_id: Optional[int | str] = Field(default=None, description="站点 ID 或通配符")
    domain: Optional[str] = Field(default=None, description="站点域名")
    name: Optional[str] = Field(default=None, description="站点名称")
    site_url: Optional[str] = Field(default=None, description="站点地址")


class HistoryDeletedEventData(ExtensibleEventData):
    """历史记录删除事件的兼容载荷。"""

    history_id: Optional[int] = Field(default=None, description="历史记录 ID")
    src: Optional[str] = Field(default=None, description="关联源路径")


class DownloadFileDeletedEventData(ExtensibleEventData):
    """下载源文件删除事件载荷。"""

    src: Optional[str] = Field(default=None, description="已删除的下载源路径")
    hash: Optional[str] = Field(default=None, description="下载任务 hash")


class DownloadDeletedEventData(ExtensibleEventData):
    """下载任务删除事件载荷。"""

    hash: str = Field(description="下载任务 hash")
    torrents: List[Dict[str, Any]] = Field(default_factory=list, description="删除前任务快照")


class UserMessageEventData(ExtensibleEventData):
    """未被宿主命令或交互消费的用户文本消息载荷。"""

    text: str = Field(description="用户文本")
    userid: Optional[Any] = Field(default=None, description="用户 ID")
    channel: Optional[str] = Field(default=None, description="消息渠道")
    source: Optional[str] = Field(default=None, description="消息来源")
    chat_id: Optional[Any] = Field(default=None, description="会话 ID")
    reply_to_message_id: Optional[Any] = Field(default=None, description="回复消息 ID")


class NoticeMessageEventData(ExtensibleEventData):
    """宿主向消息模块发送的通知事件载荷。"""

    type: Optional[Any] = Field(default=None, description="兼容消息类型")
    title: Optional[str] = Field(default=None, description="消息标题")
    text: Optional[str] = Field(default=None, description="消息正文")
    userid: Optional[Any] = Field(default=None, description="目标用户 ID")
    channel: Optional[Any] = Field(default=None, description="目标消息渠道")
    source: Optional[str] = Field(default=None, description="消息来源")


class SubscribeCompleteEventData(ExtensibleEventData):
    """订阅完成广播事件载荷。"""

    subscribe_id: int = Field(description="已完成订阅 ID")
    subscribe_info: Dict[str, Any] = Field(default_factory=dict, description="订阅快照")
    mediainfo: Dict[str, Any] = Field(default_factory=dict, description="媒体信息快照")


class SystemErrorEventData(ExtensibleEventData):
    """事件、模块、插件或调度器错误的宿主诊断载荷。"""

    type: str = Field(description="错误来源类别")
    error: str = Field(description="错误摘要")
    traceback: Optional[str] = Field(default=None, description="错误堆栈")


class MetadataScrapeEventData(ExtensibleEventData):
    """媒体文件元数据刮削事件载荷。"""

    fileitem: FileItem = Field(description="待刮削目录或文件项")
    file_list: List[str] = Field(default_factory=list, description="待刮削文件清单")
    meta: Any = Field(default=None, description="文件名解析对象")
    mediainfo: Any = Field(default=None, description="媒体信息对象")
    overwrite: bool = Field(default=False, description="是否覆盖已有元数据")
    file_contexts: List[Any] = Field(default_factory=list, description="逐文件上下文")


class MessageActionEventData(ExtensibleEventData):
    """定向插件消息交互动作载荷。"""

    plugin_id: Optional[str] = Field(default=None, description="目标插件 ID")
    text: Optional[str] = Field(default=None, description="兼容动作文本")
    input_text: Optional[str] = Field(default=None, description="用户输入文本")
    userid: Optional[Any] = Field(default=None, description="用户 ID")
    channel: Optional[str] = Field(default=None, description="消息渠道")
    source: Optional[str] = Field(default=None, description="消息来源")
    input_session_id: Optional[str] = Field(default=None, description="输入会话 ID")
    payload: Any = Field(default=None, description="插件自定义交互数据")


class WorkflowExecuteEventData(ExtensibleEventData):
    """请求执行指定工作流的事件载荷。"""

    workflow_id: int = Field(description="工作流 ID")


class NameRecognizeEventData(ExtensibleEventData):
    """影视名称辅助识别的输入和插件回写字段。"""

    title: str = Field(description="待识别标题")
    name: Optional[str] = Field(default=None, description="插件识别后的名称")
    year: Optional[Any] = Field(default=None, description="年份")
    season: Optional[Any] = Field(default=None, description="季号")
    episode: Optional[Any] = Field(default=None, description="集号")


class MusicNameRecognizeEventData(ExtensibleEventData):
    """音乐名称辅助识别的输入和插件回写字段。"""

    title: str = Field(description="待识别曲名")
    artist: Optional[str] = Field(default=None, description="艺术家")
    album: Optional[str] = Field(default=None, description="专辑")
    year: Optional[Any] = Field(default=None, description="年份")
    duration: Optional[Any] = Field(default=None, description="时长")
    name: Optional[str] = Field(default=None, description="插件识别后的曲名")


class MediaRecognizeEventData(ExtensibleEventData):
    """影视媒体身份补充识别的输入和插件回写字段。"""

    title: Optional[str] = Field(default=None, description="待识别标题")
    year: Optional[Any] = Field(default=None, description="年份")
    season: Optional[Any] = Field(default=None, description="季号")
    type: Optional[Any] = Field(default=None, description="媒体类型")
    media_source: Optional[Any] = Field(default=None, description="媒体数据源")
    media_id: Optional[Any] = Field(default=None, description="数据源原生 ID")
    mediainfo: Optional[Dict[str, Any]] = Field(default=None, description="插件回写媒体信息")


class MusicMediaRecognizeEventData(MediaRecognizeEventData):
    """音乐媒体身份补充识别的输入和插件回写字段。"""

    artists: List[str] = Field(default_factory=list, description="艺术家列表")
    album: Optional[str] = Field(default=None, description="专辑")
    isrc: Optional[str] = Field(default=None, description="ISRC")
    music_type: Optional[str] = Field(default=None, description="音乐实体类型")


class ConfigChangeEventData(BaseEventData):
    """
    ConfigChange 事件的数据模型
    """

    key: set[str] = Field(..., description="配置项的键（集合类型）")
    value: Optional[Any] = Field(default=None, description="配置项的新值")
    change_type: str = Field(
        default="update", description="配置项的变更类型，如 'add', 'update', 'delete'"
    )

    @field_validator("key", mode="before")
    @classmethod
    def convert_to_set(cls, v):
        """将输入的 str、list、dict.keys() 等转为 set"""
        def normalize(item: Any) -> str:
            """把枚举键转换为其持久化值，其余键保持字符串兼容。"""
            return str(item.value) if isinstance(item, _Enum) else str(item)

        if v is None:
            return set()
        elif isinstance(v, str):
            return {v}
        elif isinstance(v, _Enum):
            return {normalize(v)}
        elif isinstance(v, dict):
            return {normalize(k) for k in v.keys()}
        elif isinstance(v, (list, tuple)):
            return {normalize(item) for item in v}
        elif isinstance(v, set):
            return {normalize(item) for item in v}
        elif isinstance(v, Iterable):
            return {normalize(item) for item in v}
        else:
            return {normalize(v)}


class ChainEventData(BaseEventData):
    """
    链式事件数据的基类，所有具体事件数据类应继承自此类
    """

    model_config = ConfigDict(from_attributes=True, arbitrary_types_allowed=True)


class ResourceSelectionContractData(ChainEventData):
    """ResourceSelection 的稳定输入/输出契约。"""

    contexts: List[ContextSnapshot] = Field(default_factory=list)
    downloader: Optional[str] = None
    origin: Optional[str] = None
    updated: bool = False
    updated_contexts: Optional[List[ContextSnapshot]] = None
    source: Optional[str] = "未知拦截源"


class ResourceSelectionInputContractData(ChainEventData):
    """ResourceSelection 事件输入字段。"""

    contexts: List[ContextSnapshot] = Field(default_factory=list)
    downloader: Optional[str] = None
    origin: Optional[str] = None


class ResourceSelectionOutputContractData(ChainEventData):
    """ResourceSelection 事件插件回写字段。"""

    updated: bool = False
    updated_contexts: Optional[List[ContextSnapshot]] = None
    source: Optional[str] = "未知拦截源"


class ResourceDownloadContractData(ChainEventData):
    """ResourceDownload 的稳定输入/输出契约。"""

    context: Optional[ContextSnapshot] = None
    episodes: Optional[Set[int]] = None
    channel: Optional[NotificationChannel] = None
    origin: Optional[str] = None
    downloader: Optional[str] = None
    options: Optional[Dict[str, EventJsonValue]] = None
    cancel: bool = False
    source: str = "未知拦截源"
    reason: str = ""


class ResourceDownloadInputContractData(ChainEventData):
    """ResourceDownload 事件输入字段。"""

    context: Optional[ContextSnapshot] = None
    episodes: Optional[Set[int]] = None
    channel: Optional[NotificationChannel] = None
    origin: Optional[str] = None
    downloader: Optional[str] = None
    options: Optional[Dict[str, EventJsonValue]] = None


class ResourceDownloadOutputContractData(ChainEventData):
    """ResourceDownload 事件插件回写字段。"""

    cancel: bool = False
    source: str = "未知拦截源"
    reason: str = ""


class DownloadAddedContractData(BaseEventData):
    """DownloadAdded 的稳定下载上下文契约。"""

    hash: str
    context: ContextSnapshot
    username: Optional[str] = None
    downloader: Optional[str] = None
    episodes: List[int] = Field(default_factory=list)
    source: Optional[str] = None
    idempotency_key: Optional[str] = None


class TransferResultContractData(BaseEventData):
    """整理结果事件的稳定媒体与元数据契约。"""

    fileitem: Optional[FileItem] = None
    meta: Optional[MetaSnapshot] = None
    mediainfo: Optional[MediaSnapshot] = None
    transferinfo: Optional[TransferInfo] = None
    downloader: Optional[str] = None
    download_hash: Optional[str] = None
    transfer_history_id: Optional[int] = None
    idempotency_key: Optional[str] = None


class MetadataScrapeContractData(ExtensibleEventData):
    """MetadataScrape 的稳定媒体刮削契约。"""

    fileitem: FileItem
    file_list: List[str] = Field(default_factory=list)
    meta: Optional[MetaSnapshot] = None
    mediainfo: Optional[MediaSnapshot] = None
    overwrite: bool = False
    file_contexts: List[FileContextSnapshot] = Field(default_factory=list)


class SubscribeCompletionCheckContractData(ChainEventData):
    """订阅完成判定的稳定订阅、媒体和元数据契约。"""

    subscribe: Optional[_SubscribeSnapshot] = None
    mediainfo: Optional[MediaSnapshot] = None
    meta: Optional[MetaSnapshot] = None
    cancel: bool = False
    source: str = "未知来源"
    reason: str = ""


class SubscribeCompletionCheckInputContractData(ChainEventData):
    """SubscribeCompletionCheck 事件输入字段。"""

    subscribe: Optional[_SubscribeSnapshot] = None
    mediainfo: Optional[MediaSnapshot] = None
    meta: Optional[MetaSnapshot] = None


class SubscribeCompletionCheckOutputContractData(ChainEventData):
    """SubscribeCompletionCheck 事件插件回写字段。"""

    cancel: bool = False
    source: str = "未知来源"
    reason: str = ""


class PluginDataResetEventData(ChainEventData):
    """
    PluginDataReset 事件的数据模型。

    在主程序清空某个插件配置或插件数据前发出，插件可在数据被删除前完成
    自有状态补偿。事件处理器只应处理 plugin_id 与自身匹配的事件。
    """

    plugin_id: str = Field(..., description="即将被重置的插件 ID")
    reset_config: bool = Field(default=False, description="是否即将重置插件配置")
    reset_data: bool = Field(default=False, description="是否即将重置插件数据")


class AgentLLMProviderEventData(ChainEventData):
    """
    Agent LLM 供应商选择事件数据。

    事件发出方会带入当前系统配置作为默认值；插件可覆盖 provider、base_url、
    api_key、model、user_agent、use_proxy 等字段，并通过 selected_provider_id 标记本次选择，方便
    后续用量事件精确回写到同一个配额条目。
    """

    provider: Optional[str] = Field(default=None, description="LLM provider ID")
    base_url: Optional[str] = Field(default=None, description="API Base URL")
    api_key: Optional[str] = Field(default=None, description="API Key")
    model: Optional[str] = Field(default=None, description="模型名称")
    base_url_preset: Optional[str] = Field(default=None, description="Base URL 预设ID")
    user_agent: Optional[str] = Field(default=None, description="OpenAI兼容接口User-Agent")
    use_proxy: Optional[bool] = Field(default=None, description="是否使用系统代理")
    thinking_level: Optional[str] = Field(default=None, description="思考模式级别")
    api_protocol: Optional[str] = Field(default=None, description="OpenAI兼容接口API协议：auto/chat_completions/responses")
    web_search_mode: Optional[str] = Field(default=None, description="联网搜索模式：local/builtin/auto/disabled")
    selected_provider_id: Optional[str] = Field(default=None, description="插件侧供应商ID")
    selected_provider_name: Optional[str] = Field(default=None, description="插件侧供应商名称")
    source: Optional[str] = Field(default=None, description="选择来源")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="扩展元数据")


class AgentTokensUsageEventData(BaseEventData):
    """
    Agent Tokens 用量广播事件数据。

    用量事件不携带 API Key，只携带选择事件返回的 selected_provider_id 以及
    聚合后的 token 统计，避免把密钥扩散给广播订阅者。
    """

    session_id: str = Field(..., description="Agent 会话ID")
    selected_provider_id: Optional[str] = Field(default=None, description="插件侧供应商ID")
    selected_provider_name: Optional[str] = Field(default=None, description="插件侧供应商名称")
    provider: Optional[str] = Field(default=None, description="实际 LLM provider ID")
    base_url: Optional[str] = Field(default=None, description="API Base URL")
    model: Optional[str] = Field(default=None, description="模型名称")
    input_tokens: int = Field(default=0, description="输入 tokens")
    output_tokens: int = Field(default=0, description="输出 tokens")
    total_tokens: int = Field(default=0, description="总 tokens")
    cache_read_input_tokens: int = Field(default=0, description="从提示词缓存读取的输入 tokens")
    cache_write_input_tokens: int = Field(default=0, description="写入提示词缓存的输入 tokens")
    uncached_input_tokens: int = Field(default=0, description="未命中缓存的输入 tokens")
    cache_hit_ratio: Optional[float] = Field(default=None, description="提示词缓存命中率")
    cache_usage_available: bool = Field(default=False, description="供应商是否返回缓存用量明细")
    model_call_count: int = Field(default=0, description="模型调用次数")
    success: bool = Field(default=False, description="Agent 执行是否成功")
    error: Optional[str] = Field(default=None, description="失败原因")
    started_at: Optional[str] = Field(default=None, description="开始时间")
    finished_at: Optional[str] = Field(default=None, description="结束时间")
    source: str = Field(default="agent", description="事件来源")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="扩展元数据")


class AuthCredentials(ChainEventData):
    """
    AuthVerification 事件的数据模型

    Attributes:
        username (Optional[str]): 用户名，适用于 "password" grant_type
        password (Optional[str]): 用户密码，适用于 "password" grant_type
        mfa_code (Optional[str]): 一次性密码，目前仅适用于 "password" 认证类型
        code (Optional[str]): 授权码，适用于 "authorization_code" grant_type
        grant_type (str): 认证类型，如 "password", "authorization_code", "client_credentials"
        # scope (List[str]): 权限范围，如 ["read", "write"]
        token (Optional[str]): 认证令牌
        channel (Optional[str]): 认证渠道
        service (Optional[str]): 服务名称
    """

    # 输入参数
    username: Optional[str] = Field(
        None, description="用户名，适用于 'password' 认证类型"
    )
    password: Optional[str] = Field(
        None, description="用户密码，适用于 'password' 认证类型"
    )
    mfa_code: Optional[str] = Field(
        None, description="一次性密码，目前仅适用于 'password' 认证类型"
    )
    code: Optional[str] = Field(
        None, description="授权码，适用于 'authorization_code' 认证类型"
    )
    grant_type: str = Field(
        ...,
        description="认证类型，如 'password', 'authorization_code', 'client_credentials'",
    )
    # scope: List[str] = Field(default_factory=list, description="权限范围，如 ['read', 'write']")

    # 输出参数
    # grant_type 为 authorization_code 时，输出参数包括 username、token、channel、service
    token: Optional[str] = Field(default=None, description="认证令牌")
    channel: Optional[str] = Field(default=None, description="认证渠道")
    service: Optional[str] = Field(default=None, description="服务名称")

    @model_validator(mode="before")
    @classmethod
    def check_fields_based_on_grant_type(cls, values):  # noqa
        grant_type = values.get("grant_type")
        if not grant_type:
            values["grant_type"] = "password"
            grant_type = "password"

        if grant_type == "password":
            if not values.get("username") or not values.get("password"):
                raise ValueError(
                    "username and password are required for grant_type 'password'"
                )

        elif grant_type == "authorization_code":
            if not values.get("code"):
                raise ValueError("code is required for grant_type 'authorization_code'")

        return values


class AuthInterceptCredentials(ChainEventData):
    """
    AuthIntercept 事件的数据模型

    Attributes:
        # 输入参数
        username (str): 用户名
        channel (str): 认证渠道
        service (str): 服务名称
        token (str): 认证令牌
        status (str): 认证状态，"triggered" 和 "completed" 两个状态

        # 输出参数
        source (str): 拦截源，默认值为 "未知拦截源"
        cancel (bool): 是否取消认证，默认值为 False
    """

    # 输入参数
    username: Optional[str] = Field(..., description="用户名")
    channel: str = Field(..., description="认证渠道")
    service: str = Field(..., description="服务名称")
    status: str = Field(
        ...,
        description="认证状态, 包含 'triggered' 表示认证触发，'completed' 表示认证成功",
    )
    token: Optional[str] = Field(default=None, description="认证令牌")

    # 输出参数
    source: str = Field(default="未知拦截源", description="拦截源")
    cancel: bool = Field(default=False, description="是否取消认证")


class CommandRegisterEventData(ChainEventData):
    """
    CommandRegister 事件的数据模型

    Attributes:
        # 输入参数
        commands (dict): 菜单命令
        origin (str): 事件源，可以是 Chain 或具体的模块名称
        service (str): 服务名称

        # 输出参数
        source (str): 拦截源，默认值为 "未知拦截源"
        cancel (bool): 是否取消认证，默认值为 False
    """

    # 输入参数
    commands: Dict[str, dict] = Field(..., description="菜单命令")
    origin: str = Field(..., description="事件源")
    service: Optional[str] = Field(..., description="服务名称")

    # 输出参数
    cancel: bool = Field(default=False, description="是否取消注册")
    source: str = Field(default="未知拦截源", description="拦截源")


class TransferRenameBuildEventData(ChainEventData):
    """
    TransferRenameBuild 事件的数据模型

    在 ``transhandler.get_rename_path`` 渲染文件名之前发出，给插件一次往
    ``rename_dict`` 写字段的机会。典型用法是通过 ffprobe 或外部接口探测源文件，
    把分辨率、视频/音频编码、HDR 等字段写入 ``rename_dict``，主程序下一步渲染时
    就能直接用到这些字段，不需要插件事后再渲染一次去覆盖结果。

    与 ``TransferRenameEventData`` 的分工：
    - 本事件负责"往 ``rename_dict`` 里写字段"，没有输出参数；
    - ``TransferRename`` 在渲染之后触发，负责对已渲染好的字符串再做改写（大小写、
      词替换、模板覆盖等），由智能重命名一类插件使用。

    使用约定：
    - 只往 ``rename_dict`` 写字段，不要在这里改写已经渲染好的字符串；
    - ``source_path`` / ``source_item`` 为空时（如重命名预览场景），需要源文件
      才能工作的插件请直接 return；
    - ``rename_dict`` 中以双下划线开头的键（``__meta__`` / ``__mediainfo__`` 等）
      存放的是原始对象引用，只读使用，不要修改这些对象本身。

    Attributes:
        template_string (str): Jinja2 模板字符串
        rename_dict (Dict[str, Any]): 渲染上下文，可直接修改
        source_path (Optional[str]): 源文件路径，即待整理的文件路径
        source_item (Optional[FileItem]): 源文件信息，即待整理的文件信息
    """

    template_string: str = Field(..., description="模板字符串")
    rename_dict: Dict[str, Any] = Field(..., description="渲染上下文")
    source_path: Optional[str] = Field(
        None, description="源文件路径，即待整理的文件路径"
    )
    source_item: Optional[FileItem] = Field(
        None, description="源文件信息，即待整理的文件信息"
    )


class TransferRenameEventData(ChainEventData):
    """
    TransferRename 事件的数据模型

    Attributes:
        # 输入参数
        template_string (str): Jinja2 模板字符串
        rename_dict (dict): 渲染上下文
        render_str (str): 渲染生成的字符串
        path (Optional[Path]): 当前文件的目标路径
        source_path (Optional[str]): 源文件路径，即待整理的文件路径
        source_item (Optional[FileItem]): 源文件信息，即待整理的文件信息

        # 输出参数
        updated (bool): 是否已更新，默认值为 False
        updated_str (str): 更新后的字符串
        source (str): 拦截源，默认值为 "未知拦截源"
    """

    # 输入参数
    template_string: str = Field(..., description="模板字符串")
    rename_dict: Dict[str, Any] = Field(..., description="渲染上下文")
    path: Optional[Path] = Field(None, description="文件的目标路径")
    render_str: str = Field(..., description="渲染生成的字符串")
    source_path: Optional[str] = Field(
        None, description="源文件路径，即待整理的文件路径"
    )
    source_item: Optional[FileItem] = Field(
        None, description="源文件信息，即待整理的文件信息"
    )

    # 输出参数
    updated: bool = Field(default=False, description="是否已更新")
    updated_str: Optional[str] = Field(default=None, description="更新后的字符串")
    source: Optional[str] = Field(default="未知拦截源", description="拦截源")


class ResourceSelectionEventData(BaseModel):
    """
    ResourceSelection 事件的数据模型

    Attributes:
        # 输入参数
        contexts (List[Context]): 当前待选择的资源上下文列表
        source (str): 事件源，指示事件的触发来源

        # 输出参数
        updated (bool): 是否已更新，默认值为 False
        updated_contexts (Optional[List[Context]]): 已更新的资源上下文列表，默认值为 None
        source (str): 更新源，默认值为 "未知更新源"
    """

    # 输入参数
    contexts: Any = Field(None, description="待选择的资源上下文列表")
    downloader: Optional[str] = Field(None, description="下载器")
    origin: Optional[str] = Field(None, description="来源")

    # 输出参数
    updated: bool = Field(default=False, description="是否已更新")
    updated_contexts: Optional[List[Any]] = Field(
        default=None, description="已更新的资源上下文列表"
    )
    source: Optional[str] = Field(default="未知拦截源", description="拦截源")


class ResourceDownloadEventData(ChainEventData):
    """
    ResourceDownload 事件的数据模型

    Attributes:
        # 输入参数
        context (Context): 当前资源上下文
        episodes (Set[int]): 需要下载的集数
        channel (NotificationChannel): 通知渠道
        origin (str): 来源（消息通知、Subscribe、Manual等）
        downloader (str): 下载器
        options (dict): 其他参数

        # 输出参数
        cancel (bool): 是否取消下载，默认值为 False
        source (str): 拦截源，默认值为 "未知拦截源"
        reason (str): 拦截原因，描述拦截的具体原因
    """

    # 输入参数
    context: Any = Field(None, description="当前资源上下文")
    episodes: Optional[Set[int]] = Field(None, description="需要下载的集数")
    channel: Optional[NotificationChannel] = Field(None, description="通知渠道")
    origin: Optional[str] = Field(None, description="来源")
    downloader: Optional[str] = Field(None, description="下载器")
    options: Optional[dict] = Field(default={}, description="其他参数")

    # 输出参数
    cancel: bool = Field(default=False, description="是否取消下载")
    source: str = Field(default="未知拦截源", description="拦截源")
    reason: str = Field(default="", description="拦截原因")


class TransferInterceptEventData(ChainEventData):
    """
    TransferIntercept 事件的数据模型

    Attributes:
        # 输入参数
        fileitem (FileItem): 源文件
        meta (Any): 元数据
        target_storage (str): 目标存储
        target_path (Path): 目标路径
        transfer_type (str): 整理方式（copy、move、link、softlink等）
        options (dict): 其他参数

        # 输出参数
        cancel (bool): 是否取消下载，默认值为 False
        source (str): 拦截源，默认值为 "未知拦截源"
        reason (str): 拦截原因，描述拦截的具体原因
    """

    # 输入参数
    fileitem: FileItem = Field(..., description="源文件")
    meta: Optional[Any] = Field(default=None, description="元数据")
    mediainfo: Any = Field(..., description="媒体信息")
    target_storage: str = Field(..., description="目标存储")
    target_path: Path = Field(..., description="目标路径")
    transfer_type: str = Field(..., description="整理方式")
    options: Optional[dict] = Field(default=None, description="其他参数")

    # 输出参数
    cancel: bool = Field(default=False, description="是否取消整理")
    source: str = Field(default="未知拦截源", description="拦截源")
    reason: str = Field(default="", description="拦截原因")


class TransferOverwriteCheckEventData(ChainEventData):
    """
    TransferOverwriteCheck 事件的数据模型

    在覆盖模式判断（如按文件大小覆盖）执行之前触发，允许插件提供源文件与
    目标文件的真实大小（例如本地 .strm 文件指向的网盘原始文件大小），或者
    直接给出覆盖决策。

    Attributes:
        # 输入参数
        fileitem (FileItem): 源文件
        target_item (FileItem): 目标文件（已存在）
        target_storage (str): 目标存储
        target_path (Path): 目标文件路径
        overwrite_mode (str): 覆盖模式（always、size、never、latest）
        transfer_type (str): 整理方式
        options (dict): 其他参数

        # 输出参数
        source_size (Optional[int]): 由插件提供的源文件真实大小，覆盖
            fileitem.size 用于 size 模式比较；为 None 时表示不修改
        target_size (Optional[int]): 由插件提供的目标文件真实大小，覆盖
            target_item.size 用于 size 模式比较；为 None 时表示不修改
        overwrite (Optional[bool]): 由插件直接给出的覆盖决策，非 None 时
            将完全跳过 MoviePilot 内置的 size/never/latest 等比较逻辑
        source (str): 处理来源
        reason (str): 处理原因，描述插件做出决策或修改的原因
    """

    # 输入参数
    fileitem: FileItem = Field(..., description="源文件")
    target_item: FileItem = Field(..., description="目标已存在文件")
    target_storage: str = Field(..., description="目标存储")
    target_path: Path = Field(..., description="目标文件路径")
    overwrite_mode: str = Field(..., description="覆盖模式")
    transfer_type: str = Field(..., description="整理方式")
    options: Optional[dict] = Field(default=None, description="其他参数")

    # 输出参数
    source_size: Optional[int] = Field(
        default=None, description="插件提供的源文件真实大小"
    )
    target_size: Optional[int] = Field(
        default=None, description="插件提供的目标文件真实大小"
    )
    overwrite: Optional[bool] = Field(
        default=None, description="插件直接给出的覆盖决策"
    )
    source: str = Field(default="未知处理源", description="处理来源")
    reason: str = Field(default="", description="处理原因")


class DiscoverMediaSource(BaseModel):
    """
    探索媒体数据源的基类。

    ``mediaid_prefix`` 是既有插件与前端标签使用的稳定标识；
    ``media_source`` 是新的规范媒体来源，可以是内置常量或插件扩展成员。模型同时
    输出两者，并在输入时互相补齐，以兼容尚未升级的已安装插件。
    """

    name: str = Field(..., description="数据源名称")
    media_source: MediaSource = Field(..., description="内置或插件扩展媒体来源")
    mediaid_prefix: str = Field(..., description="兼容插件使用的媒体ID前缀")
    api_path: str = Field(..., description="媒体数据源API地址")
    filter_params: Optional[Dict[str, JsonData]] = Field(
        default=None, description="过滤参数"
    )
    filter_ui: Optional[List[Dict[str, JsonData]]] = Field(default=[], description="过滤参数UI配置")
    depends: Optional[Dict[str, list[str]]] = Field(
        default=None, description="UI依赖关系字典"
    )

    @model_validator(mode="before")
    @classmethod
    def normalize_media_identity(cls, value: Any) -> Any:
        """在旧前缀与规范媒体来源之间双向补齐发现源身份。"""
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        media_source = normalized.get("media_source")
        mediaid_prefix = normalized.get("mediaid_prefix")
        if media_source and not mediaid_prefix:
            normalized["mediaid_prefix"] = str(media_source)
        elif mediaid_prefix and not media_source:
            normalized["media_source"] = cls._media_source_from_prefix(
                str(mediaid_prefix)
            )
        return normalized

    @staticmethod
    def _media_source_from_prefix(mediaid_prefix: str) -> MediaSource:
        """将旧插件前缀映射为内置或插件扩展媒体来源。"""
        aliases = {
            "mangguo": MediaSource.MangoTV,
            "tencentvideo": MediaSource.TencentVideo,
        }
        if mediaid_prefix in aliases:
            return aliases[mediaid_prefix]
        return MediaSource(mediaid_prefix)


class MediaSourceInfo(BaseModel):
    """
    媒体数据源注册描述。

    插件通过该描述声明来源的展示名称、支持的媒体类型和可选受控分类字段；
    识别、搜索和刮削的实际实现仍由插件模块方法提供，宿主负责验证声明和事实。
    """

    name: str = Field(..., description="数据源展示名称")
    media_source: MediaSource = Field(..., description="规范媒体来源标识")
    media_types: List[MediaType] = Field(
        default_factory=lambda: [MediaType.MOVIE, MediaType.TV],
        description="支持的媒体类型",
    )
    classification_fields: List[ClassificationFieldDefinition] = Field(
        default_factory=list,
        description="来源声明的受控扩展分类字段；旧插件省略时保持无扩展字段",
    )


class DiscoverSourceEventData(ChainEventData):
    """
    DiscoverSource 事件的数据模型

    Attributes:
        # 输出参数
        extra_sources (List[DiscoverMediaSource]): 额外媒体数据源
    """

    # 输出参数
    extra_sources: List[DiscoverMediaSource] = Field(
        default_factory=list, description="额外媒体数据源"
    )


class RecommendMediaSource(BaseModel):
    """
    推荐媒体数据源的基类
    """

    name: str = Field(..., description="数据源名称")
    api_path: str = Field(..., description="媒体数据源API地址")
    type: str = Field(..., description="类型")


class RecommendSourceEventData(ChainEventData):
    """
    RecommendSource 事件的数据模型

    Attributes:
        # 输出参数
        extra_sources (List[RecommendMediaSource]): 额外媒体数据源
    """

    # 输出参数
    extra_sources: List[RecommendMediaSource] = Field(
        default_factory=list, description="额外媒体数据源"
    )


class MediaRecognizeConvertEventData(RequiredMediaIdentityMixin, ChainEventData):
    """
    MediaRecognizeConvert 事件的数据模型

    Attributes:
        # 输入参数
        media_source (MediaSource): 输入内置或插件扩展媒体来源
        media_id (str): 数据源原生 ID
        target_media_source (MediaSource): 需要转换到的内置或插件扩展媒体来源

        # 输出参数
        media_dict (dict): TheMovieDb/豆瓣的媒体数据
    """

    # 输入参数
    media_source: MediaSource = Field(..., description="媒体来源")
    media_id: str = Field(..., description="数据源原生 ID")
    target_media_source: MediaSource = Field(..., description="目标媒体来源")

    # 输出参数
    media_dict: dict = Field(
        default_factory=dict, description="转换后的媒体信息"
    )


class StorageOperSelectionEventData(ChainEventData):
    """
    StorageOperSelect 事件的数据模型

    Attributes:
        # 输入参数
        storage (str): 存储类型

        # 输出参数
        storage_oper (Callable): 存储操作对象
    """

    # 输入参数
    storage: Optional[str] = Field(default=None, description="存储类型")

    # 输出参数
    storage_oper: Optional[Callable] = Field(default=None, description="存储操作对象")


class SubscribeEpisodesRefreshEventData(OptionalMediaIdentityMixin, ChainEventData):
    """
    SubscribeEpisodesRefresh 事件的数据模型

    主程序在推算订阅某季总集数时发出，携带主程序本次识别到的 TMDB 当前季总集数；
    外部可据自身策略向上覆盖 total_episode（如待定集数），低于 current_total_episode 的覆盖值会被主程序钳制。

    Attributes:
        # 输入参数
        media_source (Optional[MediaSource]): 媒体来源
        media_id (Optional[str]): 数据源原生 ID
        season (Optional[int]): 季号
        mediainfo (Any): 媒体信息
        current_total_episode (int): 主程序本次识别到的 TMDB 当前季总集数
        subscribe_id (Optional[int]): 订阅 ID；订阅创建场景下尚未入库，为空
        scene (Optional[str]): 触发场景，create/refresh/precheck

        # 输出参数
        updated (bool): 外部是否覆盖了总集数，默认 False
        total_episode (Optional[int]): 覆盖后的总集数，仅在 updated=True 时生效；低于 current_total_episode 时由主程序钳制
        source (str): 覆盖来源
        reason (str): 覆盖原因
    """

    # 输入参数
    media_source: Optional[MediaSource] = Field(default=None, description="媒体数据源")
    media_id: Optional[str] = Field(default=None, description="数据源原生 ID")
    season: Optional[int] = Field(default=None, description="季号")
    mediainfo: Any = Field(default=None, description="媒体信息")
    current_total_episode: int = Field(default=0, description="主程序本次识别到的 TMDB 当前季总集数")
    subscribe_id: Optional[int] = Field(default=None, description="订阅 ID；创建场景为空")
    scene: Optional[str] = Field(default=None, description="触发场景：create/refresh/precheck")

    # 输出参数
    updated: bool = Field(default=False, description="外部是否覆盖了总集数")
    total_episode: Optional[int] = Field(default=None, description="覆盖后的总集数；低于主程序本次识别到的 TMDB 当前季总集数时由主程序钳制")
    source: str = Field(default="未知来源", description="覆盖来源")
    reason: str = Field(default="", description="覆盖原因")


class SubscribeModifiedEventData(BaseEventData):
    """
    SubscribeModified 广播事件数据。

    主程序在订阅字段被普通更新、状态入口、重置或 Agent 更新后发出。payload
    继续保持 dict 形态，scene 用于表达操作场景，fields 表达最终快照里的真实字段差异。
    """

    subscribe_id: int = Field(description="订阅 ID")
    old_subscribe_info: Dict[str, Any] = Field(default_factory=dict, description="更新前订阅快照")
    subscribe_info: Dict[str, Any] = Field(default_factory=dict, description="更新后订阅快照")
    scene: str = Field(default="update", description="触发场景：update/status/reset/agent_update")
    fields: List[str] = Field(default_factory=list, description="真实变更字段")
    idempotency_key: Optional[str] = Field(default=None, description="宿主生成的幂等键")

    @model_validator(mode="after")
    def compute_fields(self):
        self.fields = self._diff_fields(self.old_subscribe_info, self.subscribe_info)
        return self

    @staticmethod
    def _diff_fields(old_info: Dict[str, Any], new_info: Dict[str, Any]) -> List[str]:
        """
        按 old/new 快照并集计算真实字段差异；缺失 key 按 None 参与比较。
        """
        old_info = old_info or {}
        new_info = new_info or {}
        keys = set(old_info) | set(new_info)
        return sorted(key for key in keys if old_info.get(key) != new_info.get(key))

    def to_dict(self) -> Dict[str, Any]:
        """
        输出公开事件 payload，避免内部属性被未来扩展意外暴露。
        """
        payload = {
            "subscribe_id": self.subscribe_id,
            "old_subscribe_info": self.old_subscribe_info,
            "subscribe_info": self.subscribe_info,
            "scene": self.scene,
            "fields": list(self.fields),
        }
        if self.idempotency_key:
            payload["idempotency_key"] = self.idempotency_key
        return payload


class SubscribeAddedEventData(BaseEventData):
    """SubscribeAdded 广播事件的可恢复公开 payload。"""

    subscribe_id: int = Field(description="订阅 ID")
    username: Optional[str] = Field(default=None, description="发起订阅的用户")
    mediainfo: Dict[str, Any] = Field(default_factory=dict, description="媒体信息快照")
    idempotency_key: Optional[str] = Field(default=None, description="宿主生成的幂等键")


class SubscribeDeletedEventData(BaseEventData):
    """SubscribeDeleted 广播事件的可恢复公开 payload。"""

    subscribe_id: int = Field(description="订阅 ID")
    subscribe_info: Dict[str, Any] = Field(default_factory=dict, description="删除前订阅快照")
    idempotency_key: Optional[str] = Field(default=None, description="宿主生成的幂等键")


class DownloadAddedEventData(BaseEventData):
    """DownloadAdded 广播事件的插件兼容 payload。"""

    hash: str = Field(description="下载任务 hash")
    context: Any = Field(description="下载上下文对象")
    username: Optional[str] = Field(default=None, description="发起下载的用户")
    downloader: Optional[str] = Field(default=None, description="下载器名称")
    episodes: List[int] = Field(default_factory=list, description="下载剧集列表")
    source: Optional[str] = Field(default=None, description="下载来源")
    idempotency_key: Optional[str] = Field(default=None, description="宿主生成的幂等键")


class TransferResultEventData(BaseEventData):
    """TransferComplete/Failed 共用的插件兼容 payload。"""

    fileitem: Optional[FileItem] = Field(default=None, description="源文件项")
    meta: Any = Field(default=None, description="文件名解析对象")
    mediainfo: Any = Field(default=None, description="媒体信息对象")
    transferinfo: Optional[TransferInfo] = Field(default=None, description="整理结果")
    downloader: Optional[str] = Field(default=None, description="下载器名称")
    download_hash: Optional[str] = Field(default=None, description="下载任务 hash")
    transfer_history_id: Optional[int] = Field(default=None, description="整理历史 ID")
    idempotency_key: Optional[str] = Field(default=None, description="宿主生成的幂等键")


class SubscribeCompletionCheckEventData(ChainEventData):
    """
    SubscribeCompletionCheck 事件的数据模型

    在订阅被自动判定完成、即将收口（写历史并删除）之前发出，允许外部据完结策略否决本次完成

    Attributes:
        # 输入参数
        subscribe (Any): 订阅对象
        mediainfo (Any): 媒体信息
        meta (Any): 元数据

        # 输出参数
        cancel (bool): 是否否决本次完成，默认 False
        source (str): 否决来源
        reason (str): 否决原因
    """

    # 输入参数
    subscribe: Any = Field(default=None, description="订阅对象")
    mediainfo: Any = Field(default=None, description="媒体信息")
    meta: Any = Field(default=None, description="元数据")

    # 输出参数
    cancel: bool = Field(default=False, description="是否否决本次完成")
    source: str = Field(default="未知来源", description="否决来源")
    reason: str = Field(default="", description="否决原因")
