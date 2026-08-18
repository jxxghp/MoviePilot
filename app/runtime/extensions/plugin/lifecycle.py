"""插件实例生命周期应用能力。"""

from __future__ import annotations

import traceback
from collections.abc import Callable
from typing import Any, Optional


class PluginLifecycle:
    """管理插件发现、初始化、启停和热重载，不持有市场或 HTTP 路由职责。"""

    def __init__(
        self,
        *,
        classes: dict[str, Any],
        running: dict[str, Any],
        load_plugins: Callable[[Optional[str], list[str], Callable[[Any], bool]], list[Any]],
        installed_plugins: Callable[[], list[str]],
        plugin_config: Callable[[str], dict],
        auth_checker: Callable[[Any], bool],
        clear_modules: Callable[[Optional[str]], Any],
        clear_tools: Callable[[], None],
        enable_events: Callable[[Any], None],
        disable_events: Callable[[Any], None],
        log: Any,
        event_sender: Callable[..., Any],
    ) -> None:
        """保存注册表、加载器和事件端口。"""
        self._classes = classes
        self._running = running
        self._load_plugins = load_plugins
        self._installed_plugins = installed_plugins
        self._plugin_config = plugin_config
        self._auth_checker = auth_checker
        self._clear_modules = clear_modules
        self._clear_tools = clear_tools
        self._enable_events = enable_events
        self._disable_events = disable_events
        self._logger = log
        self._event_sender = event_sender

    def start(self, plugin_id: Optional[str] = None) -> None:
        """加载并初始化指定插件或全部已安装插件。"""
        installed_plugins = self._installed_plugins()

        def check_module(module: Any) -> bool:
            """判断模块是否具备宿主插件最小生命周期钩子。"""
            return hasattr(module, "init_plugin") and hasattr(module, "plugin_name")

        plugins = self._load_plugins(plugin_id, installed_plugins, check_module)
        plugins.sort(key=lambda item: getattr(item, "plugin_order", 0))
        for plugin in plugins:
            current_id = plugin.__name__
            if plugin_id and current_id != plugin_id:
                continue
            try:
                if not self._auth_checker(plugin):
                    if current_id in self._classes:
                        self._classes[current_id] = plugin
                    continue
                self._classes[current_id] = plugin
                instance = plugin()
                instance.init_plugin(self._plugin_config(current_id))
                self._running[current_id] = instance
                self._logger.info(
                    f"加载插件：{current_id} 版本：{instance.plugin_version}"
                )
                if instance.get_state():
                    self._enable_events(plugin)
                else:
                    self._disable_events(plugin)
            except Exception as error:  # noqa: BLE001
                self._logger.error(
                    f"加载插件 {current_id} 出错：{error} - {traceback.format_exc()}"
                )
        self._clear_tools()

    def initialize(self, plugin_id: str, config: dict) -> None:
        """重新应用指定插件配置并刷新事件注册状态。"""
        plugin = self._running.get(plugin_id)
        if not plugin:
            return
        plugin.init_plugin(config)
        if plugin.get_state():
            self._enable_events(type(plugin))
        else:
            self._disable_events(type(plugin))
        self._clear_tools()

    def stop(self, plugin_id: Optional[str] = None) -> None:
        """停止指定插件或全部插件，并清理模块缓存。"""
        if plugin_id:
            self._logger.info(f"正在停止插件 {plugin_id}...")
            plugin = self._running.get(plugin_id)
            plugins = {plugin_id: plugin} if plugin else {}
            if not plugin:
                self._logger.debug(f"插件 {plugin_id} 不存在或未加载")
        else:
            self._logger.info("正在停止所有插件...")
            plugins = dict(self._running)

        for current_id, plugin in plugins.items():
            self._disable_events(type(plugin))
            self._stop_plugin(plugin)

        if plugin_id:
            self._classes.pop(plugin_id, None)
            self._running.pop(plugin_id, None)
            self._clear_modules(plugin_id)
        else:
            self._classes.clear()
            self._running.clear()
            self._clear_modules(None)
        self._clear_tools()
        self._logger.info("插件停止完成")

    def reload(self, plugin_id: str, reload_event: Any) -> None:
        """重启指定插件并广播插件重载事件。"""
        self.stop(plugin_id)
        self.start(plugin_id)
        self._event_sender(reload_event, data={"plugin_id": plugin_id})

    def _stop_plugin(self, plugin: Any) -> None:
        """按插件旧 ABI 顺序关闭资源和服务。"""
        try:
            if hasattr(plugin, "close"):
                plugin.close()
            if hasattr(plugin, "stop_service"):
                plugin.stop_service()
        except Exception as error:  # noqa: BLE001
            name = plugin.get_name() if hasattr(plugin, "get_name") else type(plugin).__name__
            self._logger.warning(f"停止插件 {name} 时发生错误: {error}")
