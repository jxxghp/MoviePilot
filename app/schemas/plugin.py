"""
插件配置、运行状态、来源身份及前端扩展的数据结构。
"""

from datetime import datetime as _datetime
from enum import Enum as _Enum
from typing import Annotated as _Annotated
from typing import Dict, List, Literal, Optional, Union

from pydantic import AfterValidator as _AfterValidator
from pydantic import BaseModel, ConfigDict, Field, RootModel, field_validator
from pydantic import BeforeValidator as _BeforeValidator
from pydantic import PrivateAttr as _PrivateAttr
from pydantic import computed_field as _computed_field

from app.domain.plugin import is_physical_plugin_id as _is_physical_plugin_id
from app.domain.plugin import normalize_plugin_documentation_url as _normalize_plugin_documentation_url
from app.schemas.common import JsonData


class PluginRuntimeStatus(str, _Enum):
    """
    插件从来源同步、依赖准备到运行激活的状态。
    """

    # 插件来源同步失败
    SYNC_FAILED = "sync_failed"
    # 未找到插件源码
    SOURCE_MISSING = "source_missing"
    # 正在等待依赖准备完成
    DEPENDENCY_PENDING = "dependency_pending"
    # 已具备加载条件
    READY = "ready"
    # 插件已加载并运行
    ACTIVE = "active"
    # 被运行策略阻止加载
    BLOCKED_BY_POLICY = "blocked_by_policy"
    # 插件加载失败
    LOAD_FAILED = "load_failed"
    # 插件自身声明与当前解释器运行时不兼容（当前只有 free-threaded 一个维度）
    INCOMPATIBLE_RUNTIME = "incompatible_runtime"


class PluginSourceBindingStatus(str, _Enum):
    """
    已安装插件的在线更新仓库绑定状态。
    """

    # 已绑定在线更新来源
    BOUND = "bound"
    # 需要选择并绑定更新来源
    BINDING_REQUIRED = "binding_required"
    # 仅使用本地源码，不绑定在线来源
    LOCAL_ONLY = "local_only"


class PluginUpdateCandidate(BaseModel):
    """
    插件市场为已安装插件选择的当前更新候选。
    """

    source_type: Literal["official", "third_party"] = Field(description="候选仓库是官方来源还是第三方来源")
    source_key: str = Field(description="候选仓库的规范来源键")
    repo_url: str = Field(description="候选仓库的公开 GitHub 地址")
    version: str = Field(description="候选仓库当前可安装版本")
    is_bound: bool = Field(description="候选仓库是否为插件当前已绑定仓库")


def validate_plugin_id(value: str) -> str:
    """
    限制插件实例标识为可安全用作 Python 类名和路由段的格式。

    公开可调用：凡是把实例 ID 拼进文件系统路径、模块名或路由段的入口都应当先过这道
    校验，而不是各自另写一套字符白名单——多套规则之间迟早出现缝隙。
    实例与源插件复用安装侧的 ASCII 身份规则，兼容既有下划线 ID；不改写大小写，
    以免改变配置、数据和路由命名空间。
    :param value: 待校验的插件实例 ID
    :return: 原样返回的合法 ID
    :raise ValueError: ID 为空、不以字母开头、含 ASCII 字母数字及下划线以外的字符或超长
    """
    if len(value) > 128:
        raise ValueError("插件 ID 长度不能超过 128 个字符")
    if not _is_physical_plugin_id(value):
        raise ValueError("插件 ID 必须以字母开头且只能包含 ASCII 字母、数字或下划线")
    return value


_PluginId = _Annotated[str, _AfterValidator(validate_plugin_id)]


