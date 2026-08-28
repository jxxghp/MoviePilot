"""新增过滤规则组工具。"""

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
    publish_rule_config_changed,
    serialize_rule_group,
)
from app.agent.tools.tags import ToolTag
from app.runtime.log import logger
from app.schemas.types import SystemConfigKey


class AddRuleGroupInput(BaseModel):
    """新增过滤规则组工具的输入参数模型"""

    name: str = Field(..., description="New rule group name.")
    rule_string: str = Field(
        ...,
        description=(
            "Rule expression using built-in/custom rule IDs. "
            "Use '&', '!' inside one level, and use '>' between priority levels. "
            "Example: 'SPECSUB & CNVOI & 4K & !BLU > CNSUB & CNVOI & 4K & !BLU'."
        ),
    )
    media_type: Optional[str] = Field(
        None,
        description="Optional media type scope: '电影', '电视剧', '音乐', 'movie', 'tv', or 'music'.",
    )
    category: Optional[str] = Field(
        None,
        description="Optional media category. Only valid when media_type is set.",
    )


class AddRuleGroupTool(MoviePilotTool):
    name: str = "add_rule_group"
    tags: list[str] = [
        ToolTag.Write,
        ToolTag.FilterRule,
        ToolTag.Admin,
    ]
    description: str = (
        "Add a new filter rule group to UserFilterRuleGroups. "
        "Rule groups are matched level by level from left to right and can be linked to search/subscription flows. "
        "Before calling this tool, first use query_builtin_filter_rules and query_custom_filter_rules to confirm valid rule IDs, "
        "and optionally use query_rule_groups to imitate existing rule_string patterns."
    )
    args_schema: Type[BaseModel] = AddRuleGroupInput
    require_admin: bool = True

    def get_tool_message(self, **kwargs) -> Optional[str]:
        return f"新增规则组 {kwargs.get('name', '')}"

    async def run(
        self,
        name: str,
        rule_string: str,
        media_type: Optional[str] = None,
        category: Optional[str] = None,
        **kwargs,
    ) -> str:
        logger.info(f"执行工具: {self.name}, name={name}")

        try:
            custom_rules = get_custom_rules()
            available_rule_ids = set(get_builtin_rules().keys()) | set(
                build_custom_rule_map(custom_rules).keys()
            )
            rule_groups = get_rule_groups()
            expected_definitions = [
                group.model_dump(exclude_none=True) for group in rule_groups
            ]
            new_group, _ = normalize_rule_group(
                name=name,
                rule_string=rule_string,
                media_type=media_type,
                category=category,
                existing_groups=rule_groups,
                available_rule_ids=available_rule_ids,
            )

            rule_groups.append(new_group)
            definitions = [
                group.model_dump(exclude_none=True) for group in rule_groups
            ]
            async with self.data.async_rule_group_mutation_scope() as mutation:
                await mutation.apply(
                    definitions,
                    expected_rule_groups=expected_definitions,
                )
            await publish_rule_config_changed(
                SystemConfigKey.UserFilterRuleGroups,
                definitions,
            )
            usage = await collect_rule_group_usages(
                self.data.subscriptions,
                [new_group.name],
            )

            return json.dumps(
                {
                    "success": True,
                    "message": f"已新增规则组 {new_group.name}",
                    "rule_group": serialize_rule_group(
                        new_group, usage.get(new_group.name)
                    ),
                    "count": len(rule_groups),
                },
                ensure_ascii=False,
                indent=2,
            )
        except Exception as exc:
            logger.error(f"新增规则组失败: {exc}", exc_info=True)
            return json.dumps(
                {
                    "success": False,
                    "message": f"新增规则组失败: {exc}",
                },
                ensure_ascii=False,
            )
