"""插件宿主运行时依赖聚合与构造。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Protocol

from app.foundation.crypto import RSAUtils
from app.foundation.version import compare_version
from app.runtime.events import eventmanager
from app.runtime.extensions.plugin.access import PluginAccessPolicy
from app.runtime.extensions.plugin.admission import PluginMutationAdmission
from app.runtime.extensions.plugin.catalog import PluginCatalogFacade
from app.runtime.extensions.plugin.classification import PluginClassificationRegistry
from app.runtime.extensions.plugin.clone import PluginCloneService
from app.runtime.extensions.plugin.contracts import supports_plugin_hook
from app.runtime.extensions.plugin.database import PluginDatabase
from app.runtime.extensions.plugin.dependency import PluginDependencyService
from app.runtime.extensions.plugin.lifecycle import PluginLifecycle
from app.runtime.extensions.plugin.loader import PluginLoader
from app.runtime.extensions.plugin.metadata import PluginMetadataMapper
from app.runtime.extensions.plugin.monitor import PluginMonitorController
from app.runtime.extensions.plugin.paths import PluginPathResolver
from app.runtime.extensions.plugin.projection import PluginProjection
from app.runtime.extensions.plugin.registry import PluginRegistry
from app.runtime.extensions.plugin.storage import (
    PluginConfigStore,
    PluginInstanceStore,
    PluginStorage,
)
from app.runtime.extensions.plugin.sync import (
    LocalPluginSyncService,
    PluginSyncService,
)
from app.runtime.extensions.plugin.system import PluginSystemServices
from app.runtime.extensions.plugin.tools import PluginToolCatalog
from app.schemas.types import SystemConfigKey


class PluginRuntimeHost(Protocol):
    """声明运行时 owner 回调宿主生命周期门面的最小合同。"""

    def reload_plugin(self, plugin_id: str) -> Any:
        """重载指定插件。"""
        ...

    def remove_plugin(self, plugin_id: str) -> Any:
        """移除指定插件运行实例。"""
        ...

    @staticmethod
    def get_plugin_remote_entry(plugin_id: str, page: str) -> str:
        """构造插件远程页面入口。"""
        ...

    def _run_file_watcher(self) -> None:
        """运行插件文件监控循环。"""
        ...

    def get_plugins_from_market(
        self,
        market: str,
        package_version: Optional[str] = None,
        force: bool = False,
    ) -> Optional[list[Any]]:
        """读取指定市场目录。"""
        ...

    async def async_get_plugins_from_market(
        self,
        market: str,
        package_version: Optional[str] = None,
        force: bool = False,
    ) -> Optional[list[Any]]:
        """异步读取指定市场目录。"""
        ...


PluginCatalogFactory = Callable[[Callable[..., Any]], Any]
PluginImportService = Callable[..., None]
PluginRemoteEntryBuilder = Callable[[str, str], str]


@dataclass(frozen=True, slots=True)
class PluginRuntimeEnvironment:
    """保存由组合根提供的插件运行时外部端口。"""

    plugins_root: Path
    storage: Callable[[], PluginStorage]
    system: Callable[[], PluginSystemServices]
    database: Callable[[], PluginDatabase]
    catalog_factory: PluginCatalogFactory
    import_preparer: PluginImportService
    import_scanner: PluginImportService
    auth_level: Callable[[], int]
    remote_entry: PluginRemoteEntryBuilder
    development: Callable[[], bool]
    logger: Any


@dataclass(frozen=True, slots=True)
class PluginRuntime:
    """聚合一个 PluginManager 生命周期内唯一的职责 owner。"""

    registry: PluginRegistry
    instances: PluginInstanceStore
    configs: PluginConfigStore
    access: PluginAccessPolicy
    catalog: PluginCatalogFacade
    paths: PluginPathResolver
    local_sync: LocalPluginSyncService
    monitor: PluginMonitorController
    admission: PluginMutationAdmission
    dependencies: PluginDependencyService
    loader: PluginLoader
    tools: PluginToolCatalog
    lifecycle: PluginLifecycle
    metadata: PluginMetadataMapper
    sync: PluginSyncService
    clone: PluginCloneService
    projection: PluginProjection
    classification: PluginClassificationRegistry
    recent_local_sync: dict[str, float]
    system: Callable[[], PluginSystemServices]


def build_plugin_runtime(
    host: PluginRuntimeHost,
    environment: PluginRuntimeEnvironment,
    *,
    tool_build_max_attempts: int,
) -> PluginRuntime:
    """按依赖顺序构造唯一插件运行时，各业务能力仍由对应 owner 实现。"""
    registry = PluginRegistry()
    instances = PluginInstanceStore(storage=environment.storage)
    configs = PluginConfigStore(
        storage=environment.storage,
        database=environment.database,
        plugin_exists=lambda plugin_id: bool(registry.classes.get(plugin_id)),
    )
    access = PluginAccessPolicy(
        auth_level=environment.auth_level,
        verify_keys=RSAUtils.verify_rsa_keys,
        log=environment.logger,
    )
    loader = PluginLoader(
        plugins_root=environment.plugins_root,
        import_preparer=environment.import_preparer,
        import_scanner=environment.import_scanner,
        log=environment.logger,
    )
    tools = PluginToolCatalog(max_attempts=tool_build_max_attempts)
    classification = PluginClassificationRegistry(environment.logger)

    def refresh_classification(plugin_id: str, instance: Any) -> None:
        """读取插件当前媒体来源声明并替换其分类扩展注册。"""
        classification.remove(plugin_id)
        declarations = (
            instance.get_media_source() or []
            if supports_plugin_hook(instance, "get_media_source")
            else []
        )
        classification.replace(plugin_id, declarations)

    def load_plugins(
        plugin_id: Optional[str],
        installed_plugins: list[str],
        validator: Callable[[Any], bool],
    ) -> list[Any]:
        """加载物理插件或虚拟实例，并保持持久化实例顺序。"""
        if plugin_id:
            instance = instances.get(plugin_id)
            if instance:
                return loader.load_instance(instance, validator)
            return loader.load(plugin_id, installed_plugins, validator)
        plugins = loader.load(None, installed_plugins, validator)
        for instance in instances.all().values():
            plugins.extend(loader.load_instance(instance, validator))
        return plugins

    lifecycle = PluginLifecycle(
        classes=registry.classes,
        running=registry.running,
        load_plugins=load_plugins,
        installed_plugins=lambda: environment.storage().read(
            SystemConfigKey.UserInstalledPlugins
        ) or [],
        plugin_config=configs.read,
        auth_checker=lambda plugin: access.check(plugin),
        clear_modules=loader.clear_modules,
        clear_tools=tools.clear,
        enable_events=eventmanager.enable_event_handler,
        disable_events=eventmanager.disable_event_handler,
        runtime_status_writer=registry.set_runtime_status,
        database=environment.database,
        log=environment.logger,
        event_sender=eventmanager.send_event,
        refresh_classification=refresh_classification,
        remove_classification=classification.remove,
    )
    metadata = PluginMetadataMapper(
        plugin_instance=registry.instance,
        plugin_class=registry.plugin_class,
        annotate_system_version=lambda info: environment.system().annotate_system_version(
            info
        ),
        is_package_compatible=lambda info, version: environment.system().is_package_compatible(
            info,
            version,
        ),
        auth_checker=lambda plugin, source: access.check(plugin, source),
        version_compare=lambda source, comparison, target: (
            compare_version(source, comparison, target) is True
        ),
        log=environment.logger,
    )
    catalog = PluginCatalogFacade(
        classes=lambda: registry.classes,
        running=lambda: registry.running,
        storage=environment.storage,
        system=environment.system,
        market_catalog=lambda: environment.catalog_factory(metadata.map),
        market_loader=lambda market, package_version=None, force=False: (
            host.get_plugins_from_market(market, package_version, force)
        ),
        async_market_loader=lambda market, package_version=None, force=False: (
            host.async_get_plugins_from_market(
                market,
                package_version,
                force,
            )
        ),
        map_plugin=lambda **kwargs: metadata.map(
            plugin_id=kwargs["pid"],
            plugin_info=kwargs["plugin_info"],
            market=kwargs["market"],
            installed_plugins=kwargs["installed_apps"],
            add_time=kwargs["add_time"],
            package_version=kwargs.get("package_version"),
        ),
        auth_checker=lambda **kwargs: access.check(**kwargs),
        plugin_attr=lambda plugin_id, attribute: getattr(
            registry.instance(plugin_id),
            attribute,
            None,
        ),
        plugin_instance=instances.get,
        plugin_instances=instances.all,
        runtime_status=registry.runtime_status,
        log=environment.logger,
    )
    paths = PluginPathResolver(
        runtime_root=environment.plugins_root,
        running=lambda: registry.running,
        system=environment.system,
        strict_system_version=lambda: not environment.development(),
        log=environment.logger,
    )
    recent_local_sync: dict[str, float] = {}
    local_sync = LocalPluginSyncService(
        installed_plugins=lambda: environment.storage().read(
            SystemConfigKey.UserInstalledPlugins
        ) or [],
        candidate=lambda plugin_id: environment.system().local_candidate(plugin_id),
        system=environment.system,
        recent_sync=recent_local_sync,
        log=environment.logger,
    )
    dependencies = PluginDependencyService(
        system=environment.system,
        instances=instances.all,
        registry=registry,
        log=environment.logger,
    )
    sync = PluginSyncService(
        frozen=lambda: environment.system().is_frozen(),
        installed_plugins=lambda: environment.storage().read(
            SystemConfigKey.UserInstalledPlugins
        ) or [],
        online_plugins=catalog.online,
        local_plugins=catalog.local_repository,
        merge_plugins=lambda higher, base, _markets: catalog.merge(higher, base),
        plugin_exists=catalog.exists,
        install=lambda plugin_id, repo_url, force, startup_token: environment.system().install_plugin(
            plugin_id=plugin_id,
            repo_url=repo_url,
            force=force,
            startup_token=startup_token,
        ),
        log=environment.logger,
    )
    def source_plugin_id(plugin_id: str) -> str:
        """把虚拟实例归一到持久化的物理源码插件。"""
        instance = instances.get(plugin_id)
        return instance.source_plugin_id if instance else plugin_id

    clone = PluginCloneService(
        plugin_class=registry.plugin_class,
        plugin_exists=catalog.exists,
        source_plugin_id=source_plugin_id,
        save_instance=instances.save,
        delete_instance=instances.delete,
        read_config=configs.read,
        save_config=lambda plugin_id, config: configs.write(
            plugin_id,
            config,
            force=True,
        ),
        delete_config=lambda plugin_id: configs.delete(plugin_id, force=True),
        reload_plugin=host.reload_plugin,
        remove_plugin=host.remove_plugin,
        log=environment.logger,
    )
    projection = PluginProjection(
        registry.running,
        environment.logger,
        environment.remote_entry,
    )
    return PluginRuntime(
        registry=registry,
        instances=instances,
        configs=configs,
        access=access,
        catalog=catalog,
        paths=paths,
        local_sync=local_sync,
        monitor=PluginMonitorController(
            runner=host._run_file_watcher,
            log=environment.logger,
        ),
        admission=PluginMutationAdmission(),
        dependencies=dependencies,
        loader=loader,
        tools=tools,
        lifecycle=lifecycle,
        metadata=metadata,
        sync=sync,
        clone=clone,
        projection=projection,
        classification=classification,
        recent_local_sync=recent_local_sync,
        system=environment.system,
    )
