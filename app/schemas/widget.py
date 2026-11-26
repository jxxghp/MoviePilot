"""
小组件专用登记与有界只读快照协议；不包含业务命令或主登录凭据。
"""

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

WidgetContent = Literal[
    "overview", "downloads", "imports", "subscriptions", "movieSubscriptions", "tvSubscriptions", "musicSubscriptions"
]
WidgetIcon = Literal[
    "movie", "television", "episodes", "music", "download", "upload", "storage", "items", "clock", "cpu", "memory"
]
PluginWidgetKind = Literal["metrics", "list", "poster", "nowPlaying", "custom"]
PluginWidgetTone = Literal["default", "primary", "secondary", "muted", "accent", "success", "warning", "danger"]


class WidgetContext(BaseModel):
    """
    当前主登录的小组件身份与权限；读取不会创建、轮换或返回任何凭据。
    """

    version: Literal[1] = Field(default=1, description="小组件账户上下文协议版本")
    owner: str = Field(pattern=r"^[0-9a-f]{32}$", description="当前激活用户的不可变身份，用于核对已启用资料")
    availableContents: list[WidgetContent] = Field(max_length=6, description="按最新权限可选择的内容，无权限时为空")


class WidgetRegistrationRequest(BaseModel):
    """
    当前登录用户明确启用某个本机安装；不接受 owner、权限或来源地址。
    """

    model_config = ConfigDict(extra="forbid")

    installationId: UUID = Field(description="App 本机安装的随机 UUID，同一用户和安装的再次登记会原子轮换")


class WidgetRegistrationResponse(BaseModel):
    """
    仅在登记提交成功后返回一次的专用凭据；服务端只持久化其摘要。
    """

    version: Literal[1] = Field(default=1, description="小组件凭据协议版本")
    registrationId: UUID = Field(description="本次登记的随机标识；再次登记后旧标识失效")
    installationId: UUID = Field(description="调用方提供的本机安装标识")
    owner: str = Field(pattern=r"^[0-9a-f]{32}$", description="服务端已认证用户的不可变身份")
    secret: str = Field(repr=False, description="只用于小组件快照和自撤销的 Bearer，含至少 256 bit 随机密钥")
    expiresAt: float = Field(gt=0, allow_inf_nan=False, description="专用凭据到期的 UTC Unix 秒，不能由扩展自行续期")
    availableContents: list[WidgetContent] = Field(description="按本次最新权限可选择的内容；刷新仍重新鉴权")


class WidgetMetric(BaseModel):
    """
    后端格式化的展示指标，原生端不派生另一套业务计数。
    """

    id: str = Field(min_length=1, max_length=64, description="section 内唯一的短指标标识")
    title: str = Field(min_length=1, max_length=120, description="指标名称，最多 120 UTF-8 字节")
    value: str = Field(min_length=1, max_length=64, description="已格式化的展示值，最多 64 UTF-8 字节")
    icon: WidgetIcon = Field(description="有限展示语义，由系统组件映射为原生图标")


class WidgetItem(BaseModel):
    """
    最小媒体摘要；标识不携带下载 hash、文件路径或资源 URL。
    """

    id: str = Field(min_length=1, max_length=64, description="section 内唯一的短条目标识")
    title: str = Field(min_length=1, max_length=240, description="媒体展示名称，最多 240 UTF-8 字节")
    subtitle: str | None = Field(default=None, max_length=512, description="可选辅助信息，最多 512 UTF-8 字节")
    progress: float | None = Field(
        default=None, ge=0, le=1, allow_inf_nan=False, description="来源具有自然百分比时的完成比例"
    )
    timestamp: float | None = Field(
        default=None, gt=0, allow_inf_nan=False, description="可确认的记录 UTC Unix 秒；无明确时区时不猜测"
    )


class WidgetSection(BaseModel):
    """
    每类来源独立报告成功、空结果或失败，失败不伪造采样时间。
    """

    content: WidgetContent = Field(description="固定的内容类型")
    state: Literal["ready", "empty", "unavailable"] = Field(description="读取成功、成功但为空或来源不可用")
    updatedAt: float | None = Field(
        default=None, gt=0, allow_inf_nan=False, description="成功读取的 UTC Unix 秒；失败时为空"
    )
    title: str = Field(min_length=1, max_length=240, description="内容标题，最多 240 UTF-8 字节")
    metrics: list[WidgetMetric] = Field(default_factory=list, max_length=8, description="最多八个真实作用域的统计指标")
    items: list[WidgetItem] = Field(default_factory=list, max_length=6, description="最多六项摘要；列表长度不冒充总数")
    message: str | None = Field(default=None, max_length=240, description="可展示的固定说明，不输出内部异常或凭据")


