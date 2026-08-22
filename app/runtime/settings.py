"""运行时配置读取端口，供低层适配器避免反向依赖 Application。"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import Any


RuntimeSettingProvider = Callable[[str], Any]
_provider: RuntimeSettingProvider | None = None
_runtime_settings_service: Any | None = None
# 测试和插件可能临时替换某个模块上的 importlib.import_module；保存原始函数，
# 让兼容代理的 legacy Settings 解析不受这类局部替身影响。
_import_module = importlib.import_module


class RuntimeSettingsCompat:
    """为旧模块级 Settings 访问提供动态 runtime 配置代理。"""

    @staticmethod
    def _legacy_settings() -> Any:
        """返回旧 Settings 实例，供 runtime 尚未装配时的兼容回退使用。"""
        return _import_module("app.runtime.config").settings

    def __getattr__(self, key: str) -> Any:
        """读取当前组合根配置；未装配时沿用旧 Settings 回退。"""
        return get_runtime_setting(key)

    def __setattr__(self, key: str, value: Any) -> None:
        """把旧模块级覆盖同步到 legacy Settings，保持测试和插件注入语义。"""
        setattr(self._legacy_settings(), key, value)

    def __delattr__(self, key: str) -> None:
        """删除旧模块级覆盖，使配置对象恢复其原有属性解析。"""
        delattr(self._legacy_settings(), key)

    def model_dump(
        self,
        *,
        include: set[str] | None = None,
        exclude: set[str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """导出当前配置快照，保留旧 Settings 的序列化入口。"""
        if _runtime_settings_service is not None:
            return _runtime_settings_service.snapshot(include=include, exclude=exclude)
        return self._legacy_settings().model_dump(
            include=include, exclude=exclude, **kwargs
        )

    def update_setting(self, key: str, value: Any) -> tuple[Any, str]:
        """更新单项配置，兼容插件对模块级 Settings 的公开调用。"""
        if _runtime_settings_service is not None:
            return _runtime_settings_service.update(key, value)
        return self._legacy_settings().update_setting(key, value)

    def update_settings(self, env: dict[str, Any]) -> dict[str, tuple[Any, str]]:
        """批量更新配置，兼容旧 Settings 的管理接口。"""
        if _runtime_settings_service is not None:
            return _runtime_settings_service.update_many(env)
        return self._legacy_settings().update_settings(env=env)


def configure_runtime_settings_compat(service: Any) -> None:
    """由应用组合根注入可变配置服务，避免低层代理反向导入应用层。"""
    global _runtime_settings_service
    _runtime_settings_service = service


def configure_runtime_setting_provider(provider: RuntimeSettingProvider) -> None:
    """由启动组合根登记配置读取器，保持适配器只依赖 runtime 端口。"""
    global _provider
    _provider = provider


def get_runtime_setting(key: str) -> Any:
    """读取单项运行配置；启动早期未装配时回退旧 Settings ABI。"""
    if _provider is not None:
        return _provider(key)
    return getattr(RuntimeSettingsCompat._legacy_settings(), key)
