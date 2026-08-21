"""事件处理器声明到运行实例的显式绑定解析。"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Optional, Type

from app.runtime.event.registry import EventRegistry
from app.runtime.log import logger


@dataclass(frozen=True, slots=True)
class EventHandlerBinding:
    """描述上层运行时为某个事件处理器提供的实例绑定。"""

    instance: Optional[Any]
    owner_name: str
    run_sync_in_threadpool: bool = False
    instance_key: Optional[str] = None


HandlerInstanceResolver = Callable[
    [Type[Any]], Optional[list[EventHandlerBinding]]
]


class EventBindingResolver:
    """只通过已登记 resolver 把类处理器绑定到托管运行实例。"""

    def __init__(
        self,
        *,
        lock: Any,
        resolvers: Callable[[], dict[str, HandlerInstanceResolver]],
        instance_enabled: Optional[Callable[[Type[Any], Optional[str]], bool]] = None,
    ) -> None:
        """绑定 resolver 存储，并记录未命中的处理器用于启动诊断。

        :param lock: 保护 resolver 存储的锁
        :param resolvers: 已登记实例解析器的取用函数
        :param instance_enabled: 判定某个实例键是否仍启用的谓词，缺省时不筛选
        """
        self._lock = lock
        self._resolvers = resolvers
        self._instance_enabled = instance_enabled
        self._unresolved: set[str] = set()

    def register(self, name: str, resolver: HandlerInstanceResolver) -> None:
        """注册或替换命名实例解析器。"""
        with self._lock:
            self._resolvers()[name] = resolver

    def unresolved_handlers(self) -> tuple[str, ...]:
        """返回本进程中未被显式 resolver 接管的类处理器。"""
        with self._lock:
            return tuple(sorted(self._unresolved))

    @staticmethod
    def parse_handler_names(handler: Callable) -> tuple[str, str]:
        """解析处理器限定名中的类名和方法名。"""
        names = handler.__qualname__.split(".")
        if len(names) < 2:
            return "", names[0]
        return names[0], names[1]

    @staticmethod
    def is_class_method_declaration(handler: Callable) -> bool:
        """判断处理器是否声明在类体内（限定名含类前缀或签名首参为 self/cls）。

        模块级顶层自由函数的限定名不含 ``.``；类方法、装饰器包装和嵌套函数
        的限定名含 ``.``。局部作用域自由函数（如测试内联 handler）限定名形如
        ``func.<locals>.handler``，与装饰器包装方法 ``SiteStatistic.<locals>.
        wrapper`` 无法靠限定名区分，需按调用约定兜底：签名首参为 self/cls
        才视为类方法声明。模块卸载后残留的类方法一旦被 unbound 直调，会把
        event 吞进 self 触发 missing event TypeError，因此必须跳过等待重载自愈。
        """
        parts = handler.__qualname__.split(".")
        if len(parts) < 2:
            return False
        if "<locals>" not in parts:
            return True
        try:
            parameters = list(inspect.signature(handler).parameters.values())
        except (TypeError, ValueError):
            return True
        if not parameters:
            return True
        return parameters[0].name in ("self", "cls")

    @staticmethod
    def owner_class(handler: Callable) -> Optional[Type[Any]]:
        """从处理器对象本身解析声明类，不按字符串动态导入模块。"""
        if inspect.ismethod(handler):
            owner = handler.__self__
            return owner if isinstance(owner, type) else type(owner)
        module = inspect.getmodule(handler)
        if not module:
            return None
        owner: Any = module
        for part in handler.__qualname__.split(".")[:-1]:
            if part == "<locals>":
                return None
            owner = getattr(owner, part, None)
            if owner is None:
                return None
        return owner if isinstance(owner, type) else None

    def _record_unresolved(self, identifier: str, reason: str) -> None:
        """首次未命中时记录告警，避免重载窗口内重复刷屏。"""
        with self._lock:
            first_miss = identifier not in self._unresolved
            self._unresolved.add(identifier)
        if first_miss:
            logger.warning(reason, identifier)

    def resolve(
        self,
        handler: Callable,
    ) -> Optional[
        tuple[Callable, EventHandlerBinding, str, str]
        | list[tuple[Callable, EventHandlerBinding, str, str]]
    ]:
        """解析处理器的实例绑定。

        自由函数不属于任何类，直调路径固定唯一，返回单个绑定元组；托管类方法
        按已登记 resolver 解析，返回绑定列表（可能为空列表，表示该类当前没有
        启用实例，归属已单独停用实例的绑定同样被剔除，兄弟实例继续参与调度）；
        声明类不可解析时（插件重载 stop 阶段清除模块缓存后，窗口期内残留的旧
        类方法声明无法定位声明类，直接调用原始函数会绕过实例绑定，旧签名可能
        与事件调用约定不一致）返回 ``None``，调用方须整体跳过本次执行，等待
        重载完成后按新 handler 注册自愈。
        :param handler: 装饰阶段登记的处理器
        :return: 单个绑定元组、绑定元组列表，或 ``None``
        """
        owner_class = self.owner_class(handler)
        declared_method_name = getattr(
            handler,
            "__name__",
            self.parse_handler_names(handler)[1],
        )
        if owner_class is None:
            if self.is_class_method_declaration(handler):
                self._record_unresolved(
                    EventRegistry.handler_identifier(handler),
                    "事件处理器所属模块已卸载或声明类不可解析，跳过执行：%s",
                )
                return None
            binding = EventHandlerBinding(
                instance=None,
                owner_name=EventRegistry.handler_identifier(handler),
                run_sync_in_threadpool=True,
            )
            return (handler, binding, "", declared_method_name)

        with self._lock:
            resolvers = tuple(self._resolvers().items())
        bindings = None
        resolver_name = ""
        for name, resolver in resolvers:
            candidate = resolver(owner_class)
            if candidate is not None:
                bindings = candidate
                resolver_name = name
                break
        if bindings is None:
            self._record_unresolved(
                EventRegistry.handler_identifier(handler),
                "事件处理器未绑定显式 resolver，已跳过：%s",
            )
            return []
        logger.debug(
            "事件处理器绑定：%s -> %s",
            EventRegistry.handler_identifier(handler),
            resolver_name,
        )
        resolved: list[tuple[Callable, EventHandlerBinding, str, str]] = []
        for binding in bindings:
            if binding.instance is None:
                continue
            if self._instance_enabled and not self._instance_enabled(
                owner_class,
                binding.instance_key,
            ):
                continue
            method_name = declared_method_name
            method = getattr(binding.instance, method_name, None)
            if not callable(method):
                fallback_name = self.parse_handler_names(handler)[1]
                method = getattr(binding.instance, fallback_name, None)
                if fallback_name == method_name or not callable(method):
                    logger.warning(
                        "事件处理器 %s 无法解析为实例方法 %s.%s，跳过执行",
                        EventRegistry.handler_identifier(handler),
                        owner_class.__name__,
                        method_name,
                    )
                    continue
                method_name = fallback_name
            resolved.append((method, binding, owner_class.__name__, method_name))
        return resolved

    @staticmethod
    def as_binding_sequence(
        resolved: Optional[
            tuple[Callable, EventHandlerBinding, str, str]
            | list[tuple[Callable, EventHandlerBinding, str, str]]
        ],
    ) -> tuple[tuple[Callable, EventHandlerBinding, str, str], ...]:
        """把 `resolve()` 的返回值归一为可直接遍历的绑定序列。

        :param resolved: `resolve()` 的返回值：单个绑定元组、绑定元组列表，或 ``None``
        :return: 绑定元组序列；输入为 ``None`` 时为空序列
        """
        if resolved is None:
            return ()
        if isinstance(resolved, list):
            return tuple(resolved)
        return (resolved,)