class WidgetSnapshot(BaseModel):
    """
    协议 v1 的有限只读投影；完整 JSON 不超过 32 KiB。
    """

    version: Literal[1] = Field(default=1, description="快照协议版本")
    owner: str = Field(pattern=r"^[0-9a-f]{32}$", description="不可变用户身份，必须与本机已启用资料匹配")
    generatedAt: float = Field(gt=0, allow_inf_nan=False, description="响应生成的 UTC Unix 秒")
    expiresAt: float = Field(
        gt=0, allow_inf_nan=False, description="数据展示的最终有效期 UTC Unix 秒，最长 24 小时且不超过凭据到期"
    )
    availableContents: list[WidgetContent] = Field(max_length=6, description="最新授权的内容集合，与 sections 一一对应")
    sections: list[WidgetSection] = Field(max_length=6, description="独立来源结果，权限不足的内容不在此出现")


class PluginWidgetDescriptor(BaseModel):
    """
    已启用插件可供系统桌面绑定的单个小组件描述。
    """

    pluginId: str = Field(min_length=1, max_length=128, description="运行态插件标识")
    pluginName: str = Field(min_length=1, max_length=120, description="插件展示名称")
    widgetKey: str = Field(default="", max_length=64, description="插件内唯一的小组件键名")
    name: str = Field(min_length=1, max_length=120, description="小组件卡片名称")
    description: str | None = Field(default=None, max_length=240, description="小组件可选说明文字")
    kind: PluginWidgetKind = Field(default="metrics", description="小组件渲染形态")
    source: Literal["widget", "dashboard"] = Field(default="widget", description="声明来源，支持专属协议或仪表板兜底")
    hasPage: bool = Field(default=False, description="插件是否具备数据详情页面")


class PluginWidgetCatalog(BaseModel):
    """
    小组件专用凭据可读的已启用插件小组件目录。
    """

    version: Literal[1] = Field(default=1, description="插件小组件目录协议版本")
    owner: str = Field(pattern=r"^[0-9a-f]{32}$", description="不可变管理员身份标识")
    generatedAt: float = Field(gt=0, allow_inf_nan=False, description="目录生成的 UTC Unix 秒")
    expiresAt: float = Field(gt=0, allow_inf_nan=False, description="目录有效期的 UTC Unix 秒")
    widgets: list[PluginWidgetDescriptor] = Field(default_factory=list, max_length=64, description="可绑定的插件小组件清单")


class PluginWidgetMetricItem(BaseModel):
    """
    插件小组件预设模板中的单项统计指标。
    """

    id: str = Field(min_length=1, max_length=64, description="卡片内唯一的指标标识")
    label: str = Field(min_length=1, max_length=120, description="指标标题")
    value: str = Field(min_length=1, max_length=96, description="已格式化的指标数值")
    subValue: str | None = Field(default=None, max_length=120, description="可选趋势或辅助值")
    icon: str | None = Field(default=None, max_length=48, description="可选语义图标名称")
    tone: PluginWidgetTone | None = Field(default=None, description="可选语义着色角色")


class PluginWidgetListItem(BaseModel):
    """
    插件小组件预设列表模板中的单行条目。
    """

    id: str = Field(min_length=1, max_length=64, description="卡片内唯一的条目标识")
    title: str = Field(min_length=1, max_length=240, description="条目主标题")
    subtitle: str | None = Field(default=None, max_length=320, description="条目辅助说明")
    trailing: str | None = Field(default=None, max_length=64, description="右侧简短状态或数值")
    progress: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False, description="可选自然完成度比例，范围 0 至 1")


class PluginWidgetSnapshot(BaseModel):
    """
    单个插件桌面小组件的有界只读快照，完整序列化体积不超过 32 KiB。
    """

    version: Literal[1] = Field(default=1, description="插件小组件快照协议版本")
    owner: str = Field(pattern=r"^[0-9a-f]{32}$", description="不可变管理员身份标识")
    generatedAt: float = Field(gt=0, allow_inf_nan=False, description="快照生成的 UTC Unix 秒")
    expiresAt: float = Field(gt=0, allow_inf_nan=False, description="快照展示上限的 UTC Unix 秒")
    pluginId: str = Field(min_length=1, max_length=128, description="所属运行态插件标识")
    pluginName: str = Field(min_length=1, max_length=120, description="所属插件显示名称")
    widgetKey: str = Field(default="", max_length=64, description="插件内小组件标识")
    state: Literal["ready", "empty", "unavailable"] = Field(description="读取成功、成功但为空或暂不可用")
    updatedAt: float | None = Field(default=None, gt=0, allow_inf_nan=False, description="成功采样的 UTC Unix 秒；不可用时为空")
    kind: PluginWidgetKind = Field(default="metrics", description="小组件渲染形态")
    title: str = Field(min_length=1, max_length=240, description="卡片主标题")
    subtitle: str | None = Field(default=None, max_length=240, description="卡片副标题")
    metrics: list[PluginWidgetMetricItem] = Field(default_factory=list, max_length=8, description="预设模板指标集合，最多八项")
    items: list[PluginWidgetListItem] = Field(default_factory=list, max_length=6, description="预设模板条目集合，最多六项")
    layout: dict[str, Any] | None = Field(default=None, description="通用声明式自定义布局根节点，已经过层级与字段清洗")
    layouts: dict[str, dict[str, Any]] | None = Field(
        default=None,
        description="按 small、medium、large 尺寸分别声明的自定义布局根节点映射",
    )
    message: str | None = Field(default=None, max_length=240, description="可公开展示的状态说明文字")