class PluginInstance(BaseModel):
    """
    持久化一个共享源码插件的独立运行实例。
    """

    instance_id: _PluginId = Field(description="运行实例 ID，也是配置、数据和路由命名空间")
    source_plugin_id: _PluginId = Field(description="提供代码与前端资源的源插件 ID")
    plugin_name: Optional[str] = Field(default=None, description="实例展示名称")
    plugin_desc: Optional[str] = Field(default=None, description="实例展示描述")
    plugin_icon: Optional[str] = Field(default=None, description="实例展示图标")
    is_default_target: bool = Field(
        default=False,
        description="该实例是否为所属源插件的默认调用目标",
    )
    # 默认为真而库列默认为假：这个模型是运行时描述，被构造出来就是要拿去装载的；
    # 库列默认为假则是为了让「只写了配置」这类隐式建出的行不因此变成可装载
    is_enabled: bool = Field(
        default=True,
        description="这份配置是否应当被实例化并启动；置假即停用，配置与展示信息留存待再次启用",
    )

    @property
    def is_host(self) -> bool:
        """
        该实例是否为源插件本体自身，而非共享其源码的分身。
        """
        return self.instance_id == self.source_plugin_id

    # mypy 尚不识别 computed_field 与 property 叠加时的装饰器顺序。
    @_computed_field(  # type: ignore[prop-decorator]
        description="实例实现模式：virtual 为共享源码的分身，host 为源插件本体自身",
    )
    @property
    def mode(self) -> Literal["virtual", "host"]:
        """
        由一对身份 ID 派生实例角色，而非另存一份可能失步的副本。
        """
        return "host" if self.is_host else "virtual"


class PluginInstanceEnabledRequest(BaseModel):
    """
    启用或停用一个插件实例的请求参数。
    """

    enabled: bool = Field(
        description="目标启用状态；置假即停用，业务参数与展示信息原样留存等待再次启用"
    )


class PluginInstancePurgeRequest(BaseModel):
    """
    彻底清理一个插件实例时选定的删除范围。

    各项默认为假：清理不可逆，漏选一项只是少删了东西，多选一项则可能毁掉用户特意
    保留的数据，因而由调用方逐项明确给出，服务端不替它补默认值。
    """

    config: bool = Field(default=False, description="是否删除该实例的业务参数")
    plugin_data: bool = Field(default=False, description="是否删除该实例在插件数据表中的行")
    own_database: bool = Field(default=False, description="是否销毁该实例的自有数据库")
    data_directory: bool = Field(
        default=False,
        description="是否删除该实例在插件数据目录下的整个目录；选中时自有数据库必然一并销毁",
    )


class PluginInstancePurgeOutcome(BaseModel):
    """
    彻底清理的执行结果。
    """

    purged: List[str] = Field(default_factory=list, description="实际清掉的范围标识")
    instance_removed: bool = Field(
        default=False,
        description="实例行是否随之删除；分身会删，本体保留——它还承载着该插件的展示与启用状态",
    )


class PluginInstanceLogLevel(BaseModel):
    """
    单个实例的日志等级设置与生效结果。
    """

    instance_id: str = Field(description="实例 ID")
    configured_level: Optional[str] = Field(default=None, description="该实例设置的日志等级覆盖，None 表示未设置或已过期")
    expires_at: Optional[_datetime] = Field(default=None, description="日志等级覆盖的失效时间，None 表示不过期")
    effective_level: str = Field(description="按过期回落判定后实际生效的日志等级")


class PluginInstanceLogLevelOverview(BaseModel):
    """
    插件全部实例（含本体）的日志等级设置总览。
    """

    plugin_id: str = Field(description="插件 ID")
    instances: List[PluginInstanceLogLevel] = Field(
        default_factory=list, description="该插件全部实例的日志等级设置，首项固定是本体自身"
    )


class PluginInstanceLogLevelUpdateRequest(BaseModel):
    """
    设置实例日志等级覆盖的请求参数。
    """

    level: str = Field(description="目标日志等级，如 DEBUG、INFO、WARNING、ERROR、CRITICAL")
    expires_at: Optional[_datetime] = Field(
        default=None,
        description="覆盖失效时间，None 表示不过期；不带时区时按 UTC 解读",
    )


