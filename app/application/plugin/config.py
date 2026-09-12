"""插件配置保存、重置和运行态重建应用用例。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, ContextManager

from app.schemas.exception import PluginMutationRejectedError


@dataclass(frozen=True, slots=True)
class PluginConfigResult:
    """描述插件配置写操作是否成功及提示信息。"""

    success: bool
    message: str = ""


@dataclass(frozen=True, slots=True)
class PluginPurgeScope:
    """彻底清理一个实例时用户选定的删除范围。

    四项各自独立，由用户逐项勾选：清理动作不可逆，替用户把范围扩大到他没选的
    东西上，比让他多点一下要糟得多。
    """

    config: bool = False
    plugin_data: bool = False
    own_database: bool = False
    data_directory: bool = False

    @property
    def is_empty(self) -> bool:
        """是否一项都没选。"""
        return not any(
            (self.config, self.plugin_data, self.own_database, self.data_directory)
        )


@dataclass(frozen=True, slots=True)
class PluginPurgeResult:
    """描述彻底清理的执行结果与实际清掉的范围。"""

    success: bool
    message: str = ""
    purged: tuple[str, ...] = ()
    instance_removed: bool = False


class PluginConfigCommand:
    """协调插件配置持久化、实例初始化和运行时注册刷新。"""

    def __init__(
        self,
        *,
        save_config: Callable[[str, dict, bool], bool],
        initialize: Callable[[str, dict], Any],
        stop: Callable[[str], Any],
        delete_config: Callable[[str, bool], bool],
        delete_data: Callable[[str, bool], bool],
        reload_runtime: Callable[[str], Any],
        publish_reset: Callable[[str], Any],
        refresh_registrations: Callable[[str], Any],
        mutation: Callable[[str], ContextManager[None]],
        delete_plugin_data_rows: Callable[[str], Any],
        destroy_own_database: Callable[[str], Any],
        delete_data_directory: Callable[[str], bool],
        purge_instance: Callable[[str], bool],
        is_clone: Callable[[str], bool],
    ) -> None:
        """保存插件管理 Facade 和运行时注册刷新端口。"""
        self._save_config = save_config
        self._initialize = initialize
        self._stop = stop
        self._delete_config = delete_config
        self._delete_data = delete_data
        self._reload_runtime = reload_runtime
        self._publish_reset = publish_reset
        self._refresh_registrations = refresh_registrations
        self._mutation = mutation
        self._delete_plugin_data_rows = delete_plugin_data_rows
        self._destroy_own_database = destroy_own_database
        self._delete_data_directory = delete_data_directory
        self._purge_instance = purge_instance
        self._is_clone = is_clone

    def update(self, plugin_id: str, config: dict) -> PluginConfigResult:
        """保存配置并按既有顺序重新初始化实例及运行时注册。"""
        try:
            with self._mutation(f"更新插件 {plugin_id} 配置"):
                if not self._save_config(plugin_id, config, False):
                    return PluginConfigResult(False, "插件配置保存失败")
                self._initialize(plugin_id, config)
                self._refresh_registrations(plugin_id)
                return PluginConfigResult(True)
        except PluginMutationRejectedError as error:
            return PluginConfigResult(False, str(error))

    def purge(self, instance_id: str, scope: PluginPurgeScope) -> PluginPurgeResult:
        """按用户选定的范围彻底清理一个实例，逐项执行且互不代劳。

        与停用的区别在于不可逆：停用只把启用位置假、设置原样留着，这里才真正删除
        用户数据。因而范围完全由调用方给定，一项没选就直接拒绝，不做「那就全清吧」
        这种猜测。

        分身在清理后连同实例行一并消失——分身的存在本身就由那一行表达；本体的行则
        保留，它还承载着「这个插件应当装载」，删掉等于把装着的插件静默停用，想卸载
        插件是另一条路径。

        自有数据库与数据目录有先后：库文件就落在该目录下，不先销毁句柄直接删目录，
        Windows 下会因文件占用失败，Linux 下则留下一个仍被写入的已删除文件。选了
        删目录就必然先销毁库，不管用户有没有单独勾选它。

        :param instance_id: 实例 ID
        :param scope: 用户选定的删除范围
        :return: 执行结果与实际清掉的范围
        """
        if scope.is_empty:
            return PluginPurgeResult(False, "请至少选择一项要清理的内容")
        try:
            with self._mutation(f"彻底清理插件实例 {instance_id}"):
                self._stop(instance_id)
                purged: list[str] = []
                if scope.config:
                    self._delete_config(instance_id, True)
                    purged.append("config")
                if scope.plugin_data:
                    self._delete_plugin_data_rows(instance_id)
                    purged.append("plugin_data")
                if scope.own_database or scope.data_directory:
                    self._destroy_own_database(instance_id)
                    if scope.own_database:
                        purged.append("own_database")
                if scope.data_directory and self._delete_data_directory(instance_id):
                    purged.append("data_directory")
                instance_removed = (
                    self._purge_instance(instance_id) if self._is_clone(instance_id) else False
                )
                return PluginPurgeResult(
                    True,
                    purged=tuple(purged),
                    instance_removed=instance_removed,
                )
        except PluginMutationRejectedError as error:
            return PluginPurgeResult(False, str(error))

    def reset(self, plugin_id: str) -> PluginConfigResult:
        """通知插件补偿后停止实例、删除配置数据并重建运行态。"""
        try:
            with self._mutation(f"重置插件 {plugin_id} 配置和数据"):
                self._publish_reset(plugin_id)
                self._stop(plugin_id)
                self._delete_config(plugin_id, True)
                self._delete_data(plugin_id, True)
                self._reload_runtime(plugin_id)
                self._refresh_registrations(plugin_id)
                return PluginConfigResult(True)
        except PluginMutationRejectedError as error:
            return PluginConfigResult(False, str(error))
