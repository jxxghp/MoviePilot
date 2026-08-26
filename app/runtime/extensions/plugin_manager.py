import asyncio
import concurrent.futures
import inspect
import posixpath
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import (
    Any,
    Callable,
    ContextManager,
    Dict,
    List,
    Optional,
    Tuple,
    Type,
    Union,
)

from watchfiles import watch

from app.schemas.plugin import Plugin as _SchemaPlugin
from app.schemas.plugin import PluginDashboard as _SchemaPluginDashboard
from app.schemas.plugin import PluginInstance, PluginRuntimeStatus
from app.foundation.crypto import RSAUtils
from app.foundation.environment import is_free_threaded_runtime, is_gil_enabled
from app.foundation.singleton import Singleton
from app.foundation.version import compare_version
from app.runtime.execution import run_in_threadpool_to_completion
from app.runtime.log import logger
from app.runtime.observability import observe_compat_facade
from app.runtime.settings import get_runtime_setting
from app.runtime.thread import ThreadHelper

from app.runtime.events import EventHandlerBinding, eventmanager
from app.runtime.reload import ConfigReloadMixin
from app.runtime.extensions.plugin.loader import PluginLoader
from app.runtime.extensions.plugin.lifecycle import PluginLifecycle
from app.runtime.extensions.plugin.metadata import PluginMetadataMapper
from app.runtime.extensions.plugin.monitor import (
    PluginChangeMonitor,
    PluginMonitorController,
)
from app.runtime.extensions.plugin.projection import PluginProjection
from app.runtime.extensions.plugin.registry import PluginRegistry
from app.runtime.extensions.plugin.storage import get_plugin_storage
from app.runtime.extensions.plugin.system import get_plugin_system
from app.runtime.extensions.plugin.tools import PluginToolCatalog
from app.runtime.extensions.plugin.sync import (
    LocalPluginSyncService,
    PluginSyncService,
)
from app.runtime.extensions.plugin.clone import PluginCloneService
from app.runtime.extensions.plugin.access import PluginAccessPolicy
from app.runtime.extensions.plugin.admission import PluginMutationAdmission
from app.runtime.extensions.plugin.catalog import PluginCatalogFacade
from app.runtime.extensions.plugin.paths import PluginPathResolver
from app.runtime.extensions.plugin.dependency import (
    PluginDependencyClassification,
    PluginDependencyInstallResult,
    PluginDependencyService,
)
from app.runtime.extensions.plugin.storage import PluginConfigStore, PluginInstanceStore
from app.schemas.exception import PluginMutationRejectedError
from app.schemas.types import EventType, SystemConfigKey

LegacyDiagnosticsConfigurator = Callable[..., None]
LegacyImportScanner = Callable[..., None]
LegacyPluginImportPreparer = Callable[..., None]
SiteAuthLevelProvider = Callable[[], int]
PluginCatalogFactory = Callable[["PluginManager"], Any]
PluginRouteRefresher = Callable[[str], None]


def _ignore_legacy_diagnostics(**_kwargs) -> None:
    """在启动组合根尚未注入兼容服务时保持插件加载可用。"""


def _ignore_plugin_resource_imports(**_kwargs) -> None:
    """未进入应用启动组合时不主动创建进程级宿主资源。"""


def _unavailable_site_auth_level() -> int:
    """站点能力尚未装配时返回未认证等级。"""
    return 0


def _unavailable_plugin_catalog_factory(_manager: "PluginManager") -> Any:
    """在启动组合根尚未装配目录用例时拒绝隐式跨层构造。"""
    raise RuntimeError("插件目录应用服务尚未由启动组合根装配")


def _warn_if_plugin_enabled_gil(
        *,
        gil_enabled_before: bool,
        plugin_id: Optional[str],
) -> None:
    """记录插件加载使 free-threaded 进程重新启用 GIL 的真实转换。"""
    if (
        not is_free_threaded_runtime()
        or gil_enabled_before
        or not is_gil_enabled()
    ):
        return
    logger.warning(
        "加载插件%s后 free-threaded 运行时已启用 GIL，请检查原生扩展兼容性",
        plugin_id or "集合",
    )


def _unavailable_plugin_route_refresher(_plugin_id: str) -> None:
    """在 HTTP 组合尚未装配时拒绝发布不完整的热重载投影。"""
    raise RuntimeError("插件动态路由刷新器尚未由启动组合根装配")


_legacy_diagnostics_configurator: LegacyDiagnosticsConfigurator = (
    _ignore_legacy_diagnostics
)
_legacy_import_scanner: LegacyImportScanner = _ignore_legacy_diagnostics
_legacy_plugin_import_preparer: LegacyPluginImportPreparer = (
    _ignore_plugin_resource_imports
)
_site_auth_level_provider: SiteAuthLevelProvider = _unavailable_site_auth_level
_plugin_catalog_factory: PluginCatalogFactory = _unavailable_plugin_catalog_factory
_plugin_route_refresher: PluginRouteRefresher = _unavailable_plugin_route_refresher


def configure_plugin_legacy_import_services(
    *,
    diagnostics_configurator: LegacyDiagnosticsConfigurator,
    import_scanner: LegacyImportScanner,
) -> None:
    """由启动组合根注入插件旧导入诊断服务，避免扩展层反向依赖兼容层。"""
    global _legacy_diagnostics_configurator, _legacy_import_scanner
    _legacy_diagnostics_configurator = diagnostics_configurator
    _legacy_import_scanner = import_scanner


def configure_plugin_resource_import_preparer(
    preparer: LegacyPluginImportPreparer,
) -> None:
    """注入旧插件导入前的宿主资源准备器。"""
    global _legacy_plugin_import_preparer
    _legacy_plugin_import_preparer = preparer


def configure_site_auth_level_provider(provider: SiteAuthLevelProvider) -> None:
    """由启动组合根注入站点认证等级，避免扩展运行时依赖应用服务。"""
    global _site_auth_level_provider
    _site_auth_level_provider = provider


def configure_plugin_catalog_factory(factory: PluginCatalogFactory) -> None:
    """由启动组合根注入插件目录应用服务工厂，消除 Runtime 反向依赖。"""
    global _plugin_catalog_factory
    _plugin_catalog_factory = factory


def configure_plugin_route_refresher(refresher: PluginRouteRefresher) -> None:
    """由启动组合根注入热重载后的动态路由投影刷新器。"""
    global _plugin_route_refresher
    _plugin_route_refresher = refresher


