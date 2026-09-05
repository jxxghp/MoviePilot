from enum import Enum as _Enum
from typing import Annotated as _Annotated
from typing import Dict, List, Literal, Optional, Union

from pydantic import AfterValidator as _AfterValidator
from pydantic import BaseModel, ConfigDict, Field, RootModel, field_validator
from pydantic import PrivateAttr as _PrivateAttr

from app.schemas.common import JsonData


class PluginRuntimeStatus(str, _Enum):
    """插件从源码准备到运行激活的六类状态。"""

    SOURCE_MISSING = "source_missing"
    DEPENDENCY_PENDING = "dependency_pending"
    READY = "ready"
    ACTIVE = "active"
    BLOCKED_BY_POLICY = "blocked_by_policy"
    LOAD_FAILED = "load_failed"


class PluginSourceBindingStatus(str, _Enum):
    """已安装插件的在线更新仓库绑定状态。"""

    BOUND = "bound"
    BINDING_REQUIRED = "binding_required"
    LOCAL_ONLY = "local_only"


class PluginUpdateCandidate(BaseModel):  # type: ignore[misc]
    """插件市场为已安装插件选择的当前更新候选。"""

    source_type: Literal["official", "third_party"] = Field(description="候选仓库是官方来源还是第三方来源")
    source_key: str = Field(description="候选仓库的规范来源键")
    repo_url: str = Field(description="候选仓库的公开 GitHub 地址")
    version: str = Field(description="候选仓库当前可安装版本")
    is_bound: bool = Field(description="候选仓库是否为插件当前已绑定仓库")


def _validate_plugin_id(value: str) -> str:
    """限制插件实例标识为可安全用作 Python 类名和路由段的格式。"""
    if not value or not value[0].isalpha() or not value.isalnum():
        raise ValueError("插件 ID 必须以字母开头且只能包含字母和数字")
    if len(value) > 128:
        raise ValueError("插件 ID 长度不能超过 128 个字符")
    return value


_PluginId = _Annotated[str, _AfterValidator(_validate_plugin_id)]


class PluginInstance(BaseModel):
    """持久化一个共享源码插件的独立运行实例。"""

    instance_id: _PluginId = Field(description="运行实例 ID，也是配置、数据和路由命名空间")
    source_plugin_id: _PluginId = Field(description="提供代码与前端资源的源插件 ID")
    plugin_name: Optional[str] = Field(default=None, description="实例展示名称")
    plugin_desc: Optional[str] = Field(default=None, description="实例展示描述")
    plugin_icon: Optional[str] = Field(default=None, description="实例展示图标")
    mode: Literal["virtual"] = Field(default="virtual", description="实例实现模式")


class Plugin(BaseModel):
    """
    插件信息
    """

    _package_version: Optional[str] = _PrivateAttr(default=None)

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
    # 当前市场选择的更新候选；绑定仓库可更新时优先返回该仓库
    update_candidate: Optional[PluginUpdateCandidate] = None
    # 插件仓库绑定状态；仅已安装物理插件由后端投影真实身份
    source_binding_status: PluginSourceBindingStatus = PluginSourceBindingStatus.BOUND
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

    @property
    def package_version(self) -> Optional[str]:
        """读取仅供宿主内部候选选择使用的插件包代际。"""
        return self._package_version

    @package_version.setter
    def package_version(self, value: Optional[str]) -> None:
        """保存插件包代际，但不把它暴露到 API 响应模型。"""
        self._package_version = value


class PluginRuntimeSummary(BaseModel):
    """插件后台收敛状态和前端刷新代次。"""

    ready: bool = Field(description="本轮插件源码、依赖和加载是否已收敛")
    generation: int = Field(description="插件运行状态变化代次")
    pending_count: int = Field(description="仍处于准备阶段的插件数量")
    failed_count: int = Field(description="加载失败或被策略阻止的插件数量")
    restart_required_plugin_ids: List[str] = Field(
        default_factory=list,
        description="重启后才能完整激活新原生依赖的物理插件 ID",
    )


