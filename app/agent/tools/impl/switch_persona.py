"""切换当前激活人格工具。"""

import json
from typing import Type

from pydantic import BaseModel, Field

from app.agent.runtime import agent_runtime_manager
from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.runtime.log import logger


class SwitchPersonaInput(BaseModel):
    """切换人格工具的输入参数模型。"""

    persona_id: str = Field(
        ...,
        description=(
            "The target persona to activate. This can be the exact persona_id, label, or one of the persona aliases. "
            "If the exact persona is unclear, call query_personas first."
        ),
    )


class SwitchPersonaTool(MoviePilotTool):
    name: str = "switch_persona"
    tags: list[str] = [
        ToolTag.Write,
        ToolTag.Persona,
    ]
    description: str = (
        "Switch the active persona (人格) used by the agent runtime. "
        "This change is persistent for future turns. "
        "Use this when the user explicitly asks to change the speaking style, tone, or response persona. "
        "If the user asks for a vague style and you are not sure which persona matches best, call query_personas first."
    )
    args_schema: Type[BaseModel] = SwitchPersonaInput

    def get_tool_message(self, **kwargs) -> str:
        persona_id = kwargs.get("persona_id") or "未知人格"
        return f"切换人格: {persona_id}"

    async def run(self, persona_id: str, **kwargs) -> str:
        logger.info("执行工具: %s, 参数: persona_id=%s", self.name, persona_id)
        try:
            runtime_config = agent_runtime_manager.set_active_persona(persona_id)
            payload = {
                "success": True,
                "active_persona": runtime_config.active_persona,
                "persona": runtime_config.persona.to_dict(is_active=True),
                "message": f"已切换为人格 `{runtime_config.active_persona}`",
            }
            return json.dumps(payload, ensure_ascii=False, indent=2)
        except Exception as e:  # noqa: BLE001
            logger.error("切换人格失败: %s", e, exc_info=True)
            return json.dumps(
                {
                    "success": False,
                    "message": f"切换人格时发生错误: {str(e)}",
                },
                ensure_ascii=False,
            )
