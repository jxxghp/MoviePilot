"""插件市场历史兼容门面。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import List, Optional, Tuple

from app.adapters.external.plugin.client import PluginMarketTransport, is_local_repo_url
from app.adapters.system.plugin.dependency import PluginDependencyInstaller
from app.adapters.system.plugin.health import PluginRuntimeHealth
from app.foundation.singleton import WeakSingleton
from app.runtime.observability import observe_compat_facade
from app.runtime.settings import get_runtime_setting

PLUGIN_DIR = Path(get_runtime_setting('ROOT_PATH')) / "app" / "plugins"
LOCAL_REPO_PREFIX = "local://"
PLUGIN_SYSTEM_VERSION_FIELD = "system_version"

InstalledPluginsProvider = Callable[[], List[str]]
PluginInstallGateway = Callable[[str, str, Optional[str], Optional[str], bool], Tuple[bool, str]]
AsyncPluginInstallGateway = Callable[[str, str, Optional[str], Optional[str], bool], Awaitable[Tuple[bool, str]]]


def _empty_installed_plugins() -> List[str]:
    """组合根尚未注入配置读取器时返回空安装清单。"""
    return []


def _unconfigured_plugin_install_gateway(
    _pid: str, _repo_url: str, _package_version: Optional[str],
    _release_version: Optional[str], _force_install: bool,
) -> Tuple[bool, str]:
    """在组合根尚未装配来源门禁时拒绝插件包写入。"""
    return False, "插件安装服务尚未完成初始化"


async def _unconfigured_async_plugin_install_gateway(
    _pid: str, _repo_url: str, _package_version: Optional[str],
    _release_version: Optional[str], _force_install: bool,
) -> Tuple[bool, str]:
    """在组合根尚未装配来源门禁时拒绝异步插件包写入。"""
    return False, "插件安装服务尚未完成初始化"


_installed_plugins_provider: InstalledPluginsProvider = _empty_installed_plugins
_plugin_install_gateway: PluginInstallGateway = _unconfigured_plugin_install_gateway
_async_plugin_install_gateway: AsyncPluginInstallGateway = _unconfigured_async_plugin_install_gateway
_plugin_market_transport = PluginMarketTransport()
_plugin_runtime_health = PluginRuntimeHealth()


def configure_installed_plugins_provider(provider: InstalledPluginsProvider) -> None:
    """由启动组合层注入已安装插件读取器，避免兼容门面访问数据库。"""
    global _installed_plugins_provider
    _installed_plugins_provider = provider


def configure_plugin_install_gateway(
    *, install: PluginInstallGateway, async_install: AsyncPluginInstallGateway,
) -> None:
    """由启动组合根装配公开兼容安装入口的来源门禁。"""
    global _plugin_install_gateway, _async_plugin_install_gateway
    _plugin_install_gateway = install
    _async_plugin_install_gateway = async_install


def reset_plugin_install_gateway() -> None:
    """恢复未装配状态，供隔离测试清理进程级安装入口。"""
    global _plugin_install_gateway, _async_plugin_install_gateway
    _plugin_install_gateway = _unconfigured_plugin_install_gateway
    _async_plugin_install_gateway = _unconfigured_async_plugin_install_gateway


@observe_compat_facade("PluginHelper")
class PluginHelper(metaclass=WeakSingleton):
    """保留第三方插件公开 ABI，并把行为委托给独立 owner。"""

    PLUGIN_DEPENDENCY_INSTALL_TIMEOUT = PluginRuntimeHealth.PLUGIN_DEPENDENCY_INSTALL_TIMEOUT

    is_local_repo_url = staticmethod(is_local_repo_url)
    make_local_repo_url = staticmethod(PluginMarketTransport.make_local_repo_url)
    parse_local_repo_url = staticmethod(PluginMarketTransport.parse_local_repo_url)
    parse_local_repo_path = staticmethod(PluginMarketTransport.parse_local_repo_path)
    parse_local_repo_package_version = staticmethod(PluginMarketTransport.parse_local_repo_package_version)
    get_current_system_version = staticmethod(PluginMarketTransport.get_current_system_version)
    get_compatible_version_flags = staticmethod(PluginMarketTransport.get_compatible_version_flags)
    is_plugin_info_compatible = staticmethod(PluginMarketTransport.is_plugin_info_compatible)
    is_package_plugin_compatible = staticmethod(PluginMarketTransport.is_package_plugin_compatible)
    check_plugin_system_version = staticmethod(PluginMarketTransport.check_plugin_system_version)
    annotate_plugin_system_version = staticmethod(PluginMarketTransport.annotate_plugin_system_version)
    get_local_repo_paths = staticmethod(PluginMarketTransport.get_local_repo_paths)
    get_local_plugin_candidates = staticmethod(_plugin_market_transport.get_local_plugin_candidates)
    get_local_plugin_candidate = staticmethod(_plugin_market_transport.get_local_plugin_candidate)
    get_plugin_index_result = staticmethod(_plugin_market_transport.get_plugin_index_result)
    get_plugins = staticmethod(_plugin_market_transport.get_plugins)
    get_plugin_release_versions = staticmethod(_plugin_market_transport.get_plugin_release_versions)
    get_plugin_package_version = staticmethod(_plugin_market_transport.get_plugin_package_version)
    get_repo_info = staticmethod(PluginMarketTransport.get_repo_info)
    request_with_fallback = staticmethod(PluginMarketTransport.request_with_fallback)
    get_plugin_system_version_check_message = staticmethod(
        _plugin_market_transport.get_plugin_system_version_check_message
    )
    async_get_plugin_package_version = staticmethod(
        _plugin_market_transport.async_get_plugin_package_version
    )
    async_request_with_fallback = staticmethod(
        PluginMarketTransport.async_request_with_fallback
    )
    async_get_plugin_index_result = staticmethod(
        _plugin_market_transport.async_get_plugin_index_result
    )
    async_get_plugins = staticmethod(_plugin_market_transport.async_get_plugins)
    async_get_plugin_release_versions = staticmethod(
        _plugin_market_transport.async_get_plugin_release_versions
    )
    async_has_plugin_release_cache = staticmethod(
        _plugin_market_transport.async_has_plugin_release_cache
    )
    async_get_plugin_system_version_check_message = staticmethod(
        _plugin_market_transport.async_get_plugin_system_version_check_message
    )

    def install(
        self, pid: str, repo_url: str, package_version: Optional[str] = None,
        release_version: Optional[str] = None, force_install: bool = False,
    ) -> Tuple[bool, str]:
        """通过宿主统一 Gateway 安装插件。"""
        return _plugin_install_gateway(pid, repo_url, package_version, release_version, force_install)

    def install_local(
        self, pid: str, repo_url: str = "", force_install: bool = False,
    ) -> Tuple[bool, str]:
        """通过宿主统一 Gateway 安装本地插件。"""
        target_repo = repo_url or self.make_local_repo_url(pid)
        return _plugin_install_gateway(
            pid, target_repo, self.parse_local_repo_package_version(target_repo),
            None, force_install,
        )

    @classmethod
    def install_packages_with_fallback(
        cls, dependency_files: Path | Sequence[Path],
        find_links_dirs: Optional[List[Path]] = None,
    ) -> Tuple[bool, str]:
        """把兼容入口委托给系统运行环境 owner。"""
        return _plugin_runtime_health.install_packages_with_fallback(
            dependency_files, find_links_dirs
        )

    def find_missing_dependencies(self) -> List[str]:
        """把兼容入口委托给依赖聚合 owner。"""
        return PluginDependencyInstaller(
            _plugin_runtime_health, installed_plugins_provider=_installed_plugins_provider,
            plugin_dir=PLUGIN_DIR,
        ).find_missing()

    def install_dependencies(self, dependencies: List[str]) -> Tuple[bool, str]:
        """把兼容入口委托给依赖聚合 owner。"""
        return PluginDependencyInstaller(
            _plugin_runtime_health, installed_plugins_provider=_installed_plugins_provider,
            plugin_dir=PLUGIN_DIR,
        ).install(dependencies)

    @classmethod
    async def async_install_packages_with_fallback(
        cls, dependency_files: Path | Sequence[Path],
        find_links_dirs: Optional[List[Path]] = None,
    ) -> Tuple[bool, str]:
        """把异步兼容入口委托给系统运行环境 owner。"""
        return await _plugin_runtime_health.async_install_packages_with_fallback(
            dependency_files, find_links_dirs
        )

    async def async_install_dependencies(
        self, dependencies: List[str],
    ) -> Tuple[bool, str]:
        """把异步兼容入口委托给依赖聚合 owner。"""
        return await PluginDependencyInstaller(
            _plugin_runtime_health, installed_plugins_provider=_installed_plugins_provider,
            plugin_dir=PLUGIN_DIR,
        ).async_install(dependencies)

    async def async_find_missing_dependencies(self) -> List[str]:
        """把异步兼容入口委托给依赖聚合 owner。"""
        return await PluginDependencyInstaller(
            _plugin_runtime_health, installed_plugins_provider=_installed_plugins_provider,
            plugin_dir=PLUGIN_DIR,
        ).async_find_missing()

    async def async_install(
        self, pid: str, repo_url: str, package_version: Optional[str] = None,
        release_version: Optional[str] = None, force_install: bool = False,
    ) -> Tuple[bool, str]:
        """通过宿主统一 Gateway 异步安装插件。"""
        return await _async_plugin_install_gateway(
            pid, repo_url, package_version, release_version, force_install
        )


setattr(
    PluginHelper.get_plugin_release_versions,
    "cache_clear",
    getattr(_plugin_market_transport._get_plugin_repo_releases, "cache_clear"),
)
setattr(
    PluginHelper.get_plugin_release_versions,
    "cache_region",
    getattr(_plugin_market_transport._get_plugin_repo_releases, "cache_region"),
)
setattr(
    PluginHelper.async_get_plugin_release_versions,
    "cache_clear",
    getattr(
        _plugin_market_transport._async_get_plugin_repo_releases,
        "cache_clear",
    ),
)
setattr(
    PluginHelper.async_get_plugin_release_versions,
    "cache_region",
    getattr(
        _plugin_market_transport._async_get_plugin_repo_releases,
        "cache_region",
    ),
)
