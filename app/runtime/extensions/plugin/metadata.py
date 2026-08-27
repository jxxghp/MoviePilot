"""插件目录条目的运行态元数据映射。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Optional

from app.runtime.extensions.plugin.contracts import supports_plugin_hook
from app.schemas.plugin import Plugin


class PluginMetadataMapper:
    """把市场或本地仓条目映射为包含运行态状态的插件 DTO。"""

    def __init__(
        self,
        *,
        plugin_instance: Callable[[str], Optional[Any]],
        plugin_class: Callable[[str], Optional[Any]],
        annotate_system_version: Callable[[dict], dict],
        is_package_compatible: Callable[[dict, str], bool],
        auth_checker: Callable[[Plugin, dict], bool],
        version_compare: Callable[[str, str, str], bool],
        log: Any,
    ) -> None:
        """保存注册表、兼容判断和权限判断端口。"""
        self._plugin_instance = plugin_instance
        self._plugin_class = plugin_class
        self._annotate_system_version = annotate_system_version
        self._is_package_compatible = is_package_compatible
        self._auth_checker = auth_checker
        self._version_compare = version_compare
        self._logger = log

    def map(
        self,
        plugin_id: str,
        plugin_info: dict,
        market: str,
        installed_plugins: list[str],
        add_time: int,
        package_version: Optional[str] = None,
    ) -> Optional[Plugin]:
        """映射一个插件索引条目，不兼容或无权限时返回空。"""
        if not isinstance(plugin_info, dict):
            return None
        info = self._annotate_system_version(plugin_info.copy())
        if not self._is_package_compatible(info, package_version or ""):
            return None

        instance = self._plugin_instance(plugin_id)
        plugin_class = self._plugin_class(plugin_id)
        plugin = Plugin(id=plugin_id)
        plugin.installed = plugin_id in installed_plugins and plugin_class is not None
        plugin.has_update = False
        if plugin_class:
            installed_version = getattr(plugin_class, "plugin_version", None)
            online_version = info.get("version")
            if installed_version and online_version:
                plugin.has_update = self._version_compare(
                    installed_version,
                    "<",
                    online_version,
                )

        plugin.system_version = info.get("system_version")
        if info.get("system_version_compatible") is False:
            plugin.system_version_compatible = False
            plugin.system_version_message = info.get("system_version_message")

        plugin.state = self._state(plugin_id, instance)
        plugin.has_page = bool(
            instance and supports_plugin_hook(instance, "get_page")
        )
        if info.get("key"):
            plugin.plugin_public_key = info["key"]
        if not self._auth_checker(plugin, info):
            return None

        plugin.plugin_name = info.get("name")
        plugin.plugin_desc = info.get("description")
        plugin.plugin_version = info.get("version")
        plugin.plugin_icon = info.get("icon")
        plugin.plugin_label = self.normalize_label(info.get("labels"))
        plugin.plugin_author = info.get("author")
        plugin.history = info.get("history") or {}
        plugin.release = bool(info.get("release"))
        plugin.repo_url = market
        plugin.package_version = package_version
        plugin.is_local = False
        plugin.add_time = add_time
        return plugin

    def _state(self, plugin_id: str, instance: Optional[Any]) -> bool:
        """安全读取插件运行状态，插件异常时降级为未启用。"""
        if not instance or not hasattr(instance, "get_state"):
            return False
        try:
            return bool(instance.get_state())
        except Exception as error:  # noqa: BLE001
            self._logger.error(f"获取插件 {plugin_id} 状态出错：{error}")
            return False

    @staticmethod
    def normalize_label(labels: Any) -> Optional[str]:
        """兼容市场标签的旧字符串和新列表格式。"""
        if isinstance(labels, str):
            label = labels.strip()
            return label or None
        if isinstance(labels, list):
            normalized = [
                str(item).strip()
                for item in labels
                if str(item).strip()
            ]
            return " ".join(normalized) or None
        return None
