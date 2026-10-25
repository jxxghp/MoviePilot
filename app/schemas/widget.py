"""
小组件专用登记与有界只读快照协议；不包含业务命令或主登录凭据。
"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

WidgetContent = Literal[
    "overview", "downloads", "imports", "subscriptions", "movieSubscriptions", "tvSubscriptions", "musicSubscriptions"
]
WidgetIcon = Literal[
    "movie", "television", "episodes", "music", "download", "upload", "storage", "items", "clock", "cpu", "memory"
]


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
