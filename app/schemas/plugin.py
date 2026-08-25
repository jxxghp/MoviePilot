from enum import Enum as _Enum
from typing import Literal, Optional, List, Dict, Union

from pydantic import BaseModel, Field, RootModel, field_validator

from app.schemas.common import JsonData


class PluginRuntimeStatus(str, _Enum):
    """插件从源码准备到运行激活的六类状态。"""

    SOURCE_MISSING = "source_missing"
    DEPENDENCY_PENDING = "dependency_pending"
    READY = "ready"
    ACTIVE = "active"
    BLOCKED_BY_POLICY = "blocked_by_policy"
    LOAD_FAILED = "load_failed"


class PluginInstance(BaseModel):
    """持久化一个共享源码插件的独立运行实例。"""

    instance_id: str = Field(description="运行实例 ID，也是配置、数据和路由命名空间")
    source_plugin_id: str = Field(description="提供代码与前端资源的源插件 ID")
    plugin_name: Optional[str] = Field(default=None, description="实例展示名称")
    plugin_desc: Optional[str] = Field(default=None, description="实例展示描述")
    plugin_icon: Optional[str] = Field(default=None, description="实例展示图标")
    mode: Literal["virtual"] = Field(default="virtual", description="实例实现模式")

    @field_validator("instance_id", "source_plugin_id")
    @classmethod
    def validate_plugin_id(cls, value: str) -> str:
        """限制实例标识为可安全用作 Python 类名和路由段的格式。"""
        if not value or not value[0].isalpha() or not value.isalnum():
            raise ValueError("插件 ID 必须以字母开头且只能包含字母和数字")
        if len(value) > 128:
            raise ValueError("插件 ID 长度不能超过 128 个字符")
        return value


class Plugin(BaseModel):
    """
    插件信息
    """
    id: str = None
    # 插件名称
    plugin_name: Optional[str] = None
    # 插件描述
    plugin_desc: Optional[str] = None
    # 插件图标
    plugin_icon: Optional[str] = None
    # 插件版本
    plugin_version: Optional[str] = None
    # 插件标签
    plugin_label: Optional[str] = None
    # 插件作者
    plugin_author: Optional[str] = None
    # 作者主页
    author_url: Optional[str] = None
    # 插件配置项ID前缀
    plugin_config_prefix: Optional[str] = None
    # 加载顺序
    plugin_order: Optional[int] = 0
    # 可使用的用户级别
    auth_level: Optional[int] = 0
    # 是否已安装
    installed: Optional[bool] = False
    # 运行状态
    state: Optional[bool] = False
    # 插件源码、依赖和运行时加载状态
    runtime_status: Optional[PluginRuntimeStatus] = None
    # 是否有详情页面
    has_page: Optional[bool] = False
    # 是否有新版本
    has_update: Optional[bool] = False
    # 主系统版本是否兼容
    system_version_compatible: Optional[bool] = True
    # 主系统版本兼容提示
    system_version_message: Optional[str] = None
    # 主系统版本限定范围
    system_version: Optional[str] = None
    # 是否声明支持通过 GitHub Release 资产安装
    release: Optional[bool] = False
    # 是否本地
    is_local: Optional[bool] = False
    # 仓库地址
    repo_url: Optional[str] = None
    # 安装次数
    install_count: Optional[int] = 0
    # 更新记录
    history: Optional[dict[str, str]] = Field(default_factory=dict)
    # 添加时间，值越小表示越靠后发布
    add_time: Optional[int] = 0
    # 插件公钥
    plugin_public_key: Optional[str] = None
    # 共享代码与前端资源的源插件 ID；普通插件为空
    source_plugin_id: Optional[str] = None
    # 是否为共享源码的虚拟实例
    is_instance: Optional[bool] = False
    # 实例实现模式；存量物理分身为空
    instance_mode: Optional[str] = None


class PluginRuntimeSummary(BaseModel):
    """插件后台收敛状态和前端刷新代次。"""

    ready: bool = Field(description="本轮插件源码、依赖和加载是否已收敛")
    generation: int = Field(description="插件运行状态变化代次")
    pending_count: int = Field(description="仍处于准备阶段的插件数量")
    failed_count: int = Field(description="加载失败或被策略阻止的插件数量")


class PluginCloneRequest(BaseModel):
    """创建虚拟插件分身的请求参数。"""

    suffix: str = Field(
        min_length=1,
        max_length=20,
        pattern=r"^[A-Za-z0-9]+$",
        description="追加到当前插件 ID 后的 ASCII 字母或数字后缀",
    )
    name: str = Field(default="", description="分身展示名称")
    description: str = Field(default="", description="分身展示描述")
    icon: Optional[str] = Field(default=None, description="分身展示图标")
    version: Optional[str] = Field(
        default=None,
        description="兼容旧客户端保留，虚拟分身始终跟随源插件版本",
    )


class PluginSourceIdentity(BaseModel):  # type: ignore[misc]
    """显式换源确认所需的插件来源身份投影。"""

    plugin_id: str = Field(description="物理插件 ID")
    trusted_source_type: str = Field(description="当前可信在线来源类型")
    trusted_source_key: Optional[str] = Field(
        default=None,
        description="规范化的可信在线来源键；未绑定时为空",
    )
    binding_basis: str = Field(description="当前可信来源的建立依据")
    payload_source_type: str = Field(description="最近一次已提交载荷的来源类型")
    payload_source_key: Optional[str] = Field(
        default=None,
        description="最近一次在线载荷的来源键；本地或未知载荷为空",
    )
    revision: int = Field(ge=1, description="显式换源使用的身份 CAS revision")


