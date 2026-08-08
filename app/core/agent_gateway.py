"""
Agent 运行时网关 seam(S4 / P2)。

供业务层 chain 在 **call-time** 反向调用 agent 层,从而把 `chain → app.agent`
的依赖从模块顶层 import 降级为函数内惰性 import,打断 chain↔agent 的 import-time
循环(agent 仍在顶层依赖 chain,是健康的编排方向,保持不变)。

默认行为 = 惰性导入并返回真实的 agent 单例/类(与直接 import 字节一致);
`set_*_provider` 为未来 Rust / 进程外 agent host 预留的注入点(测试亦可借此替换),
默认不在 composition root 注册——默认即生产行为。
"""
from typing import Any, Callable, Optional

_agent_manager_provider: Optional[Callable[[], Any]] = None
_prompt_manager_provider: Optional[Callable[[], Any]] = None
_agent_llm_provider: Optional[Callable[[], Any]] = None
_agent_capability_provider: Optional[Callable[[], Any]] = None
_manual_redo_prompt_builder_provider: Optional[Callable[[], Any]] = None


def set_manual_redo_prompt_builder_provider(provider: Optional[Callable[[], Any]]) -> None:
    """注入手动重整提示词构建函数(默认惰性取真实实现)。"""
    global _manual_redo_prompt_builder_provider
    _manual_redo_prompt_builder_provider = provider


def set_agent_manager_provider(provider: Optional[Callable[[], Any]]) -> None:
    global _agent_manager_provider
    _agent_manager_provider = provider


def set_prompt_manager_provider(provider: Optional[Callable[[], Any]]) -> None:
    global _prompt_manager_provider
    _prompt_manager_provider = provider


def set_agent_llm_provider(provider: Optional[Callable[[], Any]]) -> None:
    global _agent_llm_provider
    _agent_llm_provider = provider


def set_agent_capability_provider(provider: Optional[Callable[[], Any]]) -> None:
    global _agent_capability_provider
    _agent_capability_provider = provider


def get_agent_manager() -> Any:
    """返回 AgentManager 单例(会话/后台提示词调度)。"""
    if _agent_manager_provider is not None:
        return _agent_manager_provider()
    from app.agent import agent_manager
    return agent_manager


def get_prompt_manager() -> Any:
    """返回 prompt_manager 单例(系统/任务提示词渲染)。"""
    if _prompt_manager_provider is not None:
        return _prompt_manager_provider()
    from app.agent.prompt import prompt_manager
    return prompt_manager


def get_agent_llm() -> Any:
    """返回 LLMHelper 类(模型能力查询,如 supports_image_input)。"""
    if _agent_llm_provider is not None:
        return _agent_llm_provider()
    from app.agent.llm import LLMHelper
    return LLMHelper


def get_agent_capability() -> Any:
    """返回 AgentCapabilityManager 类(音频输入能力 / 转写)。"""
    if _agent_capability_provider is not None:
        return _agent_capability_provider()
    from app.agent.llm import AgentCapabilityManager
    return AgentCapabilityManager


def get_manual_redo_prompt_builder() -> Any:
    """返回手动重整提示词构建函数(按整理历史渲染重试提示词)。"""
    if _manual_redo_prompt_builder_provider is not None:
        return _manual_redo_prompt_builder_provider()
    from app.agent.prompt.transfer_redo import build_manual_redo_prompt
    return build_manual_redo_prompt
