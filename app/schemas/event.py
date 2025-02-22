from pathlib import Path
from typing import Optional, Dict, Any, List, Set

from pydantic import BaseModel, Field, root_validator

from app.schemas import MessageChannel, FileItem


class Event(BaseModel):
    """
    事件模型
    """
    event_type: str = Field(..., description="事件类型")
    event_data: Optional[dict] = Field({}, description="事件数据")
    priority: Optional[int] = Field(0, description="事件优先级")


class BaseEventData(BaseModel):
    """
    事件数据的基类，所有具体事件数据类应继承自此类
    """
    pass


class ChainEventData(BaseEventData):
    """
    链式事件数据的基类，所有具体事件数据类应继承自此类
    """
    pass


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
    username: Optional[str] = Field(None, description="用户名，适用于 'password' 认证类型")
    password: Optional[str] = Field(None, description="用户密码，适用于 'password' 认证类型")
    mfa_code: Optional[str] = Field(None, description="一次性密码，目前仅适用于 'password' 认证类型")
    code: Optional[str] = Field(None, description="授权码，适用于 'authorization_code' 认证类型")
    grant_type: str = Field(..., description="认证类型，如 'password', 'authorization_code', 'client_credentials'")
    # scope: List[str] = Field(default_factory=list, description="权限范围，如 ['read', 'write']")

    # 输出参数
    # grant_type 为 authorization_code 时，输出参数包括 username、token、channel、service
    token: Optional[str] = Field(default=None, description="认证令牌")
    channel: Optional[str] = Field(default=None, description="认证渠道")
    service: Optional[str] = Field(default=None, description="服务名称")

    @root_validator(pre=True)
    def check_fields_based_on_grant_type(cls, values):  # noqa
        grant_type = values.get("grant_type")
        if not grant_type:
            values["grant_type"] = "password"
            grant_type = "password"

        if grant_type == "password":
            if not values.get("username") or not values.get("password"):
                raise ValueError("username and password are required for grant_type 'password'")

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
    status: str = Field(..., description="认证状态, 包含 'triggered' 表示认证触发，'completed' 表示认证成功")
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


class TransferRenameEventData(ChainEventData):
    """
    TransferRename 事件的数据模型

    Attributes:
        # 输入参数
        template_string (str): Jinja2 模板字符串
        rename_dict (dict): 渲染上下文
        render_str (str): 渲染生成的字符串
        path (Optional[Path]): 当前文件的目标路径

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
    updated_contexts: Optional[List[Any]] = Field(default=None, description="已更新的资源上下文列表")
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
    options: Optional[dict] = Field(None, description="其他参数")

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


class DiscoverMediaSource(BaseModel):
    """
    探索媒体数据源的基类
    """
    name: str = Field(..., description="数据源名称")
    mediaid_prefix: str = Field(..., description="媒体ID的前缀，不含:")
    api_path: str = Field(..., description="媒体数据源API地址")
    filter_params: Optional[Dict[str, Any]] = Field(default=None, description="过滤参数")
    filter_ui: Optional[List[dict]] = Field(default=[], description="过滤参数UI配置")
    depends: Optional[Dict[str, list]] = Field(default=None, description="UI依赖关系字典")


class DiscoverSourceEventData(ChainEventData):
    """
    DiscoverSource 事件的数据模型

    Attributes:
        # 输出参数
        extra_sources (List[DiscoverMediaSource]): 额外媒体数据源
    """
    # 输出参数
    extra_sources: List[DiscoverMediaSource] = Field(default_factory=list, description="额外媒体数据源")


class RecommendMediaSource(BaseModel):
    """
    推荐媒体数据源的基类
    """
    name: str = Field(..., description="数据源名称")
    api_path: str = Field(..., description="媒体数据源API地址")


class RecommendSourceEventData(ChainEventData):
    """
    RecommendSource 事件的数据模型

    Attributes:
        # 输出参数
        extra_sources (List[RecommendMediaSource]): 额外媒体数据源
    """
    # 输出参数
    extra_sources: List[RecommendMediaSource] = Field(default_factory=list, description="额外媒体数据源")


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
    media_dict: dict = Field(default=dict, description="转换后的媒体信息（TheMovieDb/豆瓣）")
