"""MoviePilot Agent 宿主策略的内部契约。"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, MutableMapping, Optional


class ToolOrigin(str, Enum):
    """工具调用的宿主可信入口。"""

    AGENT_INTERACTIVE = "agent_interactive"
    AGENT_API = "agent_api"
    OPERATOR_DIRECT = "operator_direct"
    BACKGROUND = "background"
    SUBAGENT = "subagent"


class PrincipalType(str, Enum):
    """调用主体类型，用于区分人、管理员集成和内部运行时。"""

    HUMAN = "human"
    SYSTEM_ADMIN_INTEGRATION = "system_admin_integration"
    SCOPED_AGENT = "scoped_agent"
    BACKGROUND = "background"
    SUBAGENT = "subagent"


class AuthSource(str, Enum):
    """主体身份的宿主认证来源。"""

    CHANNEL = "channel"
    WEB_SESSION = "web_session"
    API_TOKEN = "api_token"
    INTERNAL = "internal"
    AGENT_TOKEN = "agent_token"


class PrincipalRole(str, Enum):
    """策略授权使用的角色层级。"""

    USER = "user"
    CHANNEL_ADMIN = "channel_admin"
    SYSTEM_ADMIN = "system_admin"
    SYSTEM_INTERNAL = "system_internal"


class ActionEffect(str, Enum):
    """工具调用的实际副作用类别。"""

    SAFE_READ = "safe_read"
    SENSITIVE_READ = "sensitive_read"
    REVERSIBLE_WRITE = "reversible_write"
    DESTRUCTIVE_WRITE = "destructive_write"
    EXTERNAL_SIDE_EFFECT = "external_side_effect"
    ARBITRARY_EXECUTION = "arbitrary_execution"
    UNKNOWN = "unknown"


class ConfirmationMode(str, Enum):
    """动作在完成授权后所需的确认方式。"""

    NONE = "none"
    REQUIRED = "required"
    UNSUPPORTED = "unsupported"


class RecoveryMode(str, Enum):
    """动作可提供的执行恢复保证。"""

    NONE = "none"
    TRANSACTION = "transaction"
    BEFORE_STATE = "before_state"
    RECOVERABLE_DELETE = "recoverable_delete"
    IDEMPOTENT = "idempotent"
    RECONCILE = "reconcile"
    MANUAL_ONLY = "manual_only"


class ResultSensitivity(str, Enum):
    """工具结果进入模型、记忆和日志时的敏感等级。"""

    NORMAL = "normal"
    PRIVATE = "private"
    SECRET = "secret"
    UNKNOWN = "unknown"


class MigrationState(str, Enum):
    """工具策略当前采用宿主强制还是兼容观测。"""

    ENFORCED = "enforced"
    LEGACY_SHADOW = "legacy_shadow"


class ExecutionOutcome(str, Enum):
    """工具 handler 观测终态；成功不代表业务授权或副作用已完成。"""

    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True)
class PolicyPrincipal:
    """由可信入口建立、不可由工具参数覆盖的调用主体。"""

    principal_id: str
    principal_type: PrincipalType
    auth_source: AuthSource
    role: PrincipalRole
    scopes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ToolInvocation:
    """一次进入宿主策略层的规范化工具调用。"""

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
    """记录目录项绑定的工具实现、工厂和插件目录版本。"""

    implementation: str
    factory: str
    plugin: str


@dataclass(frozen=True)
class ActionPolicy:
    """参数级动作策略及其兼容迁移状态。"""

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
    """宿主策略层决定；shadow allow 仅表示新策略不拦截，旧门禁仍是授权事实源。"""

    allowed: bool
    confirmation_required: bool
    shadow: bool
    reason_code: str


@dataclass(frozen=True)
class PolicyObservation:
    """调用开始时生成、供完成或失败回执复用的观测对象。"""

    invocation: ToolInvocation
    policy: ActionPolicy
    decision: PolicyDecision
    input_summary: str
    started_at: float


@dataclass(frozen=True)
class ExecutionReceipt:
    """工具策略生命周期生成的非持久化脱敏回执。"""

    invocation_id: str
    tool_name: str
    origin: ToolOrigin
    decision: PolicyDecision
    outcome: ExecutionOutcome
    input_summary: str
    result_summary: Optional[str] = None
    error_summary: Optional[str] = None
    duration_ms: int = 0
    external_may_continue: bool = False  # 中断后外部操作仍可能继续，不能视为已停止。
    needs_reconcile: bool = False  # 调用方需查询外部实际状态后再决定补偿或重试。


@dataclass(frozen=True)
class ToolPolicyContext:
    """宿主入口上下文；管理员状态引用会随缓存图的每轮执行刷新。"""

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
        """根据当前宿主上下文生成本次调用主体。"""
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
        """保留用户与会话归属，并切换为子代理可信来源。"""
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