class Plugin(BaseModel):
    """
    插件信息。

    :ivar _package_version: 本地安装包版本的缓存值
    """

    _package_version: Optional[str] = _PrivateAttr(default=None)

    id: Optional[str] = Field(default=None, description="记录标识")
    plugin_name: Optional[str] = Field(default=None, description="插件名称")
    plugin_desc: Optional[str] = Field(default=None, description="插件描述")
    plugin_icon: Optional[str] = Field(default=None, description="插件图标")
    plugin_version: Optional[str] = Field(default=None, description="插件版本")
    plugin_label: Optional[str] = Field(default=None, description="插件标签")
    plugin_author: Optional[str] = Field(default=None, description="插件作者")
    author_url: Optional[str] = Field(default=None, description="作者主页")
    plugin_readme: Optional[str] = Field(default=None, description="插件使用说明的 HTTP(S) 外链；未声明时为空")
    plugin_config_prefix: Optional[str] = Field(default=None, description="插件配置项ID前缀")
    plugin_order: Optional[int] = Field(default=0, description="加载顺序")
    auth_level: Optional[int] = Field(default=0, description="可使用的用户级别")
    installed: Optional[bool] = Field(default=False, description="是否已安装")
    state: Optional[bool] = Field(default=False, description="运行状态")
    runtime_status: Optional[PluginRuntimeStatus] = Field(default=None, description="插件源码、依赖和运行时加载状态")
    has_page: Optional[bool] = Field(default=False, description="是否有详情页面")
    has_update: Optional[bool] = Field(default=False, description="是否有新版本")
    update_candidate: Optional[PluginUpdateCandidate] = Field(default=None, description="当前市场选择的更新候选；绑定仓库可更新时优先返回该仓库")
    source_binding_status: PluginSourceBindingStatus = Field(
        default=PluginSourceBindingStatus.BOUND,
        description="插件仓库绑定状态；仅已安装物理插件由后端投影真实身份",
    )
    system_version_compatible: Optional[bool] = Field(default=True, description="主系统版本是否兼容")
    system_version_message: Optional[str] = Field(default=None, description="主系统版本兼容提示")
    system_version: Optional[str] = Field(default=None, description="主系统版本限定范围")
    runtime_compatible: Optional[bool] = Field(
        default=True,
        description="插件声明与当前解释器运行时是否兼容；free-threaded（v3t）下显式声明不支持时为 False",
    )
    runtime_message: Optional[str] = Field(default=None, description="运行时不兼容的用户可读原因")
    release: Optional[bool] = Field(default=False, description="是否声明支持通过 GitHub Release 资产安装")
    is_local: Optional[bool] = Field(default=False, description="是否本地")
    repo_url: Optional[str] = Field(default=None, description="仓库地址")
    install_count: Optional[int] = Field(default=0, description="安装次数")
    history: Optional[dict[str, str]] = Field(default_factory=dict, description="更新记录")
    add_time: Optional[int] = Field(default=0, description="添加时间，值越小表示越靠后发布")
    plugin_public_key: Optional[str] = Field(default=None, description="插件公钥")
    source_plugin_id: Optional[str] = Field(default=None, description="共享代码与前端资源的源插件 ID；普通插件为空")
    is_instance: Optional[bool] = Field(default=False, description="是否为共享源码的虚拟实例")
    instance_mode: Optional[str] = Field(default=None, description="实例实现模式；存量物理分身为空")
    is_default_target: Optional[bool] = Field(default=False, description="该实例是否为所属源插件的默认调用目标")
    is_enabled: Optional[bool] = Field(default=True, description="该实例是否应当被实例化并启动；与 state 不同，后者说的是此刻在不在跑")
    log_level_effective: Optional[str] = Field(default=None, description="该实例当前生效的日志等级覆盖；未设置覆盖或覆盖已过期回落全局等级时为空")

    @field_validator("plugin_readme", mode="before")
    @classmethod
    def validate_plugin_readme(cls, value: object) -> Optional[str]:
        """
        丢弃不可作为浏览器外链的文档地址，保持存量插件元数据兼容。
        """
        return _normalize_plugin_documentation_url(value)

    @property
    def package_version(self) -> Optional[str]:
        """
        读取仅供宿主内部候选选择使用的插件包代际。
        """
        return self._package_version

    @package_version.setter
    def package_version(self, value: Optional[str]) -> None:
        """
        保存插件包代际，但不把它暴露到 API 响应模型。
        """
        self._package_version = value


class PluginDocumentation(BaseModel):
    """
    插件详情按需查询的使用说明入口，不包含远端文档正文。
    """

    url: Optional[str] = Field(default=None, description="显式声明或已确认存在的 README 外链；不可用时为空")


