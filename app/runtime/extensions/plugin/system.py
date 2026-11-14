"""
插件市场、包和依赖系统能力的运行时注入端口。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Optional, Protocol


class PluginMarketPort(Protocol):
    """
    声明运行时所需的市场读取和兼容标注能力。
    """

    def get_local_repo_paths(self) -> list[Path]:
        """
        返回可监测的本地仓库路径。

        :return: 可以由文件监控器监听的本地仓库路径列表
        """
        ...

    def get_local_candidate(
        self, plugin_id: str, **kwargs: Any,
    ) -> Optional[dict[str, Any]]:
        """
        按插件 ID 和可选来源筛选返回本地候选。

        :param plugin_id: 目标插件标识
        :param kwargs: 进一步限定本地来源和插件代际的筛选参数
        :return: 匹配的本地插件条目；没有候选时返回 None
        """
        ...

    def get_local_candidates(self) -> dict[str, dict[str, Any]]:
        """
        返回以插件 ID 为键的全部本地候选。

        :return: 以插件标识为键的本地候选条目映射
        """
        ...

    def make_local_repo_url(
        self, plugin_id: str, repo_path: Optional[object] = None,
        package_version: Optional[str] = None,
    ) -> str:
        """
        生成保留来源路径与插件代际的本地标识。

        :param plugin_id: 目标插件标识
        :param repo_path: 要编码的本地仓库路径；为 None 时由市场实现选择来源
        :param package_version: 目标插件代际；可空参数为 None 时由市场实现选择
        :return: 包含本地来源与插件代际的仓库标识
        """
        ...

    def annotate_system_version(self, plugin_info: dict[str, Any]) -> dict[str, Any]:
        """
        补充主程序版本兼容状态并返回该条目。

        :param plugin_info: 需要补充兼容性信息的插件条目，可被实现方原地更新
        :return: 补充主程序版本兼容状态后的插件条目
        """
        ...

    def annotate_runtime_compatibility(self, plugin_info: dict[str, Any]) -> dict[str, Any]:
        """
        补充运行时兼容状态并返回该条目。

        :param plugin_info: 需要补充兼容性信息的插件条目，可被实现方原地更新
        :return: 补充当前 Python 运行时兼容状态后的插件条目
        """
        ...

    def is_package_compatible(self, plugin_info: dict[str, Any], package_version: str) -> bool:
        """
        判断条目是否支持指定插件代际。

        :param plugin_info: 需要补充兼容性信息的插件条目，可被实现方原地更新
        :param package_version: 目标插件代际；可空参数为 None 时由市场实现选择
        :return: 插件条目支持目标代际时返回 True
        """
        ...


class PluginSystemServices:
    """
    保存由启动组合根注入的插件外部系统适配器。
    """

    def __init__(
        self,
        *,
        market: PluginMarketPort,
        package: Any,
        dependency: Any,
        dependency_manifest_status: Callable[[Path], Optional[bool]],
        compatible_flags: Callable[[Optional[str]], list[str]],
        frozen: Callable[[], bool],
        install: Callable[..., tuple[bool, str]],
    ) -> None:
        """
        记录市场、包、安装 Gateway、依赖和代际兼容计算端口。
        """
        self.market = market
        self.package = package
        self.dependency = dependency
        self.dependency_manifest_status = dependency_manifest_status
        self.compatible_flags = compatible_flags
        self.frozen = frozen
        self.install = install

    def local_repo_paths(self) -> list[Path]:
        """
        返回可监测的本地插件仓库路径。
        """
        return self.market.get_local_repo_paths()

    def local_candidate(self, plugin_id: str, **kwargs: Any) -> Optional[dict[str, Any]]:
        """
        读取指定本地插件候选。
        """
        return self.market.get_local_candidate(plugin_id, **kwargs)

    def local_candidates(self) -> dict[str, dict[str, Any]]:
        """
        读取全部本地插件候选。
        """
        return self.market.get_local_candidates()

    def local_repo_url(
        self,
        plugin_id: str,
        repo_path: Optional[object] = None,
        package_version: Optional[str] = None,
    ) -> str:
        """
        构造本地插件来源标识。
        """
        return self.market.make_local_repo_url(
            plugin_id,
            repo_path,
            package_version,
        )

    def annotate_system_version(self, plugin_info: dict[str, Any]) -> dict[str, Any]:
        """
        补充插件条目的主程序版本兼容信息。
        """
        return self.market.annotate_system_version(plugin_info)

    def annotate_runtime_compatibility(
        self, plugin_info: dict[str, Any]
    ) -> dict[str, Any]:
        """
        补充插件条目的运行时（free-threaded）兼容信息。
        """
        return self.market.annotate_runtime_compatibility(plugin_info)

    def is_package_compatible(self, plugin_info: dict[str, Any], package_version: str) -> bool:
        """
        判断插件条目是否兼容指定代际。
        """
        return self.market.is_package_compatible(plugin_info, package_version)

    def is_frozen(self) -> bool:
        """
        判断当前宿主是否为不可写的冻结运行模式。
        """
        return self.frozen()

    def install_plugin(
        self,
        *,
        plugin_id: str,
        repo_url: str | None,
        package_version: str | None = None,
        release_version: str | None = None,
        force: bool = False,
        local_sync: bool = False,
        explicit_source: bool = False,
        startup_token: object | None = None,
    ) -> tuple[bool, str]:
        """
        从同步运行时线程进入宿主唯一安装 Gateway。
        """
        return self.install(
            plugin_id=plugin_id,
            repo_url=repo_url,
            package_version=package_version,
            release_version=release_version,
            force=force,
            local_sync=local_sync,
            explicit_source=explicit_source,
            startup_token=startup_token,
        )

    def remove_plugin_package(self, plugin_id: str) -> bool:
        """
        通过唯一包事务 owner 删除插件物理目录。
        """
        return bool(self.package.remove_plugin(plugin_id))


_services: Optional[PluginSystemServices] = None


def configure_plugin_system(services: PluginSystemServices) -> None:
    """
    由启动组合根装配插件外部系统能力。
    """
    global _services
    _services = services


def reset_plugin_system() -> None:
    """
    清除已装配服务，仅供隔离测试恢复进程状态。
    """
    global _services
    _services = None


def get_plugin_system() -> PluginSystemServices:
    """
    返回已装配的插件外部系统端口。
    """
    if _services is None:
        raise RuntimeError("插件外部系统服务尚未由启动组合根装配")
    return _services
