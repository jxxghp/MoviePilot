"""插件类与运行实例注册表。"""

from typing import Any, Dict, List, Optional

from app.runtime.extensions.contract.instance import extension_id_of
from app.schemas.plugin import PluginRuntimeStatus


class PluginRegistry:
    """集中持有插件类和运行实例，并为读取方提供稳定快照。

    类表按插件标识索引，同一插件的全部实例共用一个类；运行态表按实例键索引，
    默认实例的实例键即裸插件标识。
    """

    def __init__(self) -> None:
        """创建彼此独立但生命周期一致的类表和实例表。"""
        self._classes: Dict[str, Any] = {}
        self._running: Dict[str, Any] = {}
        self._runtime_statuses: Dict[str, PluginRuntimeStatus] = {}
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
        """按实例键精确读取运行实例，未运行时返回空。

        本表只回答「这个键指的实体在不在」，不回答「调用没指定实例时该走哪一个」。
        裸插件标识即默认实例的实例键，命中的是默认实例本身；插件没有默认实例时不在
        其余实例里挑一个顶替，该裁决由默认调用目标负责。

        :param key: 实例键
        :return: 运行实例；未运行时为 None
        """
        return self._running.get(key)

    def any_instance(self, plugin_id: str) -> Optional[Any]:
        """取该插件任一运行实例，供读取类级属性使用。

        插件类的名称、版本这类属性同一插件的全部实例取值相同，读它们不构成调用目标
        选择，因此取哪一个实例都不改变结果。

        :param plugin_id: 插件标识
        :return: 该插件的首个运行实例；一个实例都没有时为 None
        """
        return next(
            (
                instance
                for key, instance in self._running.items()
                if extension_id_of(key) == plugin_id
            ),
            None,
        )

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

    def remove_instance(self, key: str) -> None:
        """移除单个运行实例，该插件再无实例在运行时一并移除其类与运行状态。

        :param key: 实例键
        :return: 无返回值
        """
        self._running.pop(key, None)
        plugin_id = extension_id_of(key)
        if self.instance_keys(plugin_id):
            return
        self._classes.pop(plugin_id, None)
        if self._runtime_statuses.pop(plugin_id, None) is not None:
            self._generation += 1

    def remove(self, plugin_id: str) -> None:
        """移除指定插件类、其全部运行实例及运行状态。

        :param plugin_id: 插件标识
        :return: 无返回值
        """
        self._classes.pop(plugin_id, None)
        for key in self.instance_keys(plugin_id):
            self._running.pop(key, None)
        if self._runtime_statuses.pop(plugin_id, None) is not None:
            self._generation += 1

    def clear(self) -> None:
        """原地清空注册表，保持外部持有的兼容字典引用有效。"""
        self._classes.clear()
        self._running.clear()
        if self._runtime_statuses:
            self._runtime_statuses.clear()
            self._generation += 1
