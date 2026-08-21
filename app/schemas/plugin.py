from datetime import datetime
from enum import Enum as _Enum
from typing import Optional, List, Dict, Union

from pydantic import BaseModel, Field, RootModel

from app.schemas.common import JsonData


class PluginRuntimeStatus(str, _Enum):
    """插件从源码准备到运行激活的六类状态。"""

    SOURCE_MISSING = "source_missing"
    DEPENDENCY_PENDING = "dependency_pending"
    READY = "ready"
    ACTIVE = "active"
    BLOCKED_BY_POLICY = "blocked_by_policy"
    LOAD_FAILED = "load_failed"


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


class PluginRuntimeSummary(BaseModel):
    """插件后台收敛状态和前端刷新代次。"""

    ready: bool = Field(description="本轮插件源码、依赖和加载是否已收敛")
    generation: int = Field(description="插件运行状态变化代次")
    pending_count: int = Field(description="仍处于准备阶段的插件数量")
    failed_count: int = Field(description="加载失败或被策略阻止的插件数量")


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
    instance_id: Optional[str] = Field(
        default=None, description="实例标识，默认实例取值为 default"
    )
    instance_key: Optional[str] = Field(
        default=None, description="实例键，默认实例的实例键等于插件 ID"
    )


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
    # 该远程入口所属实例运行的插件版本号，插件未声明 plugin_version 时为空
    version: Optional[str] = Field(default=None, description="插件版本号")
    # 按版本区分的联邦远程标识，格式为 `{id}#{version}`；无版本信息时与 id 相同。
    # Module Federation 的远程名是浏览器端全局单一键空间，同一插件的两个版本
    # 若都用 id 注册会互相覆盖，前端改用该字段注册可让不同版本天然不同名
    remote_key: Optional[str] = Field(default=None, description="按版本区分的联邦远程标识")


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


class ServiceInstanceRequirementInfo(BaseModel):
    """一个扩展点作用于哪一族服务实例的坐标。

    前端据此渲染实例选择器：``capability`` 指出候选从哪一族的配置列表来，``types``
    非空时只有类型落在其中的实例才是候选。不含实例名——选哪一台是用户的选择，声明
    期不存在，宿主只负责把选择项交给他。
    """

    capability: str = Field(description="能力标签，候选实例取自该族的配置列表")
    types: list[str] = Field(
        default_factory=list, description="收窄到的类型标识，为空表示该族任意类型都可选"
    )


class PluginDashboardMetaItem(BaseModel):
    """插件仪表板入口摘要。"""

    id: str
    name: Optional[str] = None
    key: Optional[str] = None
    # 实例标识，默认实例取值为 default
    instance_id: Optional[str] = None
    # 实例键，默认实例的实例键等于插件 ID
    instance_key: Optional[str] = None
    # 本仪表盘作用于哪一族服务实例，未声明时为 None
    requires_service_instance: Optional[ServiceInstanceRequirementInfo] = None


class PluginInstanceInfo(BaseModel):
    """插件实例信息，用于实例列表与创建实例的返回。"""

    instance_id: str = Field(description="实例标识，默认实例取值为 default")
    instance_key: str = Field(description="实例键，默认实例的实例键等于插件 ID")
    running: bool = Field(default=False, description="实例是否处于运行态")
    state: bool = Field(default=False, description="实例启用状态，未处于运行态时为 False")
    is_default_target: bool = Field(
        default=False, description="是否为该插件当前的默认调用目标"
    )


class PluginInstanceCreate(BaseModel):
    """创建插件实例请求体。"""

    instance_id: str = Field(
        description="新实例标识，不能包含实例键分隔符 @，且需满足单层目录名安全校验"
    )
    config: Optional[Dict[str, JsonData]] = Field(
        default=None, description="实例初始配置，为空时使用空字典"
    )


class PluginInstalledVersion(BaseModel):
    """插件已安装的一个版本。"""

    version: str = Field(description="版本号")
    directory: str = Field(description="版本目录名")
    installed_at: Optional[str] = Field(default=None, description="登记的安装时间")
    source: Optional[str] = Field(default=None, description="版本来源：market/local/migrated")
    is_current: bool = Field(default=False, description="是否为版本元信息登记的当前版本")


class PluginInstanceVersionBinding(BaseModel):
    """插件实例的版本绑定情况。"""

    instance_id: str = Field(description="实例标识，默认实例取值为 default")
    instance_key: str = Field(description="实例键，默认实例的实例键等于插件 ID")
    plugin_version: Optional[str] = Field(
        default=None, description="已生效版本；为空表示尚未成功启动过任何版本"
    )
    follow_default_version: bool = Field(
        default=True, description="是否跟随默认实例的版本"
    )
    target_version: Optional[str] = Field(
        default=None,
        description="期望版本，与已生效版本不一致表示待切换；无从解析时为空",
    )
    running: bool = Field(default=False, description="实例是否处于运行态")


class PluginVersionOverview(BaseModel):
    """插件已装版本与各实例绑定情况。"""

    plugin_id: str = Field(description="插件 ID")
    current_version: Optional[str] = Field(
        default=None, description="版本元信息登记的当前版本"
    )
    installed_versions: List[PluginInstalledVersion] = Field(
        default_factory=list, description="磁盘上可加载的已装版本"
    )
    instances: List[PluginInstanceVersionBinding] = Field(
        default_factory=list, description="各实例的版本绑定情况"
    )


class PluginInstanceVersionSet(BaseModel):
    """设置插件实例版本绑定请求体。"""

    follow_default_version: bool = Field(
        default=True, description="是否跟随默认实例的版本"
    )
    plugin_version: Optional[str] = Field(
        default=None,
        description="目标版本号；不跟随默认实例时必填，且必须是该插件已安装的版本",
    )


class PluginVersionRecycleResult(BaseModel):
    """插件版本目录回收结果。"""

    plugin_id: str = Field(description="插件 ID")
    removed: List[str] = Field(default_factory=list, description="本次删除的版本号列表")
    kept: Dict[str, str] = Field(
        default_factory=dict, description="保留下来的版本号到保留理由的映射"
    )


class PluginInstanceLogLevelInfo(BaseModel):
    """插件实例的日志等级设置与当前生效值。"""

    instance_id: str = Field(description="实例标识，默认实例取值为 default")
    configured_level: Optional[str] = Field(
        default=None, description="配置的等级覆盖；为空表示跟随全局等级"
    )
    expires_at: Optional[datetime] = Field(
        default=None, description="覆盖失效时间；为空表示不过期或未设置覆盖"
    )
    effective_level: str = Field(description="当前生效的等级，已按失效时间做过回落判定")


class PluginInstanceLogLevelSet(BaseModel):
    """设置插件实例日志等级请求体。"""

    level: str = Field(description="目标等级，取值须在 LOG_LEVELS 内：DEBUG/INFO/WARN/ERROR")
    expires_at: Optional[datetime] = Field(
        default=None, description="覆盖失效时间；为空表示不过期"
    )


class PluginInstanceLogFileInfo(BaseModel):
    """插件实例日志目录下的单个日志文件信息。"""

    name: str = Field(description="文件名，如 plugin.log 或滚动备份 plugin.log.1")
    size: int = Field(description="文件大小，单位字节")
    modified_at: datetime = Field(description="文件最后修改时间")
