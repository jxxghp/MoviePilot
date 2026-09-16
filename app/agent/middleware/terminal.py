"""子代理终端只读共享输入与单次调用作用域。"""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.agent.terminal.ownership import (
    TerminalScope,
    bind_terminal_scope,
    close_terminal_scope,
    current_terminal_scope,
    require_terminal_scope,
)


def _default_actions() -> list[Literal["read", "wait"]]:
    """每条授权独立创建动作列表，默认仅可读取或等待父终端。"""
    return ["read", "wait"]


class SubAgentTerminalGrant(BaseModel):  # type: ignore[misc]
    """父任务明确授予子代理的单个终端读取能力，不扩大只读策略。"""

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(..., min_length=1, description="Terminal session owned by the parent task.")
    actions: list[Literal["read", "wait"]] = Field(
        default_factory=_default_actions,
        min_length=1,
        description="Explicit read-only actions to share; writes and process control cannot be delegated.",
    )


def _normalize_grants(terminal_sessions: list[SubAgentTerminalGrant]) -> dict[str, frozenset[str]]:
    """合并同一父句柄的只读动作，直调入口也执行与工具 schema 相同的校验。"""
    grants: dict[str, frozenset[str]] = {}
    for value in terminal_sessions:
        grant = SubAgentTerminalGrant.model_validate(value)
        grants[grant.session_id] = grants.get(grant.session_id, frozenset()) | frozenset(grant.actions)
    return grants


@asynccontextmanager
async def subagent_terminal_scope(
    *,
    task_id: str,
    user_id: str,
    terminal_sessions: Optional[list[SubAgentTerminalGrant]] = None,
) -> AsyncIterator[str]:
    """每次子图调用独立绑定能力；授权失败不调用模型，结束仅撤销子任务能力。"""
    grants = _normalize_grants(terminal_sessions or [])
    parent = current_terminal_scope()
    if parent is not None or grants:
        parent = require_terminal_scope()
    child = TerminalScope(
        user_id=parent.user_id if parent is not None else user_id,
        task_id=task_id,
        kind="subagent",
    )
    try:
        if grants:
            # 无终端的独立子任务无需加载或创建进程级终端管理器。
            from app.agent.terminal.manager import get_terminal_session_manager

            get_terminal_session_manager().share(require_terminal_scope(), child, grants)
        context = ""
        if grants:
            context = (
                "\n\n<terminal_sessions>\n"
                "The host explicitly granted these parent terminals for the listed read-only actions. "
                "This does not authorize writes, EOF, interrupts, or termination.\n"
                + json.dumps([
                    {"session_id": session_id, "actions": sorted(actions)}
                    for session_id, actions in grants.items()
                ], ensure_ascii=False)
                + "\n</terminal_sessions>"
            )
        with bind_terminal_scope(child):
            yield context
    finally:
        await close_terminal_scope(child)
