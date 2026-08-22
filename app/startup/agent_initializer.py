from typing import Any

from app.agent.runtime_loader import (
    activate_agent_service,
    begin_agent_shutdown,
    close_materialized_terminal_sessions,
    get_agent_manager as get_runtime_agent_manager,
    get_running_agent_manager as get_runtime_running_agent_manager,
    is_tool_factory_materialized,
    reconcile_agent_service,
)
from app.agent.llm.gateway import register_llm_provider_runtime
from app.application.agent import register_agent_service_providers
from app.application.messaging.skill import register_skill_catalog_provider
from app.runtime.config import settings
from app.runtime.events import Event, eventmanager
from app.runtime.log import logger
from app.schemas.types import EventType


def _get_skill_catalog() -> Any:
    """按需返回 Agent 技能目录实现，供消息应用层消费端口。"""
    from app.agent.skills.registry import SkillHelper

    return SkillHelper()


def _get_llm_provider_runtime() -> Any:
    """按需返回 LLM provider 运行时，实现只在真实调用边界加载。"""
    from app.agent.llm.provider import LLMProviderManager

    return LLMProviderManager()


# 嵌入式启动器可显式注入 manager；常规进程使用 Capability Runtime。
agent_manager: Any = None


def _event_changed_keys(event: Event | None) -> set[str]:
    """兼容对象和 dict 两种配置事件载荷。"""
    if event is None:
        return set()
    event_data = event.event_data
    if isinstance(event_data, dict):
        keys = event_data.get("key", set())
    else:
        keys = getattr(event_data, "key", set())
    if isinstance(keys, str):
        return {keys}
    return {str(key) for key in (keys or set())}


def _get_agent_manager() -> Any:
    """兼容显式注入对象，否则按需解析 canonical manager。"""
    return agent_manager if agent_manager is not None else get_runtime_agent_manager()


def _get_running_agent_manager() -> Any | None:
    """只返回已运行实例，状态探测不得触发 Agent 物化。"""
    if agent_initializer._compat_injected:
        return agent_initializer._manager
    return get_runtime_running_agent_manager()


def _get_prompt_manager() -> Any:
    """首个提示词调用才导入模板管理器。"""
    from app.agent.prompt import prompt_manager

    return prompt_manager


def _get_capability_manager() -> Any:
    """首个多模态调用才导入 Agent 能力管理器。"""
    from app.agent.llm.capability import AgentCapabilityManager

    return AgentCapabilityManager


def _get_llm_helper() -> Any:
    """首个模型能力查询才导入 LLM helper。"""
    from app.agent.llm.helper import LLMHelper

    return LLMHelper


def _get_manual_redo_prompt_builder() -> Any:
    """首个整理接管请求才导入对应提示词构建器。"""
    from app.agent.prompt.transfer_redo import build_manual_redo_prompt

    return build_manual_redo_prompt


async def _handle_agent_config_changed(event: Event) -> None:
    """把配置事件交给当前全局 initializer，避免监听器持有过期实例。"""
    await agent_initializer.handle_config_changed(event)


class AgentInitializer:
    """
    AI智能体初始化器
    """

    def __init__(self):
        self._initialized = False
        self._manager: Any = None
        self._compat_injected = False
        self._shutdown_complete = False
        eventmanager.add_event_listener(
            EventType.ConfigChanged,
            _handle_agent_config_changed,
        )

    async def initialize(self) -> bool:
        """
        初始化AI智能体管理器
        """
        try:
            self._shutdown_complete = False
            if agent_manager is not None:
                if not settings.AI_AGENT_ENABLE:
                    logger.info("AI智能体功能未启用")
                    return True
                self._manager = agent_manager
                self._compat_injected = True
                await agent_manager.initialize()
            else:
                self._manager = await activate_agent_service()
                self._compat_injected = False
                if self._manager is None:
                    logger.info("AI智能体功能未启用")
                    return True
            self._initialized = True
            logger.info("AI智能体管理器初始化成功")
            return True

        except Exception as e:
            logger.error(f"AI智能体管理器初始化失败: {e}")
            return False

    async def handle_config_changed(self, event: Event) -> None:
        """仅在 manifest watch 命中时协调 service，关闭态保持 fail closed。"""
        changed_keys = _event_changed_keys(event)
        if not changed_keys or self._compat_injected or self._shutdown_complete:
            return
        try:
            self._manager = await reconcile_agent_service(
                reason="agent_service_config_changed",
                changed_keys=changed_keys,
                retry=True,
            )
            self._initialized = self._manager is not None
        except Exception as error:
            self._manager = None
            self._initialized = False
            logger.debug(f"配置变更协调AI智能体失败: {error}")

    async def cleanup(self) -> None:
        """清理 initializer 引用；显式注入对象同时在此关闭。"""
        try:
            manager = self._manager
            compat_injected = self._compat_injected
            if manager is None:
                return
            try:
                if compat_injected:
                    await manager.close()
                logger.info("AI智能体管理器已关闭")
            finally:
                self._initialized = False
                self._manager = None
                self._compat_injected = False

        except Exception as e:
            logger.debug(f"关闭AI智能体管理器时发生错误: {e}")


# 全局AI智能体初始化器实例
agent_initializer = AgentInitializer()

# application 门面仅保存 provider；下列注册不会导入 Agent 实现。
register_agent_service_providers(
    agent_manager_provider=_get_agent_manager,
    running_agent_manager_provider=_get_running_agent_manager,
    prompt_manager_provider=_get_prompt_manager,
    capability_manager_provider=_get_capability_manager,
    llm_helper_provider=_get_llm_helper,
    manual_redo_prompt_builder_provider=_get_manual_redo_prompt_builder,
)
register_skill_catalog_provider(_get_skill_catalog)
register_llm_provider_runtime(_get_llm_provider_runtime)


async def init_agent() -> bool:
    """
    在应用事件循环中初始化AI智能体。
    """
    try:
        return await agent_initializer.initialize()

    except Exception as e:
        logger.error(f"初始化AI智能体时发生错误: {e}")
        return False


async def stop_agent():
    """
    停止AI智能体（异步版本，用于在应用关闭时调用）
    """
    try:
        if not agent_initializer._shutdown_complete:
            if agent_initializer._compat_injected:
                await agent_initializer.cleanup()
            else:
                await begin_agent_shutdown()
                await agent_initializer.cleanup()
            agent_initializer._shutdown_complete = True
        if is_tool_factory_materialized():
            from app.agent.tools.base import shutdown_blocking_executors

            shutdown_blocking_executors(wait=False, cancel_futures=True)
    except Exception as e:
        logger.error(f"停止AI智能体时发生错误: {e}")
    finally:
        await close_materialized_terminal_sessions()
