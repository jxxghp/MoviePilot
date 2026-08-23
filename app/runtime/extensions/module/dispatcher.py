"""宿主模块与插件模块的统一调用算法。"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from typing import Any, Protocol, cast

from app.foundation.reflection import ObjectUtils
from app.runtime.execution import run_in_threadpool
from app.runtime.log import logger
from app.runtime.observability import observe_duration, record_metric
from app.runtime.extensions.module.contracts import (
    diagnose_module_callable,
    get_module_method_contract,
    is_explicit_module_method,
)
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
        with observe_duration(
            "module.provider.duration", method=method, provider_type="plugin"
        ):
            result = self.execute_plugin_modules(method, None, *args, **kwargs)
        if not self.is_valid_empty(result) and not isinstance(result, list):
            return result
        with observe_duration(
            "module.provider.duration", method=method, provider_type="system"
        ):
            return self.execute_system_modules(method, result, *args, **kwargs)

    async def async_dispatch(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """以与同步路径相同的聚合规则执行同步或异步模块方法。"""
        contract = get_module_method_contract(method)
        logger.debug("异步模块方法契约：%s -> %s", method, contract.family)
        with observe_duration(
            "module.provider.duration", method=method, provider_type="plugin"
        ):
            result = await self.async_execute_plugin_modules(
                method,
                None,
                *args,
                **kwargs,
            )
        if not self.is_valid_empty(result) and not isinstance(result, list):
            return result
        with observe_duration(
            "module.provider.duration", method=method, provider_type="system"
        ):
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
            try:
                # 防御坏插件把方法表声明成非映射类型，避免击穿整个模块调度
                if not isinstance(module_dict, Mapping):
                    raise TypeError(
                        f"插件 {plugin_id} 的模块声明必须是映射，实际是 {type(module_dict).__name__}"
                    )
                func = module_dict.get(method)
                if not func:
                    continue
                self._record_legacy_hit(
                    method,
                    caller_type="plugin",
                    abi_source="third_party_plugin",
                )
                self._diagnose_callable(method, func, f"插件 {plugin_id}")
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
                self._record_timeout(method, "plugin", err)
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
            try:
                # 防御坏插件把方法表声明成非映射类型，避免击穿整个模块调度
                if not isinstance(module_dict, Mapping):
                    raise TypeError(
                        f"插件 {plugin_id} 的模块声明必须是映射，实际是 {type(module_dict).__name__}"
                    )
                func = module_dict.get(method)
                if not func:
                    continue
                self._record_legacy_hit(
                    method,
                    caller_type="plugin",
                    abi_source="third_party_plugin",
                )
                self._diagnose_callable(method, func, f"插件 {plugin_id}")
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
                self._record_timeout(method, "plugin", err)
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
                self._record_legacy_hit(
                    method,
                    caller_type="system",
                    abi_source="host_module",
                )
                self._diagnose_callable(method, func, f"宿主模块 {module_id}")
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
                self._record_timeout(method, "system", err)
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
                self._record_legacy_hit(
                    method,
                    caller_type="system",
                    abi_source="host_module",
                )
                self._diagnose_callable(method, func, f"宿主模块 {module_id}")
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
                self._record_timeout(method, "system", err)
                self._system_error_handler(
                    err,
                    module_id,
                    module_name,
                    method,
                    **kwargs,
                )
        return result

    @staticmethod
    def _record_timeout(method: str, provider_type: str, error: Exception) -> None:
        """仅把真实超时归入低基数模块超时指标。"""
        if isinstance(error, TimeoutError):
            record_metric(
                "module.provider.timeout",
                method=method,
                provider_type=provider_type,
            )

    @staticmethod
    def _record_legacy_hit(
        method: str,
        *,
        caller_type: str,
        abi_source: str,
    ) -> None:
        """记录未知动态方法的兼容命中，便于按真实调用逐项迁移。"""
        if not is_explicit_module_method(method):
            record_metric(
                "module.contract.legacy_hit",
                method=method,
                caller_type=caller_type,
                abi_source=abi_source,
            )

    @staticmethod
    def _diagnose_callable(
        method: str,
        callback: Callable[..., Any],
        owner: str,
    ) -> None:
        """记录 Contract V2 签名偏差，兼容阶段不阻断旧插件执行。"""
        problems = diagnose_module_callable(method, callback)
        if problems:
            logger.warning(
                "%s 的模块方法 %s 与契约不一致：%s；当前仅诊断",
                owner,
                method,
                ", ".join(problems),
            )

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
            return cast(str, module.get_name())
        except Exception as err:
            logger.debug("获取模块名称出错：%s", str(err))
            return fallback
