"""查询过滤规则组工具。"""

import json
from typing import List, Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.impl._filter_rule_utils import (
    RULE_STRING_SYNTAX,
    collect_rule_group_usages,
    get_rule_groups,
    serialize_rule_group,
)
from app.agent.tools.tags import ToolTag
from app.runtime.log import logger


class QueryRuleGroupsInput(BaseModel):
    """查询规则组工具的输入参数模型"""

    group_names: Optional[List[str]] = Field(
        None,
        description="Optional list of rule group names to query. If omitted, return all rule groups.",
    )
    include_usage: bool = Field(
        True,
        description="Whether to include where each rule group is referenced by global settings or subscriptions.",
    )


class QueryRuleGroupsTool(MoviePilotTool):
    name: str = "query_rule_groups"
    tags: list[str] = [
        ToolTag.Read,
        ToolTag.FilterRule,
    ]
    description: str = (
        "Query filter rule groups (过滤规则组 / 优先级规则组). "
        "Each rule group contains a rule_string made of built-in rules and/or custom rules. "
        "Inside one level use '&', '|', '!' and optional parentheses; use '>' between levels. "
        "Levels are evaluated from left to right, and the first matched level wins. "
        "The result includes parsed levels and syntax guidance so the agent can learn existing patterns before writing a new rule group."
    )
    args_schema: Type[BaseModel] = QueryRuleGroupsInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        group_names = kwargs.get("group_names") or []
        if group_names:
            return f"查询规则组: {', '.join(group_names)}"
        return "查询所有规则组"

    async def run(
        self,
        group_names: Optional[List[str]] = None,
        include_usage: bool = True,
        **kwargs,
    ) -> str:
        logger.info(f"执行工具: {self.name}")

        try:
            rule_groups = get_rule_groups()
            if group_names:
                target_names = set(group_names)
                rule_groups = [
                    group for group in rule_groups if group.name in target_names
                ]

            usage_map = {}
            if include_usage:
                usage_map = await collect_rule_group_usages(
                    self.data.subscriptions,
                    [group.name for group in rule_groups if group.name]
                )

            serialized = [
                serialize_rule_group(group, usage_map.get(group.name))
                for group in rule_groups
            ]
            message = (
                f"找到 {len(serialized)} 个规则组"
                if serialized
                else "未找到任何规则组"
            )

            return json.dumps(
                {
                    "success": True,
                    "message": message,
                    "count": len(serialized),
                    "rule_string_syntax": RULE_STRING_SYNTAX,
                    "rule_groups": serialized,
                },
                ensure_ascii=False,
                indent=2,
            )
        except Exception as exc:
            logger.error(f"查询规则组失败: {exc}", exc_info=True)
            return json.dumps(
                {
                    "success": False,
                    "message": f"查询规则组失败: {exc}",
                    "rule_groups": [],
                },
                ensure_ascii=False,
            )
