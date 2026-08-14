"""删除自定义过滤规则工具。"""

import json
from typing import Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.agent.tools.impl._filter_rule_utils import (
    collect_custom_rule_group_refs,
    get_custom_rules,
    get_rule_groups,
    save_system_config,
)
from app.runtime.log import logger
from app.schemas.types import SystemConfigKey


class DeleteCustomFilterRuleInput(BaseModel):
    """删除自定义过滤规则工具的输入参数模型"""

    rule_id: str = Field(..., description="Custom rule ID to delete.")


class DeleteCustomFilterRuleTool(MoviePilotTool):
    name: str = "delete_custom_filter_rule"
    tags: list[str] = [
        ToolTag.Write,
        ToolTag.FilterRule,
        ToolTag.Admin,
    ]
    description: str = (
        "Delete a custom filter rule from CustomFilterRules. "
        "If the rule is still referenced by rule groups, the deletion is blocked to avoid breaking rule_string expressions."
    )
    args_schema: Type[BaseModel] = DeleteCustomFilterRuleInput
    require_admin: bool = True

    def get_tool_message(self, **kwargs) -> Optional[str]:
        return f"删除自定义过滤规则 {kwargs.get('rule_id', '')}"

    async def run(self, rule_id: str, **kwargs) -> str:
        logger.info(f"执行工具: {self.name}, rule_id={rule_id}")

        try:
            custom_rules = get_custom_rules()
            target_rule = next((rule for rule in custom_rules if rule.id == rule_id), None)
            if not target_rule:
                return json.dumps(
                    {
                        "success": False,
                        "message": f"自定义过滤规则 '{rule_id}' 不存在",
                    },
                    ensure_ascii=False,
                )

            refs = collect_custom_rule_group_refs(get_rule_groups(), [rule_id]).get(
                rule_id, []
            )
            if refs:
                return json.dumps(
                    {
                        "success": False,
                        "message": (
                            f"自定义过滤规则 '{rule_id}' 仍被规则组引用，无法删除。"
                        ),
                        "referenced_by_rule_groups": refs,
                    },
                    ensure_ascii=False,
                    indent=2,
                )

            remaining_rules = [
                rule for rule in custom_rules if rule.id != rule_id
            ]
            await save_system_config(
                SystemConfigKey.CustomFilterRules,
                [rule.model_dump(exclude_none=True) for rule in remaining_rules],
            )

            return json.dumps(
                {
                    "success": True,
                    "message": f"已删除自定义过滤规则 {rule_id}",
                    "count": len(remaining_rules),
                },
                ensure_ascii=False,
                indent=2,
            )
        except Exception as exc:
            logger.error(f"删除自定义过滤规则失败: {exc}", exc_info=True)
            return json.dumps(
                {
                    "success": False,
                    "message": f"删除自定义过滤规则失败: {exc}",
                },
                ensure_ascii=False,
            )
