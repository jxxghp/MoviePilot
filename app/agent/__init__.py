"""Agent 公共入口，按需加载完整编排实现。"""

from importlib import import_module
from typing import Any


def __getattr__(name: str) -> Any:
    """按需从 Agent 编排模块解析历史包级公开对象。"""
    orchestrator = import_module("app.agent.orchestrator")
    try:
        return getattr(orchestrator, name)
    except AttributeError as err:
        raise AttributeError(f"module 'app.agent' has no attribute {name!r}") from err


def __dir__() -> list[str]:
    """返回 Agent 包和编排模块共同提供的可发现名称。"""
    orchestrator = import_module("app.agent.orchestrator")
    return sorted(set(globals()) | set(dir(orchestrator)))
