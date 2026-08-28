"""更新过滤规则组工具。"""

import json
from typing import Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.impl._filter_rule_utils import (
    build_custom_rule_map,
    collect_rule_group_usages,
    get_builtin_rules,
    get_custom_rules,
    get_rule_groups,
    normalize_rule_group,
    rename_rule_group_references,
    save_system_config,
    serialize_rule_group,
)
from app.agent.tools.tags import ToolTag
from app.runtime.log import logger
from app.schemas.types import SystemConfigKey


class UpdateRuleGroupInput(BaseModel):
    """更新过滤规则组工具的输入参数模型"""

    current_name: str = Field(..., description="Existing rule group name to update.")
    new_name: Optional[str] = Field(
        None,
        description="New rule group name. If omitted, keep the original name.",
    )
    rule_string: Optional[str] = Field(
        None,
        description=(
            "New rule_string. If omitted, keep the original rule_string. "
            "Example: 'SPECSUB & CNVOI & 4K & !BLU > CNSUB & CNVOI & 4K & !BLU'."
        ),
    )
    media_type: Optional[str] = Field(
        None,
        description="New media type scope. Pass an empty string to clear it.",
    )
    category: Optional[str] = Field(
        None,
        description="New category. Pass an empty string to clear it.",
    )


class UpdateRuleGroupTool(MoviePilotTool):
    name: str = "update_rule_group"
    tags: list[str] = [
        ToolTag.Write,
        ToolTag.FilterRule,
        ToolTag.Admin,
    ]
    description: str = (
        "Update a filter rule group. "
        "If the rule group name changes, its references in global search/subscription settings and per-subscription bindings are updated automatically. "
        "Before changing rule_string, first use query_builtin_filter_rules and query_custom_filter_rules to confirm valid rule IDs."
    )
    args_schema: Type[BaseModel] = UpdateRuleGroupInput
    require_admin: bool = True

    def get_tool_message(self, **kwargs) -> Optional[str]:
        current_name = kwargs.get("current_name", "")
        new_name = kwargs.get("new_name")
        if new_name and new_name != current_name:
            return f"更新规则组 {current_name} -> {new_name}"
        return f"更新规则组 {current_name}"

    async def run(
        self,
        current_name: str,
        new_name: Optional[str] = None,
        rule_string: Optional[str] = None,
        media_type: Optional[str] = None,
        category: Optional[str] = None,
        **kwargs,
    ) -> str:
        logger.info(f"执行工具: {self.name}, current_name={current_name}")

        try:
            rule_groups = get_rule_groups()
            group_map = {group.name: group for group in rule_groups if group.name}
            current_group = group_map.get(current_name)
            if not current_group:
                return json.dumps(
                    {
                        "success": False,
                        "message": f"规则组 '{current_name}' 不存在",
                    },
                    ensure_ascii=False,
                )

            available_rule_ids = set(get_builtin_rules().keys()) | set(
                build_custom_rule_map(get_custom_rules()).keys()
            )
            updated_group, _ = normalize_rule_group(
                name=new_name or current_group.name,
                rule_string=(
                    rule_string
                    if rule_string is not None
                    else current_group.rule_string
                ),
                media_type=(
                    media_type
                    if media_type is not None
                    else current_group.media_type
                ),
                category=(
                    category if category is not None else current_group.category
                ),
                existing_groups=rule_groups,
                available_rule_ids=available_rule_ids,
                original_name=current_group.name,
            )

            final_groups = []
            for group in rule_groups:
                if group.name == current_group.name:
                    final_groups.append(updated_group)
                else:
                    final_groups.append(group)

            await save_system_config(
                SystemConfigKey.UserFilterRuleGroups,
                [group.model_dump(exclude_none=True) for group in final_groups],
            )

            reference_changes = {}
            if updated_group.name != current_group.name:
                reference_changes = await rename_rule_group_references(
                    self.data.subscriptions,
                    current_group.name,
                    updated_group.name,
                )

            usage = await collect_rule_group_usages(
                self.data.subscriptions,
                [updated_group.name],
            )
            return json.dumps(
                {
                    "success": True,
                    "message": f"已更新规则组 {updated_group.name}",
                    "rule_group": serialize_rule_group(
                        updated_group, usage.get(updated_group.name)
                    ),
                    "reference_updates": reference_changes,
                },
                ensure_ascii=False,
                indent=2,
            )
        except Exception as exc:
            logger.error(f"更新规则组失败: {exc}", exc_info=True)
            return json.dumps(
                {
                    "success": False,
                    "message": f"更新规则组失败: {exc}",
                },
                ensure_ascii=False,
            )
