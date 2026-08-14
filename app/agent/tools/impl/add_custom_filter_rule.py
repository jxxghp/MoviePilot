"""新增自定义过滤规则工具。"""

import json
from typing import Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.agent.tools.impl._filter_rule_utils import (
    get_custom_rules,
    normalize_custom_rule,
    save_system_config,
    serialize_custom_rule,
)
from app.runtime.log import logger
from app.schemas.types import SystemConfigKey


class AddCustomFilterRuleInput(BaseModel):
    """新增自定义过滤规则工具的输入参数模型"""

    rule_id: str = Field(
        ...,
        description="Unique custom rule ID. Only letters and numbers are allowed.",
    )
    name: str = Field(..., description="Display name of the custom rule.")
    include: Optional[str] = Field(
        None, description="Optional include regex for the rule."
    )
    exclude: Optional[str] = Field(
        None, description="Optional exclude regex for the rule."
    )
    size_range: Optional[str] = Field(
        None, description="Optional size range in MB, for example '1000-5000'."
    )
    seeders: Optional[str] = Field(
        None, description="Optional minimum seeder count as a non-negative integer."
    )
    publish_time: Optional[str] = Field(
        None,
        description="Optional publish-time filter in minutes, for example '60' or '60-1440'.",
    )


class AddCustomFilterRuleTool(MoviePilotTool):
    name: str = "add_custom_filter_rule"
    tags: list[str] = [
        ToolTag.Write,
        ToolTag.FilterRule,
        ToolTag.Admin,
    ]
    description: str = (
        "Add a custom filter rule to CustomFilterRules. "
        "The new rule can then be referenced by rule ID inside filter rule groups."
    )
    args_schema: Type[BaseModel] = AddCustomFilterRuleInput
    require_admin: bool = True

    def get_tool_message(self, **kwargs) -> Optional[str]:
        return f"新增自定义过滤规则 {kwargs.get('rule_id', '')}"

    async def run(
        self,
        rule_id: str,
        name: str,
        include: Optional[str] = None,
        exclude: Optional[str] = None,
        size_range: Optional[str] = None,
        seeders: Optional[str] = None,
        publish_time: Optional[str] = None,
        **kwargs,
    ) -> str:
        logger.info(f"执行工具: {self.name}, rule_id={rule_id}")

        try:
            custom_rules = get_custom_rules()
            new_rule = normalize_custom_rule(
                rule_id=rule_id,
                name=name,
                include=include,
                exclude=exclude,
                size_range=size_range,
                seeders=seeders,
                publish_time=publish_time,
                existing_rules=custom_rules,
            )

            custom_rules.append(new_rule)
            await save_system_config(
                SystemConfigKey.CustomFilterRules,
                [rule.model_dump(exclude_none=True) for rule in custom_rules],
            )

            return json.dumps(
                {
                    "success": True,
                    "message": f"已新增自定义过滤规则 {new_rule.id}",
                    "custom_rule": serialize_custom_rule(new_rule),
                    "count": len(custom_rules),
                },
                ensure_ascii=False,
                indent=2,
            )
        except Exception as exc:
            logger.error(f"新增自定义过滤规则失败: {exc}", exc_info=True)
            return json.dumps(
                {
                    "success": False,
                    "message": f"新增自定义过滤规则失败: {exc}",
                },
                ensure_ascii=False,
            )
