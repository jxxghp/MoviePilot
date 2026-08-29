"""插件市场、包和依赖系统能力的运行时注入端口。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Optional, cast


class PluginSystemServices:
    """保存由启动组合根注入的插件外部系统适配器。"""

    def __init__(
        self,
        *,
        market: Any,
        package: Any,
        dependency: Any,
        dependency_manifest_status: Callable[[Path], Optional[bool]],
        compatible_flags: Callable[[Optional[str]], list[str]],
        frozen: Callable[[], bool],
        install: Callable[..., tuple[bool, str]],
    ) -> None:
        """记录市场、包、安装 Gateway、依赖和代际兼容计算端口。"""
        self.market = market
        self.package = package
        self.dependency = dependency
        self.dependency_manifest_status = dependency_manifest_status
        self.compatible_flags = compatible_flags
        self.frozen = frozen
        self.install = install

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
        """从同步运行时线程进入宿主唯一安装 Gateway。"""
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
        """通过唯一包事务 owner 删除插件物理目录。"""
        return bool(self.package.remove_plugin(plugin_id))

    def modify_plugin_files(self, **kwargs: Any) -> tuple[bool, str]:
        """把旧分身源码改写调用收口到唯一包适配器。"""
        return cast(tuple[bool, str], self.package._modify_plugin_files(**kwargs))

    def modify_python_file(self, **kwargs: Any) -> tuple[bool, str]:
        """把旧 Python 类改写调用收口到唯一包适配器。"""
        return cast(tuple[bool, str], self.package._modify_python_file(**kwargs))

    def modify_federation_files(self, **kwargs: Any) -> tuple[bool, str]:
        """把旧联邦构建产物改写调用收口到唯一包适配器。"""
        return cast(
            tuple[bool, str],
            self.package._modify_federation_files(**kwargs),
        )

    def rename_federation_assets(
        self,
        dist_dir: Path,
        original_class_name: str,
        clone_class_name: str,
    ) -> None:
        """把旧联邦资源重命名调用收口到唯一包适配器。"""
        self.package._rename_federation_assets(
            dist_dir,
            original_class_name,
            clone_class_name,
        )


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
