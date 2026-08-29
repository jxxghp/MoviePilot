from typing import Any

from app.agent.llm.gateway import register_llm_provider_runtime
from app.agent.runtime_loader import (
    activate_agent_service,
    begin_agent_shutdown,
    close_materialized_terminal_sessions,
    is_tool_factory_materialized,
    reconcile_agent_service,
)
from app.agent.runtime_loader import (
    get_agent_manager as get_runtime_agent_manager,
)
from app.agent.runtime_loader import (
    get_running_agent_manager as get_runtime_running_agent_manager,
)
from app.application.agent import (
    AgentDataContext,
    register_agent_service_providers,
    reset_agent_service_providers,
)
from app.application.messaging.agent import (
    configure_web_agent_message_runtime,
    reset_web_agent_message_runtime,
)
from app.application.messaging.skill import (
    register_skill_catalog_provider,
    reset_skill_catalog_provider,
)
from app.runtime.events import Event, eventmanager
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from app.schemas.types import EventType

AGENT_BLOCKING_EXECUTOR_SHUTDOWN_TIMEOUT_SECONDS = 10.0


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
_agent_data_context: AgentDataContext | None = None
_injected_agent_manager: Any = None


def configure_agent_data_context(context: AgentDataContext) -> None:
    """登记组合根数据上下文，并丢弃未运行的旧 manager 缓存。"""
    global _agent_data_context, _injected_agent_manager
    if _agent_data_context is context:
        return
    manager = _injected_agent_manager
    if manager is not None and (
        getattr(manager, "_accepting_tasks", False) or bool(getattr(manager, "active_agents", {}))
    ):
        raise RuntimeError("AgentManager 已运行，不能替换数据上下文")
    _agent_data_context = context
    _injected_agent_manager = None


def _get_injected_agent_manager() -> Any:
    """按需构造并缓存显式注入数据上下文的 AgentManager。"""
    global _injected_agent_manager
    if _agent_data_context is None:
        raise RuntimeError("Agent 数据上下文尚未由启动组合根装配")
    if _injected_agent_manager is None:
        from app.agent.manager import AgentManager

        _injected_agent_manager = AgentManager(data=_agent_data_context)
    return _injected_agent_manager


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
    if agent_manager is not None:
        return agent_manager
    if _agent_data_context is not None:
        return _get_injected_agent_manager()
    return get_runtime_agent_manager()


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


def _get_web_agent_type() -> type:
    """首个 Web 对话请求才解析 WebAgent 运行时类型。"""
    from app.agent.web import _get_web_agent_type as get_web_agent_type

    return get_web_agent_type()


def _handle_web_agent_message(**kwargs: Any) -> object:
    """按请求构造消息链并执行传统 WebAgent 输入。"""
    from app.chain.message import MessageChain

    MessageChain().handle_message(**kwargs)
    return None


def _bind_web_agent_user_session(user_id: str, session_id: str) -> None:
    """把 Web 用户绑定到消息链的 Agent 会话。"""
    from app.chain.message import MessageChain

    MessageChain().bind_user_session(user_id, session_id)


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
        self._shutdown_started = False
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
            self._shutdown_started = False
            self._shutdown_complete = False
            if agent_manager is not None or _agent_data_context is not None:
                if not get_runtime_setting("AI_AGENT_ENABLE"):
                    logger.info("AI智能体功能未启用")
                    return True
                self._manager = agent_manager if agent_manager is not None else _get_injected_agent_manager()
                self._compat_injected = True
                await self._manager.initialize()
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
        if not changed_keys or self._compat_injected or self._shutdown_started or self._shutdown_complete:
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

    async def cleanup(self) -> bool:
        """清理 initializer 引用；未收敛的显式注入对象继续由本实例持有。"""
        try:
            manager = self._manager
            compat_injected = self._compat_injected
            if manager is None:
                return True
            if compat_injected and await manager.close() is False:
                logger.error("AI智能体管理器仍有会话 owner 未收敛")
                return False
            logger.info("AI智能体管理器已关闭")
            self._initialized = False
            self._manager = None
            self._compat_injected = False
            return True

        except Exception as e:
            logger.debug(f"关闭AI智能体管理器时发生错误: {e}")
            return False


