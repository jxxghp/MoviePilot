"""插件配置保存、重置和运行态重建应用用例。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class PluginConfigResult:
    """描述插件配置写操作是否成功及提示信息。"""

    success: bool
    message: str = ""


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

    def update(self, plugin_id: str, config: dict) -> PluginConfigResult:
        """保存配置并按既有顺序重新初始化实例及运行时注册。"""
        if not self._save_config(plugin_id, config, False):
            return PluginConfigResult(False, "插件配置保存失败")
        self._initialize(plugin_id, config)
        self._refresh_registrations(plugin_id)
        return PluginConfigResult(True)

    def reset(self, plugin_id: str) -> PluginConfigResult:
        """通知插件补偿后停止实例、删除配置数据并重建运行态。"""
        self._publish_reset(plugin_id)
        self._stop(plugin_id)
        self._delete_config(plugin_id, True)
        self._delete_data(plugin_id, True)
        self._reload_runtime(plugin_id)
        self._refresh_registrations(plugin_id)
        return PluginConfigResult(True)
