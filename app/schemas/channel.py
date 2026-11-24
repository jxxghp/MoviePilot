"""
共享渠道连接、通知路由和交互策略的数据契约。
"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, SerializeAsAny, StringConstraints, model_validator
from pydantic_core import PydanticCustomError

from app.schemas.common import JsonData

type AdmissionMode = Literal["manual", "manual_rules", "rules", "open"]
type AutoDownloadMode = Literal["off", "specified", "all"]
type RuleKind = Literal["user", "group", "private"]
type InteractionScope = Literal["search", "subscribe", "download", "schedule"]
type ChannelMode = Literal["standard", "app", "bot"]
type ChannelText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]
type ChannelRevision = Annotated[int, Field(ge=1)]


class ChannelConnectionInfo(BaseModel):
    """
    供连接、通知与交互管理共同引用的连接摘要。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: ChannelText = Field(description="由宿主分配的稳定连接 ID")
    name: ChannelText = Field(description="连接显示名称，可独立重命名")
    provider: ChannelText = Field(description="内置或插件通知渠道类型")
    mode: ChannelMode = Field(description="该连接的接入模式")
    identity_namespace: ChannelText = Field(description="地址与授权记录的不可变身份空间")
    enabled: bool = Field(description="连接总开关")
    revision: ChannelRevision = Field(description="该连接的条件写入版本")
    credential_status: Literal["ready", "verification_required"] = Field(
        default="ready", description="身份待核验时暂停连接的实际收发",
    )
    inbound_token_configured: bool = Field(default=False, description="是否已设置回调验证令牌")
    can_rotate_credential: bool = Field(default=False, description="当前连接能否进行同身份凭据核验")


class ChannelConnection(ChannelConnectionInfo):
    """
    供宿主持久化和装配使用的连接记录，凭据字段只通过专用写入接口修改。
    """

    provider_config: dict[str, JsonData] = Field(description="平台连接所需的私有配置", repr=False)
    verified_platform_account: ChannelText | None = Field(
        default=None, description="经平台确认的账号身份，只用于核验同身份轮换", repr=False,
    )


class ChannelAddress(BaseModel):
    """
    某个稳定渠道身份下经过明确选择的外部地址。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    connection_id: ChannelText = Field(description="地址所属的稳定连接 ID")
    identity_namespace: ChannelText = Field(description="地址所在的平台身份空间")
    kind: Literal["user", "conversation", "device"] = Field(description="接收对象类型")
    target: ChannelText = Field(description="该身份空间内的接收对象 ID")
    thread_id: ChannelText | None = Field(default=None, description="会话中的话题或线程 ID")

    @model_validator(mode="after")
    def validate_thread(self) -> "ChannelAddress":
        """
        限制话题地址只能依附于会话目标。
        """
        if self.thread_id is not None and self.kind != "conversation":
            raise ValueError("只有会话地址可以指定话题")
        return self


type NotificationTargetKind = Literal["bound", "account", "user", "conversation", "webhook", "all", "review"]
type NotificationAudience = Literal["any", "admin", "user", "all"]
type NotificationRouteScope = Literal["admin", "user", "all"]


class NotificationRuleAccount(BaseModel):
    """
    供管理员选择通知受众或事件归属的本地账号。
    """

    identity: ChannelText = Field(description="不会随用户名修改而变化的账号标识")
    name: str = Field(description="当前显示用户名")


type RuleClock = Annotated[str, StringConstraints(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")]


class NotificationSendTime(BaseModel):
    """
    按系统时区限定规则的发送时间，支持跨午夜。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    start: RuleClock = Field(description="开始时间，包含该分钟")
    end: RuleClock = Field(description="结束时间，包含该分钟；与开始相同表示全天")

    def allows(self, now: datetime) -> bool:
        """
        根据当前系统本地时间判断窗口，相同起止时间表示全天。
        """
        value = now.strftime("%H:%M")
        if self.start == self.end:
            return True
        if self.start < self.end:
            return self.start <= value <= self.end
        return value >= self.start or value <= self.end


