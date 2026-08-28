"""插件类与运行实例注册表。"""

from typing import Any, Dict, Optional

from app.schemas.plugin import PluginRuntimeStatus


class PluginRegistry:
    """集中持有插件类和运行实例，并为读取方提供稳定快照。"""

    def __init__(self) -> None:
        """创建彼此独立但生命周期一致的类表和实例表。"""
        self._classes: Dict[str, Any] = {}
        self._running: Dict[str, Any] = {}
        self._runtime_statuses: Dict[str, PluginRuntimeStatus] = {}
        self._restart_required_plugins: Dict[str, tuple[str, ...]] = {}
        self._settling = False
        self._generation = 0

    @property
    def classes(self) -> Dict[str, Any]:
        """返回兼容旧调用方可变访问语义的插件类表。"""
        return self._classes

    @property
    def running(self) -> Dict[str, Any]:
        """返回兼容旧调用方可变访问语义的运行实例表。"""
        return self._running

    def has_class(self, plugin_id: str) -> bool:
        """判断插件类是否已经登记。"""
        return plugin_id in self._classes

    def plugin_class(self, plugin_id: str) -> Optional[Any]:
        """读取指定插件类，未登记时返回空。"""
        return self._classes.get(plugin_id)

    def instance(self, plugin_id: str) -> Optional[Any]:
        """读取指定运行实例，未运行时返回空。"""
        return self._running.get(plugin_id)

    def plugin_ids(self) -> list[str]:
        """返回保持登记顺序的插件类 ID 快照。"""
        return list(self._classes)

    def running_ids(self) -> list[str]:
        """返回保持登记顺序的运行实例 ID 快照。"""
        return list(self._running)

    def running_snapshot(self) -> Dict[str, Any]:
        """复制运行实例表，避免插件重载期间迭代失效。"""
        return dict(self._running)

    def set_runtime_status(
        self,
        plugin_id: str,
        status: PluginRuntimeStatus,
    ) -> None:
        """记录插件当前状态，并在实际变化时推进前端刷新代次。"""
        if self._runtime_statuses.get(plugin_id) == status:
            return
        self._runtime_statuses[plugin_id] = status
        self._generation += 1

    def runtime_status(self, plugin_id: str) -> Optional[PluginRuntimeStatus]:
        """读取指定插件状态。"""
        return self._runtime_statuses.get(plugin_id)

    def runtime_status_snapshot(self) -> Dict[str, PluginRuntimeStatus]:
        """复制插件状态表，避免后台加载期间迭代失效。"""
        return dict(self._runtime_statuses)

    def mark_restart_required(
        self,
        plugin_id: str,
        distributions: tuple[str, ...],
    ) -> None:
        """记录当前进程仍持有旧原生载荷的插件和发行包。"""
        normalized = tuple(sorted(set(distributions)))
        previous = self._restart_required_plugins.get(plugin_id, ())
        merged = tuple(sorted(set(previous).union(normalized)))
        if previous == merged:
            return
        self._restart_required_plugins[plugin_id] = merged
        self._generation += 1

    def restart_required_snapshot(self) -> Dict[str, tuple[str, ...]]:
        """返回重启后才能完整激活的插件及原生发行包。"""
        return dict(self._restart_required_plugins)

    def set_settling(self, settling: bool) -> None:
        """标记启动后的插件源码与依赖收敛任务是否仍在执行。"""
        if self._settling == settling:
            return
        self._settling = settling
        self._generation += 1

    @property
    def settling(self) -> bool:
        """返回插件后台收敛任务是否仍在执行。"""
        return self._settling

    @property
    def generation(self) -> int:
        """返回状态变化代次，供读取方识别刷新边界。"""
        return self._generation

    def remove(self, plugin_id: str) -> None:
        """同时移除指定插件类、运行实例和状态。"""
        self._classes.pop(plugin_id, None)
        self._running.pop(plugin_id, None)
        if self._runtime_statuses.pop(plugin_id, None) is not None:
            self._generation += 1

    def clear(self) -> None:
        """原地清空注册表，保持外部持有的兼容字典引用有效。"""
        self._classes.clear()
        self._running.clear()
        if self._runtime_statuses:
            self._runtime_statuses.clear()
            self._generation += 1