class PluginRuntimeCommandCapability(BaseModel):  # type: ignore[misc]
    """插件运行时注册命令的安全只读投影。"""

    cmd: str = Field(description="命令标识")
    desc: Optional[str] = Field(default=None, description="命令说明")
    plugin_id: Optional[str] = Field(default=None, description="注册命令的插件 ID")


class PluginRuntimeActionCapability(BaseModel):  # type: ignore[misc]
    """插件运行时注册动作的安全只读投影。"""

    id: str = Field(description="动作标识")
    name: Optional[str] = Field(default=None, description="动作名称")


class PluginRuntimeActionGroup(BaseModel):  # type: ignore[misc]
    """按插件归组的运行时动作投影。"""

    plugin_id: Optional[str] = Field(default=None, description="注册动作的插件 ID")
    plugin_name: Optional[str] = Field(default=None, description="插件名称")
    actions: List[PluginRuntimeActionCapability] = Field(default_factory=list)


class PluginRuntimeServiceCapability(BaseModel):  # type: ignore[misc]
    """插件定时服务的安全只读投影。"""

    id: str = Field(description="服务标识")
    name: Optional[str] = Field(default=None, description="服务名称")
    trigger: Optional[str] = Field(default=None, description="定时触发器说明")


class PluginRuntimeCapabilities(BaseModel):  # type: ignore[misc]
    """插件命令、动作和定时服务的公共安全能力快照。"""

    commands: List[PluginRuntimeCommandCapability] = Field(default_factory=list)
    actions: List[PluginRuntimeActionGroup] = Field(default_factory=list)
    services: List[PluginRuntimeServiceCapability] = Field(default_factory=list)


class PluginDataKeySummary(BaseModel):  # type: ignore[misc]
    """单个插件持久化键的不含值诊断摘要。"""

    key: str = Field(description="持久化数据键")
    value_type: Literal["null", "boolean", "number", "string", "array", "object", "unknown"] = Field(
        description="值的 JSON 类型"
    )
    serialized_chars: Optional[int] = Field(
        default=None,
        ge=0,
        description="JSON 紧凑序列化字符数；异常值为空",
    )
    sensitive: bool = Field(description="键名是否符合凭据字段规则")


class PluginDataSummary(BaseModel):  # type: ignore[misc]
    """插件持久化数据的不含原值诊断摘要。"""

    plugin_id: str = Field(description="插件 ID")
    plugin_name: Optional[str] = Field(default=None, description="插件名称")
    plugin_version: Optional[str] = Field(default=None, description="插件版本")
    state: Optional[bool] = Field(default=None, description="插件是否启用")
    count: int = Field(ge=0, description="持久化数据项总数")
    total_chars: int = Field(ge=0, description="所有可序列化值的字符数总和")
    keys: List[PluginDataKeySummary] = Field(default_factory=list, description="有界键摘要")
    keys_truncated: bool = Field(description="是否还有未返回的键摘要")


class PluginInstallOutcome(BaseModel):
    """插件载荷写入成功后的前端反馈依据。"""

    restart_required: bool = Field(description="本次依赖更新是否需要重启 MoviePilot 才能完成")


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


class PluginSourceCandidate(BaseModel):  # type: ignore[misc]
    """一个可供管理员识别的脱敏插件来源候选。"""

    source_type: Literal["official", "third_party", "local"] = Field(description="来源类型；本地候选不公开路径")
    source_key: Optional[str] = Field(
        default=None,
        description="规范化在线来源键；本地候选为空",
    )
    repo_url: Optional[str] = Field(
        default=None,
        description="可明确选择的在线仓库地址；本地候选为空",
    )
    package_generation: Literal["v1", "v2", "v3"] = Field(description="当前运行时会采用的插件包代际")
    plugin_version: Optional[str] = Field(
        default=None,
        description="该来源当前可安装的插件版本",
    )