@observe_compat_facade("PluginManager")
class PluginManager(ConfigReloadMixin, metaclass=Singleton):
    """插件管理器"""
    CONFIG_WATCH = {"DEV", "PLUGIN_AUTO_RELOAD", "PLUGIN_LOCAL_REPO_PATHS"}
    AGENT_TOOLS_BUILD_MAX_ATTEMPTS = 3

    def __init__(self):
        """初始化插件注册表、缓存和开发模式监控状态。"""
        self._plugin_registry = PluginRegistry()
        # 旧属性继续引用注册表拥有的可变字典，保持插件和测试的访问身份。
        self._plugins = self._plugin_registry.classes
        self._running_plugins = self._plugin_registry.running
        self._plugin_instance_store = PluginInstanceStore(
            storage=lambda: get_plugin_storage(),
        )
        # 配置Key
        self._config_key: str = "plugin.%s"
        self._plugin_config_store = PluginConfigStore(
            storage=lambda: get_plugin_storage(),
            plugin_exists=lambda pid: bool(self._plugins.get(pid)),
            key_prefix=self._config_key,
        )
        self._plugin_access = PluginAccessPolicy(
            auth_level=lambda: _site_auth_level_provider(),
            verify_keys=RSAUtils.verify_rsa_keys,
            log=logger,
        )
        self._plugin_catalog_view = PluginCatalogFacade(
            classes=lambda: self._plugins,
            running=lambda: self._running_plugins,
            storage=lambda: get_plugin_storage(),
            system=get_plugin_system,
            market_catalog=lambda: self._plugin_catalog(),
            market_loader=lambda market, package_version=None, force=False: (
                self.get_plugins_from_market(market, package_version, force)
            ),
            async_market_loader=lambda market, package_version=None, force=False: (
                self.async_get_plugins_from_market(market, package_version, force)
            ),
            map_plugin=lambda **kwargs: self._process_plugin_info(**kwargs),
            auth_checker=lambda **kwargs: self.__set_and_check_auth_level(**kwargs),
            plugin_attr=lambda pid, attr: self.get_plugin_attr(pid, attr),
            plugin_instance=self.get_plugin_instance,
            plugin_instances=self.get_plugin_instances,
            runtime_status=self._plugin_registry.runtime_status,
            log=logger,
        )
        # 本地插件同步写入运行目录后的短时忽略窗口
        self._recent_local_sync: Dict[str, float] = {}
        self._monitor_suppression_lock = threading.Lock()
        self._suppressed_monitor_plugins: Dict[str, int] = {}
        self._plugin_paths = PluginPathResolver(
            runtime_root=get_runtime_setting('ROOT_PATH') / "app" / "plugins",
            running=lambda: self._running_plugins,
            system=get_plugin_system,
            strict_system_version=lambda: not get_runtime_setting('DEV'),
            log=logger,
        )
        self._local_plugin_sync = LocalPluginSyncService(
            installed_plugins=lambda: get_plugin_storage().read(
                SystemConfigKey.UserInstalledPlugins
            ) or [],
            candidate=lambda pid: get_plugin_system().local_candidate(pid),
            system=get_plugin_system,
            recent_sync=self._recent_local_sync,
            log=logger,
        )
        self._plugin_monitor = PluginMonitorController(
            runner=self._run_file_watcher,
            log=logger,
        )
        self._plugin_quiesce_lock = threading.RLock()
        self._plugin_quiesce_future: Optional[
            concurrent.futures.Future[bool]
        ] = None
        self._plugin_service_quiesce_future: Optional[
            concurrent.futures.Future[bool]
        ] = None
        self._plugin_mutation_admission = PluginMutationAdmission()
        self._plugin_runtime_closed = False
        self._plugin_dependencies = PluginDependencyService(
            system=get_plugin_system,
            log=logger,
        )
        self._plugin_loader = PluginLoader(
            plugins_root=get_runtime_setting('ROOT_PATH') / "app" / "plugins",
            import_preparer=lambda **kwargs: _legacy_plugin_import_preparer(**kwargs),
            import_scanner=lambda **kwargs: _legacy_import_scanner(**kwargs),
            log=logger,
        )
        self._plugin_tool_catalog = PluginToolCatalog(
            max_attempts=self.AGENT_TOOLS_BUILD_MAX_ATTEMPTS
        )
        self._plugin_lifecycle = PluginLifecycle(
            classes=self._plugins,
            running=self._running_plugins,
            load_plugins=lambda pid, installed, check: self._load_runtime_plugins(
                pid,
                installed,
                check,
            ),
            installed_plugins=lambda: get_plugin_storage().read(
                SystemConfigKey.UserInstalledPlugins
            ) or [],
            plugin_config=self.get_plugin_config,
            auth_checker=lambda plugin: self.__set_and_check_auth_level(plugin=plugin),
            clear_modules=lambda pid: self._clear_plugin_modules(pid),
            clear_tools=self.clear_plugin_agent_tools_cache,
            enable_events=eventmanager.enable_event_handler,
            disable_events=eventmanager.disable_event_handler,
            runtime_status_writer=self._plugin_registry.set_runtime_status,
            log=logger,
            event_sender=eventmanager.send_event,
        )
        self._plugin_metadata = PluginMetadataMapper(
            plugin_instance=self._plugin_registry.instance,
            plugin_class=self._plugin_registry.plugin_class,
            annotate_system_version=lambda info: get_plugin_system().annotate_system_version(
                info
            ),
            is_package_compatible=lambda info, version: get_plugin_system().is_package_compatible(
                info,
                version,
            ),
            auth_checker=lambda plugin, source: self.__set_and_check_auth_level(
                plugin,
                source,
            ),
            version_compare=compare_version,
            log=logger,
        )
        self._plugin_sync = PluginSyncService(
            frozen=lambda: get_plugin_system().is_frozen(),
            installed_plugins=lambda: get_plugin_storage().read(
                SystemConfigKey.UserInstalledPlugins
            ) or [],
            online_plugins=lambda: self.get_online_plugins(),
            local_plugins=lambda: self.get_local_repo_plugins(),
            merge_plugins=lambda higher, base, _markets: self.process_plugins_list(
                higher,
                base,
            ),
            plugin_exists=lambda plugin_id, version: self.is_plugin_exists(
                plugin_id,
                version,
            ),
            install=lambda plugin_id, repo_url, force, startup_token: get_plugin_system().install_plugin(
                plugin_id=plugin_id,
                repo_url=repo_url,
                force=force,
                startup_token=startup_token,
            ),
            log=logger,
        )
        self._plugin_clone = PluginCloneService(
            plugin_class=self._plugin_registry.plugin_class,
            plugin_exists=lambda plugin_id: self.is_plugin_exists(plugin_id),
            source_plugin_id=self.get_plugin_source_id,
            save_instance=self._plugin_instance_store.save,
            delete_instance=self._plugin_instance_store.delete,
            read_config=self.get_plugin_config,
            save_config=lambda plugin_id, config: self.save_plugin_config(
                plugin_id,
                config,
                force=True,
            ),
            delete_config=lambda plugin_id: self.delete_plugin_config(
                plugin_id,
                force=True,
            ),
            reload_plugin=self.reload_plugin,
            remove_plugin=self.remove_plugin,
            log=logger,
        )
        # 事件总线只通过通用解析器访问运行中的插件实例。
        eventmanager.register_handler_instance_resolver(
            "plugins",
            self.resolve_event_handler_instance,
        )
    def resolve_event_handler_instance(
            self,
            owner_class: Type[Any],
    ) -> Optional[EventHandlerBinding]:
        """为插件声明的事件方法解析当前运行实例。"""
        plugin_id = owner_class.__name__
        # 旧测试与部分扩展会替换私有映射来构造隔离运行态，解析器继续尊重该接缝。
        if plugin_id not in self._plugins:
            return None
        plugin = self._running_plugins.get(plugin_id)
        owner_name = plugin_id
        if plugin and callable(getattr(plugin, "get_name", None)):
            owner_name = plugin.get_name()
        return EventHandlerBinding(
            instance=plugin,
            owner_name=owner_name,
            run_sync_in_threadpool=True,
        )

    def init_config(self):
        """按最新系统配置完整重启插件。"""
        try:
            with self.mutation("配置热重载"):
                # 停止已有插件
                self.stop()
                classification = self.classify_plugins()
                self.apply_plugin_dependency_classification(classification)
                for plugin_id in classification.ready:
                    self.start(plugin_id)
        except PluginMutationRejectedError as error:
            logger.warning(str(error))

    def start(self, pid: Optional[str] = None) -> Dict[str, PluginRuntimeStatus]:
        """
        启动加载插件
        :param pid: 插件ID，为空加载所有插件
        """

        try:
            with self.mutation("启动插件"):
                with self._plugin_quiesce_lock:
                    _legacy_diagnostics_configurator(
                        enabled=get_runtime_setting('DEBUG'),
                        emitter=logger.warning,
                    )
                    gil_enabled_before = is_gil_enabled()
                    try:
                        return self._plugin_lifecycle.start(pid)
                    finally:
                        _warn_if_plugin_enabled_gil(
                            gil_enabled_before=gil_enabled_before,
                            plugin_id=pid,
                        )
        except PluginMutationRejectedError as error:
            logger.warning(str(error))
            if pid:
                return {pid: PluginRuntimeStatus.LOAD_FAILED}
            return {}

    def init_plugin(self, plugin_id: str, conf: dict):
        """
        初始化插件
        :param plugin_id: 插件ID
        :param conf: 插件配置
        """
        try:
            with self.mutation("初始化插件配置"):
                with self._plugin_quiesce_lock:
                    self._plugin_lifecycle.initialize(plugin_id, conf)
        except PluginMutationRejectedError as error:
            logger.warning(str(error))

    def clear_plugin_agent_tools_cache(self) -> None:
        """
        清空插件智能体工具注册表缓存。
        """
        self._plugin_tool_catalog.clear()

    def get_plugin_agent_tools_revision(self) -> int:
        """
        获取插件智能体工具注册表版本号。
        """
        return self._plugin_tool_catalog.revision

    @property
    def _plugin_agent_tools_revision(self) -> int:
        """兼容读取旧私有字段，实际版本由独立工具目录持有。"""
        return self._plugin_tool_catalog.revision

    def stop(self, pid: Optional[str] = None) -> None:
        """
        停止插件服务
        :param pid: 插件ID，为空停止所有插件
        """
        try:
            with self.mutation("停止插件"):
                with self._plugin_quiesce_lock:
                    self._plugin_lifecycle.stop(pid)
        except PluginMutationRejectedError as error:
            logger.warning(str(error))

    def mutation(self, operation: str) -> ContextManager[None]:
        """为一个完整插件可变事务取得可跨异步边界传播的准入 lease。"""
        return self._plugin_mutation_admission.hold(operation)

    def reopen_plugins(self) -> bool:
        """为新应用生命周期解除运行时封口，仍活跃的 quiesce owner 禁止复用。"""
        with self._plugin_quiesce_lock:
            futures = (
                self._plugin_quiesce_future,
                self._plugin_service_quiesce_future,
            )
            if any(future is not None and not future.done() for future in futures):
                logger.warning("插件后台服务仍在停止，无法开启新的应用生命周期")
                return False
            if self._plugin_runtime_closed and self._running_plugins:
                logger.warning("上一应用生命周期仍持有插件实例，拒绝解除运行时封口")
                return False
            if not self._plugin_mutation_admission.reopen():
                logger.warning("上一应用生命周期仍有插件可变事务，拒绝解除运行时封口")
                return False
            self._plugin_runtime_closed = False
            return True

    async def quiesce_plugins(self, timeout: float = 240.0) -> bool:
        """封口变更事务并停用插件 handler，超时后保留 Future ownership。"""
        if self._plugin_mutation_admission.is_held():
            logger.warning("插件可变事务不能等待自身收敛，拒绝在事务内执行停机")
            return False
        with self._plugin_quiesce_lock:
            self._plugin_runtime_closed = True
            self._plugin_mutation_admission.seal()
            future = self._plugin_quiesce_future
            if future is None or future.done():
                future = ThreadHelper().submit(self._quiesce_after_mutations)
                self._plugin_quiesce_future = future
        try:
            result = await asyncio.wait_for(
                asyncio.shield(asyncio.wrap_future(future)),
                timeout=max(0.0, timeout),
            )
            return bool(result)
        except asyncio.TimeoutError:
            logger.error(f"插件后台服务未在 {timeout:g} 秒内收敛")
            return False
        except Exception as error:  # noqa: BLE001  Future 异常必须转为生命周期结果
            logger.error(f"插件后台服务停止失败：{error}", exc_info=True)
            return False
        finally:
            if future.done():
                with self._plugin_quiesce_lock:
                    if self._plugin_quiesce_future is future:
                        self._plugin_quiesce_future = None

    async def quiesce_plugin_services(self, timeout: float = 240.0) -> bool:
        """在事件结算后执行旧插件停机 hook，并有界等待同步 owner。"""
        with self._plugin_quiesce_lock:
            prepare_future = self._plugin_quiesce_future
            if prepare_future is not None and not prepare_future.done():
                logger.warning("插件事件入口仍在封口，拒绝提前关闭插件资源")
                return False
            if not self._plugin_runtime_closed:
                logger.warning("插件运行时尚未封口，拒绝关闭插件资源")
                return False
            if self._plugin_mutation_admission.active_count:
                logger.warning("插件可变事务仍在执行，拒绝关闭插件资源")
                return False
            future = self._plugin_service_quiesce_future
            if future is None or future.done():
                future = ThreadHelper().submit(
                    self._plugin_lifecycle.quiesce_services,
                )
                self._plugin_service_quiesce_future = future
        try:
            result = await asyncio.wait_for(
                asyncio.shield(asyncio.wrap_future(future)),
                timeout=max(0.0, timeout),
            )
            return bool(result)
        except asyncio.TimeoutError:
            logger.error(f"插件旧停机 hook 未在 {timeout:g} 秒内收敛")
            return False
        except Exception as error:  # noqa: BLE001  Future 异常必须转为生命周期结果
            logger.error(f"插件旧停机 hook 执行失败：{error}", exc_info=True)
            return False
        finally:
            if future.done():
                with self._plugin_quiesce_lock:
                    if self._plugin_service_quiesce_future is future:
                        self._plugin_service_quiesce_future = None

    def finalize_plugins(self) -> bool:
        """确认 quiesce owner 已结束后禁用 handler 并卸载插件实例。"""
        with self._plugin_quiesce_lock:
            futures = (
                self._plugin_quiesce_future,
                self._plugin_service_quiesce_future,
            )
        if any(future is not None and not future.done() for future in futures):
            logger.warning("插件后台服务仍在停止，拒绝释放运行实例")
            return False
        if self._plugin_mutation_admission.active_count:
            logger.warning("插件可变事务仍在执行，拒绝释放运行实例")
            return False
        return self._plugin_lifecycle.finalize()

    def _quiesce_after_mutations(self) -> bool:
        """等待已获准变更自然结束后，再停用插件事件入口。"""
        self._plugin_mutation_admission.wait_until_idle()
        return self._plugin_lifecycle.quiesce_handlers()

    @staticmethod
    def _load_selective_plugins(pid: Optional[str], installed_plugins: List[str],
                                check_module_func: Callable) -> List[Any]:
        """
        选择性加载插件，只import符合条件的插件
        :param pid: 指定插件ID，为空则加载所有已安装插件
        :param installed_plugins: 已安装插件列表
        :param check_module_func: 模块检查函数
        :return: 插件类列表
        """
        return PluginLoader(
            plugins_root=get_runtime_setting('ROOT_PATH') / "app" / "plugins",
            import_preparer=lambda **kwargs: _legacy_plugin_import_preparer(**kwargs),
            import_scanner=lambda **kwargs: _legacy_import_scanner(**kwargs),
            log=logger,
        ).load(pid, installed_plugins, check_module_func)

    def _load_runtime_plugins(
        self,
        pid: Optional[str],
        installed_plugins: List[str],
        check_module_func: Callable[[Any], bool],
    ) -> List[Any]:
        """加载物理插件或虚拟实例，并保留旧选择性加载方法的合同。"""
        if pid:
            instance = self._plugin_instance_store.get(pid)
            if instance:
                return self._plugin_loader.load_instance(instance, check_module_func)
            return self._plugin_loader.load(pid, installed_plugins, check_module_func)

        plugins = self._plugin_loader.load(
            None,
            installed_plugins,
            check_module_func,
        )
        for instance in self._plugin_instance_store.all().values():
            plugins.extend(
                self._plugin_loader.load_instance(instance, check_module_func)
            )
        return plugins

    @property
    def running_plugins(self) -> Dict[str, Any]:
        """
        获取运行态插件列表
        :return: 运行态插件列表
        """
        return self._plugin_registry.running

    @property
    def plugins(self) -> Dict[str, Any]:
        """
        获取插件列表
        :return: 插件列表
        """
        return self._plugin_registry.classes

    def on_config_changed(self):
        """在插件监控配置变化后重建文件监控。"""
        self.reload_monitor()

    def get_reload_name(self) -> str:
        """返回配置重载日志使用的功能名称。"""
        return "插件文件修改监测"

    def start_monitor(self, *, reopen: bool = False) -> None:
        """按当前配置启动监控；新生命周期可显式解除既有封口。"""
        if reopen and not self._plugin_monitor.reopen():
            return
        if (
            not self.is_plugin_settling()
            and (
                get_runtime_setting('DEV')
                or get_runtime_setting('PLUGIN_AUTO_RELOAD')
            )
        ):
            self._plugin_monitor.start()

    def reload_monitor(self):
        """
        重新加载插件文件修改监测
        """
        self._plugin_monitor.reload(
            enabled=(
                not self.is_plugin_settling()
                and (
                    get_runtime_setting('DEV')
                    or get_runtime_setting('PLUGIN_AUTO_RELOAD')
                )
            )
        )

    def stop_monitor(self, timeout: float = 5.0) -> bool:
        """停止插件文件监控，并返回线程是否在预算内真正退出。"""
        return self._plugin_monitor.stop(timeout=timeout)

    def close_monitor(self, timeout: float = 5.0) -> bool:
        """封口当前生命周期的文件监控，并返回线程是否真正退出。"""
        return self._plugin_monitor.close(timeout=timeout)

    def _run_file_watcher(self):
        """
        运行 watchfiles 监视器的主循环。
        """
        PluginChangeMonitor(
            runtime_root=get_runtime_setting('ROOT_PATH') / "app" / "plugins",
            local_roots=get_plugin_system().local_repo_paths,
            stop_event=self._plugin_monitor.stop_event,
            recent_sync=self._recent_local_sync,
            federated_change=self._get_federated_plugin_change,
            runtime_plugin=self._get_plugin_id_from_path,
            monitor_suppressed=self.is_plugin_monitor_suppressed,
            local_candidate=self._get_local_plugin_candidate_from_path,
            sync_local=self._sync_local_plugin_if_installed,
            reload_plugin=self._reload_plugin_tree_from_monitor,
            dependency_manifest_status=(
                get_plugin_system().dependency_manifest_status
            ),
            watch=watch,
            log=logger,
        ).run()

    def _reload_plugin_tree_from_monitor(
        self,
        plugin_id: str,
    ) -> PluginRuntimeStatus:
        """重载源码树，并发布源插件及虚拟实例的动态路由投影。"""
        with self.mutation("热重载插件路由"):
            status = self.reload_plugin_tree(plugin_id)
            reload_targets = self.get_plugin_reload_targets(plugin_id)
            for reload_target in reload_targets:
                _plugin_route_refresher(reload_target)
            return status

    def _get_federated_plugin_change(
        self,
        event_path: Path,
    ) -> Optional[Tuple[str, Optional[dict], bool]]:
        """
        识别运行态 Vue 插件声明目录内的构建产物变化。

        :return: 插件 ID、本地仓库候选和联邦入口是否完整；非联邦目录变化返回 None。
        """
        return self._plugin_paths.federated_change(event_path)

    def _get_plugin_id_from_path(self, event_path: Path) -> Optional[str]:
        """
        根据文件路径解析出插件的ID。
        :param event_path: 被修改文件的 Path 对象。
        :return: 插件ID字符串，如果不是有效插件文件则返回 None。
        """
        return self._plugin_paths.runtime_plugin(event_path)

    def _get_local_plugin_candidate_from_path(self, event_path: Path) -> Optional[dict]:
        """
        根据本地插件仓库路径解析具体插件候选，保留 plugins/plugins.v2 来源差异
        """
        return self._plugin_paths.local_candidate(event_path)

    def _sync_local_plugin_if_installed(self, pid: str, candidate: Optional[dict] = None) -> bool:
        """
        已安装本地插件源码变化时，同步到运行目录
        """
        try:
            with self.mutation("同步本地插件源码"):
                return self._local_plugin_sync.sync(pid, candidate)
        except PluginMutationRejectedError as error:
            logger.warning(str(error))
            return False

    @contextmanager
    def suppress_plugin_monitor(self, plugin_id: str):
        """在插件目录原子更新期间阻止文件监控抢先重载半成品。"""
        with self.mutation("更新插件包"):
            normalized_id = plugin_id.lower()
            with self._monitor_suppression_lock:
                self._suppressed_monitor_plugins[normalized_id] = (
                    self._suppressed_monitor_plugins.get(normalized_id, 0) + 1
                )
            try:
                yield
            finally:
                with self._monitor_suppression_lock:
                    count = self._suppressed_monitor_plugins.get(normalized_id, 0)
                    if count <= 1:
                        self._suppressed_monitor_plugins.pop(normalized_id, None)
                    else:
                        self._suppressed_monitor_plugins[normalized_id] = count - 1

    def is_plugin_monitor_suppressed(self, plugin_id: str) -> bool:
        """判断指定插件是否处于安装或替换写入阶段。"""
        with self._monitor_suppression_lock:
            return self._suppressed_monitor_plugins.get(plugin_id.lower(), 0) > 0

    def remove_plugin(self, plugin_id: str):
        """
        从内存中移除一个插件
        :param plugin_id: 插件ID
        """
        try:
            with self.mutation("移除插件实例"):
                with self._plugin_quiesce_lock:
                    self._plugin_lifecycle.stop(plugin_id)
                    self._plugin_registry.remove(plugin_id)
        except PluginMutationRejectedError as error:
            logger.warning(str(error))

    def reload_plugin(self, plugin_id: str) -> PluginRuntimeStatus:
        """
        将一个插件重新加载到内存
        :param plugin_id: 插件ID
        """
        try:
            with self.mutation("重新加载插件"):
                with self._plugin_quiesce_lock:
                    gil_enabled_before = is_gil_enabled()
                    try:
                        return self._plugin_lifecycle.reload(
                            plugin_id,
                            EventType.PluginReload,
                        )
                    finally:
                        _warn_if_plugin_enabled_gil(
                            gil_enabled_before=gil_enabled_before,
                            plugin_id=plugin_id,
                        )
        except PluginMutationRejectedError as error:
            logger.warning(str(error))
            return PluginRuntimeStatus.LOAD_FAILED

    def reload_plugin_tree(self, plugin_id: str) -> PluginRuntimeStatus:
        """重载源码插件，并同步刷新所有引用该源码的虚拟实例。"""
        try:
            with self.mutation("重载插件实例树"):
                with self._plugin_quiesce_lock:
                    source_plugin_id = self.get_plugin_source_id(plugin_id)
                    status = self.reload_plugin(source_plugin_id)
                    for instance in self._plugin_instance_store.for_source(source_plugin_id):
                        self.reload_plugin(instance.instance_id)
                    return status
        except PluginMutationRejectedError as error:
            logger.warning(str(error))
            return PluginRuntimeStatus.LOAD_FAILED

    def get_plugin_reload_targets(self, plugin_id: str) -> List[str]:
        """返回源码更新后需要刷新注册信息的源插件及其实例 ID。"""
        source_plugin_id = self.get_plugin_source_id(plugin_id)
        return [
            source_plugin_id,
            *(
                instance.instance_id
                for instance in self._plugin_instance_store.for_source(
                    source_plugin_id
                )
            ),
        ]

    @staticmethod
    def _clear_plugin_modules(plugin_id: Optional[str] = None):
        """
        清除插件及其所有子模块的缓存
        :param plugin_id: 插件ID
        """

        return PluginLoader(
            plugins_root=get_runtime_setting('ROOT_PATH') / "app" / "plugins",
            import_preparer=lambda **kwargs: _legacy_plugin_import_preparer(**kwargs),
            import_scanner=lambda **kwargs: _legacy_import_scanner(**kwargs),
            log=logger,
        ).clear_modules(plugin_id)

    def sync(
        self,
        startup_token: object | None = None,
        *,
        online_restore_plugins: set[str] | None = None,
    ) -> List[str]:
        """
        安装本地不存在或需要更新的插件
        """

        with self.mutation("同步插件包"):
            return self._plugin_sync.sync(
                startup_token,
                online_restore_plugins=online_restore_plugins,
            )

    @staticmethod
    def install_plugin_missing_dependencies() -> List[str]:
        """
        安装插件中缺失或不兼容的依赖项
        """
        manager = PluginManager()
        with manager.mutation("安装插件依赖"):
            return PluginDependencyService(
                system=get_plugin_system,
                log=logger,
            ).install_missing()

    @staticmethod
    def install_plugin_missing_dependencies_with_status() -> PluginDependencyInstallResult:
        """安装插件缺失依赖并返回缺失项及安装成功状态。"""
        manager = PluginManager()
        with manager.mutation("安装插件依赖"):
            return PluginDependencyService(
                system=get_plugin_system,
                log=logger,
            ).install_missing_with_status()

    @staticmethod
    async def async_install_plugin_missing_dependencies_with_status() -> PluginDependencyInstallResult:
        """在异步启动链中恢复插件依赖并保留取消语义。"""
        manager = PluginManager()
        with manager.mutation("安装插件依赖"):
            return await PluginDependencyService(
                system=get_plugin_system,
                log=logger,
            ).async_install_missing_with_status()

    def classify_plugins(self) -> PluginDependencyClassification:
        """按源码依赖状态分类物理插件，并把结果映射到虚拟实例。"""
        source_classification = PluginDependencyService(
            system=get_plugin_system,
            log=logger,
        ).classify_plugins()
        ready = list(source_classification.ready)
        missing_dependencies = list(source_classification.missing_dependencies)
        missing_source = list(source_classification.missing_source)
        source_ready = set(source_classification.ready)
        source_pending = set(source_classification.missing_dependencies)
        for instance in self._plugin_instance_store.all().values():
            if instance.source_plugin_id in source_ready:
                ready.append(instance.instance_id)
            elif instance.source_plugin_id in source_pending:
                missing_dependencies.append(instance.instance_id)
            else:
                missing_source.append(instance.instance_id)
        return PluginDependencyClassification(
            ready=tuple(ready),
            missing_dependencies=tuple(missing_dependencies),
            missing_source=tuple(missing_source),
        )

    def apply_plugin_dependency_classification(
        self,
        classification: PluginDependencyClassification,
    ) -> None:
        """把源码和依赖分类写入运行状态，已激活插件保持当前结果。"""
        running_ids = set(self._plugin_registry.running_ids())
        for plugin_id in classification.missing_source:
            self._plugin_registry.set_runtime_status(
                plugin_id,
                PluginRuntimeStatus.SOURCE_MISSING,
            )
        for plugin_id in classification.missing_dependencies:
            self._plugin_registry.set_runtime_status(
                plugin_id,
                PluginRuntimeStatus.DEPENDENCY_PENDING,
            )
        for plugin_id in classification.ready:
            current_status = self._plugin_registry.runtime_status(plugin_id)
            if (
                plugin_id in running_ids
                and current_status is not PluginRuntimeStatus.DEPENDENCY_PENDING
            ):
                continue
            self._plugin_registry.set_runtime_status(
                plugin_id,
                PluginRuntimeStatus.READY,
            )

    def set_plugin_settling(self, settling: bool) -> None:
        """更新启动后的插件恢复任务状态。"""
        self._plugin_registry.set_settling(settling)

    def get_plugin_runtime_statuses(self) -> Dict[str, PluginRuntimeStatus]:
        """返回插件运行状态快照。"""
        return self._plugin_registry.runtime_status_snapshot()

    def get_plugin_runtime_generation(self) -> int:
        """返回插件状态变化代次。"""
        return self._plugin_registry.generation

    def is_plugin_settling(self) -> bool:
        """返回插件源码和依赖是否仍在后台恢复。"""
        return self._plugin_registry.settling

    def get_plugin_config(self, pid: str) -> dict:
        """
        获取插件配置
        :param pid: 插件ID
        """
        return self._plugin_config_store.read(pid)

    def get_plugin_instances(self) -> dict[str, PluginInstance]:
        """返回全部有效虚拟插件实例描述。"""
        return self._plugin_instance_store.all()

    def get_plugin_instance(self, plugin_id: str) -> Optional[PluginInstance]:
        """返回指定虚拟插件实例描述，物理插件返回空。"""
        return self._plugin_instance_store.get(plugin_id)

    def get_plugin_source_id(self, plugin_id: str) -> str:
        """解析插件运行身份对应的源码身份，普通插件保持原值。"""
        instance = self._plugin_instance_store.get(plugin_id)
        return instance.source_plugin_id if instance else plugin_id

    def get_plugin_source_instances(self, plugin_id: str) -> List[PluginInstance]:
        """返回直接引用指定源码插件的虚拟实例。"""
        return self._plugin_instance_store.for_source(plugin_id)

    def delete_plugin_instance(self, plugin_id: str) -> bool:
        """删除虚拟实例描述；调用方仍负责停止实例和清理业务数据。"""
        try:
            with self.mutation("删除插件实例描述"):
                return self._plugin_instance_store.delete(plugin_id)
        except PluginMutationRejectedError as error:
            logger.warning(str(error))
            return False

    def save_plugin_config(self, pid: str, conf: dict, force: bool = False) -> bool:
        """
        保存插件配置
        :param pid: 插件ID
        :param conf: 配置
        :param force: 强制保存
        """
        try:
            with self.mutation("保存插件配置"):
                return self._plugin_config_store.write(pid, conf, force)
        except PluginMutationRejectedError as error:
            logger.warning(str(error))
            return False

    async def async_save_plugin_config(
        self, pid: str, conf: dict, force: bool = False
    ) -> bool:
        """
        异步保存插件配置。
        :param pid: 插件ID
        :param conf: 配置
        :param force: 强制保存
        """
        try:
            with self.mutation("保存插件配置"):
                return await self._plugin_config_store.async_write(pid, conf, force)
        except PluginMutationRejectedError as error:
            logger.warning(str(error))
            return False

    def delete_plugin_config(self, pid: str, force: bool = False) -> bool:
        """
        删除插件配置
        :param pid: 插件ID
        :param force: 插件停止后仍允许按插件 ID 删除持久化配置
        """
        try:
            with self.mutation("删除插件配置"):
                return self._plugin_config_store.delete(pid, force)
        except PluginMutationRejectedError as error:
            logger.warning(str(error))
            return False

    def delete_plugin_data(self, pid: str, force: bool = False) -> bool:
        """
        删除插件数据
        :param pid: 插件ID
        :param force: 插件停止后仍允许按插件 ID 删除持久化数据
        """
        try:
            with self.mutation("删除插件数据"):
                return self._plugin_config_store.delete_data(pid, force)
        except PluginMutationRejectedError as error:
            logger.warning(str(error))
            return False

    def get_plugin_state(self, pid: str) -> bool:
        """
        获取插件状态
        :param pid: 插件ID
        """
        plugin = self._plugin_registry.instance(pid)
        return plugin.get_state() if plugin else False

    def _plugin_projection(self) -> PluginProjection:
        """构造绑定当前运行态插件注册表的能力投影服务。"""
        return PluginProjection(
            self._plugin_registry.running,
            logger,
            self.get_plugin_remote_entry,
        )

    def _plugin_catalog(self) -> Any:
        """构造绑定当前市场客户端和插件 DTO 映射器的目录应用服务。"""
        return _plugin_catalog_factory(self)

    def get_plugin_commands(self, pid: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        获取插件命令
        [{
            "cmd": "/xx",
            "event": EventType.xx,
            "desc": "xxxx",
            "data": {},
            "pid": "",
        }]
        """
        return self._plugin_projection().commands(pid)

    def get_plugin_apis(self, pid: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        获取插件API
        [{
            "path": "/xx",
            "endpoint": self.xxx,
            "methods": ["GET", "POST"],
            "summary": "API名称",
            "description": "API说明",
            "allow_anonymous": false
        }]
        """
        return self._plugin_projection().apis(pid)

    def get_plugin_services(self, pid: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        获取插件服务
        [{
            "id": "服务ID",
            "name": "服务名称",
            "trigger": "触发器：cron、interval、date、CronTrigger.from_crontab()",
            "func": self.xxx,
            "kwargs": {} # 定时器参数,
            "func_kwargs": {} # 方法参数
        }]
        """
        return self._plugin_projection().services(pid)

    def get_plugin_modules(self, pid: Optional[str] = None) -> Dict[tuple, Dict[str, Any]]:
        """
        获取插件模块
        {
            plugin_id: {
                method: function
            }
        }
        """
        return self._plugin_projection().modules(pid)

    def get_media_sources(self, pid: Optional[str] = None) -> List[Dict[str, Any]]:
        """获取运行中插件声明的媒体数据源。"""
        return self._plugin_projection().media_sources(pid)

    def get_plugin_actions(self, pid: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        获取插件动作
        [{
            "id": "动作ID",
            "name": "动作名称",
            "func": self.xxx,
            "kwargs": {} # 需要附加传递的参数
        }]
        """
        return self._plugin_projection().actions(pid)

    @staticmethod
    def _copy_plugin_agent_tools(
        tools_info: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        复制插件智能体工具注册信息，避免调用方修改缓存内容。
        """
        return PluginToolCatalog.copy(tools_info)

    def get_plugin_agent_tools(self, pid: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        获取插件智能体工具
        [{
            "plugin_id": "插件ID",
            "plugin_name": "插件名称",
            "tools": [ToolClass1, ToolClass2, ...]
        }]
        """
        return self._plugin_tool_catalog.get(
            self._running_plugins,
            plugin_id=pid,
            log=logger,
        )

    @staticmethod
    def get_plugin_remote_entry(plugin_id: str, dist_path: str) -> str:
        """
        获取插件的远程入口地址
        :param plugin_id: 插件 ID
        :param dist_path: 插件的分发路径
        :return: 远程入口地址
        """
        dist_path = dist_path.strip("/")
        path = posixpath.join(
            "plugin",
            "file",
            plugin_id.lower(),
            dist_path,
            "remoteEntry.js",
        )
        if not path.startswith("/"):
            path = "/" + path
        return path

    def get_plugin_remotes(self, pid: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        获取插件联邦组件列表
        """
        return self._plugin_projection().remotes(pid)

    def get_plugin_auth_providers(self) -> List[Dict[str, Any]]:
        """
        聚合插件声明的登录认证提供方。

        :return: 插件认证入口列表
        """
        return self._plugin_projection().auth_providers()

    def get_plugin_sidebar_nav(self) -> List[Dict[str, Any]]:
        """
        聚合所有已启用 Vue 插件的侧栏导航项（get_sidebar_nav）。
        """
        return self._plugin_projection().sidebar()

    def get_plugin_dashboard_meta(self) -> List[Dict[str, str]]:
        """
        获取所有插件仪表盘元信息
        """
        return self._plugin_projection().dashboard_metadata()

    def get_plugin_dashboard(self, pid: str, key: str, user_agent: str = None) -> Optional[_SchemaPluginDashboard]:
        """
        获取插件仪表盘
        """
        return self._plugin_projection().dashboard(pid, key, user_agent)

    def get_plugin_attr(self, pid: str, attr: str) -> Any:
        """
        获取插件属性
        :param pid: 插件ID
        :param attr: 属性名
        """
        plugin = self._plugin_registry.instance(pid)
        if not plugin:
            return None
        if not hasattr(plugin, attr):
            return None
        return getattr(plugin, attr)

    def run_plugin_method(self, pid: str, method: str, *args, **kwargs) -> Any:
        """
        运行插件方法
        :param pid: 插件ID
        :param method: 方法名
        :param args: 参数
        :param kwargs: 关键字参数
        """
        plugin = self._plugin_registry.instance(pid)
        if not plugin:
            return None
        if not hasattr(plugin, method):
            return None
        return getattr(plugin, method)(*args, **kwargs)

    async def async_run_plugin_method(self, pid: str, method: str, *args, **kwargs) -> Any:
        """
        异步运行插件方法，同步实现经受控线程入口执行
        :param pid: 插件ID
        :param method: 方法名
        :param args: 参数
        :param kwargs: 关键字参数
        """
        plugin = self._plugin_registry.instance(pid)
        if not plugin:
            return None
        if not hasattr(plugin, method):
            return None
        method_func = getattr(plugin, method)
        if inspect.iscoroutinefunction(method_func):
            return await method_func(*args, **kwargs)
        return await run_in_threadpool_to_completion(method_func, *args, **kwargs)

    def get_plugin_ids(self) -> List[str]:
        """
        获取所有插件ID
        """
        return self._plugin_registry.plugin_ids()

    def get_running_plugin_ids(self) -> List[str]:
        """
        获取所有运行态插件ID
        """
        return self._plugin_registry.running_ids()

    def get_online_plugins(self, force: bool = False) -> List[_SchemaPlugin]:
        """
        获取所有在线插件信息
        """
        return self._plugin_catalog_view.online(force)

    def get_local_plugins(self) -> List[_SchemaPlugin]:
        """
        获取所有本地已下载的插件信息
        """
        return self._plugin_catalog_view.local()

    def get_installed_plugins(self) -> List[_SchemaPlugin]:
        """按安装清单返回插件，即使运行时尚未加载也保留卡片。"""
        return self._plugin_catalog_view.installed()

    def get_local_plugin_version(self, pid: str) -> Optional[str]:
        """
        获取指定已安装插件的本地版本，不触发全部插件的状态、页面和权限计算。

        插件类由运行期动态加载，旧插件可能未声明版本属性，因此缺失时返回 None。
        """
        return self._plugin_catalog_view.local_version(pid)

    def get_local_repo_plugins(self) -> List[_SchemaPlugin]:
        """
        获取本地插件仓库目录中的插件信息
        """
        return self._plugin_catalog_view.local_repository()

    def is_plugin_exists(self, pid: str, version: str = None) -> bool:
        """
        判断插件是否存在，并满足版本要求(有传入version时)
        :param pid: 插件ID
        :param version: 插件版本
        """
        return self._plugin_catalog_view.exists(pid, version)

    def get_plugins_from_market(self, market: str,
                                package_version: Optional[str] = None,
                                force: bool = False) -> Optional[List[_SchemaPlugin]]:
        """
        从指定的市场获取插件信息
        :param market: 市场的 URL 或标识
        :param package_version: 首选插件版本 (如 "v2", "v3")，如果不指定则获取 v1 版本
        :param force: 是否强制刷新（忽略缓存）
        :return: 返回插件的列表，若获取失败返回 []
        """
        return self._plugin_catalog_view.get_from_market(
            market,
            package_version,
            force,
        )

    def process_plugins_list(self, higher_version_plugins: List[_SchemaPlugin],
                             base_version_plugins: List[_SchemaPlugin]) -> List[_SchemaPlugin]:
        """
        处理插件列表：合并、去重、排序、保留最高版本
        :param higher_version_plugins: 高版本插件列表
        :param base_version_plugins: 基础版本插件列表
        :return: 处理后的插件列表
        """
        return self._plugin_catalog_view.merge(
            higher_version_plugins,
            base_version_plugins,
        )

    def _process_plugin_info(self, pid: str, plugin_info: dict, market: str,
                             installed_apps: List[str], add_time: int,
                             package_version: Optional[str] = None) -> Optional[_SchemaPlugin]:
        """
        处理单个插件信息，创建 schemas.Plugin 对象
        :param pid: 插件ID
        :param plugin_info: 插件信息字典
        :param market: 市场URL
        :param installed_apps: 已安装插件列表
        :param add_time: 添加顺序
        :param package_version: 包版本
        :return: 创建的插件对象，如果验证失败返回None
        """
        return self._plugin_metadata.map(
            plugin_id=pid,
            plugin_info=plugin_info,
            market=market,
            installed_plugins=installed_apps,
            add_time=add_time,
            package_version=package_version,
        )

    @staticmethod
    def _normalize_plugin_label(labels: Any) -> Optional[str]:
        """
        规整插件市场标签字段，兼容旧字符串和新列表格式。

        :param labels: 插件市场 package 中的 labels 字段
        :return: 用空格拼接后的标签字符串，无法识别或为空时返回 None
        """
        return PluginMetadataMapper.normalize_label(labels)

    async def async_get_online_plugins(
            self,
            force: bool = False,
            progress_callback: Optional[Callable[..., None]] = None,
    ) -> List[_SchemaPlugin]:
        """
        异步获取所有在线插件信息
        :param force: 是否强制刷新（忽略缓存）
        :param progress_callback: 定时服务进度更新回调
        """
        return await self._plugin_catalog_view.async_online(
            force,
            progress_callback,
        )

    async def async_get_online_plugin_candidates(
            self,
            force: bool = False,
    ) -> List[_SchemaPlugin]:
        """获取按仓库保留的在线插件候选，供来源准入和更新选择。"""
        return await self._plugin_catalog_view.async_online_candidates(force)

    async def async_get_plugins_from_market(self, market: str,
                                            package_version: Optional[str] = None,
                                            force: bool = False) -> Optional[List[_SchemaPlugin]]:
        """
        异步从指定的市场获取插件信息
        :param market: 市场的 URL 或标识
        :param package_version: 首选插件版本 (如 "v2", "v3")，如果不指定则获取 v1 版本
        :param force: 是否强制刷新（忽略缓存）
        :return: 返回插件的列表，若获取失败返回 []
        """
        return await self._plugin_catalog_view.async_get_from_market(
            market,
            package_version,
            force,
        )

    def __set_and_check_auth_level(
        self,
        plugin: Union[_SchemaPlugin, Type[Any]],
        source: Optional[Union[dict, Type[Any]]] = None,
    ) -> bool:
        """
        设置并检查插件的认证级别
        :param plugin: 插件对象或包含 auth_level 属性的对象
        :param source: 可选的字典对象或类对象，可能包含 "level" 或 "auth_level" 键
        :return: 如果插件的认证级别有效且当前环境的认证级别满足要求，返回 True，否则返回 False
        """
        return self._plugin_access.check(plugin, source)

    @staticmethod
    def __get_plugin_private_key(plugin_id: str) -> Optional[str]:
        """
        根据插件标识获取对应的私钥
        :param plugin_id: 插件标识
        :return: 对应的插件私钥，如果未找到则返回 None
        """
        return PluginAccessPolicy.private_key(plugin_id)

    def clone_plugin(self, plugin_id: str, suffix: str, name: str, description: str,
                     version: str = None, icon: str = None) -> Tuple[bool, str]:
        """
        创建插件分身
        :param plugin_id: 原插件ID
        :param suffix: 分身后缀
        :param name: 分身名称
        :param description: 分身描述
        :param version: 自定义版本号
        :param icon: 自定义图标URL
        :return: (是否成功, 错误信息)
        """
        try:
            with self.mutation("创建插件分身"):
                return self._plugin_clone.clone(
                    plugin_id=plugin_id,
                    suffix=suffix,
                    name=name,
                    description=description,
                    version=version,
                    icon=icon,
                )
        except PluginMutationRejectedError as error:
            logger.warning(str(error))
            return False, str(error)

    def _modify_plugin_files(self, plugin_dir: Path, original_id: str, suffix: str,
                             name: str, description: str, version: str = None,
                             icon: str = None) -> Tuple[bool, str]:
        """
        兼容旧内部调用，将分身文件改写委托给包适配器。
        :param plugin_dir: 插件目录
        :param original_id: 原插件ID
        :param suffix: 分身后缀
        :param name: 分身名称
        :param description: 分身描述
        :param version: 自定义版本号
        :param icon: 自定义图标URL
        :return: (是否成功, 错误信息)
        """
        original_plugin_class = self._plugins.get(original_id)
        if not original_plugin_class:
            return False, f"无法获取原插件类 {original_id}"
        return get_plugin_system().package._modify_plugin_files(
            plugin_dir=plugin_dir,
            original_class_name=original_plugin_class.__name__,
            suffix=suffix,
            name=name,
            description=description,
            version=version,
            icon=icon,
        )

    @staticmethod
    def _modify_python_file(file_path: Path, original_class_name: str,
                            clone_class_name: str, name: str, description: str,
                            version: str = None, icon: str = None) -> Tuple[bool, str]:
        """
        兼容旧内部调用，将 Python 文件改写委托给包适配器。
        """
        return get_plugin_system().package._modify_python_file(
            file_path=file_path,
            original_class_name=original_class_name,
            clone_class_name=clone_class_name,
            name=name,
            description=description,
            version=version,
            icon=icon,
        )

    def _modify_federation_files(self, dist_dir: Path, original_class_name: str,
                                 clone_class_name: str) -> Tuple[bool, str]:
        """
        兼容旧内部调用，将联邦文件改写委托给包适配器。
        """
        return get_plugin_system().package._modify_federation_files(
            dist_dir=dist_dir,
            original_class_name=original_class_name,
            clone_class_name=clone_class_name,
        )

    @staticmethod
    def _rename_federation_assets(dist_dir: Path, original_class_name: str, clone_class_name: str):
        """
        兼容旧内部调用，将资源重命名委托给包适配器。
        """
        get_plugin_system().package._rename_federation_assets(
            dist_dir,
            original_class_name,
            clone_class_name,
        )
