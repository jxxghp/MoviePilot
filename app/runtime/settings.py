"""运行时配置读取端口，供低层适配器避免反向依赖 Application。"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import Any


RuntimeSettingProvider = Callable[[str], Any]
_provider: RuntimeSettingProvider | None = None


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
