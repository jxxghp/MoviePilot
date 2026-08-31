"""Agent 人格统一管理工具。"""

import json
from typing import Literal, Optional, Type

from pydantic import BaseModel, Field, model_validator

from app.agent.runtime import agent_runtime_manager
from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.runtime.log import logger

PersonaAction = Literal["list", "switch", "update"]


class PersonaInput(BaseModel):  # type: ignore[misc]
    """查询、切换或更新 Agent 人格的统一输入参数。"""

    action: PersonaAction = Field(
        ...,
        description="Action to perform: list, switch, or update.",
    )
    query: Optional[str] = Field(
        None,
        description=("Optional list filter for persona_id, label, description, or aliases."),
    )
    persona_id: Optional[str] = Field(
        None,
        description=(
            "Target persona for switch or update. Existing labels and aliases are "
            "accepted; new personas require a lowercase persona_id."
        ),
    )
    label: Optional[str] = Field(None, description="Replacement display label.")
    description: Optional[str] = Field(
        None,
        description="Replacement description of the intended persona style.",
    )
    aliases: Optional[list[str]] = Field(
        None,
        description="Full replacement list of persona aliases.",
    )
    instructions: Optional[str] = Field(
        None,
        description="Full replacement PERSONA.md body without YAML frontmatter.",
    )
    append_instructions: Optional[list[str]] = Field(
        None,
        description="Additional persona rules appended to the existing body.",
    )
    create_if_missing: bool = Field(
        False,
        description="Create a new runtime persona when update cannot resolve one.",
    )

    @model_validator(mode="after")  # type: ignore[misc]
    def validate_action(self) -> "PersonaInput":
        """按动作校验目标人格和更新字段。"""
        if self.query is not None:
            self.query = self.query.strip() or None
        if self.persona_id is not None:
            self.persona_id = self.persona_id.strip()
        if self.action in {"switch", "update"} and not self.persona_id:
            raise ValueError(f"{self.action} 必须提供 persona_id")
        if self.action == "update" and not any(
            value is not None
            for value in (
                self.label,
                self.description,
                self.aliases,
                self.instructions,
                self.append_instructions,
            )
        ):
            raise ValueError("update 必须提供至少一个人格定义字段")
        return self


class PersonaTool(MoviePilotTool):
    """通过一个动作式接口查询、切换或管理 Agent 人格。"""

    name: str = "persona"
    tags: list[str] = [ToolTag.Read, ToolTag.Write, ToolTag.Persona]
    description: str = (
        "Manage agent personas with one structured action. Use list to discover personas "
        "and the active state, switch when the user explicitly requests a speaking style, "
        "and update only for an explicit administrator request to create or rewrite a "
        "persona definition."
    )
    args_schema: Type[BaseModel] = PersonaInput

    def get_tool_message(self, **kwargs: object) -> str:
        """生成统一人格操作的用户可见提示。"""
        action = str(kwargs.get("action") or "list")
        action_name = {
            "list": "查询人格",
            "switch": "切换人格",
            "update": "更新人格定义",
        }.get(action, action)
        target = kwargs.get("persona_id") or kwargs.get("query")
        return f"{action_name}: {target}" if target else action_name

    async def run(  # type: ignore[override]
        self,
        action: PersonaAction,
        query: Optional[str] = None,
        persona_id: Optional[str] = None,
        label: Optional[str] = None,
        description: Optional[str] = None,
        aliases: Optional[list[str]] = None,
        instructions: Optional[str] = None,
        append_instructions: Optional[list[str]] = None,
        create_if_missing: bool = False,
        **kwargs: object,
    ) -> str:
        """执行查询、切换或更新人格定义。"""
        payload = PersonaInput(
            action=action,
            query=query,
            persona_id=persona_id,
            label=label,
            description=description,
            aliases=aliases,
            instructions=instructions,
            append_instructions=append_instructions,
            create_if_missing=create_if_missing,
        )
        logger.info(
            "执行工具: %s, 参数: action=%s, persona_id=%s",
            self.name,
            payload.action,
            payload.persona_id,
        )
        try:
            if payload.action == "list":
                return self._list_personas(payload.query)
            if payload.action == "switch":
                return self._switch_persona(payload.persona_id)
            if not await self.is_admin_user():
                return json.dumps(
                    {
                        "success": False,
                        "message": "只有系统管理员才能更新人格定义。",
                    },
                    ensure_ascii=False,
                )
            return self._update_persona(payload)
        except Exception as error:  # noqa: BLE001
            logger.error(
                "人格操作失败: action=%s, error=%s",
                payload.action,
                error,
                exc_info=True,
            )
            return json.dumps(
                {
                    "success": False,
                    "message": f"人格操作失败: {str(error)}",
                },
                ensure_ascii=False,
            )

    @staticmethod
    def _list_personas(query: Optional[str]) -> str:
        """列出可用人格并按可选关键词过滤。"""
        runtime_config = agent_runtime_manager.load_runtime_config()
        personas = runtime_config.list_personas()
        if query:
            normalized = query.casefold()
            personas = [
                persona
                for persona in personas
                if normalized in persona["persona_id"].casefold()
                or normalized in persona["label"].casefold()
                or normalized in persona["description"].casefold()
                or any(normalized in alias.casefold() for alias in persona["aliases"])
            ]
        return json.dumps(
            {
                "active_persona": runtime_config.active_persona,
                "count": len(personas),
                "personas": personas,
            },
            ensure_ascii=False,
            indent=2,
        )

    @staticmethod
    def _switch_persona(persona_id: Optional[str]) -> str:
        """切换当前持久化激活人格。"""
        if persona_id is None:
            raise ValueError("switch 缺少 persona_id")
        runtime_config = agent_runtime_manager.set_active_persona(persona_id)
        return json.dumps(
            {
                "success": True,
                "active_persona": runtime_config.active_persona,
                "persona": runtime_config.persona.to_dict(is_active=True),
                "message": f"已切换为人格 `{runtime_config.active_persona}`",
            },
            ensure_ascii=False,
            indent=2,
        )

    @staticmethod
    def _update_persona(payload: PersonaInput) -> str:
        """创建或更新一项运行时人格定义。"""
        if payload.persona_id is None:
            raise ValueError("update 缺少 persona_id")
        persona, created = agent_runtime_manager.update_persona_definition(
            payload.persona_id,
            label=payload.label,
            description=payload.description,
            aliases=payload.aliases,
            instructions=payload.instructions,
            append_instructions=payload.append_instructions,
            create_if_missing=payload.create_if_missing,
        )
        runtime_config = agent_runtime_manager.load_runtime_config()
        return json.dumps(
            {
                "success": True,
                "created": created,
                "active_persona": runtime_config.active_persona,
                "persona": persona.to_dict(is_active=persona.persona_id == runtime_config.active_persona),
                "message": (
                    f"已创建人格 `{persona.persona_id}`" if created else f"已更新人格 `{persona.persona_id}` 的定义"
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