class PluginSourceChangeRequest(BaseModel):  # type: ignore[misc]
    """管理员显式切换插件在线来源的请求参数。"""

    repo_url: str = Field(min_length=1, description="明确选择的目标插件仓库地址")
    expected_revision: int = Field(
        ge=1,
        description="提交换源时必须匹配的当前身份 revision",
    )
    release_version: Optional[str] = Field(
        default=None,
        description="指定安装的 Release 资产版本；为空时使用当前索引版本",
    )

    @field_validator("repo_url")  # type: ignore[misc]
    @classmethod
    def normalize_repo_url(cls, value: str) -> str:
        """拒绝只含空白的仓库地址，并移除首尾空白。"""
        normalized = value.strip()
        if not normalized:
            raise ValueError("显式换源必须指定目标在线来源")
        return normalized


class PluginDashboard(Plugin):
    """
    插件仪表盘
    """
    id: Optional[str] = None
    # 名称
    name: Optional[str] = None
    # 仪表板key
    key: Optional[str] = None
    # 演染模式
    render_mode: Optional[str] = Field(default="vuetify")
    # 全局配置
    attrs: Optional[dict[str, JsonData]] = Field(default_factory=dict)
    # col列数
    cols: Optional[dict[str, JsonData]] = Field(default_factory=dict)
    # 页面元素
    elements: Optional[List[dict[str, JsonData]]] = Field(default_factory=list)


class PluginSidebarNavItem(BaseModel):
    """
    插件侧栏导航项（前端全页路由）
    """
    plugin_id: str = Field(description="插件 ID")
    nav_key: str = Field(description="导航键，对应 URL 段")
    title: str = Field(description="侧栏标题")
    icon: str = Field(default="mdi-puzzle", description="MDI 图标名")
    section: str = Field(
        description="分组：start / discovery / subscribe / organize / system",
    )
    permission: Optional[str] = Field(
        default=None,
        description="权限：subscribe / discovery / search / manage / admin",
    )
    order: int = Field(default=0, description="同组内排序，越小越靠前")


class PluginRatingRequest(BaseModel):
    """插件评分请求"""

    rating: float = Field(
        ge=0.1,
        le=5.0,
        multiple_of=0.1,
        description="评分，范围 0.1 至 5.0，精确到 0.1",
    )


class PluginRating(BaseModel):
    """插件评分结果"""

    plugin_id: str = Field(description="插件 ID")
    average_rating: float = Field(default=0.0, description="平均评分")
    rating_count: int = Field(default=0, description="评分人数")
    user_rating: Optional[float] = Field(default=None, description="当前安装实例评分")


class PluginRatingMap(RootModel[Dict[str, PluginRating]]):
    """插件 ID 与评分结果的映射。"""


class PluginMemoryInfo(BaseModel):
    """插件内存信息"""
    plugin_id: str = Field(description="插件ID")
    plugin_name: str = Field(description="插件名称")
    plugin_version: str = Field(description="插件版本")
    total_memory_bytes: int = Field(description="总内存使用量(字节)")
    total_memory_mb: float = Field(description="总内存使用量(MB)")
    object_count: int = Field(description="对象数量")
    calculation_time_ms: float = Field(description="计算耗时(毫秒)")
    timestamp: float = Field(description="统计时间戳")
    error: Optional[str] = Field(default=None, description="错误信息")
    object_details: Optional[List[Dict[str, JsonData]]] = Field(default=None, description="大对象详情")


class PluginRemoteInfo(BaseModel):
    """插件模块联邦远程入口。"""

    id: str
    url: str
    name: str
    source_plugin_id: Optional[str] = None


class PluginReleaseItem(BaseModel):
    """可安装的插件 Release 版本。"""

    version: str
    tag_name: str
    name: str
    published_at: Optional[str] = None
    body: str = ""
    asset_name: str
    is_latest: bool = False
    is_current: bool = False


class PluginReleaseData(BaseModel):
    """插件 Release 能力与版本列表。"""

    release_supported: bool = False
    latest_version: Optional[str] = None
    current_version: Optional[str] = None
    items: List[PluginReleaseItem] = Field(default_factory=list)


class PluginFolderConfigData(BaseModel):
    """新版插件文件夹配置（对象格式，含展示配置与插件列表）。"""

    # 文件夹内插件 ID 列表
    plugins: List[str] = Field(default_factory=list)
    # 文件夹排序值
    order: Optional[int] = None
    # 文件夹图标
    icon: Optional[str] = None
    # 文件夹颜色
    color: Optional[str] = None
    # 文件夹渐变
    gradient: Optional[str] = None
    # 文件夹背景
    background: Optional[str] = None
    # 是否显示图标（前端字段为驼峰命名）
    show_icon: Optional[bool] = Field(default=None, alias="showIcon")


class PluginFoldersData(RootModel[Dict[str, Union[List[str], PluginFolderConfigData]]]):
    """插件文件夹与插件配置映射，兼容旧版数组格式与新版对象格式。"""


class PluginDashboardMetaItem(BaseModel):
    """插件仪表板入口摘要。"""

    id: str
    name: Optional[str] = None
    key: Optional[str] = None
