"""
MoviePilot Agent 宿主策略的内部契约。
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, MutableMapping, Optional


class ToolOrigin(str, Enum):
    """
    工具调用的宿主可信入口。
    """

    # 由用户交互会话触发
    AGENT_INTERACTIVE = "agent_interactive"
    # 由智能体 API 入口触发
    AGENT_API = "agent_api"
    # 由操作员直接调用工具
    OPERATOR_DIRECT = "operator_direct"
    # 由宿主后台任务触发
    BACKGROUND = "background"
    # 由受控子智能体触发
    SUBAGENT = "subagent"


class PrincipalType(str, Enum):
    """
    调用主体类型，用于区分人、管理员集成和内部运行时。
    """

    # 具有宿主身份的自然人用户
    HUMAN = "human"
    # 使用系统管理员身份的外部集成
    SYSTEM_ADMIN_INTEGRATION = "system_admin_integration"
    # 受权限范围限制的智能体身份
    SCOPED_AGENT = "scoped_agent"
    # 宿主后台执行主体
    BACKGROUND = "background"
    # 由上级智能体派生的执行主体
    SUBAGENT = "subagent"


class AuthSource(str, Enum):
    """
    主体身份的宿主认证来源。
    """

    # 通过消息渠道解析用户身份
    CHANNEL = "channel"
    # 通过网页登录会话认证
    WEB_SESSION = "web_session"
    # 通过系统 API 令牌认证
    API_TOKEN = "api_token"
    # 由宿主内部执行边界建立身份
    INTERNAL = "internal"
    # 通过智能体专用令牌认证
    AGENT_TOKEN = "agent_token"


class PrincipalRole(str, Enum):
    """
    策略授权使用的角色层级。
    """

    # 普通用户权限
    USER = "user"
    # 渠道管理权限
    CHANNEL_ADMIN = "channel_admin"
    # 系统管理权限
    SYSTEM_ADMIN = "system_admin"
    # 宿主内部执行权限
    SYSTEM_INTERNAL = "system_internal"


class ActionEffect(str, Enum):
    """
    工具调用的实际副作用类别。
    """

    # 读取不涉及敏感内容的业务数据
    SAFE_READ = "safe_read"
    # 读取私密或凭据等敏感内容
    SENSITIVE_READ = "sensitive_read"
    # 可以恢复的业务写入
    REVERSIBLE_WRITE = "reversible_write"
    # 删除或覆盖等破坏性写入
    DESTRUCTIVE_WRITE = "destructive_write"
    # 会改变外部系统状态的操作
    EXTERNAL_SIDE_EFFECT = "external_side_effect"
    # 执行动态代码或命令
    ARBITRARY_EXECUTION = "arbitrary_execution"
    # 尚未归类的副作用，不能视为安全读取
    UNKNOWN = "unknown"


class ConfirmationMode(str, Enum):
    """
    动作在完成授权后所需的确认方式。
    """

    # 通过授权后无需额外确认
    NONE = "none"
    # 执行前必须取得确认
    REQUIRED = "required"
    # 当前调用路径不支持所需确认
    UNSUPPORTED = "unsupported"


class RecoveryMode(str, Enum):
    """
    动作可提供的执行恢复保证。
    """

    # 没有声明恢复保证
    NONE = "none"
    # 通过事务回滚恢复
    TRANSACTION = "transaction"
    # 利用保存的操作前状态恢复
    BEFORE_STATE = "before_state"
    # 通过可恢复删除机制还原
    RECOVERABLE_DELETE = "recoverable_delete"
    # 同一操作可幂等重试
    IDEMPOTENT = "idempotent"
    # 先核对外部实际状态再决定后续操作
    RECONCILE = "reconcile"
    # 只能由人工介入恢复
    MANUAL_ONLY = "manual_only"


class ResultSensitivity(str, Enum):
    """
    工具结果进入模型、记忆和日志时的敏感等级。
    """

    # 可按普通业务结果处理
    NORMAL = "normal"
    # 包含用户私密信息
    PRIVATE = "private"
    # 包含密钥、令牌等秘密信息
    SECRET = "secret"
    # 尚未确认敏感等级，需保守处理
    UNKNOWN = "unknown"


class MigrationState(str, Enum):
    """
    工具策略当前采用宿主强制还是兼容观测。
    """

    # 由新宿主策略实际执行门禁
    ENFORCED = "enforced"
    # 新策略只做观测，仍沿用原有门禁
    LEGACY_SHADOW = "legacy_shadow"


class ExecutionOutcome(str, Enum):
    """
    工具实际回执状态；提交未完成和结果未知不能视为业务成功。
    """

    # 业务操作已确认成功
    SUCCEEDED = "succeeded"
    # 业务操作已确认失败
    FAILED = "failed"
    # 操作已经提交但尚未完成
    PENDING = "pending"
    # 无法确认真实执行结果
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PolicyPrincipal:
    """
    由可信入口建立、不可由工具参数覆盖的调用主体。

    :param principal_id: 可信入口解析的调用主体标识
    :param principal_type: 区分用户、管理员集成和内部执行方的主体类型
    :param auth_source: 建立该身份使用的认证来源
    :param role: 宿主解析的授权角色，不接受工具参数覆盖
    :param scopes: 额外限定的权限范围，未限定时为空元组
    """

    principal_id: str
    principal_type: PrincipalType
    auth_source: AuthSource
    role: PrincipalRole
    scopes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ToolInvocation:
    """
    一次进入宿主策略层的规范化工具调用。

    :param invocation_id: 本次工具调用的唯一标识
    :param tool_name: 策略目录识别的工具名称
    :param arguments: 本次调用的规范化参数映射
    :param principal: 由宿主可信入口建立的调用主体
    :param session_id: 所属智能体会话标识
    :param origin: 由宿主确认的工具调用入口
    :param channel: 关联的消息渠道；不经渠道调用时为 None
    :param source: 关联的消息来源；没有来源信息时为 None
    """

    invocation_id: str
    tool_name: str
    arguments: Mapping[str, Any]
    principal: PolicyPrincipal
    session_id: str
    origin: ToolOrigin
    channel: Optional[str] = None
    source: Optional[str] = None


@dataclass(frozen=True)
class ToolRevision:
    """
    记录目录项绑定的工具实现、工厂和插件目录版本。

    :param implementation: 工具实现版本或实现指纹
    :param factory: 构造工具的工厂版本
    :param plugin: 工具所属插件目录的版本
    """

    implementation: str
    factory: str
    plugin: str


@dataclass(frozen=True)
class ActionPolicy:
    """
    参数级动作策略及其兼容迁移状态。

    :param effect: 根据具体参数识别的实际副作用类别
    :param required_role: 执行该动作要求的最低授权角色
    :param confirmation: 授权通过后所需的用户确认方式
    :param recovery: 动作可用的恢复或结果核对机制
    :param result_sensitivity: 结果进入模型、记忆和日志时的敏感等级
    :param migration_state: 采用强制执行或历史兼容观测的迁移状态
    :param policy_version: 用于记录和比较策略的版本标识
    :param interactive_allowed: 是否允许交互会话发起此动作
    :param machine_allowed: 是否允许机器接口发起此动作
    :param background_allowed: 是否允许后台任务发起此动作
    :param subagent_allowed: 是否允许子智能体发起此动作
    """

    effect: ActionEffect
    required_role: PrincipalRole
    confirmation: ConfirmationMode
    recovery: RecoveryMode
    result_sensitivity: ResultSensitivity
    migration_state: MigrationState
    policy_version: str = "p1-g1-v1"
    interactive_allowed: bool = True
    machine_allowed: bool = True
    background_allowed: bool = True
    subagent_allowed: bool = True


@dataclass(frozen=True)
class PolicyDecision:
    """
    宿主策略层决定；shadow allow 仅表示新策略不拦截，旧门禁仍是授权事实源。

    :param allowed: 新策略是否允许继续调用；观测模式下仍需通过原有门禁
    :param confirmation_required: 执行前是否还需要用户确认
    :param shadow: 是否只记录策略观测而不替代原有授权门禁
    :param reason_code: 允许或拒绝的稳定原因码
    """

    allowed: bool
    confirmation_required: bool
    shadow: bool
    reason_code: str


@dataclass(frozen=True)
class PolicyObservation:
    """
    调用开始时生成、供完成或失败回执复用的观测对象。

    :param invocation: 本次规范化工具调用
    :param policy: 本次调用使用的参数级动作策略
    :param decision: 进入执行前的策略决定
    :param input_summary: 脱敏后的输入摘要
    :param started_at: 开始观测时的单调时钟秒数，用于计算耗时
    """

    invocation: ToolInvocation
    policy: ActionPolicy
    decision: PolicyDecision
    input_summary: str
    started_at: float


@dataclass(frozen=True)
class ExecutionReceipt:
    """
    工具策略生命周期生成的非持久化脱敏回执。

    :param invocation_id: 对应工具调用的唯一标识
    :param tool_name: 实际执行的工具名称
    :param origin: 工具调用的可信入口
    :param decision: 执行前作出的宿主策略决定
    :param outcome: 实际执行状态，待完成和未知结果不等同于成功
    :param input_summary: 脱敏后的输入摘要
    :param result_summary: 脱敏后的结果摘要；没有可用结果时为 None
    :param error_summary: 脱敏后的错误摘要；没有错误时为 None
    :param duration_ms: 本次执行耗时，单位为毫秒，默认 0
    :param external_may_continue: 中断后外部操作是否仍可能继续，不能据此假定已停止
    :param needs_reconcile: 是否需先查询外部真实状态，再决定补偿或重试
    """

    invocation_id: str
    tool_name: str
    origin: ToolOrigin
    decision: PolicyDecision
    outcome: ExecutionOutcome
    input_summary: str
    result_summary: Optional[str] = None
    error_summary: Optional[str] = None
    duration_ms: int = 0
    external_may_continue: bool = False
    needs_reconcile: bool = False


@dataclass(frozen=True)
class ToolPolicyContext:
    """
    宿主入口上下文；管理员状态引用会随缓存图的每轮执行刷新。

    :param session_id: 工具调用所属的智能体会话标识
    :param user_id: 由可信入口绑定的用户标识
    :param origin: 本轮执行的可信来源
    :param principal_type: 本轮执行的主体类型
    :param auth_source: 主体身份的认证来源
    :param agent_context: 随每轮执行更新的可变宿主上下文，包含管理员状态和权限范围
    :param channel: 关联消息渠道；没有渠道时为 None
    :param source: 关联消息来源；没有来源时为 None
    """

    session_id: str
    user_id: str
    origin: ToolOrigin
    principal_type: PrincipalType
    auth_source: AuthSource
    agent_context: MutableMapping[str, Any] = field(repr=False, compare=False)
    channel: Optional[str] = None
    source: Optional[str] = None

    @property
    def principal(self) -> PolicyPrincipal:
        """
        根据当前宿主上下文生成本次调用主体。
        """
        if self.principal_type in {PrincipalType.BACKGROUND, PrincipalType.SUBAGENT}:
            default_role = PrincipalRole.SYSTEM_INTERNAL
        else:
            default_role = PrincipalRole.USER
        role = (
            PrincipalRole.SYSTEM_ADMIN
            if bool(self.agent_context.get("is_admin"))
            else default_role
        )
        raw_scopes = self.agent_context.get("policy_scopes") or ()
        scopes = tuple(str(scope) for scope in raw_scopes if scope)
        return PolicyPrincipal(
            principal_id=str(self.user_id or self.principal_type.value),
            principal_type=self.principal_type,
            auth_source=self.auth_source,
            role=role,
            scopes=scopes,
        )

    def for_subagent(self) -> "ToolPolicyContext":
        """
        保留用户与会话归属，并切换为子代理可信来源。
        """
        return ToolPolicyContext(
            session_id=self.session_id,
            user_id=self.user_id,
            origin=ToolOrigin.SUBAGENT,
            principal_type=PrincipalType.SUBAGENT,
            auth_source=AuthSource.INTERNAL,
            agent_context=self.agent_context,
            channel=self.channel,
            source=self.source,
        )


__all__ = [
    "ActionEffect",
    "ActionPolicy",
    "AuthSource",
    "ConfirmationMode",
    "ExecutionOutcome",
    "ExecutionReceipt",
    "MigrationState",
    "PolicyDecision",
    "PolicyObservation",
    "PolicyPrincipal",
    "PrincipalRole",
    "PrincipalType",
    "RecoveryMode",
    "ResultSensitivity",
    "ToolInvocation",
    "ToolOrigin",
    "ToolPolicyContext",
    "ToolRevision",
]