class PluginLifecycleActivity(BaseModel):
    """
    当前仍持有执行权的插件操作，不包含配置或凭据。
    """

    operation: str = Field(description="正在执行的生命周期操作或变更事务")
    plugin_id: Optional[str] = Field(default=None, description="当前处理的插件 ID；全局操作准备阶段为空")
    hook: Optional[str] = Field(default=None, description="当前执行的插件钩子或加载阶段；未进入时为空")
    elapsed_seconds: float = Field(ge=0, description="本次操作已经持续的秒数")
    hook_elapsed_seconds: float = Field(default=0, ge=0, description="当前钩子已经持续的秒数")


class PluginRuntimeSummary(BaseModel):
    """
    插件后台收敛状态和前端刷新代次。
    """

    ready: bool = Field(description="本轮插件源码、依赖和加载已收敛，且没有在途变更或生命周期操作")
    generation: int = Field(description="插件运行状态变化代次")
    pending_count: int = Field(description="仍处于准备阶段的插件数量")
    failed_count: int = Field(description="加载失败或被策略阻止的插件数量")
    restart_required_plugin_ids: List[str] = Field(
        default_factory=list,
        description="重启后才能完整激活新原生依赖的物理插件 ID",
    )
    gil_enabled_plugin_ids: List[str] = Field(
        default_factory=list,
        description="当前进程内加载时导致 free-threaded 运行时回退到 GIL 的插件 ID",
    )
    active_operation: Optional[PluginLifecycleActivity] = Field(
        default=None,
        description="仍持有执行权的插件操作；调用方超时后仍会保留，直至实际结束",
    )


class PluginRuntimeCommandCapability(BaseModel):
    """
    插件运行时注册命令的安全只读投影。
    """

    cmd: str = Field(description="命令标识")
    desc: Optional[str] = Field(default=None, description="命令说明")
    plugin_id: Optional[str] = Field(default=None, description="注册命令的插件 ID")


class PluginRuntimeActionCapability(BaseModel):
    """
    插件运行时注册动作的安全只读投影。
    """

    id: str = Field(description="动作标识")
    name: Optional[str] = Field(default=None, description="动作名称")


class PluginRuntimeActionGroup(BaseModel):
    """
    按插件归组的运行时动作投影。
    """

    plugin_id: Optional[str] = Field(default=None, description="注册动作的插件 ID")
    plugin_name: Optional[str] = Field(default=None, description="插件名称")
    actions: List[PluginRuntimeActionCapability] = Field(default_factory=list, description="可用动作列表")


class PluginRuntimeServiceCapability(BaseModel):
    """
    插件定时服务的安全只读投影。
    """

    id: str = Field(description="服务标识")
    name: Optional[str] = Field(default=None, description="服务名称")
    trigger: Optional[str] = Field(default=None, description="定时触发器说明")


class PluginRuntimeCapabilities(BaseModel):
    """
    插件命令、动作和定时服务的公共安全能力快照。
    """

    commands: List[PluginRuntimeCommandCapability] = Field(default_factory=list, description="菜单命令")
    actions: List[PluginRuntimeActionGroup] = Field(default_factory=list, description="可用动作列表")
    services: List[PluginRuntimeServiceCapability] = Field(default_factory=list, description="插件声明的服务能力")


class PluginDataKeySummary(BaseModel):
    """
    单个插件持久化键的不含值诊断摘要。
    """

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


class PluginDataSummary(BaseModel):
    """
    插件持久化数据的不含原值诊断摘要。
    """

    plugin_id: str = Field(description="插件 ID")
    plugin_name: Optional[str] = Field(default=None, description="插件名称")
    plugin_version: Optional[str] = Field(default=None, description="插件版本")
    state: Optional[bool] = Field(default=None, description="插件是否启用")
    count: int = Field(ge=0, description="持久化数据项总数")
    total_chars: int = Field(ge=0, description="所有可序列化值的字符数总和")
    keys: List[PluginDataKeySummary] = Field(default_factory=list, description="有界键摘要")
    keys_truncated: bool = Field(description="是否还有未返回的键摘要")


class PluginInstallOutcome(BaseModel):
    """
    插件载荷写入成功后的前端反馈依据。
    """

    restart_required: bool = Field(description="本次依赖更新是否需要重启 MoviePilot 才能完成")


