"""插件实例日志等级查询与设置。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any, Optional

from app.runtime.log import (
    clear_plugin_instance_log_level,
    get_effective_plugin_instance_log_level,
    get_plugin_instance_log_level_override,
    set_plugin_instance_log_level,
)
from app.schemas.plugin import PluginInstance


class PluginLogLevelControl:
    """组装插件全部实例（含本体）的日志等级设置，并同步落盘与进程内缓存。"""

    def __init__(
        self,
        *,
        plugin_exists: Callable[[str], bool],
        get_instance: Callable[[str], Optional[PluginInstance]],
        instances_for_source: Callable[[str], list[PluginInstance]],
        save_instance: Callable[[PluginInstance], None],
        get_host_instance: Callable[[str], Optional[PluginInstance]],
        save_host_instance: Callable[[PluginInstance], None],
    ) -> None:
        """保存插件存在性判定和本体、分身实例各自的持久化端口。"""
        self._plugin_exists = plugin_exists
        self._get_instance = get_instance
        self._instances_for_source = instances_for_source
        self._save_instance = save_instance
        self._get_host_instance = get_host_instance
        self._save_host_instance = save_host_instance

    @staticmethod
    def _default_host_instance(plugin_id: str) -> PluginInstance:
        """本体从未被显式绑定过版本或日志等级时的默认视图，跟随当前版本和全局等级。"""
        return PluginInstance(
            instance_id=plugin_id,
            source_plugin_id=plugin_id,
            mode="host",
            follow_current_version=True,
        )

    def _host_instance(self, plugin_id: str) -> PluginInstance:
        """读取源插件本体的实例描述，从未绑定过时给出默认视图。"""
        return self._get_host_instance(plugin_id) or self._default_host_instance(plugin_id)

    def _resolve(self, plugin_id: str, instance_id: str) -> PluginInstance:
        """按插件 ID 和实例 ID 定位实例描述，实例须真实归属该插件。

        :raise LookupError: 插件不存在，或实例不存在／不归属该插件
        """
        if not self._plugin_exists(plugin_id):
            raise LookupError(f"插件 {plugin_id} 不存在")
        if instance_id == plugin_id:
            return self._host_instance(plugin_id)
        instance = self._get_instance(instance_id)
        if instance is None or instance.source_plugin_id != plugin_id:
            raise LookupError(f"插件实例 {instance_id} 不存在")
        return instance

    @staticmethod
    def _describe(instance: PluginInstance) -> dict[str, Any]:
        """把一个实例的日志等级覆盖投影为查询响应条目。"""
        override = get_plugin_instance_log_level_override(instance.instance_id)
        configured_level, expires_at = override if override is not None else (None, None)
        return {
            "instance_id": instance.instance_id,
            "configured_level": configured_level,
            "expires_at": expires_at,
            "effective_level": get_effective_plugin_instance_log_level(instance.instance_id),
        }

    def list_levels(self, plugin_id: str) -> list[dict[str, Any]]:
        """列出插件全部实例（含本体）当前的日志等级设置。

        :param plugin_id: 插件 ID
        :return: 每个实例的等级设置条目列表，首项固定是本体自身
        :raise LookupError: 插件不存在
        """
        if not self._plugin_exists(plugin_id):
            raise LookupError(f"插件 {plugin_id} 不存在")
        instances = [
            self._host_instance(plugin_id),
            *self._instances_for_source(plugin_id),
        ]
        return [self._describe(instance) for instance in instances]

    def set_level(
        self,
        plugin_id: str,
        instance_id: str,
        level: str,
        expires_at: Optional[datetime] = None,
    ) -> None:
        """设置指定实例的日志等级覆盖，写入进程内缓存后立即落盘。

        :param plugin_id: 插件 ID
        :param instance_id: 实例 ID
        :param level: 目标日志等级
        :param expires_at: 覆盖失效时间，None 表示不过期
        :raise LookupError: 插件不存在，或实例不存在／不归属该插件
        :raise ValueError: level 不是受支持的等级名
        """
        instance = self._resolve(plugin_id, instance_id)
        set_plugin_instance_log_level(instance_id, level, expires_at)
        updated = instance.model_copy(
            update={"log_level": level.strip().upper(), "log_expires_at": expires_at}
        )
        if instance.mode == "host":
            self._save_host_instance(updated)
        else:
            self._save_instance(updated)

    def clear_level(self, plugin_id: str, instance_id: str) -> None:
        """清除指定实例的日志等级覆盖，运行期立即回落全局等级；重复清除保持幂等。

        :param plugin_id: 插件 ID
        :param instance_id: 实例 ID
        :raise LookupError: 插件不存在，或实例不存在／不归属该插件
        """
        instance = self._resolve(plugin_id, instance_id)
        clear_plugin_instance_log_level(instance_id)
        if instance.log_level is None and instance.log_expires_at is None:
            return
        updated = instance.model_copy(update={"log_level": None, "log_expires_at": None})
        if instance.mode == "host":
            self._save_host_instance(updated)
        else:
            self._save_instance(updated)
