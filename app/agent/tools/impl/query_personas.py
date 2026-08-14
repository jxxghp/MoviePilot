"""查询可用人格工具。"""

import json
from typing import Optional, Type

from pydantic import BaseModel, Field

from app.agent.runtime import agent_runtime_manager
from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.runtime.log import logger


class QueryPersonasInput(BaseModel):
    """查询人格工具的输入参数模型。"""

    query: Optional[str] = Field(
        None,
        description=(
            "Optional search keyword for persona_id, label, description, or aliases. "
            "Use this when the user asks for a certain speaking style but the exact persona name is unknown."
        ),
    )


class QueryPersonasTool(MoviePilotTool):
    name: str = "query_personas"
    tags: list[str] = [
        ToolTag.Read,
        ToolTag.Persona,
    ]
    description: str = (
        "List all available personas (人格) and show which one is currently active. "
        "Use this before switching persona when the user asks for a different speaking style but does not name "
        "an exact persona_id. The result includes persona_id, label, description, aliases, and whether it is active."
    )
    args_schema: Type[BaseModel] = QueryPersonasInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        query = kwargs.get("query")
        if query:
            return f"查询人格列表: {query}"
        return "查询人格列表"

    async def run(self, query: Optional[str] = None, **kwargs) -> str:
        logger.info("执行工具: %s, 参数: query=%s", self.name, query)
        try:
            runtime_config = agent_runtime_manager.load_runtime_config()
            personas = runtime_config.list_personas()

            if query:
                normalized = query.strip().casefold()
                personas = [
                    persona
                    for persona in personas
                    if normalized in persona["persona_id"].casefold()
                    or normalized in persona["label"].casefold()
                    or normalized in persona["description"].casefold()
                    or any(normalized in alias.casefold() for alias in persona["aliases"])
                ]

            payload = {
                "active_persona": runtime_config.active_persona,
                "count": len(personas),
                "personas": personas,
            }
            return json.dumps(payload, ensure_ascii=False, indent=2)
        except Exception as e:  # noqa: BLE001
            logger.error("查询人格列表失败: %s", e, exc_info=True)
            return json.dumps(
                {
                    "success": False,
                    "message": f"查询人格列表时发生错误: {str(e)}",
                },
                ensure_ascii=False,
            )
