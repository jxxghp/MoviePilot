"""
应用启动和关闭组件的声明模型。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Optional


class LifecycleMode(StrEnum):
    """
    声明组件在哪一种应用运行模式下启用。
    """

    # 安全模式和常规模式都启用组件。
    ALWAYS = "always"
    # 仅常规模式启用组件。
    NORMAL_ONLY = "normal_only"


class LifecycleFailurePolicy(StrEnum):
    """
    声明生命周期回调失败后的控制流策略。
    """

    # 回调失败时中止后续生命周期步骤。
    FAIL_FAST = "fail_fast"
    # 记录失败后继续处理其余组件。
    CONTINUE = "continue"


@dataclass(frozen=True, slots=True)
class LifecycleComponent:
    """
    描述一个可检查的启动或关闭组件及其顺序和失败策略。

    :param name: 组件在生命周期清单中的唯一名称
    :param dependencies: 必须先启动的组件名称，默认无依赖
    :param mode: 启用组件的运行模式，默认所有模式
    :param start: 可选启动回调；None 表示只参与依赖声明或关闭
    :param stop: 可选关闭回调；None 表示无需主动关闭
    :param start_order: 可选的启动顺序权重；None 使用依赖拓扑顺序
    :param stop_order: 可选的关闭顺序权重；None 使用默认逆序
    :param start_timeout_seconds: 可选启动超时秒数；None 表示无组件专属超时
    :param stop_timeout_seconds: 可选关闭超时秒数；None 表示无组件专属超时
    :param start_failure: 启动失败时立即中止或继续的策略，默认立即中止
    :param stop_failure: 关闭失败时中止或继续的策略，默认继续
    """

    name: str
    dependencies: tuple[str, ...] = ()
    mode: LifecycleMode = LifecycleMode.ALWAYS
    start: Optional[Callable[[], object]] = None
    stop: Optional[Callable[[], object]] = None
    start_order: Optional[int] = None
    stop_order: Optional[int] = None
    start_timeout_seconds: Optional[float] = None
    stop_timeout_seconds: Optional[float] = None
    start_failure: LifecycleFailurePolicy = LifecycleFailurePolicy.FAIL_FAST
    stop_failure: LifecycleFailurePolicy = LifecycleFailurePolicy.CONTINUE

    def enabled(self, safe_mode: bool) -> bool:
        """
        判断组件是否应在当前安全模式设置下启用。
        """
        return self.mode is LifecycleMode.ALWAYS or not safe_mode


def lifecycle_manifest(
    components: tuple[LifecycleComponent, ...],
    *,
    safe_mode: bool,
) -> tuple[dict[str, object], ...]:
    """
    导出当前模式下启用组件的稳定、可序列化生命周期清单。
    """
    return tuple(
        {
            "name": component.name,
            "dependencies": component.dependencies,
            "mode": component.mode.value,
            "start_order": component.start_order,
            "stop_order": component.stop_order,
            "start_timeout_seconds": component.start_timeout_seconds,
            "stop_timeout_seconds": component.stop_timeout_seconds,
            "start_failure": component.start_failure.value,
            "stop_failure": component.stop_failure.value,
        }
        for component in components
        if component.enabled(safe_mode)
    )
