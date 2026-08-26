"""运行时配置读取端口，供低层适配器避免反向依赖 Application。"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import Any

RuntimeSettingProvider = Callable[[str], Any]
RuntimeSettingUpdater = Callable[[str, Any], tuple[Any, str]]
_provider: RuntimeSettingProvider | None = None
_updater: RuntimeSettingUpdater | None = None
_MISSING = object()
# 测试和插件可能临时替换某个模块上的 importlib.import_module；保存原始函数，
# 让启动前的 legacy Settings 回退不受这类局部替身影响。
_import_module = importlib.import_module


def _legacy_settings() -> Any:
    """返回启动前读取配置用的旧 Settings 实例。"""
    return _import_module("app.runtime.config").settings


def configure_runtime_setting_provider(provider: RuntimeSettingProvider) -> None:
    """由启动组合根登记配置读取器，保持适配器只依赖 runtime 端口。"""
    global _provider
    _provider = provider


def configure_runtime_setting_updater(updater: RuntimeSettingUpdater) -> None:
    """由启动组合根登记配置写入器，避免低层调用方依赖 Application。"""
    global _updater
    _updater = updater


def get_runtime_setting(key: str, default: Any = _MISSING) -> Any:
    """读取单项运行配置；可选默认值保留旧 `getattr` 容错语义。"""
    try:
        if _provider is not None:
            return _provider(key)
        return getattr(_legacy_settings(), key)
    except AttributeError:
        if default is _MISSING:
            raise
        return default


def update_runtime_setting(key: str, value: Any) -> tuple[Any, str]:
    """更新单项运行配置；启动早期沿用旧 Settings 的兼容写入。"""
    if _updater is not None:
        return _updater(key, value)
    return _legacy_settings().update_setting(key, value)


def has_runtime_setting(key: str) -> bool:
    """判断运行配置是否声明指定键，供低层 manifest 校验使用。"""
    if _provider is not None:
        try:
            _provider(key)
        except AttributeError:
            return False
        return True
    return hasattr(_legacy_settings(), key)