def _blank_clone_suffix_to_none(value: object) -> object:
    """
    把「没填后缀」的三种写法归一成同一个值。

    前端的后缀输入框留空时可能整个字段不带、带 null，也可能带一个空串或只有空白；
    三者表达的都是「由服务端挑一个」。不归一的话空串会撞上格式校验，用户看到的是
    一条与他的操作对不上的格式错误，而不是自动分配。
    """
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return value


_CloneSuffix = _Annotated[Optional[str], _BeforeValidator(_blank_clone_suffix_to_none)]


class PluginCloneRequest(BaseModel):
    """
    创建虚拟插件分身的请求参数。
    """

    # 格式与长度在这一层判定：非法后缀拼出的实例 ID 用不了 Python 类名与路由段，
    # 等落库之后再报错意味着已经写进一行、还要靠回滚把它擦掉
    suffix: _CloneSuffix = Field(
        default=None,
        max_length=20,
        pattern=r"^[A-Za-z0-9]+$",
        description="追加到当前插件 ID 后的 ASCII 字母或数字后缀；留空时由服务端自动分配",
    )
    name: str = Field(default="", description="分身展示名称")
    description: str = Field(default="", description="分身展示描述")
    icon: Optional[str] = Field(default=None, description="分身展示图标")
    version: Optional[str] = Field(
        default=None,
        description="兼容旧客户端保留，虚拟分身始终跟随源插件版本",
    )
    restore_previous: bool = Field(
        default=True,
        description="该后缀名下留有一个已停用的分身时是否沿用它的业务参数，为假时按源插件模板重建",
    )


class PluginCloneOutcome(BaseModel):
    """
    一次分身创建或恢复的结果。

    实例 ID 必须回传：后缀可以由服务端自动分配，调用方因此再也算不出它，而后续要
    拿它去打开配置、刷新列表或跳转。
    """

    instance_id: str = Field(description="新建或恢复出来的分身实例 ID")


class PluginRestorableInstance(BaseModel):
    """
    一个已停用、其设置仍留存可被恢复的分身实例。

    在册启用的分身不在此列：它们的配置正在被使用，拿来「恢复」没有意义，摆进选择器
    只会让用户误以为能把一个活着的实例再创建一遍。
    """

    instance_id: str = Field(description="分身实例 ID")
    suffix: str = Field(description="该实例相对源插件 ID 的后缀")
    plugin_name: Optional[str] = Field(default=None, description="停用前登记的展示名称")
    plugin_desc: Optional[str] = Field(default=None, description="停用前登记的展示描述")
    has_config: bool = Field(default=False, description="是否留有业务参数")


class PluginSourceIdentity(BaseModel):
    """
    显式换源确认所需的插件来源身份投影。
    """

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


class PluginSourceCandidate(BaseModel):
    """
    一个可供管理员识别的脱敏插件来源候选。
    """

    source_type: Literal["official", "third_party", "local"] = Field(description="来源类型")
    source_key: Optional[str] = Field(
        default=None,
        description="规范化在线来源键；本地候选为空",
    )
    repo_url: Optional[str] = Field(
        default=None,
        description="可明确选择的在线仓库地址或本地仓库标识",
    )
    package_generation: Literal["v1", "v2", "v3"] = Field(description="当前运行时会采用的插件包代际")
    plugin_version: Optional[str] = Field(
        default=None,
        description="该来源当前可安装的插件版本",
    )


class PluginSourceOptions(BaseModel):
    """
    来源选择界面所需的当前身份、候选和准入状态。
    """

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


class PluginSourceInstallRequest(BaseModel):
    """
    管理员为未绑定插件明确选择初始在线来源的请求参数。
    """

    repo_url: str = Field(min_length=1, description="明确选择的目标插件仓库地址")
    release_version: Optional[str] = Field(
        default=None,
        description="指定安装的 Release 资产版本；为空时使用当前索引版本",
    )
    force: bool = Field(
        default=False,
        description="是否强制重新下载并安装所选来源载荷",
    )

    @field_validator("repo_url")
    @classmethod
    def normalize_repo_url(cls, value: str) -> str:
        """
        拒绝只含空白或本地路径标识的来源选择。
        """
        normalized = value.strip()
        if not normalized:
            raise ValueError("显式安装必须指定目标在线来源")
        if normalized.startswith("local://"):
            raise ValueError("显式来源安装只接受在线插件仓库")
        return normalized


