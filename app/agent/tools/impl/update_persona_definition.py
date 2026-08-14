"""更新人格定义工具。"""

import json
from typing import Optional, Type

from pydantic import BaseModel, Field

from app.agent.runtime import agent_runtime_manager
from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.runtime.log import logger


class UpdatePersonaDefinitionInput(BaseModel):
    """更新人格定义工具的输入参数模型。"""

    persona_id: str = Field(
        ...,
        description=(
            "Target persona to update. For existing personas this can be persona_id, label, or alias. "
            "For new personas, provide the new lowercase persona_id."
        ),
    )
    label: Optional[str] = Field(
        None,
        description="Optional new label shown to users, such as 默认 or 说明型.",
    )
    description: Optional[str] = Field(
        None,
        description="Optional short description of the persona's intended style.",
    )
    aliases: Optional[list[str]] = Field(
        None,
        description="Optional full replacement list of aliases for this persona.",
    )
    instructions: Optional[str] = Field(
        None,
        description=(
            "Optional full replacement body for PERSONA.md, excluding YAML frontmatter. "
            "Use this when the persona definition should be rewritten completely."
        ),
    )
    append_instructions: Optional[list[str]] = Field(
        None,
        description=(
            "Optional extra persona rules to append to the existing PERSONA body. "
            "Use this for small adjustments such as '回答更短' or '复杂问题给两步解释'."
        ),
    )
    create_if_missing: bool = Field(
        False,
        description="Whether to create a new runtime persona if the target persona does not already exist.",
    )


class UpdatePersonaDefinitionTool(MoviePilotTool):
    name: str = "update_persona_definition"
    tags: list[str] = [
        ToolTag.Write,
        ToolTag.Persona,
        ToolTag.Admin,
    ]
    description: str = (
        "Create or update a runtime persona definition (人格定义) without manually editing PERSONA.md files. "
        "Use this when the user explicitly asks to modify how a persona is defined, such as changing tone rules, "
        "rewriting the persona body, adjusting aliases, or creating a new persona."
    )
    args_schema: Type[BaseModel] = UpdatePersonaDefinitionInput
    require_admin: bool = True

    def get_tool_message(self, **kwargs) -> str:
        persona_id = kwargs.get("persona_id") or "未知人格"
        action = "创建/更新人格定义"
        return f"{action}: {persona_id}"

    async def run(
        self,
        persona_id: str,
        label: Optional[str] = None,
        description: Optional[str] = None,
        aliases: Optional[list[str]] = None,
        instructions: Optional[str] = None,
        append_instructions: Optional[list[str]] = None,
        create_if_missing: bool = False,
        **kwargs,
    ) -> str:
        logger.info("执行工具: %s, 参数: persona_id=%s", self.name, persona_id)
        if not any(
            value is not None
            for value in (label, description, aliases, instructions, append_instructions)
        ):
            return json.dumps(
                {
                    "success": False,
                    "message": "未提供任何要更新的人格定义字段。",
                },
                ensure_ascii=False,
            )

        try:
            persona, created = agent_runtime_manager.update_persona_definition(
                persona_id,
                label=label,
                description=description,
                aliases=aliases,
                instructions=instructions,
                append_instructions=append_instructions,
                create_if_missing=create_if_missing,
            )
            runtime_config = agent_runtime_manager.load_runtime_config()
            payload = {
                "success": True,
                "created": created,
                "active_persona": runtime_config.active_persona,
                "persona": persona.to_dict(
                    is_active=persona.persona_id == runtime_config.active_persona
                ),
                "message": (
                    f"已创建人格 `{persona.persona_id}`"
                    if created
                    else f"已更新人格 `{persona.persona_id}` 的定义"
                ),
            }
            return json.dumps(payload, ensure_ascii=False, indent=2)
        except Exception as e:  # noqa: BLE001
            logger.error("更新人格定义失败: %s", e, exc_info=True)
            return json.dumps(
                {
                    "success": False,
                    "message": f"更新人格定义时发生错误: {str(e)}",
                },
                ensure_ascii=False,
            )
