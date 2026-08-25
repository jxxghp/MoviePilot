"""插件本地运行态和远程市场目录投影。"""

from __future__ import annotations

import importlib.util
from collections.abc import Callable, Mapping
from typing import Any, Optional

from app.foundation.version import compare_version
from app.runtime.settings import RuntimeSettingsCompat

settings = RuntimeSettingsCompat()
from app.runtime.extensions.plugin.contracts import supports_plugin_hook
from app.runtime.extensions.plugin.storage import PluginStorage
from app.runtime.extensions.plugin.system import PluginSystemServices
from app.schemas.plugin import Plugin, PluginInstance, PluginRuntimeStatus
from app.schemas.types import SystemConfigKey


class PluginCatalogFacade:
    """把插件目录应用服务与运行态注册表连接起来。"""

    def __init__(
        self,
        *,
        classes: Callable[[], Mapping[str, Any]],
        running: Callable[[], Mapping[str, Any]],
        storage: Callable[[], PluginStorage],
        system: Callable[[], PluginSystemServices],
        market_catalog: Callable[[], Any],
        market_loader: Callable[..., Any],
        async_market_loader: Callable[..., Any],
        map_plugin: Callable[..., Optional[Plugin]],
        auth_checker: Callable[..., bool],
        plugin_attr: Callable[[str, str], Any],
        plugin_instance: Callable[[str], Optional[PluginInstance]],
        plugin_instances: Callable[[], dict[str, PluginInstance]],
        runtime_status: Callable[[str], Optional[PluginRuntimeStatus]],
        log: Any,
    ) -> None:
        """保存注册表、目录服务和插件外部系统端口。"""
        self._classes = classes
        self._running = running
        self._storage = storage
        self._system = system
        self._market_catalog = market_catalog
        self._market_loader = market_loader
        self._async_market_loader = async_market_loader
        self._map_plugin = map_plugin
        self._auth_checker = auth_checker
        self._plugin_attr = plugin_attr
        self._plugin_instance = plugin_instance
        self._plugin_instances = plugin_instances
        self._runtime_status = runtime_status
        self._logger = log

    def online(self, force: bool = False) -> list[Plugin]:
        """读取所有兼容代际的在线插件目录。"""
        if not settings.PLUGIN_MARKET:
            return []
        markets = [item for item in settings.PLUGIN_MARKET.split(",") if item]
        result = self._market_catalog().collect(
            markets=markets,
            compatible_flags=self._system().compatible_flags(settings.VERSION_FLAG),
            force=force,
            loader=self._market_loader,
        )
        self._logger.info(f"获取到 {len(result)} 个线上插件")
        return result

    def local(self) -> list[Plugin]:
        """把已加载插件投影为本地插件目录 DTO。"""
        installed = self._installed_ids()
        plugins: list[Plugin] = []
        for plugin_id, plugin_class in self._classes().items():
            plugin_instance = self._running().get(plugin_id)
            instance = self._plugin_instance(plugin_id)
            plugin = Plugin(
                id=plugin_id,
                installed=plugin_id in installed,
                state=self._safe_state(plugin_id, plugin_instance),
                runtime_status=self._runtime_status(plugin_id),
                has_page=supports_plugin_hook(plugin_class, "get_page"),
                plugin_public_key=getattr(plugin_class, "plugin_public_key", None),
                plugin_name=getattr(plugin_class, "plugin_name", None),
                plugin_desc=getattr(plugin_class, "plugin_desc", None),
                plugin_version=getattr(plugin_class, "plugin_version", None),
                plugin_icon=getattr(plugin_class, "plugin_icon", None),
                plugin_author=getattr(plugin_class, "plugin_author", None),
                author_url=getattr(plugin_class, "author_url", None),
                plugin_order=getattr(plugin_class, "plugin_order", 0),
                has_update=False,
                is_local=True,
                source_plugin_id=getattr(plugin_class, "plugin_source_id", None),
                is_instance=instance is not None,
                instance_mode=instance.mode if instance else None,
            )
            if not self._auth_checker(plugin=plugin, source=plugin_class):
                continue
            plugins.append(plugin)
        plugins.sort(key=lambda item: getattr(item, "plugin_order", 0))
        return plugins

    def installed(self) -> list[Plugin]:
        """按安装清单投影插件，未加载项目仍返回可观察占位卡片。"""
        installed_ids = self._installed_ids()
        local_by_id = {
            plugin.id: plugin
            for plugin in self.local()
            if plugin.installed and plugin.id
        }
        result = []
        for plugin_id in installed_ids:
            plugin = local_by_id.get(plugin_id)
            if plugin:
                result.append(plugin)
                continue
            instance = self._plugin_instance(plugin_id)
            result.append(Plugin(
                id=plugin_id,
                plugin_name=plugin_id,
                installed=True,
                state=False,
                runtime_status=self._runtime_status(plugin_id),
                is_local=True,
                source_plugin_id=(
                    instance.source_plugin_id if instance else None
                ),
                is_instance=instance is not None,
                instance_mode=instance.mode if instance else None,
            ))
        # 展示顺序由持久化安装清单保留，避免后台恢复或占位卡片出现后改变用户看到的位置。
        # 前端可用用户级 PluginOrder 覆盖，plugin_order 只用于运行期插件发现顺序。
        return result

    def local_version(self, plugin_id: str) -> Optional[str]:
        """读取指定已安装插件版本，不触发全量目录投影。"""
        installed = self._installed_ids()
        if plugin_id not in installed:
            return None
        plugin_class = self._classes().get(plugin_id)
        return getattr(plugin_class, "plugin_version", None)

    def local_repository(self) -> list[Plugin]:
        """读取本地插件仓候选并映射为目录 DTO。"""
        installed = self._storage().read(SystemConfigKey.UserInstalledPlugins) or []
        candidates = self._system().local_candidates()
        plugins: list[Plugin] = []
        for plugin_id, info in candidates.items():
            package_version = info.get("package_version")
            plugin = self._map_plugin(
                pid=plugin_id,
                plugin_info=info,
                market=self._system().local_repo_url(
                    plugin_id,
                    None,
                    package_version,
                ),
                installed_apps=installed,
                add_time=0,
                package_version=package_version,
            )
            if plugin:
                plugin.is_local = True
                plugins.append(plugin)
        plugins.sort(key=lambda item: getattr(item, "plugin_order", 0))
        self._logger.info(f"获取到 {len(plugins)} 个本地插件")
        return plugins

    def exists(self, plugin_id: str, version: Optional[str] = None) -> bool:
        """判断插件包和已加载版本是否满足安装前置条件。"""
        if not plugin_id:
            return False
        try:
            instance = self._plugin_instance(plugin_id)
            source_plugin_id = (
                instance.source_plugin_id if instance else plugin_id
            )
            package_name = f"app.plugins.{source_plugin_id.lower()}"
            spec = importlib.util.find_spec(package_name)
            if spec is None or spec.origin is None:
                return False
            local_version = self._plugin_attr(plugin_id, "plugin_version")
            if not local_version and instance:
                local_version = self._plugin_attr(
                    instance.source_plugin_id,
                    "plugin_version",
                )
            if not local_version:
                return False
            if version and not compare_version(local_version, ">=", version):
                self._logger.warning(
                    f"Plugin {plugin_id} version: {local_version} "
                    f"(older than version: {version})"
                )
                return False
            return True
        except Exception as error:
            self._logger.debug(f"获取插件是否在本地包中存在失败，{error}")
            return False

    def _installed_ids(self) -> list[str]:
        """合并物理安装清单和虚拟实例清单并保持各自持久化顺序。"""
        installed = list(
            self._storage().read(SystemConfigKey.UserInstalledPlugins) or []
        )
        for instance_id in self._plugin_instances():
            if instance_id not in installed:
                installed.append(instance_id)
        return installed

    def get_from_market(
        self,
        market: str,
        package_version: Optional[str] = None,
        force: bool = False,
    ) -> list[Plugin]:
        """读取并映射指定插件市场。"""
        return self._market_catalog().load(market, package_version, force)

    async def async_online(
        self,
        force: bool = False,
        progress_callback: Optional[Callable[..., None]] = None,
    ) -> list[Plugin]:
        """异步读取所有兼容代际的在线插件目录。"""
        if not settings.PLUGIN_MARKET:
            if progress_callback:
                progress_callback(value=100, text="未配置插件市场，跳过刷新")
            return []
        markets = [item for item in settings.PLUGIN_MARKET.split(",") if item]
        result = await self._market_catalog().async_collect(
            markets=markets,
            compatible_flags=self._system().compatible_flags(settings.VERSION_FLAG),
            force=force,
            loader=self._async_market_loader,
            progress_callback=progress_callback,
        )
        self._logger.info(f"获取到 {len(result)} 个线上插件")
        return result

    async def async_get_from_market(
        self,
        market: str,
        package_version: Optional[str] = None,
        force: bool = False,
    ) -> list[Plugin]:
        """异步读取并映射指定插件市场。"""
        return await self._market_catalog().async_load(
            market,
            package_version,
            force,
        )

    def merge(self, higher: list[Plugin], base: list[Plugin]) -> list[Plugin]:
        """合并不同代际插件目录并保留市场优先级。"""
        markets = [item for item in settings.PLUGIN_MARKET.split(",") if item]
        return self._market_catalog().merge(higher, base, markets)

    def _safe_state(self, plugin_id: str, plugin: Any) -> bool:
        """读取插件状态，单个插件异常不阻断整个本地目录。"""
        if not plugin or not hasattr(plugin, "get_state"):
            return False
        try:
            return bool(plugin.get_state())
        except Exception as error:
            self._logger.error(f"获取插件 {plugin_id} 状态出错：{error}")
            return False
