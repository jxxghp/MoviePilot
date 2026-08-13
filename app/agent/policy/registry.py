"""工具策略例外与参数级策略解析。"""

from typing import Any, Mapping

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
SAFE_READ_TOOL_NAMES = frozenset(
    {
        "list_slash_commands",
        "query_installed_plugins",
        "query_personas",
        "query_schedulers",
        "query_workflows",
    }
)


# 该清单只校验固定工具 inventory；未命中的固定或动态工具同样默认 LEGACY_SHADOW。
BUILTIN_LEGACY_SHADOW_INVENTORY = frozenset(
    {
        "add_custom_filter_rule",
        "add_download_tasks",
        "add_rule_group",
        "add_subscribe",
        "ask_user_choice",
        "browse_webpage",
        "create_agent_task",
        "delete_agent_task",
        "delete_custom_filter_rule",
        "delete_download_history",
        "delete_download_tasks",
        "delete_rule_group",
        "delete_subscribe",
        "delete_transfer_history",
        "edit_file",
        "execute_command",
        "get_recommendations",
        "get_search_results",
        "install_plugin",
        "list_directory",
        "query_agent_tasks",
        "query_builtin_filter_rules",
        "query_custom_filter_rules",
        "query_custom_identifiers",
        "query_directory_settings",
        "query_doctor_report",
        "query_download_tasks",
        "query_downloaders",
        "query_episode_schedule",
        "query_library_exists",
        "query_library_latest",
        "query_market_plugins",
        "query_media_detail",
        "query_plugin_capabilities",
        "query_plugin_config",
        "query_plugin_data",
        "query_popular_subscribes",
        "query_rule_groups",
        "query_site_userdata",
        "query_sites",
        "query_subscribe_history",
        "query_subscribe_shares",
        "query_subscribes",
        "query_system_settings",
        "query_transfer_history",
        "read_file",
        "recognize_captcha",
        "recognize_media",
        "reload_plugin",
        "run_agent_task",
        "run_scheduler",
        "run_slash_command",
        "run_workflow",
        "scrape_metadata",
        "search_media",
        "search_person",
        "search_person_credits",
        "search_subscribe",
        "search_torrents",
        "search_web",
        "send_local_file",
        "send_message",
        "send_voice_message",
        "switch_persona",
        "test_site",
        "transfer_file",
        "uninstall_plugin",
        "update_agent_task",
        "update_custom_filter_rule",
        "update_custom_identifiers",
        "update_download_tasks",
        "update_persona_definition",
        "update_plugin_config",
        "update_rule_group",
        "update_site",
        "update_site_cookie",
        "update_subscribe",
        "update_system_settings",
        "write_file",
    }
)


class ToolPolicyRegistry:
    """解析固定和动态工具的宿主策略。"""

    def __init__(
        self,
        *,
        safe_read_tool_names: frozenset[str] = SAFE_READ_TOOL_NAMES,
        builtin_legacy_shadow_inventory: frozenset[str] = (
            BUILTIN_LEGACY_SHADOW_INVENTORY
        ),
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
        return set(
            self._safe_read_tool_names | self._builtin_legacy_shadow_inventory
        )

    def resolve(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, Any],
        requires_admin: bool,
    ) -> ActionPolicy:
        """根据工具名和宿主权限声明解析当前参数级策略。"""
        required_role = (
            PrincipalRole.SYSTEM_ADMIN if requires_admin else PrincipalRole.USER
        )
        if (
            tool_name == "query_system_settings"
            and arguments.get("show_secrets") is True
        ):
            return ActionPolicy(
                effect=ActionEffect.SENSITIVE_READ,
                required_role=PrincipalRole.SYSTEM_ADMIN,
                confirmation=ConfirmationMode.REQUIRED,
                recovery=RecoveryMode.NONE,
                result_sensitivity=ResultSensitivity.SECRET,
                migration_state=MigrationState.ENFORCED,
                policy_version="p1-g2a2-v1",
                machine_allowed=True,
                background_allowed=False,
                subagent_allowed=False,
            )
        if tool_name in self._safe_read_tool_names:
            return ActionPolicy(
                effect=ActionEffect.SAFE_READ,
                required_role=required_role,
                confirmation=ConfirmationMode.NONE,
                recovery=RecoveryMode.NONE,
                result_sensitivity=ResultSensitivity.NORMAL,
                # 角色门禁仍由既有授权事实源判断，管理员读取保持兼容观测。
                migration_state=(
                    MigrationState.LEGACY_SHADOW
                    if requires_admin
                    else MigrationState.ENFORCED
                ),
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
]
