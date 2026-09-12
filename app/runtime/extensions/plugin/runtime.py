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
from app.runtime.extensions.plugin.binding import PluginVersionBinding
from app.runtime.extensions.plugin.catalog import PluginCatalogFacade
from app.runtime.extensions.plugin.classification import PluginClassificationRegistry
from app.runtime.extensions.plugin.clone import PluginCloneService
from app.runtime.extensions.plugin.contracts import supports_plugin_hook
from app.runtime.extensions.plugin.database import PluginDatabase
from app.runtime.extensions.plugin.dependency import PluginDependencyService
from app.runtime.extensions.plugin.lifecycle import PluginLifecycle
from app.runtime.extensions.plugin.loader import PluginLoader
from app.runtime.extensions.plugin.loglevel import PluginLogLevelControl
from app.runtime.extensions.plugin.metadata import PluginMetadataMapper
from app.runtime.extensions.plugin.monitor import PluginMonitorController
from app.runtime.extensions.plugin.paths import PluginPathResolver
from app.runtime.extensions.plugin.projection import PluginProjection
from app.runtime.extensions.plugin.registry import PluginRegistry
from app.runtime.extensions.plugin.storage import (
    PluginConfigStore,
    PluginInstanceDirectory,
    PluginInstanceStore,
    PluginStorage,
)
from app.runtime.extensions.plugin.sync import (
    LocalPluginSyncService,
    PluginSyncService,
)
from app.runtime.extensions.plugin.system import PluginSystemServices
from app.runtime.extensions.plugin.target import PluginDefaultTargetControl
from app.runtime.extensions.plugin.tools import PluginToolCatalog
from app.runtime.extensions.plugin.version import (
    plugin_version_dirs,
    resolve_instance_version_dir,
)
from app.schemas.plugin import PluginInstance
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
    def get_plugin_remote_entry(
        plugin_id: str, page: str, version: Optional[str] = None
    ) -> str:
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
PluginRemoteEntryBuilder = Callable[[str, str, Optional[str]], str]
PluginMultiVersionBlockers = Callable[[str, list[Path]], list[str]]


@dataclass(frozen=True, slots=True)
class PluginRuntimeEnvironment:
    """保存由组合根提供的插件运行时外部端口。"""

    plugins_root: Path
    storage: Callable[[], PluginStorage]
    instance_directory: Callable[[], PluginInstanceDirectory]
    system: Callable[[], PluginSystemServices]
    database: Callable[[], PluginDatabase]
    catalog_factory: PluginCatalogFactory
    import_preparer: PluginImportService
    import_scanner: PluginImportService
    auth_level: Callable[[], int]
    remote_entry: PluginRemoteEntryBuilder
    development: Callable[[], bool]
    logger: Any
    multi_version_blockers: PluginMultiVersionBlockers
    set_default_target: Callable[[str, str], bool]
    clear_default_target: Callable[[str], None]
    refresh_registrations: Callable[[str], None] | None = None
    pending_installation: Callable[[str], bool] | None = None


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
    version_binding: PluginVersionBinding
    log_level: PluginLogLevelControl
    default_target: PluginDefaultTargetControl
    projection: PluginProjection
    classification: PluginClassificationRegistry
    recent_local_sync: dict[str, float]
    system: Callable[[], PluginSystemServices]


def _pending_installation_checker(
    persisted: Callable[[str], bool] | None,
    host: PluginRuntimeHost,
) -> Callable[[str], bool]:
    """合并 journal 与包写入窗口的同步回收安全判据。"""
    monitor_suppressed = getattr(host, "is_plugin_monitor_suppressed", None)

    def check(plugin_id: str) -> bool:
        """只要任一安装事务事实未收敛，就阻止版本目录删除。"""
        if persisted is not None and persisted(plugin_id):
            return True
        return bool(callable(monitor_suppressed) and monitor_suppressed(plugin_id))

    return check


