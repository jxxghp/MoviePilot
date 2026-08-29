"""Agent 公共入口，仅按白名单延迟加载稳定契约。"""

from importlib import import_module
from typing import Any

_PUBLIC_SYMBOLS = {
    "AgentChain": ("app.chain.agent", "AgentChain"),
    "AgentManager": ("app.agent.manager", "AgentManager"),
    "HEARTBEAT_SESSION_PREFIX": ("app.agent.orchestrator", "HEARTBEAT_SESSION_PREFIX"),
    "MoviePilotAgent": ("app.agent.orchestrator", "MoviePilotAgent"),
    "ReplyMode": ("app.agent.contracts", "ReplyMode"),
    "UNSUPPORTED_IMAGE_INPUT_MESSAGE": ("app.agent.orchestrator", "UNSUPPORTED_IMAGE_INPUT_MESSAGE"),
    "agent_manager": ("app.agent.manager", "agent_manager"),
}


def __getattr__(name: str) -> Any:
    """按白名单延迟解析稳定 Agent 契约。"""
    target = _PUBLIC_SYMBOLS.get(name)
    if target is None:
        raise AttributeError(f"module 'app.agent' has no attribute {name!r}")
    module_name, symbol_name = target
    value = getattr(import_module(module_name), symbol_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """返回包内名称与白名单公开契约。"""
    return sorted(set(globals()) | set(_PUBLIC_SYMBOLS))
