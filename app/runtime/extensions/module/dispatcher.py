"""宿主模块与插件模块的统一调用算法。"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from fastapi.concurrency import run_in_threadpool

from app.foundation.reflection import ObjectUtils
from app.runtime.log import logger
from app.runtime.extensions.module.contracts import get_module_method_contract
from app.schemas.exception import RateLimitExceededException


class ModuleCatalog(Protocol):
    """声明模块调度器消费的最小模块目录能力。"""

    def get_running_modules(self, method: str) -> Any:
        """返回实现指定方法的运行中宿主模块。"""


class PluginModuleCatalog(Protocol):
    """声明模块调度器消费的最小插件模块目录能力。"""

    def get_plugin_modules(
        self,
    ) -> Mapping[tuple[str, str], Mapping[str, Callable[..., Any]]]:
        """返回插件标识到模块方法表的当前快照。"""


ModuleErrorHandler = Callable[..., None]
AsyncFunctionRunner = Callable[..., Any]


class ModuleInvocationDispatcher:
    """按既有聚合、短路和异常规则执行插件与宿主模块。"""

    def __init__(
        self,
        *,
        module_catalog: ModuleCatalog,
        plugin_catalog: PluginModuleCatalog,
        plugin_error_handler: ModuleErrorHandler,
        system_error_handler: ModuleErrorHandler,
        rate_limit_handler: ModuleErrorHandler,
        async_function_runner: AsyncFunctionRunner = run_in_threadpool,
    ) -> None:
        """保存模块目录和策略回调，不主动发现或创建任何运行时资源。"""
        self._module_catalog = module_catalog
        self._plugin_catalog = plugin_catalog
        self._plugin_error_handler = plugin_error_handler
        self._system_error_handler = system_error_handler
        self._rate_limit_handler = rate_limit_handler
        self._async_function_runner = async_function_runner

    @staticmethod
    def is_valid_empty(result: Any) -> bool:
        """保持旧协议中 ``None`` 与全 ``None`` 元组的空结果定义。"""
        if isinstance(result, tuple):
            return all(value is None for value in result)
        return result is None

    def dispatch(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """先执行插件模块，再按优先级执行宿主模块。"""
        contract = get_module_method_contract(method)
        logger.debug("模块方法契约：%s -> %s", method, contract.family)
        result = self.execute_plugin_modules(method, None, *args, **kwargs)
        if not self.is_valid_empty(result) and not isinstance(result, list):
            return result
        return self.execute_system_modules(method, result, *args, **kwargs)

    async def async_dispatch(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """以与同步路径相同的聚合规则执行同步或异步模块方法。"""
        contract = get_module_method_contract(method)
        logger.debug("异步模块方法契约：%s -> %s", method, contract.family)
        result = await self.async_execute_plugin_modules(
            method,
            None,
            *args,
            **kwargs,
        )
        if not self.is_valid_empty(result) and not isinstance(result, list):
            return result
        return await self.async_execute_system_modules(
            method,
            result,
            *args,
            **kwargs,
        )

    def execute_plugin_modules(
        self,
        method: str,
        result: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """同步执行插件方法，保留插件顺序、短路和列表合并语义。"""
        for plugin, module_dict in self._plugin_catalog.get_plugin_modules().items():
            plugin_id, plugin_name = plugin
            func = module_dict.get(method)
            if not func:
                continue
            try:
                logger.info("请求插件 %s 执行：%s ...", plugin_name, method)
                if self.is_valid_empty(result):
                    result = func(*args, **kwargs)
                elif isinstance(result, list):
                    temp = func(*args, **kwargs)
                    if isinstance(temp, list):
                        result.extend(temp)
                else:
                    break
            except RateLimitExceededException as err:
                self._rate_limit_handler(
                    err,
                    "插件",
                    plugin_id,
                    method,
                    **kwargs,
                )
            except Exception as err:
                self._plugin_error_handler(
                    err,
                    plugin_id,
                    plugin_name,
                    method,
                    **kwargs,
                )
        return result

    async def async_execute_plugin_modules(
        self,
        method: str,
        result: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """异步执行插件方法，并把同步函数移入线程池。"""
        for plugin, module_dict in self._plugin_catalog.get_plugin_modules().items():
            plugin_id, plugin_name = plugin
            func = module_dict.get(method)
            if not func:
                continue
            try:
                logger.info("请求插件 %s 执行：%s ...", plugin_name, method)
                if self.is_valid_empty(result):
                    result = await self._async_call(func, *args, **kwargs)
                elif isinstance(result, list):
                    temp = await self._async_call(func, *args, **kwargs)
                    if isinstance(temp, list):
                        result.extend(temp)
                else:
                    break
            except RateLimitExceededException as err:
                self._rate_limit_handler(
                    err,
                    "插件",
                    plugin_id,
                    method,
                    **kwargs,
                )
            except Exception as err:
                self._plugin_error_handler(
                    err,
                    plugin_id,
                    plugin_name,
                    method,
                    **kwargs,
                )
        return result

    def execute_system_modules(
        self,
        method: str,
        result: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """同步执行按优先级排序的宿主模块，并支持签名接力。"""
        logger.debug("请求系统模块执行：%s ...", method)
        modules = sorted(
            self._module_catalog.get_running_modules(method),
            key=lambda module: module.get_priority(),
        )
        for module in modules:
            module_id = module.__class__.__name__
            module_name = self._module_name(module, module_id)
            try:
                func = getattr(module, method)
                if self.is_valid_empty(result):
                    result = func(*args, **kwargs)
                elif ObjectUtils.check_signature(func, result):
                    result = func(result)
                elif isinstance(result, list):
                    temp = func(*args, **kwargs)
                    if isinstance(temp, list):
                        result.extend(temp)
                else:
                    break
            except RateLimitExceededException as err:
                self._rate_limit_handler(
                    err,
                    "模块",
                    module_id,
                    method,
                    **kwargs,
                )
            except Exception as err:
                self._system_error_handler(
                    err,
                    module_id,
                    module_name,
                    method,
                    **kwargs,
                )
        return result

    async def async_execute_system_modules(
        self,
        method: str,
        result: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """异步执行宿主模块，并保持同步路径的签名接力与聚合顺序。"""
        logger.debug("请求系统模块执行：%s ...", method)
        modules = sorted(
            self._module_catalog.get_running_modules(method),
            key=lambda module: module.get_priority(),
        )
        for module in modules:
            module_id = module.__class__.__name__
            module_name = self._module_name(module, module_id)
            try:
                func = getattr(module, method)
                if self.is_valid_empty(result):
                    result = await self._async_call(func, *args, **kwargs)
                elif ObjectUtils.check_signature(func, result):
                    result = await self._async_call(func, result)
                elif isinstance(result, list):
                    temp = await self._async_call(func, *args, **kwargs)
                    if isinstance(temp, list):
                        result.extend(temp)
                else:
                    break
            except RateLimitExceededException as err:
                self._rate_limit_handler(
                    err,
                    "模块",
                    module_id,
                    method,
                    **kwargs,
                )
            except Exception as err:
                self._system_error_handler(
                    err,
                    module_id,
                    module_name,
                    method,
                    **kwargs,
                )
        return result

    async def _async_call(
        self,
        func: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """调用协程函数，或通过注入的线程池执行器运行同步函数。"""
        if inspect.iscoroutinefunction(func):
            return await func(*args, **kwargs)
        return await self._async_function_runner(func, *args, **kwargs)

    @staticmethod
    def _module_name(module: Any, fallback: str) -> str:
        """读取模块展示名，失败时回退到稳定类名。"""
        try:
            return module.get_name()
        except Exception as err:
            logger.debug("获取模块名称出错：%s", str(err))
            return fallback
