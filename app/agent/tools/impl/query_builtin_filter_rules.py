"""查询内置过滤规则工具。"""

import json
from typing import Optional, Type, List

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.agent.tools.impl._filter_rule_utils import (
    get_builtin_rules,
    serialize_builtin_rule,
    RULE_STRING_SYNTAX,
)
from app.runtime.log import logger


class QueryBuiltinFilterRulesInput(BaseModel):
    """查询内置过滤规则工具的输入参数模型"""

    rule_ids: Optional[List[str]] = Field(
        None,
        description="Optional list of built-in rule IDs to query. If omitted, return all built-in rules.",
    )


class QueryBuiltinFilterRulesTool(MoviePilotTool):
    name: str = "query_builtin_filter_rules"
    tags: list[str] = [
        ToolTag.Read,
        ToolTag.FilterRule,
    ]
    description: str = (
        "Query built-in filter rules defined by the backend filter module. "
        "These rule IDs can be used directly inside rule_string expressions for filter rule groups. "
        "Use this tool before add_rule_group or update_rule_group to learn valid built-in rule IDs."
    )
    args_schema: Type[BaseModel] = QueryBuiltinFilterRulesInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        rule_ids = kwargs.get("rule_ids") or []
        if rule_ids:
            return f"查询内置过滤规则: {', '.join(rule_ids)}"
        return "查询所有内置过滤规则"

    async def run(
        self,
        rule_ids: Optional[List[str]] = None,
        **kwargs,
    ) -> str:
        logger.info(f"执行工具: {self.name}")

        try:
            builtin_rules = get_builtin_rules()
            if rule_ids:
                target_ids = set(rule_ids)
                builtin_rules = {
                    rule_id: payload
                    for rule_id, payload in builtin_rules.items()
                    if rule_id in target_ids
                }

            serialized = [
                serialize_builtin_rule(rule_id, payload)
                for rule_id, payload in builtin_rules.items()
            ]
            return json.dumps(
                {
                    "success": True,
                    "count": len(serialized),
                    "rule_string_syntax": RULE_STRING_SYNTAX,
                    "rules": serialized,
                },
                ensure_ascii=False,
                indent=2,
            )
        except Exception as exc:
            logger.error(f"查询内置过滤规则失败: {exc}", exc_info=True)
            return json.dumps(
                {
                    "success": False,
                    "message": f"查询内置过滤规则失败: {exc}",
                    "rules": [],
                },
                ensure_ascii=False,
            )
