"""Agent 内部使用的 LLM 适配层，公开对象按需解析。"""

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.agent.llm.capability import (
        AgentCapabilityManager,
        AgentCapabilityProvider,
        AudioCapabilityProvider,
        MiMoAudioProvider,
        OpenAIAudioProvider,
        OpenAIChatAudioProvider,
    )
    from app.agent.llm.helper import LLMHelper, LLMTestError, LLMTestTimeout
    from app.agent.llm.provider import (
        LLMProviderAuthError,
        LLMProviderError,
        LLMProviderManager,
        render_auth_result_html,
    )


_EXPORT_MODULES = {
    "LLMHelper": "app.agent.llm.helper",
    "LLMTestError": "app.agent.llm.helper",
    "LLMTestTimeout": "app.agent.llm.helper",
    "AgentCapabilityManager": "app.agent.llm.capability",
    "AgentCapabilityProvider": "app.agent.llm.capability",
    "AudioCapabilityProvider": "app.agent.llm.capability",
    "MiMoAudioProvider": "app.agent.llm.capability",
    "OpenAIChatAudioProvider": "app.agent.llm.capability",
    "OpenAIAudioProvider": "app.agent.llm.capability",
    "LLMProviderAuthError": "app.agent.llm.provider",
    "LLMProviderError": "app.agent.llm.provider",
    "LLMProviderManager": "app.agent.llm.provider",
    "render_auth_result_html": "app.agent.llm.provider",
}


def __getattr__(name: str) -> Any:
    """首次访问公开对象时只加载其所属适配模块。"""
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module 'app.agent.llm' has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """让延迟公开对象继续支持交互式发现。"""
    return sorted(set(globals()) | set(_EXPORT_MODULES))

__all__ = [
    "LLMHelper",
    "AgentCapabilityManager",
    "AgentCapabilityProvider",
    "AudioCapabilityProvider",
    "LLMProviderAuthError",
    "LLMProviderError",
    "LLMProviderManager",
    "LLMTestError",
    "LLMTestTimeout",
    "MiMoAudioProvider",
    "OpenAIChatAudioProvider",
    "OpenAIAudioProvider",
    "render_auth_result_html",
]
