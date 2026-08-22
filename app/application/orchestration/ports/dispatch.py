"""能力端口客户端依赖的模块调度契约与独立调度器。"""

from __future__ import annotations

import traceback
from typing import Any, List, Optional, Protocol

from app.application.orchestration.context import ChainRuntimeContext, get_chain_runtime_context
from app.runtime.extensions.projection.dispatcher import ModuleInvocationDispatcher
from app.runtime.log import logger
from app.schemas.exception import RateLimitExceededException
from app.schemas.types import EventType


class CapabilityDispatch(Protocol):
    """声明能力端口客户端消费的最小模块调度能力。"""

    def run_module(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """运行包含该方法的所有模块，然后返回结果。"""

    async def async_run_module(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """异步运行包含该方法的所有模块，然后返回结果。"""

    def broadcast(self, method: str, *args: Any, **kwargs: Any) -> None:
        """把方法通知给全部实现该方法的插件与模块，不收集任何结果。"""

    async def async_broadcast(self, method: str, *args: Any, **kwargs: Any) -> None:
        """异步把方法通知给全部实现该方法的插件与模块，不收集任何结果。"""

    def multicast(self, method: str, *args: Any, **kwargs: Any) -> List[Any]:
        """在实现该方法的能力族内收集全部非空答案。"""

    async def async_multicast(self, method: str, *args: Any, **kwargs: Any) -> List[Any]:
        """异步在实现该方法的能力族内收集全部非空答案。"""

    def unicast(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """在实现该方法的能力族内仲裁单一答案。"""

    async def async_unicast(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """异步在实现该方法的能力族内仲裁单一答案。"""

    def pipeline(self, method: str, initial: Any, *args: Any, **kwargs: Any) -> Any:
        """在实现该方法的能力族内按提供者顺序接力增强同一产出。"""

    async def async_pipeline(self, method: str, initial: Any, *args: Any, **kwargs: Any) -> Any:
        """异步在实现该方法的能力族内按提供者顺序接力增强同一产出。"""


class ModuleErrorReporter:
    """模块与插件执行异常的告警策略。"""

    def __init__(self, event_manager: Any, message_helper: Any) -> None:
        """
        :param event_manager: 事件管理器，用于广播系统错误事件
        :param message_helper: 系统消息助手，用于推送错误提示
        """
        self._event_manager = event_manager
        self._message_helper = message_helper

    def handle_plugin_error(
            self, err: Exception, plugin_id: str, plugin_name: str, method: str, **kwargs
    ) -> None:
        """
        处理插件模块执行错误
        """
        if kwargs.get("raise_exception"):
            raise err
        logger.error(
            f"运行插件 {plugin_id} 模块 {method} 出错：{str(err)}\n{traceback.format_exc()}"
        )
        self._message_helper.put(
            title=f"{plugin_name} 发生了错误", message=str(err), role="plugin"
        )
        self._event_manager.send_event(
            EventType.SystemError,
            {
                "type": "plugin",
                "plugin_id": plugin_id,
                "plugin_name": plugin_name,
                "plugin_method": method,
                "error": str(err),
                "traceback": traceback.format_exc(),
            },
        )

    def handle_system_error(
            self, err: Exception, module_id: str, module_name: str, method: str, **kwargs
    ) -> None:
        """
        处理系统模块执行错误
        """
        if kwargs.get("raise_exception"):
            raise err
        logger.error(
            f"运行模块 {module_id}.{method} 出错：{str(err)}\n{traceback.format_exc()}"
        )
        self._message_helper.put(
            title=f"{module_name}发生了错误", message=str(err), role="system"
        )
        self._event_manager.send_event(
            EventType.SystemError,
            {
                "type": "module",
                "module_id": module_id,
                "module_name": module_name,
                "module_method": method,
                "error": str(err),
                "traceback": traceback.format_exc(),
            },
        )

    @staticmethod
    def handle_rate_limit_error(
            err: RateLimitExceededException, source_type: str, source_id: str,
            method: str, **kwargs
    ) -> None:
        """
        处理本地限流跳过，避免预期的限流状态进入系统错误告警。
        """
        if kwargs.get("raise_exception"):
            raise err
        logger.info(f"{source_type} {source_id}.{method} 已限流，跳过执行：{str(err)}")


class ModuleCapabilityDispatch:
    """不依赖处理链的模块能力调度器，供按域组合能力端口的服务直接持有。"""

    def __init__(self, runtime_context: Optional[ChainRuntimeContext] = None) -> None:
        """
        :param runtime_context: 运行时上下文，未显式传入时使用默认 provider
        """
        context = runtime_context or get_chain_runtime_context()
        self._error_reporter = ModuleErrorReporter(
            event_manager=context.event_manager,
            message_helper=context.message_helper,
        )
        self._dispatcher = ModuleInvocationDispatcher(
            module_catalog=context.module_manager,
            plugin_catalog=context.plugin_manager,
            plugin_error_handler=self._error_reporter.handle_plugin_error,
            system_error_handler=self._error_reporter.handle_system_error,
            rate_limit_handler=self._error_reporter.handle_rate_limit_error,
        )

    def run_module(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """
        运行包含该方法的所有模块，然后返回结果
        当kwargs包含命名参数raise_exception时，如模块方法抛出异常且raise_exception为True，则同步抛出异常

        :param method: 模块方法名称
        """
        return self._dispatcher.dispatch(method, *args, **kwargs)

    async def async_run_module(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """
        异步运行包含该方法的所有模块，然后返回结果
        支持异步和同步方法的混合调用

        :param method: 模块方法名称
        """
        return await self._dispatcher.async_dispatch(method, *args, **kwargs)

    def broadcast(self, method: str, *args: Any, **kwargs: Any) -> None:
        """
        把方法通知给全部实现该方法的插件与模块，不收集任何结果

        :param method: 模块方法名称
        """
        self._dispatcher.broadcast(method, *args, **kwargs)

    async def async_broadcast(self, method: str, *args: Any, **kwargs: Any) -> None:
        """
        异步把方法通知给全部实现该方法的插件与模块，不收集任何结果

        :param method: 模块方法名称
        """
        await self._dispatcher.async_broadcast(method, *args, **kwargs)

    def multicast(self, method: str, *args: Any, **kwargs: Any) -> List[Any]:
        """
        在实现该方法的能力族内收集全部非空答案，返回 None 的提供者不计入结果

        :param method: 模块方法名称
        :return: 按插件优先、模块优先级排序的非空结果列表
        """
        return self._dispatcher.multicast(method, *args, **kwargs)

    async def async_multicast(self, method: str, *args: Any, **kwargs: Any) -> List[Any]:
        """
        异步在实现该方法的能力族内收集全部非空答案

        :param method: 模块方法名称
        :return: 按插件优先、模块优先级排序的非空结果列表
        """
        return await self._dispatcher.async_multicast(method, *args, **kwargs)

    def unicast(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """
        在实现该方法的能力族内仲裁单一答案，首个非空结果即为最终答案

        :param method: 模块方法名称
        :return: 首个非空结果；无人认领时返回 None
        """
        return self._dispatcher.unicast(method, *args, **kwargs)

    async def async_unicast(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """
        异步在实现该方法的能力族内仲裁单一答案

        :param method: 模块方法名称
        :return: 首个非空结果；无人认领时返回 None
        """
        return await self._dispatcher.async_unicast(method, *args, **kwargs)


class CapabilityPorts:
    """能力端口客户端基类，按业务域封装一组模块能力调用。"""

    def __init__(self, dispatch: CapabilityDispatch) -> None:
        """
        :param dispatch: 模块调度器，提供广播、多播、单播与全量分发原语
        """
        self._dispatch = dispatch
