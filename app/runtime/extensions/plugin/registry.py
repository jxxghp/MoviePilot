"""插件类与运行实例注册表。"""

from typing import Any, Dict, List, Optional

from app.runtime.extensions.instance import (
    extension_id_of,
    resolve_running_instance,
)


class PluginRegistry:
    """集中持有插件类和运行实例，并为读取方提供稳定快照。

    类表按插件标识索引，同一插件的全部实例共用一个类；运行态表按实例键索引，
    默认实例的实例键即裸插件标识。
    """

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

    def has_class(self, key: str) -> bool:
        """判断插件类是否已经登记。

        :param key: 插件标识或实例键
        :return: 该插件的类已登记时为 True
        """
        return extension_id_of(key) in self._classes

    def plugin_class(self, key: str) -> Optional[Any]:
        """按族回落读取插件类，未登记时返回空。

        :param key: 插件标识或实例键
        :return: 插件类；未登记时为 None
        """
        return self._classes.get(extension_id_of(key))

    def instance(self, key: str) -> Optional[Any]:
        """读取指定运行实例，未运行时返回空。

        :param key: 实例键；传插件标识且该插件只有一个实例在运行时回落到该实例
        :return: 运行实例；未运行或无法唯一确定时为 None
        """
        return resolve_running_instance(self._running, key)

    def instance_keys(self, plugin_id: str) -> List[str]:
        """列出指定插件当前在运行的全部实例键。

        :param plugin_id: 插件标识
        :return: 保持登记顺序的实例键列表
        """
        return [key for key in self._running if extension_id_of(key) == plugin_id]

    def plugin_ids(self) -> List[str]:
        """返回保持登记顺序的插件类 ID 快照。"""
        return list(self._classes)

    def running_ids(self) -> List[str]:
        """返回保持登记顺序的运行实例键快照。"""
        return list(self._running)

    def running_plugin_ids(self) -> List[str]:
        """返回运行实例按族去重后的插件 ID 快照，保持首个实例的登记顺序。"""
        return list(dict.fromkeys(extension_id_of(key) for key in self._running))

    def running_snapshot(self) -> Dict[str, Any]:
        """复制运行实例表，避免插件重载期间迭代失效。"""
        return dict(self._running)

    def remove_instance(self, key: str) -> None:
        """移除单个运行实例，该插件再无实例在运行时一并移除其类。

        :param key: 实例键
        :return: 无返回值
        """
        self._running.pop(key, None)
        plugin_id = extension_id_of(key)
        if not self.instance_keys(plugin_id):
            self._classes.pop(plugin_id, None)

    def remove(self, plugin_id: str) -> None:
        """移除指定插件类及其全部运行实例。

        :param plugin_id: 插件标识
        :return: 无返回值
        """
        self._classes.pop(plugin_id, None)
        for key in self.instance_keys(plugin_id):
            self._running.pop(key, None)

    def clear(self) -> None:
        """原地清空注册表，保持外部持有的兼容字典引用有效。"""
        self._classes.clear()
        self._running.clear()
