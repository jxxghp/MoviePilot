# -*- coding: utf-8 -*-
"""
插件安装上报器（reporter）：core/plugin 在插件安装成功后经此上报安装统计。

具体实现（MoviePilotServerHelper.install_plugin_reg，依赖 db / http / 外部 MP 统计服务器）由组合根在
启动时注入，core 自身不直接依赖 helper：

  - 由组合根（app/startup/lifecycle.py）注入 MoviePilotServerHelper.install_plugin_reg;
  - 未注册时为 no-op（与 settings.PLUGIN_STATISTIC_SHARE=False 时跳过上报的语义一致,
    且上报本就是 fire-and-forget,调用方忽略返回值）;
  - 本模块仅依赖 typing。
"""
from typing import Any, Callable, Optional

_install_reporter: Optional[Callable[..., Any]] = None


def set_plugin_install_reporter(reporter: Callable[..., Any]) -> None:
    """
    注册插件安装上报器（由组合根调用）。
    """
    global _install_reporter
    _install_reporter = reporter


def report_plugin_install(plugin_id: str, repo_url: Optional[str] = None) -> Any:
    """
    上报单个插件安装统计；未注册 reporter 时为 no-op，返回 None。
    """
    if _install_reporter is None:
        return None
    return _install_reporter(plugin_id=plugin_id, repo_url=repo_url)