class PluginSourceChangeRequest(BaseModel):
    """
    管理员显式切换插件在线来源的请求参数。
    """

    repo_url: str = Field(min_length=1, description="明确选择的目标插件仓库地址")
    expected_revision: int = Field(
        ge=1,
        description="提交换源时必须匹配的当前身份 revision",
    )
    release_version: Optional[str] = Field(
        default=None,
        description="指定安装的 Release 资产版本；为空时使用当前索引版本",
    )

    @field_validator("repo_url")
    @classmethod
    def normalize_repo_url(cls, value: str) -> str:
        """
        拒绝只含空白或本地路径标识的换源目标。
        """
        normalized = value.strip()
        if not normalized:
            raise ValueError("显式换源必须指定目标在线来源")
        if normalized.startswith("local://"):
            raise ValueError("显式换源只接受在线插件仓库")
        return normalized


class PluginDashboard(Plugin):
    """
    插件仪表盘。
    """

    id: Optional[str] = Field(default=None, description="记录标识")
    name: Optional[str] = Field(default=None, description="名称")
    key: Optional[str] = Field(default=None, description="仪表板key")
    render_mode: Optional[str] = Field(default="vuetify", description="演染模式")
    attrs: Optional[dict[str, JsonData]] = Field(default_factory=dict, description="全局配置")
    cols: Optional[dict[str, JsonData]] = Field(default_factory=dict, description="col列数")
    elements: Optional[List[dict[str, JsonData]]] = Field(default_factory=list, description="页面元素")


