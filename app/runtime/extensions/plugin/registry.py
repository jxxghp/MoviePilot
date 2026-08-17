"""插件类与运行实例注册表。"""

from typing import Any, Dict, Optional


class PluginRegistry:
    """集中持有插件类和运行实例，并为读取方提供稳定快照。"""

    def __init__(self) -> None:
        """创建彼此独立但生命周期一致的类表和实例表。"""
        self._classes: Dict[str, Any] = {}
        self._running: Dict[str, Any] = {}

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

    def remove(self, plugin_id: str) -> None:
        """同时移除指定插件类和运行实例。"""
        self._classes.pop(plugin_id, None)
        self._running.pop(plugin_id, None)

    def clear(self) -> None:
        """原地清空注册表，保持外部持有的兼容字典引用有效。"""
        self._classes.clear()
        self._running.clear()
