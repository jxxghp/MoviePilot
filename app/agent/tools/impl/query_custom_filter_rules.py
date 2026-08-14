"""查询自定义过滤规则工具。"""

import json
from typing import Optional, Type, List

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.agent.tools.impl._filter_rule_utils import (
    collect_custom_rule_group_refs,
    get_custom_rules,
    get_rule_groups,
    serialize_custom_rule,
)
from app.runtime.log import logger


class QueryCustomFilterRulesInput(BaseModel):
    """查询自定义过滤规则工具的输入参数模型"""

    rule_ids: Optional[List[str]] = Field(
        None,
        description="Optional list of custom rule IDs to query. If omitted, return all custom rules.",
    )
    include_group_refs: bool = Field(
        True,
        description="Whether to include which rule groups reference each custom rule.",
    )


class QueryCustomFilterRulesTool(MoviePilotTool):
    name: str = "query_custom_filter_rules"
    tags: list[str] = [
        ToolTag.Read,
        ToolTag.FilterRule,
    ]
    description: str = (
        "Query custom filter rules stored in CustomFilterRules. "
        "Custom rules can be referenced from rule_string expressions in filter rule groups. "
        "Use this tool before add_rule_group or update_rule_group to learn valid custom rule IDs."
    )
    args_schema: Type[BaseModel] = QueryCustomFilterRulesInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        rule_ids = kwargs.get("rule_ids") or []
        if rule_ids:
            return f"查询自定义过滤规则: {', '.join(rule_ids)}"
        return "查询所有自定义过滤规则"

    async def run(
        self,
        rule_ids: Optional[List[str]] = None,
        include_group_refs: bool = True,
        **kwargs,
    ) -> str:
        logger.info(f"执行工具: {self.name}")

        try:
            custom_rules = get_custom_rules()
            if rule_ids:
                target_ids = set(rule_ids)
                custom_rules = [
                    rule for rule in custom_rules if rule.id in target_ids
                ]

            refs = {}
            if include_group_refs:
                refs = collect_custom_rule_group_refs(
                    get_rule_groups(),
                    [rule.id for rule in custom_rules if rule.id],
                )

            serialized = [
                serialize_custom_rule(rule, refs.get(rule.id))
                for rule in custom_rules
            ]
            return json.dumps(
                {
                    "success": True,
                    "count": len(serialized),
                    "rules": serialized,
                },
                ensure_ascii=False,
                indent=2,
            )
        except Exception as exc:
            logger.error(f"查询自定义过滤规则失败: {exc}", exc_info=True)
            return json.dumps(
                {
                    "success": False,
                    "message": f"查询自定义过滤规则失败: {exc}",
                    "rules": [],
                },
                ensure_ascii=False,
            )
