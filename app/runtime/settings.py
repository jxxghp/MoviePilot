"""运行时配置读取端口，供低层适配器避免反向依赖 Application。"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import Any


RuntimeSettingProvider = Callable[[str], Any]
_provider: RuntimeSettingProvider | None = None


class RuntimeSettingsCompat:
    """为旧模块级 Settings 访问提供动态 runtime 配置代理。"""

    def __getattr__(self, key: str) -> Any:
        """读取当前组合根配置；未装配时沿用旧 Settings 回退。"""
        return get_runtime_setting(key)

    def __setattr__(self, key: str, value: Any) -> None:
        """把旧模块级覆盖同步到 legacy Settings，保持测试和插件注入语义。"""
        legacy_settings = importlib.import_module("app.runtime.config").settings
        setattr(legacy_settings, key, value)

    def __delattr__(self, key: str) -> None:
        """删除旧模块级覆盖，使配置对象恢复其原有属性解析。"""
        legacy_settings = importlib.import_module("app.runtime.config").settings
        delattr(legacy_settings, key)


def configure_runtime_setting_provider(provider: RuntimeSettingProvider) -> None:
    """由启动组合根登记配置读取器，保持适配器只依赖 runtime 端口。"""
    global _provider
    _provider = provider


def get_runtime_setting(key: str) -> Any:
    """读取单项运行配置；启动早期未装配时回退旧 Settings ABI。"""
    if _provider is not None:
        return _provider(key)
    legacy_settings = importlib.import_module("app.runtime.config").settings
    return getattr(legacy_settings, key)
