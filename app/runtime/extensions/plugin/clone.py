"""插件分身创建运行时用例。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Optional


class PluginCloneService:
    """协调插件包复制、安装清单、配置复制和运行态刷新。"""

    def __init__(
        self,
        *,
        plugin_class: Callable[[str], Optional[Any]],
        plugin_exists: Callable[[str], bool],
        package_clone: Callable[..., tuple[bool, str]],
        installed_plugins: Callable[[], list[str]],
        save_installed_plugins: Callable[[list[str]], Any],
        read_config: Callable[[str], dict],
        save_config: Callable[[str, dict], bool],
        reload_plugin: Callable[[str], Any],
        running_plugin: Callable[[str], Optional[Any]],
        initialize_plugin: Callable[[str, dict], Any],
        log: Any,
    ) -> None:
        """保存包、持久化和运行态端口。"""
        self._plugin_class = plugin_class
        self._plugin_exists = plugin_exists
        self._package_clone = package_clone
        self._installed_plugins = installed_plugins
        self._save_installed_plugins = save_installed_plugins
        self._read_config = read_config
        self._save_config = save_config
        self._reload_plugin = reload_plugin
        self._running_plugin = running_plugin
        self._initialize_plugin = initialize_plugin
        self._logger = log

    def clone(
        self,
        *,
        plugin_id: str,
        suffix: str,
        name: str,
        description: str,
        version: Optional[str] = None,
        icon: Optional[str] = None,
    ) -> tuple[bool, str]:
        """创建插件分身并保持原有默认禁用配置语义。"""
        if not plugin_id or not suffix:
            return False, "插件ID和分身后缀不能为空"
        original_class = self._plugin_class(plugin_id)
        if original_class is None:
            return False, f"原插件 {plugin_id} 不存在"

        clone_id = f"{plugin_id}{suffix.lower()}"
        if self._plugin_exists(clone_id):
            return False, f"分身插件 {clone_id} 已存在"

        try:
            success, message = self._package_clone(
                plugin_id=plugin_id,
                clone_id=clone_id,
                original_class_name=original_class.__name__,
                suffix=suffix.lower(),
                name=name,
                description=description,
                version=version,
                icon=icon,
            )
            if not success:
                return False, message

            installed = list(self._installed_plugins())
            if clone_id not in installed:
                installed.append(clone_id)
                self._save_installed_plugins(installed)

            original_config = self._read_config(plugin_id)
            if original_config:
                clone_config = dict(original_config)
                clone_config["enable"] = False
                clone_config["enabled"] = False
                self._save_config(clone_id, clone_config)

            self._reload_plugin(clone_id)
            clone_instance = self._running_plugin(clone_id)
            clone_config = self._read_config(clone_id)
            if clone_instance and clone_config:
                self._initialize_plugin(clone_id, clone_config)
            self._logger.info(f"插件分身 {clone_id} 创建成功")
            return True, clone_id
        except Exception as error:  # noqa: BLE001
            self._logger.error(f"创建插件分身失败：{error}")
            return False, f"创建插件分身失败：{error}"
