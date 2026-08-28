"""删除过滤规则组工具。"""

import json
from typing import Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.impl._filter_rule_utils import (
    get_rule_groups,
    publish_rule_config_changed,
)
from app.agent.tools.tags import ToolTag
from app.runtime.log import logger
from app.schemas.types import SystemConfigKey


class DeleteRuleGroupInput(BaseModel):
    """删除过滤规则组工具的输入参数模型"""

    name: str = Field(..., description="Rule group name to delete.")


class DeleteRuleGroupTool(MoviePilotTool):
    name: str = "delete_rule_group"
    tags: list[str] = [
        ToolTag.Write,
        ToolTag.FilterRule,
        ToolTag.Admin,
    ]
    description: str = (
        "Delete a filter rule group from UserFilterRuleGroups. "
        "The tool also removes dangling references from global settings and subscriptions."
    )
    args_schema: Type[BaseModel] = DeleteRuleGroupInput
    require_admin: bool = True

    def get_tool_message(self, **kwargs) -> Optional[str]:
        return f"删除规则组 {kwargs.get('name', '')}"

    async def run(self, name: str, **kwargs) -> str:
        logger.info(f"执行工具: {self.name}, name={name}")

        try:
            rule_groups = get_rule_groups()
            expected_definitions = [
                group.model_dump(exclude_none=True) for group in rule_groups
            ]
            if not any(group.name == name for group in rule_groups):
                return json.dumps(
                    {
                        "success": False,
                        "message": f"规则组 '{name}' 不存在",
                    },
                    ensure_ascii=False,
                )

            remaining_groups = [
                group for group in rule_groups if group.name != name
            ]
            definitions = [
                group.model_dump(exclude_none=True) for group in remaining_groups
            ]
            async with self.data.async_rule_group_mutation_scope() as mutation:
                result = await mutation.apply(
                    definitions,
                    expected_rule_groups=expected_definitions,
                    previous_name=name,
                )
            await publish_rule_config_changed(
                SystemConfigKey.UserFilterRuleGroups,
                definitions,
            )
            reference_changes = result.to_dict()

            return json.dumps(
                {
                    "success": True,
                    "message": f"已删除规则组 {name}",
                    "count": len(remaining_groups),
                    "reference_updates": reference_changes,
                },
                ensure_ascii=False,
                indent=2,
            )
        except Exception as exc:
            logger.error(f"删除规则组失败: {exc}", exc_info=True)
            return json.dumps(
                {
                    "success": False,
                    "message": f"删除规则组失败: {exc}",
                },
                ensure_ascii=False,
            )