class NotificationRule(BaseModel):
    """
    对一份连接选择事件、受众和一个明确的投递方式。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(default="", max_length=80, description="规则显示名称，旧规则可为空")
    send_time: NotificationSendTime | None = Field(default=None, description="发送窗口，为空时全天生效")
    message_types: tuple[ChannelText, ...] = Field(max_length=32, description="匹配的事件分类")
    enabled: bool = Field(default=True, description="是否参与投递")
    audience: NotificationAudience = Field(default="any", description="旧单选通知范围；新客户端使用 audiences")
    audiences: tuple[NotificationRouteScope, ...] | None = Field(
        default=None, max_length=3, description="多选通知类别；空数组沿用全局范围，空值兼容旧 audience",
    )
    kind: NotificationTargetKind = Field(description="接收对象类型")
    target: ChannelText | None = Field(default=None, description="平台用户或会话的明确地址")
    thread_id: ChannelText | None = Field(default=None, description="会话内的话题或线程")
    fallback_only: bool = Field(default=False, description="仅在原消息在此连接没有已绑定目标时生效")
    legacy_usernames: tuple[ChannelText, ...] = Field(default=(), max_length=100, description="保留旧消息用户名筛选")
    owner_identity: ChannelText | None = Field(default=None, description="匹配事件关联账号的不可变身份")

    @model_validator(mode="before")
    @classmethod
    def normalize_audiences(cls, value: object) -> object:
        """
        规范多选并投影旧字段，旧数据和在途快照无需迁移即可读取。
        """
        if not isinstance(value, dict):
            return value
        audiences = value.get("audiences")
        if isinstance(audiences, (list, tuple)) and all(isinstance(item, str) for item in audiences):
            selected = tuple(dict.fromkeys(audiences))
            return {**value, "audiences": selected, "audience": selected[0] if len(selected) == 1 else "any"}
        return value

    @property
    def matched_audiences(self) -> tuple[NotificationRouteScope, ...]:
        """
        返回显式类别，空集合表示不额外收窄宿主已解析的通知范围。
        """
        if self.audiences is not None:
            return self.audiences
        return () if self.audience == "any" else (self.audience,)

    @model_validator(mode="after")
    def validate_destination(self) -> "NotificationRule":
        """
        要求目标完整，待确认的历史配置不能参与投递。
        """
        if self.enabled and not self.message_types:
            raise ValueError("启用规则前须选择消息类型")
        needs_target = self.kind in {"account", "user", "conversation"}
        if needs_target != (self.target is not None):
            raise ValueError("指定账号、用户或会话必须明确指定地址")
        if self.thread_id and self.kind != "conversation":
            raise ValueError("只有会话可以指定话题")
        if self.kind == "review" and self.enabled:
            raise ValueError("待确认规则须选定接收对象后启用")
        if "user" in self.matched_audiences and self.kind not in {"bound", "account", "review"}:
            raise ValueError("个人通知只可投递至其绑定地址")
        if self.kind == "all" and self.matched_audiences != ("all",):
            raise ValueError("平台全体投递必须限定为所有用户范围")
        return self


class NotificationRoute(BaseModel):
    """
    针对一份共享连接独立保存的事件投递设置。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    connection_id: ChannelText = Field(description="所引用的稳定连接 ID")
    identity_namespace: ChannelText = Field(description="配置路由时确认的连接身份空间")
    enabled: bool = Field(description="事件通知开关，不控制指令回复")
    message_types: tuple[ChannelText, ...] = Field(default=(), description="启用的事件通知类型")
    rules: tuple[NotificationRule, ...] | None = Field(default=None, max_length=100, exclude_if=lambda value: value is None, description="内置模块的独立投递规则，插件兼容时为空")
    revision: ChannelRevision = Field(description="该路由的条件写入版本")


class InteractionMatch(BaseModel):
    """
    渠道实例内精确匹配的用户 ID、群组 ID 或私聊。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(default="", max_length=80, description="规则显示名称，旧规则可为空")
    kind: RuleKind
    target: ChannelText | None = None
    enabled: bool = True

    @model_validator(mode="after")
    def validate_target(self) -> "InteractionMatch":
        """
        群组和用户必须指定确切的 ID。
        """
        if (self.kind == "private") != (self.target is None):
            raise ValueError("用户和群组必须指定 ID，私聊无需填写 ID")
        return self


class AdmissionRule(InteractionMatch):
    """
    匹配后授予当前事件的业务权限。
    """

    scopes: tuple[InteractionScope, ...] = Field(min_length=1, max_length=4)


class InteractionPolicy(BaseModel):
    """
    针对一份共享连接独立保存的机器人交互设置。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    connection_id: ChannelText = Field(description="所引用的稳定连接 ID")
    identity_namespace: ChannelText = Field(description="配置交互时确认的连接身份空间")
    enabled: bool = Field(description="机器人交互开关")
    platform_options: dict[str, JsonData] = Field(default_factory=dict, description="由所属模块校验的平台交互选项")
    admission: AdmissionMode = Field(
        default="manual", description="机器人接入策略",
    )
    admission_rules: tuple[AdmissionRule, ...] = Field(default=(), max_length=100)
    auto_download: AutoDownloadMode = Field(
        default="off", description="普通搜索命中后是否自动择优，仍须拥有下载权限",
    )
    auto_download_rules: tuple[InteractionMatch, ...] = Field(default=(), max_length=100)
    revision: ChannelRevision = Field(description="该策略的条件写入版本")



