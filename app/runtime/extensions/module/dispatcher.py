"""宿主模块与插件模块的统一调用算法。"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterator, Mapping
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

    def providers_for(self, method: str) -> Any:
        """返回实现指定方法且按优先级排序的运行中宿主模块。"""


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

    def broadcast(self, method: str, *args: Any, **kwargs: Any) -> None:
        """把方法通知给全部提供者，不收集结果也不短路。

        :param method: 模块方法名称
        :param args: 透传给提供者的位置参数
        :param kwargs: 透传给提供者的命名参数
        :return: 无返回值
        """
        for plugin_id, plugin_name, func in self._plugin_providers(method):
            self._invoke_plugin(method, plugin_id, plugin_name, func, *args, **kwargs)
        for module in self._broadcast_modules(method):
            self._invoke_module(method, module, *args, **kwargs)

    async def async_broadcast(self, method: str, *args: Any, **kwargs: Any) -> None:
        """以广播语义执行同步或异步方法，同步函数移出事件循环。

        :param method: 模块方法名称
        :param args: 透传给提供者的位置参数
        :param kwargs: 透传给提供者的命名参数
        :return: 无返回值
        """
        for plugin_id, plugin_name, func in self._plugin_providers(method):
            await self._async_invoke_plugin(
                method,
                plugin_id,
                plugin_name,
                func,
                *args,
                **kwargs,
            )
        for module in self._broadcast_modules(method):
            await self._async_invoke_module(method, module, *args, **kwargs)

    def multicast(self, method: str, *args: Any, **kwargs: Any) -> list:
        """在能力族内收集全部提供者的非空答案。

        :param method: 模块方法名称
        :param args: 透传给提供者的位置参数
        :param kwargs: 透传给提供者的命名参数
        :return: 按插件优先、宿主优先级排序的非空结果列表
        """
        results: list[Any] = []
        for plugin_id, plugin_name, func in self._plugin_providers(method):
            result = self._invoke_plugin(
                method,
                plugin_id,
                plugin_name,
                func,
                *args,
                **kwargs,
            )
            if not self.is_valid_empty(result):
                results.append(result)
        for module in self._module_catalog.providers_for(method):
            result = self._invoke_module(method, module, *args, **kwargs)
            if not self.is_valid_empty(result):
                results.append(result)
        return results

    async def async_multicast(self, method: str, *args: Any, **kwargs: Any) -> list:
        """以多播语义收集同步或异步提供者的全部非空答案。

        :param method: 模块方法名称
        :param args: 透传给提供者的位置参数
        :param kwargs: 透传给提供者的命名参数
        :return: 按插件优先、宿主优先级排序的非空结果列表
        """
        results: list[Any] = []
        for plugin_id, plugin_name, func in self._plugin_providers(method):
            result = await self._async_invoke_plugin(
                method,
                plugin_id,
                plugin_name,
                func,
                *args,
                **kwargs,
            )
            if not self.is_valid_empty(result):
                results.append(result)
        for module in self._module_catalog.providers_for(method):
            result = await self._async_invoke_module(method, module, *args, **kwargs)
            if not self.is_valid_empty(result):
                results.append(result)
        return results

    def unicast(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """在能力族内仲裁出单一答案，首个非空结果即为最终答案。

        :param method: 模块方法名称
        :param args: 透传给提供者的位置参数
        :param kwargs: 透传给提供者的命名参数
        :return: 首个非空结果；无人认领时返回 ``None``
        """
        for plugin_id, plugin_name, func in self._plugin_providers(method):
            result = self._invoke_plugin(
                method,
                plugin_id,
                plugin_name,
                func,
                *args,
                **kwargs,
            )
            if not self.is_valid_empty(result):
                return result
        for module in self._module_catalog.providers_for(method):
            result = self._invoke_module(method, module, *args, **kwargs)
            if not self.is_valid_empty(result):
                return result
        return None

    async def async_unicast(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """以单播语义仲裁同步或异步提供者的首个非空答案。

        :param method: 模块方法名称
        :param args: 透传给提供者的位置参数
        :param kwargs: 透传给提供者的命名参数
        :return: 首个非空结果；无人认领时返回 ``None``
        """
        for plugin_id, plugin_name, func in self._plugin_providers(method):
            result = await self._async_invoke_plugin(
                method,
                plugin_id,
                plugin_name,
                func,
                *args,
                **kwargs,
            )
            if not self.is_valid_empty(result):
                return result
        for module in self._module_catalog.providers_for(method):
            result = await self._async_invoke_module(method, module, *args, **kwargs)
            if not self.is_valid_empty(result):
                return result
        return None

    def _plugin_providers(
        self,
        method: str,
    ) -> Iterator[tuple[str, str, Callable[..., Any]]]:
        """遍历插件注入的同名方法。

        :param method: 模块方法名称
        :return: `(插件 ID, 插件名称, 插件方法)` 迭代器
        """
        plugin_modules = self._plugin_catalog.get_plugin_modules()
        for (plugin_id, plugin_name), module_dict in plugin_modules.items():
            func = module_dict.get(method)
            if func:
                yield plugin_id, plugin_name, func

    def _broadcast_modules(self, method: str) -> list:
        """线性扫描全部运行模块，得到广播的宿主提供者。

        :param method: 模块方法名称
        :return: 按 `get_priority()` 升序排列的宿主模块列表
        """
        return sorted(
            self._module_catalog.get_running_modules(method),
            key=lambda module: module.get_priority(),
        )

    def _invoke_plugin(
        self,
        method: str,
        plugin_id: str,
        plugin_name: str,
        func: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """执行单个插件提供者，异常按插件策略上报后视为未认领。

        :param method: 模块方法名称
        :param plugin_id: 插件 ID
        :param plugin_name: 插件名称
        :param func: 插件注入的方法
        :param args: 透传给插件方法的位置参数
        :param kwargs: 透传给插件方法的命名参数
        :return: 插件返回值；限流或出错时返回 ``None``
        """
        try:
            logger.info("请求插件 %s 执行：%s ...", plugin_name, method)
            return func(*args, **kwargs)
        except RateLimitExceededException as err:
            self._rate_limit_handler(err, "插件", plugin_id, method, **kwargs)
        except Exception as err:
            self._plugin_error_handler(err, plugin_id, plugin_name, method, **kwargs)
        return None

    async def _async_invoke_plugin(
        self,
        method: str,
        plugin_id: str,
        plugin_name: str,
        func: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """执行单个插件提供者，协程直接等待，同步函数移入线程池。

        :param method: 模块方法名称
        :param plugin_id: 插件 ID
        :param plugin_name: 插件名称
        :param func: 插件注入的方法
        :param args: 透传给插件方法的位置参数
        :param kwargs: 透传给插件方法的命名参数
        :return: 插件返回值；限流或出错时返回 ``None``
        """
        try:
            logger.info("请求插件 %s 执行：%s ...", plugin_name, method)
            return await self._async_call(func, *args, **kwargs)
        except RateLimitExceededException as err:
            self._rate_limit_handler(err, "插件", plugin_id, method, **kwargs)
        except Exception as err:
            self._plugin_error_handler(err, plugin_id, plugin_name, method, **kwargs)
        return None

    def _invoke_module(
        self,
        method: str,
        module: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """执行单个宿主提供者，异常按系统策略上报后视为未认领。

        :param method: 模块方法名称
        :param module: 运行中的宿主模块实例
        :param args: 透传给模块方法的位置参数
        :param kwargs: 透传给模块方法的命名参数
        :return: 模块返回值；限流或出错时返回 ``None``
        """
        module_id = module.__class__.__name__
        module_name = self._module_name(module, module_id)
        try:
            return getattr(module, method)(*args, **kwargs)
        except RateLimitExceededException as err:
            self._rate_limit_handler(err, "模块", module_id, method, **kwargs)
        except Exception as err:
            self._system_error_handler(err, module_id, module_name, method, **kwargs)
        return None

    async def _async_invoke_module(
        self,
        method: str,
        module: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """执行单个宿主提供者，协程直接等待，同步函数移入线程池。

        :param method: 模块方法名称
        :param module: 运行中的宿主模块实例
        :param args: 透传给模块方法的位置参数
        :param kwargs: 透传给模块方法的命名参数
        :return: 模块返回值；限流或出错时返回 ``None``
        """
        module_id = module.__class__.__name__
        module_name = self._module_name(module, module_id)
        try:
            return await self._async_call(
                getattr(module, method),
                *args,
                **kwargs,
            )
        except RateLimitExceededException as err:
            self._rate_limit_handler(err, "模块", module_id, method, **kwargs)
        except Exception as err:
            self._system_error_handler(err, module_id, module_name, method, **kwargs)
        return None

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
