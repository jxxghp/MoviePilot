# -*- coding: utf-8 -*-
"""
插件来源（plugin source）提供者：core/plugin 经此获取插件来源对象（PluginHelper：插件市场拉取 /
安装 / 依赖管理 / 本地仓库发现）。

  - 默认懒加载返回真实 PluginHelper 单例（函数内惰性 import，使 core 顶层不依赖 helper）；
  - 可经 set_plugin_source_provider 注入替代实现（为未来 Rust / 进程外插件宿主预留接入点）；
  - 默认未在组合根注册 provider——默认值即生产行为，待出现第二实现时再注册替代 provider。
"""
from typing import Any, Callable, Optional

_provider: Optional[Callable[[], Any]] = None


def set_plugin_source_provider(provider: Callable[[], Any]) -> None:
    """
    注入插件来源提供者（为未来替代实现预留；生产默认无需调用）。
    """
    global _provider
    _provider = provider


def get_plugin_source() -> Any:
    """
    获取插件来源对象。已注入 provider 时用之；否则懒加载返回真实 PluginHelper 单例。
    """
    if _provider is not None:
        return _provider()
    # 默认:懒加载真实 PluginHelper（WeakSingleton），避免在 core 顶层 import helper
    from app.helper.plugin import PluginHelper
    return PluginHelper()


def get_version_backward_compatible_flags() -> dict:
    """
    获取可向后扫描的插件索引版本标记表。

    延迟取自插件来源实现，避免 core 顶层产生对上层的依赖。
    """
    from app.helper.plugin import VERSION_BACKWARD_COMPATIBLE_FLAGS

    return VERSION_BACKWARD_COMPATIBLE_FLAGS
