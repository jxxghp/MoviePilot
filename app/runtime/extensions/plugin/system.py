"""插件市场、包和依赖系统能力的运行时注入端口。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Optional


class PluginSystemServices:
    """保存由启动组合根注入的插件外部系统适配器。"""

    def __init__(
        self,
        *,
        market: Any,
        package: Any,
        dependency: Any,
        compatible_flags: Callable[[Optional[str]], list[str]],
        frozen: Callable[[], bool],
    ) -> None:
        """记录市场、包、依赖和代际兼容计算端口。"""
        self.market = market
        self.package = package
        self.dependency = dependency
        self.compatible_flags = compatible_flags
        self.frozen = frozen

    def local_repo_paths(self) -> list[Path]:
        """返回可监测的本地插件仓库路径。"""
        return self.market.get_local_repo_paths()

    def local_candidate(self, plugin_id: str, **kwargs: Any) -> Optional[dict]:
        """读取指定本地插件候选。"""
        return self.market.get_local_candidate(plugin_id, **kwargs)

    def local_candidates(self) -> dict[str, dict]:
        """读取全部本地插件候选。"""
        return self.market.get_local_candidates()

    def local_repo_url(
        self,
        plugin_id: str,
        repo_path: Optional[object] = None,
        package_version: Optional[str] = None,
    ) -> str:
        """构造本地插件来源标识。"""
        return self.market.make_local_repo_url(
            plugin_id,
            repo_path,
            package_version,
        )

    def annotate_system_version(self, plugin_info: dict) -> dict:
        """补充插件条目的主程序版本兼容信息。"""
        return self.market.annotate_system_version(plugin_info)

    def is_package_compatible(self, plugin_info: dict, package_version: str) -> bool:
        """判断插件条目是否兼容指定代际。"""
        return self.market.is_package_compatible(plugin_info, package_version)

    def is_frozen(self) -> bool:
        """判断当前宿主是否为不可写的冻结运行模式。"""
        return self.frozen()


_services: Optional[PluginSystemServices] = None


def configure_plugin_system(services: PluginSystemServices) -> None:
    """由启动组合根装配插件外部系统能力。"""
    global _services
    _services = services


def reset_plugin_system() -> None:
    """清除已装配服务，仅供隔离测试恢复进程状态。"""
    global _services
    _services = None


def get_plugin_system() -> PluginSystemServices:
    """返回已装配的插件外部系统端口。"""
    if _services is None:
        raise RuntimeError("插件外部系统服务尚未由启动组合根装配")
    return _services