class PluginSourceOptions(BaseModel):  # type: ignore[misc]
    """来源选择界面所需的当前身份、候选和准入状态。"""

    plugin_id: str = Field(description="物理插件 ID")
    inventory_complete: bool = Field(description="本轮配置市场是否全部得到确定读取结果")
    selection_status: Literal["selected", "unavailable", "conflict", "incomplete"] = Field(
        description="未指定新来源时的当前准入状态"
    )
    selection_reason: str = Field(description="当前准入状态的人类可读原因")
    identity: Optional[PluginSourceIdentity] = Field(
        default=None,
        description="已安装插件的来源身份；未建立身份时为空",
    )
    candidates: List[PluginSourceCandidate] = Field(
        default_factory=list,
        description="按来源归并后的在线候选及可选本地候选",
    )


class PluginSourceInstallRequest(BaseModel):  # type: ignore[misc]
    """管理员为未绑定插件明确选择初始在线来源的请求参数。"""

    repo_url: str = Field(min_length=1, description="明确选择的目标插件仓库地址")
    release_version: Optional[str] = Field(
        default=None,
        description="指定安装的 Release 资产版本；为空时使用当前索引版本",
    )
    force: bool = Field(
        default=False,
        description="是否强制重新下载并安装所选来源载荷",
    )

    @field_validator("repo_url")  # type: ignore[misc]
    @classmethod
    def normalize_repo_url(cls, value: str) -> str:
        """拒绝只含空白或本地路径标识的来源选择。"""
        normalized = value.strip()
        if not normalized:
            raise ValueError("显式安装必须指定目标在线来源")
        if normalized.startswith("local://"):
            raise ValueError("显式来源安装只接受在线插件仓库")
        return normalized


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
        """拒绝只含空白或本地路径标识的换源目标。"""
        normalized = value.strip()
        if not normalized:
            raise ValueError("显式换源必须指定目标在线来源")
        if normalized.startswith("local://"):
            raise ValueError("显式换源只接受在线插件仓库")
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
    plugins: List[str] = Field(
        default_factory=list,
        description="Ordered installed plugin IDs assigned to this folder.",
    )
    # 文件夹排序值
    order: Optional[int] = Field(default=None, description="Folder display-order value.")
    # 文件夹图标
    icon: Optional[str] = Field(default=None, description="Optional folder icon name.")
    # 文件夹颜色
    color: Optional[str] = Field(default=None, description="Optional folder foreground color.")
    # 文件夹渐变
    gradient: Optional[str] = Field(default=None, description="Optional folder gradient definition.")
    # 文件夹背景
    background: Optional[str] = Field(default=None, description="Optional folder background color or style.")
    # 是否显示图标（前端字段为驼峰命名）
    show_icon: Optional[bool] = Field(
        default=None,
        alias="showIcon",
        description="Whether the frontend should display the folder icon.",
    )


class PluginFoldersData(RootModel[Dict[str, Union[List[str], PluginFolderConfigData]]]):
    """插件文件夹与插件配置映射，兼容旧版数组格式与新版对象格式。"""


class PluginFolderUpdateRequest(BaseModel):  # type: ignore[misc]
    """插件文件夹名称和展示字段的增量更新请求。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    new_name: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=128,
        description="Optional replacement folder name.",
    )
    icon: Optional[str] = Field(default=None, description="Optional folder icon name.")
    color: Optional[str] = Field(
        default=None,
        description="Optional folder foreground color.",
    )
    gradient: Optional[str] = Field(
        default=None,
        description="Optional folder gradient definition.",
    )
    background: Optional[str] = Field(
        default=None,
        description="Optional folder background color or style.",
    )
    show_icon: Optional[bool] = Field(
        default=None,
        alias="showIcon",
        description="Whether the frontend should display the folder icon.",
    )


class PluginFolderPluginsUpdateRequest(BaseModel):  # type: ignore[misc]
    """插件文件夹成员顺序的条件替换请求。"""

    plugins: List[str] = Field(description="Ordered installed plugin IDs assigned to this folder.")
    expected_plugins: Optional[List[str]] = Field(
        default=None,
        description="Last observed ordered plugin IDs used to reject stale replacements.",
    )


class PluginDashboardMetaItem(BaseModel):
    """插件仪表板入口摘要。"""

    id: str
    name: Optional[str] = None
    key: Optional[str] = None
