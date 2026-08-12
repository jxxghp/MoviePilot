"""固定工具迁移注册表与参数级策略解析。"""

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


# 这些读取已具备清晰的无副作用语义，用于证明新宿主边界不会改变正常结果。
SAFE_READ_TOOL_NAMES = frozenset(
    {
        "list_slash_commands",
        "query_installed_plugins",
        "query_personas",
        "query_schedulers",
        "query_workflows",
    }
)


# 其余固定工具先显式处于兼容观测状态，待领域叶子 Goal 逐个迁移。
LEGACY_SHADOW_TOOL_NAMES = frozenset(
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
    """解析固定和动态工具的 P1-G1 迁移策略。"""

    def __init__(
        self,
        *,
        safe_read_tool_names: frozenset[str] = SAFE_READ_TOOL_NAMES,
        legacy_shadow_tool_names: frozenset[str] = LEGACY_SHADOW_TOOL_NAMES,
    ) -> None:
        """建立互斥的固定工具迁移表。"""
        overlap = safe_read_tool_names & legacy_shadow_tool_names
        if overlap:
            raise ValueError(f"工具策略迁移表存在重复项: {sorted(overlap)}")
        self._safe_read_tool_names = safe_read_tool_names
        self._legacy_shadow_tool_names = legacy_shadow_tool_names

    @property
    def builtin_tool_names(self) -> set[str]:
        """返回注册表覆盖的全部固定工具名。"""
        return set(self._safe_read_tool_names | self._legacy_shadow_tool_names)

    def resolve(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, Any],
        requires_admin: bool,
    ) -> ActionPolicy:
        """根据工具名和宿主权限声明解析当前迁移策略。"""
        del arguments  # 参数级迁移由后续领域 Goal 逐项加入。
        required_role = (
            PrincipalRole.SYSTEM_ADMIN if requires_admin else PrincipalRole.USER
        )
        if tool_name in self._safe_read_tool_names:
            return ActionPolicy(
                effect=ActionEffect.SAFE_READ,
                required_role=required_role,
                confirmation=ConfirmationMode.NONE,
                recovery=RecoveryMode.NONE,
                result_sensitivity=ResultSensitivity.NORMAL,
                # 角色门禁仍可能异步识别渠道管理员；G1 不复制旧授权事实源。
                migration_state=(
                    MigrationState.LEGACY_SHADOW
                    if requires_admin
                    else MigrationState.ENFORCED
                ),
            )

        # 固定未迁移工具和动态工具都保持现有执行能力，但不得被视为安全读取。
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
    "DEFAULT_TOOL_POLICY_REGISTRY",
    "LEGACY_SHADOW_TOOL_NAMES",
    "SAFE_READ_TOOL_NAMES",
    "ToolPolicyRegistry",
]
