"""插件可见性和特殊密钥权限策略。"""

import os
from collections.abc import Callable
from typing import Any, Optional


class PluginAccessPolicy:
    """根据站点认证等级和插件公钥判断插件是否可投影。"""

    def __init__(
        self,
        *,
        auth_level: Callable[[], int],
        verify_keys: Callable[..., bool],
        log: Any,
    ) -> None:
        """保存认证等级、密钥校验和日志端口。"""
        self._auth_level = auth_level
        self._verify_keys = verify_keys
        self._logger = log

    @staticmethod
    def private_key(plugin_id: str) -> Optional[str]:
        """按插件 ID 读取特殊密钥认证使用的环境变量。"""
        try:
            return os.environ.get(f"PLUGIN_{plugin_id.upper()}_PRIVATE_KEY")
        except Exception:
            return None

    def check(self, plugin: Any, source: Optional[Any] = None) -> bool:
        """设置插件认证等级并判断当前环境是否允许该插件。"""
        if source:
            if isinstance(source, dict) and "level" in source:
                plugin.auth_level = source.get("level")
            elif hasattr(source, "auth_level"):
                plugin.auth_level = source.auth_level
        elif not hasattr(plugin, "auth_level"):
            return True

        level = self._auth_level()
        if (
            level > 1
            and plugin.auth_level == 99
            and hasattr(plugin, "plugin_public_key")
        ):
            plugin_id = (
                getattr(plugin, "plugin_source_id", None)
                or getattr(plugin, "id", None)
                if not isinstance(plugin, type)
                else getattr(plugin, "plugin_source_id", None) or plugin.__name__
            )
            public_key = plugin.plugin_public_key
            if public_key and plugin_id:
                private_key = self.private_key(plugin_id)
                return self._verify_keys(
                    public_key=public_key,
                    private_key=private_key,
                )
        return level >= plugin.auth_level
