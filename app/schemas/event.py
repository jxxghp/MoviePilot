from pathlib import Path
from typing import Iterable, Optional, Dict, Any, List, Set, Callable

from pydantic import BaseModel, Field, field_validator, model_validator

from app.schemas.message import MessageChannel
from app.schemas.file import FileItem


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

    pass


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
        if v is None:
            return set()
        elif isinstance(v, str):
            return {v}
        elif isinstance(v, dict):
            return set(str(k) for k in v.keys())
        elif isinstance(v, (list, tuple)):
            return set(str(item) for item in v)
        elif isinstance(v, set):
            return set(str(item) for item in v)
        elif isinstance(v, Iterable):
            return set(str(item) for item in v)
        else:
            return {str(v)}


class ChainEventData(BaseEventData):
    """
    链式事件数据的基类，所有具体事件数据类应继承自此类
    """

    pass


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
        channel (MessageChannel): 通知渠道
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
    channel: Optional[MessageChannel] = Field(None, description="通知渠道")
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
    探索媒体数据源的基类
    """

    name: str = Field(..., description="数据源名称")
    mediaid_prefix: str = Field(..., description="媒体ID的前缀，不含:")
    api_path: str = Field(..., description="媒体数据源API地址")
    filter_params: Optional[Dict[str, Any]] = Field(
        default=None, description="过滤参数"
    )
    filter_ui: Optional[List[dict]] = Field(default=[], description="过滤参数UI配置")
    depends: Optional[Dict[str, list]] = Field(
        default=None, description="UI依赖关系字典"
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


class MediaRecognizeConvertEventData(ChainEventData):
    """
    MediaRecognizeConvert 事件的数据模型

    Attributes:
        # 输入参数
        mediaid (str): 媒体ID，格式为`前缀:ID值`，如 tmdb:12345、douban:1234567
        convert_type (str): 转换类型 仅支持：themoviedb/douban，需要转换为对应的媒体数据并返回

        # 输出参数
        media_dict (dict): TheMovieDb/豆瓣的媒体数据
    """

    # 输入参数
    mediaid: str = Field(..., description="媒体ID")
    convert_type: str = Field(..., description="转换类型（themoviedb/douban）")

    # 输出参数
    media_dict: dict = Field(
        default_factory=dict, description="转换后的媒体信息（TheMovieDb/豆瓣）"
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


class SubscribeEpisodesRefreshEventData(ChainEventData):
    """
    SubscribeEpisodesRefresh 事件的数据模型

    主程序在推算订阅某季总集数时发出，携带按 TMDB 季集数算出的默认值，外部可据自身策略覆盖total_episode（如待定集数）

    Attributes:
        # 输入参数
        tmdbid (Optional[int]): TMDB ID
        doubanid (Optional[str]): 豆瓣 ID
        season (Optional[int]): 季号
        mediainfo (Any): 媒体信息
        current_total_episode (int): 主程序按 TMDB 季集数算出的默认总集数
        subscribe_id (Optional[int]): 订阅 ID；订阅创建场景下尚未入库，为空
        scene (Optional[str]): 触发场景，create/refresh/precheck

        # 输出参数
        updated (bool): 外部是否覆盖了总集数，默认 False
        total_episode (Optional[int]): 覆盖后的总集数，仅在 updated=True 时生效
        source (str): 覆盖来源
        reason (str): 覆盖原因
    """

    # 输入参数
    tmdbid: Optional[int] = Field(default=None, description="TMDB ID")
    doubanid: Optional[str] = Field(default=None, description="豆瓣 ID")
    season: Optional[int] = Field(default=None, description="季号")
    mediainfo: Any = Field(default=None, description="媒体信息")
    current_total_episode: int = Field(default=0, description="按 TMDB 季集数算出的默认总集数")
    subscribe_id: Optional[int] = Field(default=None, description="订阅 ID；创建场景为空")
    scene: Optional[str] = Field(default=None, description="触发场景：create/refresh/precheck")

    # 输出参数
    updated: bool = Field(default=False, description="外部是否覆盖了总集数")
    total_episode: Optional[int] = Field(default=None, description="覆盖后的总集数")
    source: str = Field(default="未知来源", description="覆盖来源")
    reason: str = Field(default="", description="覆盖原因")


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