class InteractionPolicyView(InteractionPolicy):
    """
    API 只读投影允许所属模块补充兼容字段，持久化模型仍拒绝未知字段。
    """

    model_config = ConfigDict(extra="allow", frozen=True)
    __pydantic_extra__: dict[str, JsonData] = Field(init=False, description="所属平台的 JSON 兼容字段，禁止任意 Python 对象")


class ChannelSettings(BaseModel):
    """
    独立渠道资源在同一次配置事务内保存的快照。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    connections: dict[str, ChannelConnection] = Field(default_factory=dict, description="按稳定 ID 存储的连接")
    notifications: dict[str, NotificationRoute] = Field(default_factory=dict, description="按连接 ID 存储的通知路由")
    interactions: dict[str, InteractionPolicy] = Field(default_factory=dict, description="按连接 ID 存储的交互策略")

    orders: dict[Literal["notification", "interaction"], tuple[ChannelText, ...]] = Field(default_factory=dict)



    @model_validator(mode="after")
    def validate_references(self) -> "ChannelSettings":
        """
        保证显示名称唯一，两个功能资源引用当前连接及其身份空间。
        """
        names: set[str] = set()
        for connection_id, connection in self.connections.items():
            if connection_id != connection.id:
                raise ValueError("连接 ID 与记录不一致")
            key = connection.name.casefold()
            if key in names:
                raise PydanticCustomError("channel_connection_name_conflict", "连接名称已存在，请使用其他名称")
            names.add(key)
        for records in (self.notifications, self.interactions):
            for connection_id, record in records.items():
                referenced = self.connections.get(connection_id)
                if not referenced or record.connection_id != connection_id:
                    raise ValueError("功能配置必须引用现有连接")
                if record.identity_namespace != referenced.identity_namespace:
                    raise ValueError("功能配置与连接身份不一致")
        return self


type ChannelFeature = Literal["notification", "interaction"]


class ChannelCardOrder(BaseModel):
    """
    按页面保存完整顺序，用当前顺序检查写入冲突。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    expected_order: tuple[ChannelText, ...]
    order: tuple[ChannelText, ...]


class ChannelConnectionForm(BaseModel):
    """
    单独读取可编辑连接字段，机密值无回显。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    revision: ChannelRevision
    values: dict[str, JsonData]
    editable_fields: tuple[str, ...]


class ChannelConnectionEdit(BaseModel):
    """
    更新既有共享连接；公开字段由宿主校验，专属配置由所属模块校验。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: ChannelText = Field(description="连接的显示名称")
    enabled: bool = Field(description="连接总开关")
    provider_config: dict[str, JsonData] | None = Field(default=None, repr=False)
    inbound_token: SecretStr | None = Field(
        default=None, min_length=32, max_length=256, description="连接的专用入站令牌；本编辑接口统一要求 32 至 256 字符，平台可施加更严格约束", repr=False,
    )
    expected_revision: ChannelRevision = Field(description="读取时得到的连接版本")


class ChannelConnectionDelete(BaseModel):
    """
    使用确认时展示的三种资源版本移除共享连接。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    expected_revision: ChannelRevision = Field(description="连接版本")
    notification_revision: ChannelRevision | None = Field(description="通知路由版本，未配置时为空")
    interaction_revision: ChannelRevision | None = Field(description="交互策略版本，不支持时为空")


class ChannelConnectionReferences(BaseModel):
    """
    连接移除确认时读取的成员和任务引用数量。
    """

    member_count: int = Field(ge=0, description="此连接的成员和申请记录数量")
    task_count: int = Field(ge=0, description="以此连接成员为执行主体的任务记录数量")


class ChannelCredentialRotation(BaseModel):
    """
    提交平台凭据以核验并轮换已有连接身份。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    secret: SecretStr = Field(
        min_length=1, max_length=512, description="待核验的平台凭据，只接收写入", repr=False,
    )
    expected_revision: ChannelRevision = Field(description="读取时得到的连接版本")


