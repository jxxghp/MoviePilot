"""工具策略例外与参数级策略解析。"""

from typing import Any, Mapping

from app.agent.policy.api import resolve_api_operation
from app.agent.policy.contracts import (
    ActionEffect,
    ActionPolicy,
    ConfirmationMode,
    MigrationState,
    PrincipalRole,
    RecoveryMode,
    ResultSensitivity,
)

# 这些非管理员读取在运行时解析为强制 SAFE_READ；管理员门禁仍沿用原有授权事实源。
SAFE_READ_TOOL_NAMES: frozenset[str] = frozenset()


def requests_system_setting_secrets(arguments: Mapping[str, Any]) -> bool:
    """判断结构化 API 参数是否请求读取未脱敏系统设置。"""
    if str(arguments.get("operation_id") or "") != "config.system.get":
        return False
    for location in ("body", "query"):
        values = arguments.get(location)
        if isinstance(values, Mapping) and values.get("show_secrets") is True:
            return True
    return False


# 该清单只校验固定工具 inventory；未命中的固定或动态工具同样默认 LEGACY_SHADOW。
BUILTIN_LEGACY_SHADOW_INVENTORY = frozenset(
    {
        "apply_patch",
        "ask_user_choice",
        "browse_webpage",
        "agent_task",
        "edit_file",
        "execute_command",
        "query_doctor_report",
        "read_file",
        "recognize_captcha",
        "search_web",
        "send_local_file",
        "send_message",
        "send_voice_message",
        "persona",
        "write_file",
        "moviepilot_api",
    }
)


class ToolPolicyRegistry:
    """解析固定和动态工具的宿主策略。"""

    def __init__(
        self,
        *,
        safe_read_tool_names: frozenset[str] = SAFE_READ_TOOL_NAMES,
        builtin_legacy_shadow_inventory: frozenset[str] = (BUILTIN_LEGACY_SHADOW_INVENTORY),
    ) -> None:
        """建立 SAFE_READ 例外与固定工具 inventory。"""
        overlap = safe_read_tool_names & builtin_legacy_shadow_inventory
        if overlap:
            raise ValueError(f"工具策略 inventory 存在重复项: {sorted(overlap)}")
        self._safe_read_tool_names = safe_read_tool_names
        self._builtin_legacy_shadow_inventory = builtin_legacy_shadow_inventory

    @property
    def builtin_tool_inventory(self) -> set[str]:
        """返回用于测试校验的固定工具 inventory。"""
        return set(self._safe_read_tool_names | self._builtin_legacy_shadow_inventory)

    def resolve(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, Any],
        requires_admin: bool,
    ) -> ActionPolicy:
        """根据工具名和宿主权限声明解析当前参数级策略。"""
        required_role = PrincipalRole.SYSTEM_ADMIN if requires_admin else PrincipalRole.USER
        if tool_name == "agent_task":
            action = str(arguments.get("action") or "")
            effect = {
                "create": ActionEffect.REVERSIBLE_WRITE,
                "list": ActionEffect.SAFE_READ,
                "update": ActionEffect.REVERSIBLE_WRITE,
                "run": ActionEffect.EXTERNAL_SIDE_EFFECT,
                "delete": ActionEffect.DESTRUCTIVE_WRITE,
            }.get(action, ActionEffect.UNKNOWN)
            return ActionPolicy(
                effect=effect,
                required_role=PrincipalRole.SYSTEM_ADMIN,
                confirmation=(ConfirmationMode.NONE if effect is ActionEffect.SAFE_READ else ConfirmationMode.REQUIRED),
                recovery=(RecoveryMode.NONE if effect is ActionEffect.SAFE_READ else RecoveryMode.RECONCILE),
                result_sensitivity=ResultSensitivity.NORMAL,
                migration_state=MigrationState.ENFORCED,
            )
        if tool_name == "persona":
            action = str(arguments.get("action") or "")
            if action == "list":
                return ActionPolicy(
                    effect=ActionEffect.SAFE_READ,
                    required_role=PrincipalRole.USER,
                    confirmation=ConfirmationMode.NONE,
                    recovery=RecoveryMode.NONE,
                    result_sensitivity=ResultSensitivity.NORMAL,
                    migration_state=MigrationState.ENFORCED,
                )
            return ActionPolicy(
                effect=(ActionEffect.REVERSIBLE_WRITE if action in {"switch", "update"} else ActionEffect.UNKNOWN),
                required_role=(PrincipalRole.SYSTEM_ADMIN if action == "update" else PrincipalRole.USER),
                confirmation=ConfirmationMode.REQUIRED,
                recovery=RecoveryMode.BEFORE_STATE,
                result_sensitivity=ResultSensitivity.NORMAL,
                migration_state=MigrationState.ENFORCED,
            )
        if tool_name == "moviepilot_api":
            operation_id = str(arguments.get("operation_id") or "")
            operation = resolve_api_operation(operation_id)
            if operation is None:
                return ActionPolicy(
                    effect=ActionEffect.UNKNOWN,
                    required_role=PrincipalRole.SYSTEM_ADMIN,
                    confirmation=ConfirmationMode.REQUIRED,
                    recovery=RecoveryMode.MANUAL_ONLY,
                    result_sensitivity=ResultSensitivity.UNKNOWN,
                    migration_state=MigrationState.ENFORCED,
                    machine_allowed=False,
                    background_allowed=False,
                    subagent_allowed=False,
                )
            if requests_system_setting_secrets(arguments):
                return ActionPolicy(
                    effect=ActionEffect.SENSITIVE_READ,
                    required_role=PrincipalRole.SYSTEM_ADMIN,
                    confirmation=ConfirmationMode.REQUIRED,
                    recovery=RecoveryMode.MANUAL_ONLY,
                    result_sensitivity=ResultSensitivity.SECRET,
                    migration_state=MigrationState.ENFORCED,
                    machine_allowed=True,
                    background_allowed=False,
                    subagent_allowed=False,
                )
            return ActionPolicy(
                effect=operation.effect,
                required_role=(PrincipalRole.SYSTEM_ADMIN if requires_admin else operation.required_role),
                confirmation=operation.confirmation,
                recovery=operation.recovery,
                result_sensitivity=operation.result_sensitivity,
                migration_state=MigrationState.ENFORCED,
            )
        if tool_name in self._safe_read_tool_names:
            return ActionPolicy(
                effect=ActionEffect.SAFE_READ,
                required_role=required_role,
                confirmation=ConfirmationMode.NONE,
                recovery=RecoveryMode.NONE,
                result_sensitivity=ResultSensitivity.NORMAL,
                # 角色门禁仍由既有授权事实源判断，管理员读取保持兼容观测。
                migration_state=(MigrationState.LEGACY_SHADOW if requires_admin else MigrationState.ENFORCED),
            )

        # 除明确例外外，固定和动态工具都保持现有能力，但不得被视为安全读取。
        return ActionPolicy(
            effect=ActionEffect.UNKNOWN,
            required_role=required_role,
            confirmation=ConfirmationMode.REQUIRED,
            recovery=RecoveryMode.MANUAL_ONLY,
            result_sensitivity=ResultSensitivity.UNKNOWN,
            migration_state=MigrationState.LEGACY_SHADOW,
        )


DEFAULT_TOOL_POLICY_REGISTRY = ToolPolicyRegistry()


__all__ = [
    "BUILTIN_LEGACY_SHADOW_INVENTORY",
    "DEFAULT_TOOL_POLICY_REGISTRY",
    "SAFE_READ_TOOL_NAMES",
    "ToolPolicyRegistry",
    "requests_system_setting_secrets",
]
