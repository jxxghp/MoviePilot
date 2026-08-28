"""更新自定义过滤规则工具。"""

import json
from typing import Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.impl._filter_rule_utils import (
    collect_custom_rule_group_refs,
    get_custom_rules,
    get_rule_groups,
    normalize_custom_rule,
    publish_rule_config_changed,
    replace_rule_id_in_rule_string,
    save_system_config,
    serialize_custom_rule,
)
from app.agent.tools.tags import ToolTag
from app.runtime.log import logger
from app.schemas.types import SystemConfigKey


class UpdateCustomFilterRuleInput(BaseModel):
    """更新自定义过滤规则工具的输入参数模型"""

    current_rule_id: str = Field(
        ..., description="Existing custom rule ID to update."
    )
    new_rule_id: Optional[str] = Field(
        None,
        description="New rule ID. If omitted, keep the original rule ID.",
    )
    name: Optional[str] = Field(
        None, description="New display name. If omitted, keep the original name."
    )
    include: Optional[str] = Field(
        None,
        description="New include regex. Pass an empty string to clear it.",
    )
    exclude: Optional[str] = Field(
        None,
        description="New exclude regex. Pass an empty string to clear it.",
    )
    size_range: Optional[str] = Field(
        None,
        description="New size range in MB. Pass an empty string to clear it.",
    )
    seeders: Optional[str] = Field(
        None,
        description="New minimum seeder count. Pass an empty string to clear it.",
    )
    publish_time: Optional[str] = Field(
        None,
        description="New publish-time filter in minutes. Pass an empty string to clear it.",
    )


class UpdateCustomFilterRuleTool(MoviePilotTool):
    name: str = "update_custom_filter_rule"
    tags: list[str] = [
        ToolTag.Write,
        ToolTag.FilterRule,
        ToolTag.Admin,
    ]
    description: str = (
        "Update an existing custom filter rule. "
        "If the rule ID is renamed, all rule groups that reference the old ID are updated automatically."
    )
    args_schema: Type[BaseModel] = UpdateCustomFilterRuleInput
    require_admin: bool = True

    def get_tool_message(self, **kwargs) -> Optional[str]:
        current_rule_id = kwargs.get("current_rule_id", "")
        new_rule_id = kwargs.get("new_rule_id")
        if new_rule_id and new_rule_id != current_rule_id:
            return f"更新自定义过滤规则 {current_rule_id} -> {new_rule_id}"
        return f"更新自定义过滤规则 {current_rule_id}"

    async def run(
        self,
        current_rule_id: str,
        new_rule_id: Optional[str] = None,
        name: Optional[str] = None,
        include: Optional[str] = None,
        exclude: Optional[str] = None,
        size_range: Optional[str] = None,
        seeders: Optional[str] = None,
        publish_time: Optional[str] = None,
        **kwargs,
    ) -> str:
        logger.info(f"执行工具: {self.name}, current_rule_id={current_rule_id}")

        try:
            custom_rules = get_custom_rules()
            rule_map = {rule.id: rule for rule in custom_rules if rule.id}
            current_rule = rule_map.get(current_rule_id)
            if not current_rule:
                return json.dumps(
                    {
                        "success": False,
                        "message": f"自定义过滤规则 '{current_rule_id}' 不存在",
                    },
                    ensure_ascii=False,
                )

            updated_rule = normalize_custom_rule(
                rule_id=new_rule_id or current_rule.id,
                name=name if name is not None else current_rule.name,
                include=include if include is not None else current_rule.include,
                exclude=exclude if exclude is not None else current_rule.exclude,
                size_range=(
                    size_range if size_range is not None else current_rule.size_range
                ),
                seeders=seeders if seeders is not None else current_rule.seeders,
                publish_time=(
                    publish_time
                    if publish_time is not None
                    else current_rule.publish_time
                ),
                existing_rules=custom_rules,
                original_rule_id=current_rule.id,
            )

            expected_custom_rules = [
                rule.model_dump(exclude_none=True) for rule in custom_rules
            ]
            final_rules = [
                updated_rule if rule.id == current_rule.id else rule
                for rule in custom_rules
            ]
            custom_rule_definitions = [
                rule.model_dump(exclude_none=True) for rule in final_rules
            ]

            rule_groups = get_rule_groups()
            expected_rule_groups = [
                group.model_dump(exclude_none=True) for group in rule_groups
            ]
            updated_rule_groups = rule_groups
            renamed_group_refs = []
            if updated_rule.id != current_rule.id:
                updated_rule_groups = []
                for group in rule_groups:
                    if not group.rule_string:
                        updated_rule_groups.append(group)
                        continue
                    new_rule_string = replace_rule_id_in_rule_string(
                        group.rule_string,
                        current_rule.id,
                        updated_rule.id,
                    )
                    if new_rule_string == group.rule_string:
                        updated_rule_groups.append(group)
                        continue
                    renamed_group_refs.append(group.name)
                    updated_rule_groups.append(
                        group.model_copy(update={"rule_string": new_rule_string})
                    )

                rule_group_definitions = [
                    group.model_dump(exclude_none=True)
                    for group in updated_rule_groups
                ]
                async with self.data.async_rule_group_mutation_scope() as mutation:
                    await mutation.apply(
                        rule_group_definitions,
                        expected_rule_groups=expected_rule_groups,
                        custom_rules=custom_rule_definitions,
                        expected_custom_rules=expected_custom_rules,
                    )
                await publish_rule_config_changed(
                    SystemConfigKey.CustomFilterRules,
                    custom_rule_definitions,
                )
                await publish_rule_config_changed(
                    SystemConfigKey.UserFilterRuleGroups,
                    rule_group_definitions,
                )
            else:
                await save_system_config(
                    SystemConfigKey.CustomFilterRules,
                    custom_rule_definitions,
                )

            updated_refs = collect_custom_rule_group_refs(
                updated_rule_groups,
                [updated_rule.id],
            )
            return json.dumps(
                {
                    "success": True,
                    "message": f"已更新自定义过滤规则 {updated_rule.id}",
                    "custom_rule": serialize_custom_rule(
                        updated_rule,
                        updated_refs.get(updated_rule.id),
                    ),
                    "rule_groups_updated_for_rule_id_rename": renamed_group_refs,
                },
                ensure_ascii=False,
                indent=2,
            )
        except Exception as exc:
            logger.error(f"更新自定义过滤规则失败: {exc}", exc_info=True)
            return json.dumps(
                {
                    "success": False,
                    "message": f"更新自定义过滤规则失败: {exc}",
                },
                ensure_ascii=False,
            )