class NotificationRouteEdit(BaseModel):
    """
    可独立保存的事件投递设置。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = Field(description="事件通知开关")
    message_types: tuple[ChannelText, ...] | None = Field(default=None, description="旧客户端的消息类型开关")
    rules: tuple[NotificationRule, ...] | None = Field(default=None, max_length=100, description="保存完整的投递规则列表")
    expected_revision: ChannelRevision = Field(description="读取时得到的路由版本")


class InteractionPolicyEdit(BaseModel):
    """
    可独立保存的机器人交互设置。
    """

    model_config = ConfigDict(extra="allow", frozen=True)
    __pydantic_extra__: dict[str, JsonData] = Field(init=False, description="所属平台的 JSON 兼容字段，禁止任意 Python 对象")

    enabled: bool = Field(description="机器人交互开关")
    platform_options: dict[str, JsonData] | None = Field(default=None, description="由所属模块校验的平台交互选项")
    admission: AdmissionMode | None = Field(default=None, description="省略时保留当前接入方式")
    admission_rules: tuple[AdmissionRule, ...] | None = Field(default=None, max_length=100)
    auto_download: AutoDownloadMode | None = Field(default=None, description="省略时保留当前值")
    auto_download_rules: tuple[InteractionMatch, ...] | None = Field(default=None, max_length=100)
    expected_revision: ChannelRevision = Field(description="读取时得到的策略版本")


class ChannelPlatformCapabilities(BaseModel):
    """
    模块公开的平台事实与表单能力，不携带凭据或授予业务权限。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    rule_kinds: tuple[RuleKind, ...] = Field(default=(), description="能从认证事件验证的匹配条件")
    target_kinds: tuple[NotificationTargetKind, ...] = Field(default=(), description="支持的通知接收对象")
    account_targets: bool = Field(default=False, description="是否能在宿主限定账号集合内选择设备")
    legacy_usernames: bool = Field(default=False, description="是否保留正式版本的用户名筛选")
    thread_id: bool = Field(default=False, description="会话是否支持明确的线程或话题")
    rule_labels: dict[str, str] = Field(default_factory=dict, description="匹配条件的翻译键")
    target_labels: dict[str, str] = Field(default_factory=dict, description="投递对象的翻译键")
    interaction_form: str | None = Field(default=None, description="平台专属交互表单的扩展标识")
    inbound_token: bool = Field(default=False, description="是否支持专用回调令牌编辑")
    login_management: bool = Field(default=False, description="是否提供平台登录管理表单")
    credential_label: str = Field(default="channelSettings.newCredential", description="凭据编辑标签的翻译键")
    credential_action: str = Field(default="channelSettings.rotateCredential", description="凭据轮换动作的翻译键")


class ChannelSettingsView(BaseModel):
    """
    设置页读取的渠道目录，提供连接、通知管理和交互管理的当前配置。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    connections: tuple[ChannelConnectionInfo, ...] = Field(description="共享连接及其版本")
    notifications: tuple[NotificationRoute, ...] = Field(description="可配置的通知路由及其版本")
    interactions: tuple[SerializeAsAny[InteractionPolicy], ...] = Field(description="可配置的交互策略及其版本")
    notification_target_kinds: dict[ChannelText, tuple[NotificationTargetKind, ...]] = Field(default_factory=dict, description="按连接公布支持的投递目标")
    admission_modes: dict[ChannelText, tuple[AdmissionMode, ...]] = Field(description="每个交互连接可以验证的接入方式")
    rule_kinds: dict[ChannelText, tuple[RuleKind, ...]] = Field(default_factory=dict)
    orders: dict[ChannelFeature, tuple[ChannelText, ...]] = Field(default_factory=dict)
    providers: dict[str, str] = Field(default_factory=dict, description="模块目录声明的连接类型及显示名称")
    capabilities: dict[ChannelText, ChannelPlatformCapabilities] = Field(default_factory=dict, description="各连接的模块能力与表单扩展描述")
    can_edit_provider_config: bool = Field(default=True)
    can_delete_connections: bool = Field(default=True, description="是否支持完整删除共享连接")


class ChannelConnectionCreate(BaseModel):
    """
    用一份私有连接配置创建新的稳定身份。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: ChannelText = Field(description="连接显示名称")
    provider: ChannelText = Field(description="内置或插件通知渠道类型")
    provider_config: dict[str, JsonData] = Field(description="平台连接所需私有配置", repr=False)