def _stop_plugin_for_version_binding(
    lifecycle: PluginLifecycle,
    plugin_id: str,
) -> bool:
    """只在旧实例完全收敛后结束它，供版本绑定切换使用。

    ``PluginLifecycle.stop`` 为兼容旧调用方保留了无返回值 ABI，并且采用强制
    finalize 语义；版本切换不能把一个 ``stop_service`` 失败的实例当成已停止，
    否则后续启动会覆盖注册表而遗留旧任务和路由。因此这里使用两阶段生命周期
    端口，只有 quiesce 成功才允许 finalize，并把最终结果显式返回给绑定服务。
    """
    if not lifecycle.quiesce(plugin_id):
        return False
    return bool(lifecycle.finalize(plugin_id))


def build_plugin_runtime(
    host: PluginRuntimeHost,
    environment: PluginRuntimeEnvironment,
    *,
    tool_build_max_attempts: int,
) -> PluginRuntime:
    """按依赖顺序构造唯一插件运行时，各业务能力仍由对应 owner 实现。"""
    registry = PluginRegistry()
    instances = PluginInstanceStore(
        storage=environment.storage,
        directory=environment.instance_directory,
    )
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
        host_binding=instances.get_host,
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
        loadable_plugins: list[str],
        validator: Callable[[Any], bool],
        version: Optional[str] = None,
    ) -> list[Any]:
        """加载物理插件或虚拟实例，并保持持久化实例顺序。

        ``version`` 仅在按单个实例 ID 加载时生效，用于版本切换失败后以某个
        具体版本重试；批量加载全部安装插件与实例时忽略该参数，各实例按自身
        绑定解析期望版本。
        """
        if plugin_id:
            instance = instances.get(plugin_id)
            if instance:
                return loader.load_instance(instance, validator, version=version)
            return loader.load(plugin_id, loadable_plugins, validator)
        plugins = loader.load(None, loadable_plugins, validator)
        # 只装载启用的配置：停用的分身仍登记在册、卡片可见，但不该被实例化
        for instance in instances.enabled().values():
            plugins.extend(loader.load_instance(instance, validator))
        return plugins

    lifecycle = PluginLifecycle(
        classes=registry.classes,
        running=registry.running,
        load_plugins=load_plugins,
        # 本体的装载判据归口到实例表的启用位；安装清单只回答「包在不在磁盘上」
        loadable_plugins=lambda: list(instances.enabled_hosts()),
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
        # 只给在册实例：已卸载的分身配置还留着等恢复，但它不该出现在「我的插件」里，
        # 那会让一个已经卸载掉的东西看起来仍然装着
        plugin_instances=instances.enabled,
        host_instances=instances.all_hosts,
        runtime_status=registry.runtime_status,
        log=environment.logger,
    )
    def version_binding_of(plugin_id: str) -> Optional[PluginInstance]:
        """读取该 ID 对应实例的版本绑定，分身优先、回落到源插件本体。

        只查分身会让本体恒为空，静态资源随之按当前版本解析，而代码已按本体钉定的
        版本加载——同一个插件的资源与代码分处两个版本目录。

        :param plugin_id: 实例 ID 或源插件 ID
        :return: 该实例的版本绑定记录；都没有时为 None
        """
        return instances.get(plugin_id) or instances.get_host(plugin_id)

    paths = PluginPathResolver(
        runtime_root=environment.plugins_root,
        running=lambda: registry.running,
        system=environment.system,
        strict_system_version=lambda: not environment.development(),
        get_instance=version_binding_of,
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
        instances=instances.enabled,
        registry=registry,
        log=environment.logger,
        instance_directory=lambda source_plugin_id, instance: resolve_instance_version_dir(
            environment.plugins_root / source_plugin_id.lower(),
            instance,
        ),
        host_instance=instances.get_host,
    )
    def version_installed(plugin_id: str, version: Optional[str] = None) -> bool:
        """判断插件的指定版本是否已经安装。

        先看磁盘上的已装版本目录：``catalog.exists`` 拿运行中实例的版本做比较，
        而本体一旦被钉在旧版本，运行版本就恒低于目录里的最新版本，同步会每次启动
        都判定「最新版未安装」并重新装一遍。判据落到磁盘才与「是否已安装」相符。

        :param plugin_id: 插件ID
        :param version: 目标版本号，为空时只判插件本身是否可用
        :return: 该版本是否已安装
        """
        if version:
            plugin_root = environment.plugins_root / plugin_id.lower()
            if version in plugin_version_dirs(plugin_root):
                return True
        return catalog.exists(plugin_id, version)

    sync = PluginSyncService(
        frozen=lambda: environment.system().is_frozen(),
        installed_plugins=lambda: environment.storage().read(
            SystemConfigKey.UserInstalledPlugins
        ) or [],
        online_plugins=catalog.online,
        # 启动恢复必须保留本地仓库扫描失败，不能把异常降级为空候选后
        # 再从在线市场下载覆盖当前载荷。
        local_plugins=lambda: catalog.local_repository(raise_errors=True),
        merge_plugins=lambda higher, base, _markets: catalog.merge(higher, base),
        plugin_exists=version_installed,
        install=lambda plugin_id, repo_url, force, startup_token: environment.system().install_plugin(
            plugin_id=plugin_id,
            repo_url=repo_url,
            force=force,
            startup_token=startup_token,
        ),
        runtime_status_writer=registry.set_runtime_status,
        log=environment.logger,
    )
    def source_plugin_id(plugin_id: str) -> str:
        """把虚拟实例归一到持久化的物理源码插件。"""
        instance = instances.get(plugin_id)
        return instance.source_plugin_id if instance else plugin_id

    clone = PluginCloneService(
        plugin_class=registry.plugin_class,
        plugin_exists=catalog.exists,
        get_instance=instances.get,
        get_any_instance=instances.get_any,
        all_instances_for_source=instances.all_for_source,
        disable_instance=instances.disable,
        source_plugin_id=source_plugin_id,
        installed_versions=lambda plugin_id: tuple(
            plugin_version_dirs(environment.plugins_root / plugin_id.lower())
        ),
        save_instance=instances.save,
        purge_instance=instances.purge,
        read_config=configs.read,
        save_config=lambda plugin_id, config: configs.write(
            plugin_id,
            config,
            force=True,
        ),
        delete_data=lambda plugin_id: configs.delete_data(plugin_id, force=True),
        has_data=configs.has_data,
        reload_plugin=host.reload_plugin,
        remove_plugin=host.remove_plugin,
        log=environment.logger,
    )
    version_binding = PluginVersionBinding(
        plugins_root=environment.plugins_root,
        plugin_exists=lambda plugin_id: registry.plugin_class(plugin_id) is not None,
        get_instance=instances.get,
        instances_for_source=instances.for_source,
        all_instances_for_source=instances.all_for_source,
        save_instance=instances.save,
        get_host_instance=instances.get_host,
        save_host_instance=instances.save_host,
        running=lambda: registry.running,
        start=lambda instance_id, version: lifecycle.start(instance_id, version=version),
        stop=lambda plugin_id: _stop_plugin_for_version_binding(lifecycle, plugin_id),
        multi_version_blockers=environment.multi_version_blockers,
        display_name=lambda instance_id: getattr(
            registry.plugin_class(instance_id),
            "plugin_name",
            None,
        ),
        log=environment.logger,
        refresh_registrations=environment.refresh_registrations,
        pending_installation=_pending_installation_checker(
            environment.pending_installation,
            host,
        ),
    )
    log_level = PluginLogLevelControl(
        plugin_exists=lambda plugin_id: registry.plugin_class(plugin_id) is not None,
        get_instance=instances.get,
        instances_for_source=instances.for_source,
        get_host_instance=instances.get_host,
        read_log_level=configs.read_log_level,
        write_log_level=configs.write_log_level,
    )
    default_target = PluginDefaultTargetControl(
        plugin_exists=lambda plugin_id: registry.plugin_class(plugin_id) is not None,
        get_instance=instances.get,
        instances_for_source=instances.for_source,
        get_host_instance=instances.get_host,
        save_host_instance=instances.save_host,
        running=lambda: registry.running,
        set_default_target=environment.set_default_target,
        clear_default_target=environment.clear_default_target,
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
        version_binding=version_binding,
        log_level=log_level,
        default_target=default_target,
        projection=projection,
        classification=classification,
        recent_local_sync=recent_local_sync,
        system=environment.system,
    )
