"""
62c91ad8b057 的固定配置格式；不随运行时平台扩展变更。
"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
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
        default="ready",
        description="身份待核验时暂停连接的实际收发",
    )
    inbound_token_configured: bool = Field(default=False, description="是否已设置回调验证令牌")
    can_rotate_credential: bool = Field(default=False, description="当前连接能否进行同身份凭据核验")


class ChannelConnection(ChannelConnectionInfo):
    """
    供宿主持久化和装配使用的连接记录，凭据字段只通过专用写入接口修改。
    """

    provider_config: dict[str, JsonData] = Field(description="平台连接所需的私有配置", repr=False)
    verified_platform_account: ChannelText | None = Field(
        default=None,
        description="经平台确认的账号身份，只用于核验同身份轮换",
        repr=False,
    )


type NotificationTargetKind = Literal["bound", "account", "user", "conversation", "webhook", "all", "review"]

type NotificationAudience = Literal["any", "admin", "user", "all"]

type NotificationRouteScope = Literal["admin", "user", "all"]

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
        default=None,
        max_length=3,
        description="多选通知类别；空数组沿用全局范围，空值兼容旧 audience",
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
    rules: tuple[NotificationRule, ...] | None = Field(
        default=None,
        max_length=100,
        exclude_if=lambda value: value is None,
        description="内置模块的独立投递规则，插件兼容时为空",
    )
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
    platform_options: dict[str, JsonData] = Field(default_factory=dict, description="冻结升级格式的平台交互选项")
    admission: AdmissionMode = Field(
        default="manual",
        description="机器人接入策略",
    )
    admission_rules: tuple[AdmissionRule, ...] = Field(default=(), max_length=100)
    auto_download: AutoDownloadMode = Field(
        default="off",
        description="普通搜索命中后是否自动择优，仍须拥有下载权限",
    )
    auto_download_rules: tuple[InteractionMatch, ...] = Field(default=(), max_length=100)
    revision: ChannelRevision = Field(description="该策略的条件写入版本")


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