class PluginSidebarNavItem(BaseModel):
    """
    插件侧栏导航项（前端全页路由）。
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


class PluginAgentPet(BaseModel):
    """
    插件声明的 Agent 助手形象（前端按 plugin_id + key 选择并挂载联邦组件）。
    """

    plugin_id: str = Field(description="插件实例 ID，分身为分身自身 ID")
    source_plugin_id: str = Field(description="提供前端产物的源插件 ID，非分身时等于 plugin_id")
    plugin_name: str = Field(description="插件展示名")
    key: str = Field(description="形象 key，插件内唯一，匹配 [a-z0-9_-]{1,32}")
    name: str = Field(description="形象展示名")
    description: Optional[str] = Field(default=None, description="形象一句话说明")
    mode: Literal["stage", "renderer"] = Field(
        description="渲染模式：stage（插件拥有角色与视口图层）/ renderer（宿主负责入口行为，插件只绘制）",
    )
    component: str = Field(default="AgentPet", description="联邦暴露名，不含 ./ 前缀")
    api_version: int = Field(default=1, description="形象契约版本，前端忽略不认识的版本")
    plugin_version: Optional[str] = Field(
        default=None,
        description="提供形象的插件版本；前端据此判断插件是否升级，以决定是否重试加载失败的形象",
    )
    remote_url: str = Field(
        description="插件联邦入口 remoteEntry.js 地址，规则同 GET /plugin/remotes 的 url；前端据此直接注册远程入口",
    )
    preview_url: Optional[str] = Field(
        default=None,
        description="预览图 URL：相对路径已解析为与 remoteEntry 同源的插件静态文件地址，或原样的 http(s)/data URL",
    )
    avatar_url: Optional[str] = Field(
        default=None,
        description="方形头像 URL，用于 Agent 面板头部与消息头像；解析规则同 preview_url，未声明为空",
    )
    bubbles: Optional[Literal["host", "self"]] = Field(
        default=None,
        description="气泡归属，仅 stage 模式：host（宿主按锚点绘制）/ self（插件自绘）；renderer 模式为空",
    )
    random_actions: Optional[List[str]] = Field(
        default=None,
        description="宿主随机动作候选，仅 renderer 模式；为空表示使用宿主动作全集",
    )


class PluginRatingRequest(BaseModel):
    """
    插件评分请求。
    """

    rating: float = Field(
        ge=0.1,
        le=5.0,
        multiple_of=0.1,
        description="评分，范围 0.1 至 5.0，精确到 0.1",
    )


class PluginRating(BaseModel):
    """
    插件评分结果。
    """

    plugin_id: str = Field(description="插件 ID")
    average_rating: float = Field(default=0.0, description="平均评分")
    rating_count: int = Field(default=0, description="评分人数")
    user_rating: Optional[float] = Field(default=None, description="当前安装实例评分")


class PluginRatingMap(RootModel[Dict[str, PluginRating]]):
    """
    插件 ID 与评分结果的映射。
    """


class PluginMemoryInfo(BaseModel):
    """
    插件内存信息。
    """

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
    """
    插件模块联邦远程入口。
    """

    id: str = Field(description="记录标识")
    url: str = Field(description="访问地址")
    name: str = Field(description="显示名称")
    source_plugin_id: Optional[str] = Field(default=None, description="静默丢弃，投影层写进去也到不了前端")


class PluginReleaseItem(BaseModel):
    """
    可安装的插件 Release 版本。
    """

    version: str = Field(description="版本号")
    tag_name: str = Field(description="版本发布标签")
    name: str = Field(description="显示名称")
    published_at: Optional[str] = Field(default=None, description="版本发布时间")
    body: str = Field(default="", description="正文内容")
    asset_name: str = Field(description="发布版本的安装包文件名")
    is_latest: bool = Field(default=False, description="是否为最新发布版本")
    is_current: bool = Field(default=False, description="是否为当前安装的版本")


class PluginReleaseData(BaseModel):
    """
    插件 Release 能力与版本列表。
    """

    release_supported: bool = Field(default=False, description="插件是否支持按发布版本安装")
    latest_version: Optional[str] = Field(default=None, description="最新可用版本")
    current_version: Optional[str] = Field(default=None, description="当前已安装版本")
    items: List[PluginReleaseItem] = Field(default_factory=list, description="可供安装的插件发布版本列表")


class PluginFolderConfigData(BaseModel):
    """
    新版插件文件夹配置（对象格式，含展示配置与插件列表）。
    """

    # 文件夹内插件 ID 列表
    plugins: List[str] = Field(
        default_factory=list,
        description="按顺序分配到此文件夹的已安装插件标识",
    )
    # 文件夹排序值
    order: Optional[int] = Field(default=None, description="文件夹显示顺序")
    # 文件夹图标
    icon: Optional[str] = Field(default=None, description="文件夹图标名称")
    # 文件夹颜色
    color: Optional[str] = Field(default=None, description="文件夹的显示颜色")
    # 文件夹渐变
    gradient: Optional[str] = Field(default=None, description="文件夹的渐变背景配置")
    # 文件夹背景
    background: Optional[str] = Field(default=None, description="文件夹的背景样式")
    # 是否显示图标（前端字段为驼峰命名）
    show_icon: Optional[bool] = Field(
        default=None,
        alias="showIcon",
        description="是否显示文件夹图标",
    )


class PluginFoldersData(RootModel[Dict[str, Union[List[str], PluginFolderConfigData]]]):
    """
    插件文件夹与插件配置映射，兼容旧版数组格式与新版对象格式。
    """


class PluginFolderUpdateRequest(BaseModel):
    """
    插件文件夹名称和展示字段的增量更新请求。
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    new_name: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=128,
        description="修改后的名称",
    )
    icon: Optional[str] = Field(default=None, description="文件夹图标名称")
    color: Optional[str] = Field(
        default=None,
        description="文件夹的显示颜色",
    )
    gradient: Optional[str] = Field(
        default=None,
        description="文件夹的渐变背景配置",
    )
    background: Optional[str] = Field(
        default=None,
        description="文件夹的背景样式",
    )
    show_icon: Optional[bool] = Field(
        default=None,
        alias="showIcon",
        description="是否显示文件夹图标",
    )


class PluginFolderPluginsUpdateRequest(BaseModel):
    """
    插件文件夹成员顺序的条件替换请求。
    """

    plugins: List[str] = Field(description="按顺序分配到此文件夹的已安装插件标识")
    expected_plugins: Optional[List[str]] = Field(
        default=None,
        description="最近读取的插件标识顺序，用于拒绝过期替换",
    )


class PluginDashboardMetaItem(BaseModel):
    """
    插件仪表板入口摘要。
    """

    id: str = Field(description="记录标识")
    name: Optional[str] = Field(default=None, description="显示名称")
    key: Optional[str] = Field(default=None, description="缓存条目的键")
