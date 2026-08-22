"""扩展契约：宿主共享机制识别与取用一个扩展所需的共同面。"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Optional, Protocol, runtime_checkable

from app.foundation.reflection import ObjectUtils


class ExtensionDistribution(StrEnum):
    """扩展的发行方式，只描述扩展如何进入宿主，不影响其能力。"""

    BUILTIN = "builtin"
    MARKET = "market"


class ExtensionFaultScope(StrEnum):
    """扩展执行失败时的归属方，决定错误上报通道与告警文案。"""

    HOST = "模块"
    PLUGIN = "插件"


def is_implemented_callable(candidate: Any) -> bool:
    """判断一个属性是否为已实现的可调用扩展点。

    :param candidate: 待检查的属性值
    :return: 属性可调用且函数体不是空实现时为 True
    """
    if not candidate or not callable(candidate):
        return False
    return ObjectUtils.check_method(candidate)


def supports_extension_hook(extension: Any, name: str) -> bool:
    """判断扩展是否实现了指定扩展点。

    :param extension: 扩展实例或扩展类
    :param name: 扩展点名称
    :return: 该扩展点已实现时为 True
    """
    return is_implemented_callable(getattr(extension, name, None))


def declared_method_names(extension: Any) -> Iterator[str]:
    """列出扩展公开声明的方法名，跳过属性描述符以免触发求值。

    :param extension: 扩展实例或扩展类
    :return: 按实例字典优先、MRO 其次的顺序去重的方法名迭代器
    """
    seen: set[str] = set()
    for name, value in getattr(extension, "__dict__", {}).items():
        if name.startswith("_") or name in seen:
            continue
        seen.add(name)
        if callable(value):
            yield name
    for klass in type(extension).__mro__:
        if klass is object:
            continue
        for name, attribute in vars(klass).items():
            if name.startswith("_") or name in seen:
                continue
            seen.add(name)
            if inspect.isroutine(attribute) or isinstance(
                attribute,
                (staticmethod, classmethod),
            ):
                yield name


def extension_capability_names(extension: Any) -> tuple[str, ...]:
    """列出扩展可被分发触达的方法名。

    :param extension: 扩展实例
    :return: 已实现的公开方法名元组
    """
    return tuple(
        name
        for name in declared_method_names(extension)
        if supports_extension_hook(extension, name)
    )


@runtime_checkable
class ExtensionView(Protocol):
    """宿主共享机制取用扩展的统一视图。"""

    @property
    def extension_id(self) -> str:
        """扩展在宿主内的稳定标识。"""

    @property
    def display_name(self) -> str:
        """扩展面向用户的展示名。"""

    @property
    def distribution(self) -> ExtensionDistribution:
        """扩展的发行方式。"""

    @property
    def priority(self) -> int:
        """同一能力下的仲裁顺序，数值越小越先被询问。"""

    def is_enabled(self) -> bool:
        """返回扩展当前是否处于启用状态。"""

    def initialize(self, config: Optional[dict] = None) -> None:
        """按给定配置建立扩展自有的连接、线程或客户端资源。"""

    def terminate(self) -> None:
        """释放扩展自有的资源。"""

    def self_test(self) -> Optional[tuple[bool, str]]:
        """执行连通性自检，返回结果与错误信息。"""

    def supports_hook(self, name: str) -> bool:
        """判断扩展是否实现了指定扩展点。"""

    def capability_names(self) -> tuple[str, ...]:
        """列出扩展可被分发触达的方法名。"""

    def capability(self, name: str) -> Optional[Callable[..., Any]]:
        """取用指定名称的可分发方法，未提供时返回 ``None``。"""


@dataclass(frozen=True, slots=True)
class ExtensionProvider:
    """扩展对某个方法的一次可调用实现。"""

    extension_id: str
    display_name: str
    distribution: ExtensionDistribution
    fault_scope: ExtensionFaultScope
    invoke: Callable[..., Any]
    relays_result: bool = False
    announces_invocation: bool = False


@runtime_checkable
class ExtensionProviderSource(Protocol):
    """把一类发行方式的扩展目录投影为分发可消费的提供者。"""

    @property
    def distribution(self) -> ExtensionDistribution:
        """本目录提供的扩展发行方式。"""

    def announce_phase(self, method: str) -> None:
        """在本目录参与接力前记录一次阶段日志。"""

    def notify_providers(self, method: str) -> Iterable[ExtensionProvider]:
        """返回通知语义下应被触达的全部提供者。"""

    def answer_providers(self, method: str) -> Iterable[ExtensionProvider]:
        """返回仲裁语义下按优先级排序的提供者。"""
