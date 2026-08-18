"""插件 Agent 工具目录缓存。"""

from __future__ import annotations

import threading
from typing import Any, Mapping, Optional

from app.runtime.extensions.plugin.contracts import supports_plugin_hook


class PluginToolCatalog:
    """按插件运行态版本构建并缓存 Agent 工具声明。"""

    def __init__(self, *, max_attempts: int = 3) -> None:
        """创建空目录，并限制状态持续变化时的重试次数。"""
        self._max_attempts = max_attempts
        self._cache: dict[str, list[dict[str, Any]]] = {}
        self._lock = threading.Lock()
        self._revision = 0

    @property
    def revision(self) -> int:
        """返回当前插件工具目录版本。"""
        with self._lock:
            return self._revision

    def clear(self) -> None:
        """清空目录缓存并推进版本号。"""
        with self._lock:
            self._cache.clear()
            self._revision += 1

    def get(
        self,
        running_plugins: Mapping[str, Any],
        *,
        plugin_id: Optional[str] = None,
        log: Any,
    ) -> list[dict[str, Any]]:
        """返回指定插件或全部运行插件的工具声明快照。"""
        cache_key = plugin_id or "__all__"
        for _attempt in range(self._max_attempts):
            with self._lock:
                cache_revision = self._revision
                cached = self._cache.get(cache_key)
            if cached is not None:
                return self.copy(cached)

            tools_info = []
            for current_id, plugin in dict(running_plugins).items():
                if plugin_id and plugin_id != current_id:
                    continue
                if not supports_plugin_hook(plugin, "get_agent_tools"):
                    continue
                try:
                    if not plugin.get_state():
                        continue
                    tools = plugin.get_agent_tools()
                    if tools:
                        tools_info.append({
                            "plugin_id": current_id,
                            "plugin_name": plugin.plugin_name,
                            "tools": tools,
                        })
                except Exception as err:
                    log.error(
                        f"获取插件 {current_id} 智能体工具出错：{str(err)}"
                    )
            with self._lock:
                if cache_revision != self._revision:
                    continue
                self._cache[cache_key] = self.copy(tools_info)
                return tools_info
        raise RuntimeError("插件工具注册表持续变化，无法建立当前快照")

    @staticmethod
    def copy(tools_info: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """复制工具注册信息，避免调用方修改缓存内容。"""
        return [
            {
                **plugin_info,
                "tools": list(plugin_info.get("tools", [])),
            }
            for plugin_info in tools_info
        ]
