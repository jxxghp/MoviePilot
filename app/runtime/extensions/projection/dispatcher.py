"""按扩展契约取用提供者的统一调用算法。"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from typing import Any, Protocol

from app.foundation.reflection import ObjectUtils
from app.runtime.execution import run_in_threadpool
from app.runtime.extensions.contract.extension import (
    ExtensionFaultScope,
    ExtensionProvider,
    ExtensionProviderSource,
)
from app.runtime.extensions.lifecycle.host_module_adapter import HostModuleProviderSource
from app.runtime.extensions.contract.module_method import get_module_method_contract
from app.runtime.extensions.projection.plugin import PluginProviderSource
from app.runtime.log import logger
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
    """按既有聚合、短路和异常规则执行各发行方式的扩展提供者。"""

    def __init__(
        self,
        *,
        module_catalog: ModuleCatalog,
        plugin_catalog: PluginModuleCatalog,
        plugin_error_handler: ModuleErrorHandler,
        system_error_handler: ModuleErrorHandler,
        rate_limit_handler: ModuleErrorHandler,
        async_function_runner: AsyncFunctionRunner = run_in_threadpool,
        extra_sources: Sequence[ExtensionProviderSource] = (),
    ) -> None:
        """保存扩展目录和策略回调，不主动发现或创建任何运行时资源。

        :param module_catalog: 宿主模块目录
        :param plugin_catalog: 插件模块目录
        :param plugin_error_handler: 市场扩展执行异常的上报回调
        :param system_error_handler: 内建扩展执行异常的上报回调
        :param rate_limit_handler: 本地限流跳过的上报回调
        :param async_function_runner: 把同步方法移出事件循环的执行器
        :param extra_sources: 追加的扩展目录，按给定顺序参与分发
        """
        self._sources: tuple[ExtensionProviderSource, ...] = (
            PluginProviderSource(plugin_catalog),
            HostModuleProviderSource(module_catalog),
            *extra_sources,
        )
        self._fault_handlers = {
            ExtensionFaultScope.PLUGIN: plugin_error_handler,
            ExtensionFaultScope.HOST: system_error_handler,
        }
        self._rate_limit_handler = rate_limit_handler
        self._async_function_runner = async_function_runner

    @staticmethod
    def is_valid_empty(result: Any) -> bool:
        """判断结果是否为空，``None`` 与全 ``None`` 元组都视为未认领。

        :param result: 提供者返回值
        :return: 结果为空时为 True
        """
        if isinstance(result, tuple):
            return all(value is None for value in result)
        return result is None

    def dispatch(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """按发行方式分阶段接力执行，先得到的标量答案终止后续阶段。"""
        self._log_contract(method)
        result: Any = None
        for source in self._sources:
            source.announce_phase(method)
            result = self._execute_chain(
                source.notify_providers(method),
                method,
                result,
                *args,
                **kwargs,
            )
            if self._is_settled(result):
                break
        return result

    async def async_dispatch(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """以与同步路径相同的聚合规则执行同步或异步扩展方法。"""
        self._log_contract(method)
        result: Any = None
        for source in self._sources:
            source.announce_phase(method)
            result = await self._async_execute_chain(
                source.notify_providers(method),
                method,
                result,
                *args,
                **kwargs,
            )
            if self._is_settled(result):
                break
        return result

    def broadcast(self, method: str, *args: Any, **kwargs: Any) -> None:
        """把方法通知给全部提供者，不收集结果也不短路。

        :param method: 模块方法名称
        :param args: 透传给提供者的位置参数
        :param kwargs: 透传给提供者的命名参数
        :return: 无返回值
        """
        for provider in self._notify_providers(method):
            self._invoke(provider, method, *args, **kwargs)

    async def async_broadcast(self, method: str, *args: Any, **kwargs: Any) -> None:
        """以广播语义执行同步或异步方法，同步函数移出事件循环。

        :param method: 模块方法名称
        :param args: 透传给提供者的位置参数
        :param kwargs: 透传给提供者的命名参数
        :return: 无返回值
        """
        for provider in self._notify_providers(method):
            await self._async_invoke(provider, method, *args, **kwargs)

    def multicast(self, method: str, *args: Any, **kwargs: Any) -> list[Any]:
        """在能力族内收集全部提供者的非空答案。

        :param method: 模块方法名称
        :param args: 透传给提供者的位置参数
        :param kwargs: 透传给提供者的命名参数
        :return: 按发行方式与优先级排序的非空结果列表
        """
        results: list[Any] = []
        for provider in self._answer_providers(method):
            result = self._invoke(provider, method, *args, **kwargs)
            if not self.is_valid_empty(result):
                results.append(result)
        return results

    async def async_multicast(self, method: str, *args: Any, **kwargs: Any) -> list[Any]:
        """以多播语义收集同步或异步提供者的全部非空答案。

        :param method: 模块方法名称
        :param args: 透传给提供者的位置参数
        :param kwargs: 透传给提供者的命名参数
        :return: 按发行方式与优先级排序的非空结果列表
        """
        results: list[Any] = []
        for provider in self._answer_providers(method):
            result = await self._async_invoke(provider, method, *args, **kwargs)
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
        for provider in self._answer_providers(method):
            result = self._invoke(provider, method, *args, **kwargs)
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
        for provider in self._answer_providers(method):
            result = await self._async_invoke(provider, method, *args, **kwargs)
            if not self.is_valid_empty(result):
                return result
        return None

    def pipeline(self, method: str, initial: Any, *args: Any, **kwargs: Any) -> Any:
        """在能力族内按提供者顺序接力，每个提供者在上一个的产出上继续增强。

        :param method: 模块方法名称
        :param initial: 交给第一个提供者的初始产出
        :param args: 每次调用都附加在产出之后透传给提供者的位置参数
        :param kwargs: 每次调用都透传给提供者的命名参数
        :return: 全部提供者接力增强后的最终产出；提供者返回空结果时保留上一轮产出
            继续传给下一个；无提供者时原样返回 ``initial``
        """
        result = initial
        for provider in self._answer_providers(method):
            enhanced = self._invoke(provider, method, result, *args, **kwargs)
            if not self.is_valid_empty(enhanced):
                result = enhanced
        return result

    async def async_pipeline(
        self, method: str, initial: Any, *args: Any, **kwargs: Any
    ) -> Any:
        """以管道语义接力执行同步或异步提供者，逐个增强同一个产出。

        :param method: 模块方法名称
        :param initial: 交给第一个提供者的初始产出
        :param args: 每次调用都附加在产出之后透传给提供者的位置参数
        :param kwargs: 每次调用都透传给提供者的命名参数
        :return: 全部提供者接力增强后的最终产出；提供者返回空结果时保留上一轮产出
            继续传给下一个；无提供者时原样返回 ``initial``
        """
        result = initial
        for provider in self._answer_providers(method):
            enhanced = await self._async_invoke(provider, method, result, *args, **kwargs)
            if not self.is_valid_empty(enhanced):
                result = enhanced
        return result

    @staticmethod
    def _log_contract(method: str) -> None:
        """记录方法命中的能力族契约。

        :param method: 模块方法名称
        :return: 无返回值
        """
        contract = get_module_method_contract(method)
        logger.debug("模块方法契约：%s -> %s", method, contract.family)

    def _is_settled(self, result: Any) -> bool:
        """判断接力结果是否已成为不可再合并的最终答案。

        :param result: 当前接力结果
        :return: 结果非空且不是可继续合并的列表时为 True
        """
        return not self.is_valid_empty(result) and not isinstance(result, list)

    def _notify_providers(self, method: str) -> Iterator[ExtensionProvider]:
        """按目录顺序遍历应被通知的全部提供者。

        :param method: 模块方法名称
        :return: 提供者迭代器
        """
        for source in self._sources:
            yield from source.notify_providers(method)

    def _answer_providers(self, method: str) -> Iterator[ExtensionProvider]:
        """按目录顺序遍历参与仲裁的提供者。

        :param method: 模块方法名称
        :return: 提供者迭代器
        """
        for source in self._sources:
            yield from source.answer_providers(method)

    def _invoke(
        self,
        provider: ExtensionProvider,
        method: str,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """执行单个提供者，异常按其归属策略上报后视为未认领。

        :param provider: 提供者记录
        :param method: 模块方法名称
        :param args: 透传给提供者的位置参数
        :param kwargs: 透传给提供者的命名参数
        :return: 提供者返回值；限流或出错时返回 ``None``
        """
        try:
            self._announce_invocation(provider, method)
            return provider.invoke(*args, **kwargs)
        except RateLimitExceededException as err:
            self._report_rate_limit(provider, method, err, **kwargs)
        except Exception as err:
            self._report_fault(provider, method, err, **kwargs)
        return None

    async def _async_invoke(
        self,
        provider: ExtensionProvider,
        method: str,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """执行单个提供者，协程直接等待，同步函数移入线程池。

        :param provider: 提供者记录
        :param method: 模块方法名称
        :param args: 透传给提供者的位置参数
        :param kwargs: 透传给提供者的命名参数
        :return: 提供者返回值；限流或出错时返回 ``None``
        """
        try:
            self._announce_invocation(provider, method)
            return await self._async_call(provider.invoke, *args, **kwargs)
        except RateLimitExceededException as err:
            self._report_rate_limit(provider, method, err, **kwargs)
        except Exception as err:
            self._report_fault(provider, method, err, **kwargs)
        return None

    def _execute_chain(
        self,
        providers: Iterable[ExtensionProvider],
        method: str,
        result: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """沿提供者顺序接力聚合结果，保持空结果补位、列表合并与标量短路。

        :param providers: 参与接力的提供者
        :param method: 模块方法名称
        :param result: 上一阶段的接力结果
        :param args: 透传给提供者的位置参数
        :param kwargs: 透传给提供者的命名参数
        :return: 本阶段结束后的接力结果
        """
        for provider in providers:
            try:
                self._announce_invocation(provider, method)
                if self.is_valid_empty(result):
                    result = provider.invoke(*args, **kwargs)
                elif provider.relays_result and ObjectUtils.check_signature(
                    provider.invoke,
                    result,
                ):
                    result = provider.invoke(result)
                elif isinstance(result, list):
                    temp = provider.invoke(*args, **kwargs)
                    if isinstance(temp, list):
                        result.extend(temp)
                else:
                    break
            except RateLimitExceededException as err:
                self._report_rate_limit(provider, method, err, **kwargs)
            except Exception as err:
                self._report_fault(provider, method, err, **kwargs)
        return result

    async def _async_execute_chain(
        self,
        providers: Iterable[ExtensionProvider],
        method: str,
        result: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """以同步路径的接力规则执行同步或异步提供者。

        :param providers: 参与接力的提供者
        :param method: 模块方法名称
        :param result: 上一阶段的接力结果
        :param args: 透传给提供者的位置参数
        :param kwargs: 透传给提供者的命名参数
        :return: 本阶段结束后的接力结果
        """
        for provider in providers:
            try:
                self._announce_invocation(provider, method)
                if self.is_valid_empty(result):
                    result = await self._async_call(provider.invoke, *args, **kwargs)
                elif provider.relays_result and ObjectUtils.check_signature(
                    provider.invoke,
                    result,
                ):
                    result = await self._async_call(provider.invoke, result)
                elif isinstance(result, list):
                    temp = await self._async_call(provider.invoke, *args, **kwargs)
                    if isinstance(temp, list):
                        result.extend(temp)
                else:
                    break
            except RateLimitExceededException as err:
                self._report_rate_limit(provider, method, err, **kwargs)
            except Exception as err:
                self._report_fault(provider, method, err, **kwargs)
        return result

    @staticmethod
    def _announce_invocation(provider: ExtensionProvider, method: str) -> None:
        """按提供者声明记录本次调用请求。

        :param provider: 提供者记录
        :param method: 模块方法名称
        :return: 无返回值
        """
        if provider.announces_invocation:
            logger.info(
                "请求%s %s 执行：%s ...",
                provider.fault_scope.value,
                provider.display_name,
                method,
            )

    def _report_fault(
        self,
        provider: ExtensionProvider,
        method: str,
        err: Exception,
        **kwargs: Any,
    ) -> None:
        """把执行异常交给提供者归属方的上报策略。

        :param provider: 提供者记录
        :param method: 模块方法名称
        :param err: 捕获到的异常
        :param kwargs: 透传给上报策略的命名参数
        :return: 无返回值
        """
        handler = self._fault_handlers[provider.fault_scope]
        handler(err, provider.extension_id, provider.display_name, method, **kwargs)

    def _report_rate_limit(
        self,
        provider: ExtensionProvider,
        method: str,
        err: RateLimitExceededException,
        **kwargs: Any,
    ) -> None:
        """把本地限流跳过交给限流上报策略。

        :param provider: 提供者记录
        :param method: 模块方法名称
        :param err: 捕获到的限流异常
        :param kwargs: 透传给上报策略的命名参数
        :return: 无返回值
        """
        self._rate_limit_handler(
            err,
            provider.fault_scope.value,
            provider.extension_id,
            method,
            **kwargs,
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