# 全局AI智能体初始化器实例
agent_initializer = AgentInitializer()


def configure_agent_ports() -> None:
    """在 Agent 启动阶段原子登记全部 Application provider。"""
    reset_agent_ports()
    try:
        register_agent_service_providers(
            agent_manager_provider=_get_agent_manager,
            running_agent_manager_provider=_get_running_agent_manager,
            prompt_manager_provider=_get_prompt_manager,
            capability_manager_provider=_get_capability_manager,
            llm_helper_provider=_get_llm_helper,
            manual_redo_prompt_builder_provider=_get_manual_redo_prompt_builder,
            web_agent_type_provider=_get_web_agent_type,
        )
        register_skill_catalog_provider(_get_skill_catalog)
        register_llm_provider_runtime(_get_llm_provider_runtime)
        configure_web_agent_message_runtime(
            message_handler=_handle_web_agent_message,
            session_binder=_bind_web_agent_user_session,
        )
    except Exception:
        reset_agent_ports()
        raise


def reset_agent_ports() -> None:
    """清除 Agent、技能和 LLM provider，支持失败回滚与重复 lifespan。"""
    register_llm_provider_runtime(None)
    reset_skill_catalog_provider()
    reset_web_agent_message_runtime()
    reset_agent_service_providers()


async def init_agent() -> bool:
    """
    在应用事件循环中初始化AI智能体。
    """
    configure_agent_ports()
    try:
        initialized = await agent_initializer.initialize()
        if not initialized:
            reset_agent_ports()
        return initialized

    except Exception as e:
        reset_agent_ports()
        logger.error(f"初始化AI智能体时发生错误: {e}")
        return False


async def stop_agent() -> bool:
    """
    停止AI智能体，并在全部会话和工具资源释放后返回 True。
    """
    converged = True
    close_blocking_executors = None
    agent_initializer._shutdown_started = True
    try:
        if is_tool_factory_materialized():
            from app.agent.tools.base import (
                begin_blocking_executor_shutdown,
            )
            from app.agent.tools.base import (
                close_blocking_executors as close_executors,
            )

            # 必须在任何 manager await 之前封口，避免旧会话趁收尾窗口提交新同步调用。
            begin_blocking_executor_shutdown(cancel_futures=True)
            close_blocking_executors = close_executors
    except Exception as e:
        logger.error(f"封住AI智能体阻塞工具提交时发生错误: {e}")
        converged = False

    try:
        if not agent_initializer._shutdown_complete:
            if agent_initializer._compat_injected:
                service_converged = await agent_initializer.cleanup()
            else:
                service_converged = await begin_agent_shutdown()
                if service_converged is not False:
                    service_converged = await agent_initializer.cleanup()
            converged = converged and service_converged is not False
    except Exception as e:
        logger.error(f"停止AI智能体时发生错误: {e}")
        converged = False

    if close_blocking_executors is not None:
        try:
            blocking_converged = await close_blocking_executors(
                timeout_seconds=AGENT_BLOCKING_EXECUTOR_SHUTDOWN_TIMEOUT_SECONDS,
                cancel_futures=True,
            )
            converged = converged and blocking_converged
        except Exception as e:
            logger.error(f"关闭AI智能体阻塞工具线程池时发生错误: {e}")
            converged = False

    if converged:
        try:
            await close_materialized_terminal_sessions()
        except Exception as e:
            logger.error(f"关闭AI智能体终端会话时发生错误: {e}")
            converged = False
    if converged:
        reset_agent_ports()
    agent_initializer._shutdown_complete = converged
    return converged
