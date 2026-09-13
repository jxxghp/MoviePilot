"""插件本地运行态和远程市场目录投影。"""

from __future__ import annotations

import importlib.util
from collections.abc import Callable, Mapping
from typing import Any, Optional

from app.domain.plugin import split_plugin_market_repo_urls
from app.foundation.version import compare_version
from app.runtime.extensions.plugin.contracts import supports_plugin_hook
from app.runtime.extensions.plugin.storage import PluginStorage
from app.runtime.extensions.plugin.system import PluginSystemServices
from app.runtime.log import get_plugin_instance_log_level_override
from app.runtime.settings import get_runtime_setting
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
        host_instances: Callable[[], dict[str, PluginInstance]],
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
        self._host_instances = host_instances
        self._runtime_status = runtime_status
        self._logger = log

    def online(self, force: bool = False) -> list[Plugin]:
        """读取所有兼容代际的在线插件目录。"""
        plugin_market = get_runtime_setting('PLUGIN_MARKET')
        if not plugin_market:
            return []
        markets = split_plugin_market_repo_urls(plugin_market)
        result = self._market_catalog().collect(
            markets=markets,
            compatible_flags=self._system().compatible_flags(
                get_runtime_setting('VERSION_FLAG')
            ),
            force=force,
            loader=self._market_loader,
        )
        self._logger.info(f"获取到 {len(result)} 个线上插件")
        return result

    def local(self) -> list[Plugin]:
        """把已加载插件投影为本地插件目录 DTO。"""
        installed = self._installed_ids()
        # 本体行一次取全：逐张卡片各查一次会让插件列表的查询次数随插件数线性增长
        host_instances = self._host_instances()
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
                **self._instance_overlay(plugin_id, instance or host_instances.get(plugin_id)),
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
        host_instances = self._host_instances()
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
                **self._instance_overlay(plugin_id, instance or host_instances.get(plugin_id)),
            ))
        # 展示顺序由持久化安装清单保留，避免后台恢复或占位卡片出现后改变用户看到的位置。
        # 前端可用用户级 PluginOrder 覆盖，plugin_order 只用于运行期插件发现顺序。
        return result

    @staticmethod
    def _instance_overlay(
        instance_id: str,
        record: Optional[PluginInstance],
    ) -> dict[str, Any]:
        """把该实例的默认目标置位与日志等级覆盖投影为卡片列表的只读叠加字段。

        日志等级直接按实例 ID 查进程内覆盖缓存，不回表：本体与分身在覆盖表里共用
        同一个命名空间，而卡片的 ``plugin_id`` 本身就是运行实例的 ID，本体等于插件
        ID、分身等于分身实例 ID，再去查一次实例行只会为同一个键多走一趟数据库。
        默认目标置位与启用位是落盘状态、进程内没有副本，因而由调用方把已经取到的
        实例行（分身来自遍历，本体来自批量取到的字典）传进来，同样不额外发查询；
        没有对应行时按未置位、未启用处理——没有这一行就意味着它不会被装载，也不可能
        是默认调用目标。

        :param instance_id: 运行实例 ID
        :param record: 该实例已在内存中的实例行，没有登记过时为 None
        :return: 可直接展开进 ``Plugin(...)`` 构造参数的字段字典
        """
        override = get_plugin_instance_log_level_override(instance_id)
        return {
            "is_default_target": record.is_default_target if record else False,
            "is_enabled": record.is_enabled if record else False,
            "log_level_effective": override[0] if override is not None else None,
        }

    def local_version(self, plugin_id: str) -> Optional[str]:
        """读取指定已安装插件版本，不触发全量目录投影。"""
        installed = self._installed_ids()
        if plugin_id not in installed:
            return None
        plugin_class = self._classes().get(plugin_id)
        return getattr(plugin_class, "plugin_version", None)

    def local_repository(self, *, raise_errors: bool = False) -> list[Plugin]:
        """读取本地插件仓候选并映射为目录 DTO。"""
        installed = self._storage().read(SystemConfigKey.UserInstalledPlugins) or []
        try:
            candidates = self._system().local_candidates()
        except Exception as error:  # noqa: BLE001 - 展示失败不能阻断整个插件目录
            self._logger.warning(f"读取本地插件仓候选失败，已跳过本地目录展示：{error}")
            if raise_errors:
                raise
            return []
        plugins: list[Plugin] = []
        for plugin_id, info in candidates.items():
            package_version = info.get("package_version")
            repo_url = info.get("repo_url")
            if not isinstance(repo_url, str) or not repo_url.startswith("local://"):
                repo_url = self._system().local_repo_url(
                    plugin_id,
                    info.get("repo_path"),
                    package_version,
                )
            plugin = self._map_plugin(
                pid=plugin_id,
                plugin_info=info,
                market=repo_url,
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
        plugin_market = get_runtime_setting('PLUGIN_MARKET')
        if not plugin_market:
            if progress_callback:
                progress_callback(value=100, text="未配置插件市场，跳过刷新")
            return []
        markets = split_plugin_market_repo_urls(plugin_market)
        result = await self._market_catalog().async_collect(
            markets=markets,
            compatible_flags=self._system().compatible_flags(
                get_runtime_setting('VERSION_FLAG')
            ),
            force=force,
            loader=self._async_market_loader,
            progress_callback=progress_callback,
        )
        self._logger.info(f"获取到 {len(result)} 个线上插件")
        return result

    async def async_online_candidates(self, force: bool = False) -> list[Plugin]:
        """读取在线目录并保留每个仓库的最高候选，供来源准入使用。"""
        plugin_market = get_runtime_setting('PLUGIN_MARKET')
        if not plugin_market:
            return []
        markets = split_plugin_market_repo_urls(plugin_market)
        result: list[Plugin] = await self._market_catalog().async_collect(
            markets=markets,
            compatible_flags=self._system().compatible_flags(
                get_runtime_setting('VERSION_FLAG')
            ),
            force=force,
            loader=self._async_market_loader,
            preserve_sources=True,
        )
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
        plugin_market = get_runtime_setting('PLUGIN_MARKET')
        markets = split_plugin_market_repo_urls(plugin_market)
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
