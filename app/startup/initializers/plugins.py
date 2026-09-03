import asyncio
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from app.adapters.external.market import (
    LOCAL_REPO_PREFIX,
    configure_installed_plugins_provider,
    configure_plugin_install_gateway,
    configure_plugin_runtime_owners,
    reset_plugin_runtime_owners,
)
from app.adapters.external.plugin.client import (
    VERSION_BACKWARD_COMPATIBLE_FLAGS,
    PluginMarketClient,
    split_plugin_market_repo_urls,
)
from app.adapters.external.server import MoviePilotServerHelper
from app.adapters.system.host import SystemUtils
from app.adapters.system.plugin.manifest import dependency_manifest_status
from app.application.commands import init_commands
from app.application.configuration import (
    get_api_runtime_config_snapshot,
    get_configured_system_config,
)
from app.application.plugin.catalog import (
    PluginCatalogQuery,
    PluginCatalogService,
    configure_plugin_catalog_query,
    reset_plugin_catalog_query,
)
from app.application.plugin.data import DeletePluginDataCommand
from app.application.plugin.gateway import (
    PluginInstallGateway,
    configure_plugin_install_service,
)
from app.application.plugin.identity import (
    PluginPayloadSourceType,
    TrustedPluginSourceType,
    normalize_physical_plugin_id,
)
from app.application.plugin.install import PluginInstallCommand
from app.application.plugin.inventory import PluginCandidateInventoryReader
from app.application.plugin.lifecycle import PluginStartupLease
from app.application.plugin.migration import (
    PluginIdentityMigrationService,
    configure_plugin_identity_migration,
    get_plugin_identity_migration,
)
from app.application.plugin.rating import (
    PluginRatingService,
    configure_plugin_rating_service,
    reset_plugin_rating_service,
)
from app.application.plugin.recovery import (
    PluginInstallationRecoveryService,
    configure_plugin_installation_recovery,
)
from app.application.plugin.release import (
    PluginReleaseService,
    configure_plugin_release_service,
    reset_plugin_release_service,
)
from app.application.plugin.routes import register_plugin_api
from app.application.plugin.runtime import (
    PluginRuntime as PluginRuntimePort,
)
from app.application.plugin.runtime import (
    configure_plugin_runtime,
    get_plugin_manager,
)
from app.application.plugin.transaction import (
    PluginPersistenceService,
    get_plugin_persistence,
)
from app.application.scheduling import update_plugin_job
from app.application.site.sites import SitesHelper  # pylint: disable=import-error,no-name-in-module
from app.db.oper.plugindata import PluginDataOper
from app.db.plugin.registry import (
    destroy_database,
    ensure_database,
    release_database,
)
from app.db.session import SessionFactory
from app.db.uow import SqlAlchemyUnitOfWork
from app.foundation.version import compare_version
from app.runtime.cache import async_fresh
from app.runtime.compat.diagnostics import (
    configure_legacy_import_diagnostics,
    scan_plugin_legacy_imports,
)
from app.runtime.compat.resources import scan_plugin_resource_imports
from app.runtime.execution import run_in_threadpool_to_completion
from app.runtime.extensions.plugin.database import (
    PluginDatabase,
    configure_plugin_database,
    get_plugin_database,
)
from app.runtime.extensions.plugin.dependency import PluginDependencyInstallResult
from app.runtime.extensions.plugin.manager import (
    PluginManager,
    configure_plugin_catalog_factory,
    configure_plugin_legacy_import_services,
    configure_plugin_resource_import_preparer,
    configure_plugin_route_refresher,
    configure_plugin_runtime_factory,
    configure_site_auth_level_provider,
)
from app.runtime.extensions.plugin.runtime import (
    PluginRuntime,
    PluginRuntimeEnvironment,
    PluginRuntimeHost,
    build_plugin_runtime,
)
from app.runtime.extensions.plugin.storage import (
    PluginStorage,
    configure_plugin_storage,
    get_plugin_storage,
)
from app.runtime.extensions.plugin.system import (
    PluginSystemServices,
    configure_plugin_system,
    get_plugin_system,
)
from app.runtime.log import logger
from app.runtime.loop import main_loop_registry
from app.runtime.resources import acquire_managed_resource
from app.runtime.settings import get_runtime_setting
from app.schemas.exception import PluginMutationRejectedError
from app.schemas.plugin import PluginRuntimeStatus
from app.schemas.types import SystemConfigKey
from app.startup.composition.plugin import (
    compose_plugin_market,
    get_composed_plugin_market_client,
    reset_plugin_market_composition,
)


async def _async_write_plugin_config(key, value):
    """通过数据库操作器异步保存插件运行时配置。"""
    return await get_configured_system_config().async_set(key, value)


def _delete_plugin_data(plugin_id: str) -> None:
    """用独占同步会话执行插件重置的数据删除事务。"""
    session = SessionFactory()
    try:
        DeletePluginDataCommand(
            repository=PluginDataOper(session),
            unit_of_work=SqlAlchemyUnitOfWork(session),
        ).execute(plugin_id)
    finally:
        session.close()


def _build_plugin_database() -> PluginDatabase:
    """把插件自有数据库端口装配到 db 层的建库、释放与销毁实现。"""
    return PluginDatabase(
        ensure=ensure_database,
        release=release_database,
        destroy=destroy_database,
    )


def _prepare_legacy_plugin_import(*, plugin_id: str, plugin_dir: Path) -> None:
    """在执行旧插件顶层代码前准备其静态导入所需的宿主资源。"""
    for capability_id in scan_plugin_resource_imports(plugin_id, plugin_dir):
        acquire_managed_resource(
            capability_id,
            reason="legacy_plugin_import",
        )


def build_plugin_runtime_graph(host: PluginRuntimeHost) -> PluginRuntime:
    """在启动组合根构造插件 Runtime 的完整依赖图。"""
    return build_plugin_runtime(
        host,
        PluginRuntimeEnvironment(
            plugins_root=Path(get_runtime_setting('ROOT_PATH')) / "app" / "plugins",
            storage=lambda: get_plugin_storage(),
            system=lambda: get_plugin_system(),
            database=lambda: get_plugin_database(),
            catalog_factory=lambda mapper: _build_plugin_catalog(mapper),
            import_preparer=_prepare_legacy_plugin_import,
            import_scanner=scan_plugin_legacy_imports,
            auth_level=lambda: SitesHelper().auth_level,
            remote_entry=host.get_plugin_remote_entry,
            development=lambda: bool(get_runtime_setting('DEV')),
            logger=logger,
        ),
        tool_build_max_attempts=PluginManager.AGENT_TOOLS_BUILD_MAX_ATTEMPTS,
    )


def configure_plugin_runtime_services() -> None:
    """在模块对象图构造前发布插件 Runtime 工厂和应用层提供器。"""
    configure_plugin_catalog_factory(_build_plugin_catalog)
    configure_plugin_runtime_factory(build_plugin_runtime_graph)
    configure_plugin_runtime(
        lambda: cast(PluginRuntimePort, PluginManager()),
        existing_provider=lambda: cast(
            PluginRuntimePort | None,
            PluginManager.get_existing_instance(),
        ),
    )


def configure_plugin_services() -> None:
    """在模块持久化端口就绪后装配完整插件应用服务。"""
    market_composition = compose_plugin_market(
        installed_plugins_provider=lambda: get_configured_system_config().get(
            SystemConfigKey.UserInstalledPlugins
        )
        or []
    )
    market_transport = market_composition.transport
    market_client = market_composition.client
    package_manager = market_composition.package
    configure_plugin_runtime_owners(
        health=market_composition.health,
        dependency=market_composition.dependency,
    )
    plugin_manager = get_plugin_manager()
    inventory_reader = PluginCandidateInventoryReader(
        market_loader=market_client.get_plugin_index_result,
        async_market_loader=market_client.async_get_plugin_index_result,
        local_candidate_loader=market_client.get_local_candidates,
    )
    persistence = get_plugin_persistence()

    async def refresh_plugin_releases(plugin_id: str, repo_url: str) -> None:
        """绕过已有 Release 缓存执行后台刷新。"""
        async with async_fresh(True):
            await market_transport.async_get_plugin_release_versions(
                plugin_id, repo_url
            )

    configure_plugin_release_service(
        PluginReleaseService(
            installed_plugins=plugin_manager.get_installed_plugins,
            local_repo_plugins=plugin_manager.get_local_repo_plugins,
            market_plugins=plugin_manager.async_get_plugins_from_market,
            local_version=plugin_manager.get_local_plugin_version,
            identity=persistence.get_identity,
            version_flag=lambda: get_api_runtime_config_snapshot().version_flag,
            compatible_flags=lambda flag: (
                VERSION_BACKWARD_COMPATIBLE_FLAGS.get(flag, []) if flag else []
            ),
            has_release_cache=market_transport.async_has_plugin_release_cache,
            releases=market_transport.async_get_plugin_release_versions,
            refresh_releases=refresh_plugin_releases,
        )
    )
    configure_plugin_catalog_query(
        PluginCatalogQuery(
            installed_plugins=plugin_manager.get_installed_plugins,
            local_plugins=plugin_manager.get_local_plugins,
            local_repo_plugins=plugin_manager.get_local_repo_plugins,
            online_candidates=plugin_manager.async_get_online_plugin_candidates,
            process_plugins=plugin_manager.process_plugins_list,
            identities=persistence.list_identities,
        )
    )
    configure_plugin_rating_service(
        PluginRatingService(
            installed_plugins=lambda: get_configured_system_config().get(
                SystemConfigKey.UserInstalledPlugins
            )
            or [],
            statistic=MoviePilotServerHelper.async_get_plugin_statistic,
            ratings=MoviePilotServerHelper.async_get_plugin_ratings,
            rating=MoviePilotServerHelper.async_get_plugin_rating,
            submit=MoviePilotServerHelper.async_submit_plugin_rating,
        )
    )

    async def load_inventory(force: bool):
        """读取本轮配置市场和本地仓库的完整候选事实。"""
        return await inventory_reader.async_load(
            split_plugin_market_repo_urls(get_runtime_setting('PLUGIN_MARKET')),
            force=force,
        )

    async def reload_plugin_tree(plugin_id: str) -> object:
        """在线程池中重建源插件及其全部虚拟实例。"""
        return await run_in_threadpool_to_completion(
            plugin_manager.reload_plugin_tree,
            plugin_id,
        )

    async def refresh_plugin_registrations(plugin_id: str) -> None:
        """刷新源插件及其虚拟实例的调度、命令和路由注册。"""
        for target_id in plugin_manager.get_plugin_reload_targets(plugin_id):
            await run_in_threadpool_to_completion(
                _register_plugin_runtime,
                target_id,
            )

    command = PluginInstallCommand(
        persistence=persistence,
        installed_plugins_reader=lambda: get_configured_system_config().get(
            SystemConfigKey.UserInstalledPlugins
        ) or [],
        plugin_ids_provider=plugin_manager.get_plugin_ids,
        packages=package_manager,
        install_reporter=lambda plugin_id, repo_url: (
            MoviePilotServerHelper.async_install_plugin_reg(
                plugin_id=plugin_id,
                repo_url=repo_url,
            )
        ),
        target_reloader=reload_plugin_tree,
        rollback_reloader=reload_plugin_tree,
        registration_refresher=refresh_plugin_registrations,
        mutation=plugin_manager.mutation,
        package_write_guard=plugin_manager.suppress_plugin_monitor,
        restart_required_recorder=plugin_manager.mark_plugin_restart_required,
        clock=lambda: datetime.now(timezone.utc),
        transaction_id_factory=lambda: uuid.uuid4().hex,
    )
    gateway = PluginInstallGateway(
        inventory=load_inventory,
        identity=persistence.get_identity,
        candidate_compatibility=lambda candidate: (
            market_transport.check_plugin_system_version(candidate.dto)
        ),
        executor=command,
        clock=lambda: datetime.now(timezone.utc),
    )
    configure_plugin_install_service(gateway)
    configure_plugin_installation_recovery(
        PluginInstallationRecoveryService(
            persistence=persistence,
            packages=package_manager,
        )
    )
    configure_plugin_identity_migration(
        PluginIdentityMigrationService(
            persistence=persistence,
            inventory=load_inventory,
            installed_plugins=lambda: get_configured_system_config().get(
                SystemConfigKey.UserInstalledPlugins
            ) or [],
            is_virtual_instance=lambda plugin_id: (
                plugin_manager.get_plugin_instance(plugin_id) is not None
            ),
            clock=lambda: datetime.now(timezone.utc),
        )
    )

    def install_from_compat_helper(
        plugin_id: str,
        repo_url: str,
        package_version: str | None,
        release_version: str | None,
        force: bool,
    ) -> tuple[bool, str]:
        """保留本地来源定位；在线兼容参数不得升级为选源授权。"""
        local_sync = bool(repo_url and repo_url.startswith(LOCAL_REPO_PREFIX))
        return _run_plugin_install_sync(
            gateway,
            plugin_id=plugin_id,
            repo_url=repo_url if local_sync else "",
            package_version=package_version,
            release_version=release_version,
            force=force,
            local_sync=local_sync,
            explicit_source=local_sync,
        )

    async def async_install_from_compat_helper(
        plugin_id: str,
        repo_url: str,
        package_version: str | None,
        release_version: str | None,
        force: bool,
    ) -> tuple[bool, str]:
        """异步保留本地来源定位；在线兼容参数不得升级为选源授权。"""
        local_sync = bool(repo_url and repo_url.startswith(LOCAL_REPO_PREFIX))
        return await _run_plugin_install_async(
            gateway,
            plugin_id=plugin_id,
            repo_url=repo_url if local_sync else "",
            package_version=package_version,
            release_version=release_version,
            force=force,
            local_sync=local_sync,
            explicit_source=local_sync,
        )

    configure_plugin_install_gateway(
        install=install_from_compat_helper,
        async_install=async_install_from_compat_helper,
    )
    configure_plugin_legacy_import_services(
        diagnostics_configurator=configure_legacy_import_diagnostics,
        import_scanner=scan_plugin_legacy_imports,
    )
    configure_plugin_resource_import_preparer(_prepare_legacy_plugin_import)
    configure_site_auth_level_provider(lambda: SitesHelper().auth_level)
    configure_installed_plugins_provider(
        lambda: get_configured_system_config().get(SystemConfigKey.UserInstalledPlugins) or []
    )
    configure_plugin_route_refresher(register_plugin_api)
    configure_plugin_system(PluginSystemServices(
        market=market_client,
        package=package_manager,
        dependency=market_composition.dependency,
        dependency_manifest_status=dependency_manifest_status,
        compatible_flags=lambda flag: (
            [flag] + VERSION_BACKWARD_COMPATIBLE_FLAGS.get(flag, [])
            if flag else []
        ),
        frozen=SystemUtils.is_frozen,
        install=lambda **kwargs: _run_plugin_install_sync(gateway, **kwargs),
    ))
    configure_plugin_storage(PluginStorage(
        read=lambda key: get_configured_system_config().get(key),
        write=lambda key, value: get_configured_system_config().set(key, value),
        async_write=_async_write_plugin_config,
        delete=lambda key: get_configured_system_config().delete(key),
        delete_data=_delete_plugin_data,
    ))
    configure_plugin_database(_build_plugin_database())


def _register_plugin_runtime(plugin_id: str) -> None:
    """重建一个插件的定时任务、命令和动态路由注册。"""
    update_plugin_job(plugin_id)
    init_commands(plugin_id)
    register_plugin_api(plugin_id)


async def _collect_online_restore_plugins(
    persistence: PluginPersistenceService,
    installed_plugins: list[str],
) -> set[str]:
    """找出当前载荷为本地且仍保留可信在线来源的物理插件。"""
    restore_plugins: set[str] = set()
    seen: set[str] = set()
    for plugin_id in installed_plugins:
        try:
            normalized_id = normalize_physical_plugin_id(plugin_id)
        except ValueError:
            continue
        if normalized_id in seen:
            continue
        seen.add(normalized_id)
        identity = await persistence.get_identity(normalized_id)
        if (
            identity is not None
            and identity.trusted_source_type is not TrustedPluginSourceType.UNKNOWN
            and identity.payload_source_type is PluginPayloadSourceType.LOCAL
        ):
            restore_plugins.add(normalized_id)
    return restore_plugins


async def _run_plugin_install_async(
    gateway: PluginInstallGateway,
    *,
    plugin_id: str,
    repo_url: str,
    package_version: str | None,
    release_version: str | None,
    force: bool,
    local_sync: bool,
    explicit_source: bool,
    startup_token: PluginStartupLease | None = None,
) -> tuple[bool, str]:
    """把公开异步兼容入口转为统一 Gateway 结果。"""
    try:
        result = await gateway.install(
            plugin_id=plugin_id,
            repo_url=repo_url or None,
            package_version=package_version,
            release_version=release_version,
            force=force,
            explicit_source=explicit_source,
            startup_token=startup_token,
            local_sync=local_sync,
        )
        return result.success, result.message
    except Exception as error:  # noqa: BLE001 - 公开兼容入口以结果表达失败
        logger.error("插件 %s 异步安装失败：%s", plugin_id, error)
        return False, str(error)


def _run_plugin_install_sync(
    gateway: PluginInstallGateway,
    *,
    plugin_id: str,
    repo_url: str,
    package_version: str | None,
    release_version: str | None,
    force: bool,
    local_sync: bool,
    explicit_source: bool,
    startup_token: PluginStartupLease | None = None,
) -> tuple[bool, str]:
    """从插件工作线程把同步兼容调用提交到宿主事件循环。"""
    try:
        loop = main_loop_registry.require()
    except RuntimeError:
        return False, "插件安装服务当前不可用"
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None
    if current_loop is loop:
        return False, "事件循环内请使用 PluginHelper.async_install()"
    future = asyncio.run_coroutine_threadsafe(
        _run_plugin_install_async(
            gateway,
            plugin_id=plugin_id,
            repo_url=repo_url,
            package_version=package_version,
            release_version=release_version,
            force=force,
            local_sync=local_sync,
            explicit_source=explicit_source,
            startup_token=startup_token,
        ),
        loop,
    )
    try:
        return future.result()
    except Exception as error:  # noqa: BLE001 - 兼容入口以结果表达失败
        logger.error("插件 %s 同步安装失败：%s", plugin_id, error)
        return False, str(error)


def _build_plugin_catalog(plugin_mapper: Callable[..., Any]) -> PluginCatalogService:
    """在组合根连接目录用例、市场客户端、持久化读取和插件 DTO 映射。"""
    client = get_composed_plugin_market_client()
    return PluginCatalogService(
        market_loader=client.get_plugins,
        async_market_loader=client.async_get_plugins,
        installed_plugins_provider=lambda: get_configured_system_config().get(
            SystemConfigKey.UserInstalledPlugins
        ) or [],
        plugin_mapper=plugin_mapper,
        is_local_repo=PluginMarketClient.is_local_repo_url,
        version_compare=compare_version,
        warning=logger.warning,
        error=logger.error,
    )


async def sync_plugins(
    startup_token: PluginStartupLease | None = None,
) -> bool:
    """
    初始化安装插件，并动态注册后台任务及API
    """
    plugin_manager = None
    try:
        loop = main_loop_registry.require()
        plugin_manager = PluginManager()
        with plugin_manager.mutation("启动后同步插件"):
            await get_plugin_identity_migration().migrate()
            installed_plugins = get_configured_system_config().get(
                SystemConfigKey.UserInstalledPlugins
            ) or []
            online_restore_plugins = await _collect_online_restore_plugins(
                get_plugin_persistence(),
                installed_plugins,
            )
            plugin_manager.set_plugin_settling(True)
            return await _sync_plugins_admitted(
                plugin_manager,
                loop,
                startup_token,
                online_restore_plugins,
            )
    except PluginMutationRejectedError as error:
        logger.warning(str(error))
        return False
    except Exception as e:
        logger.error(f"插件初始化过程中出现异常: {e}")
        return False


async def _sync_plugins_admitted(
    plugin_manager: PluginManager,
    loop,
    startup_token: PluginStartupLease | None,
    online_restore_plugins: set[str],
) -> bool:
    """在一个 admission lease 内完成包、依赖、实例和动态路由同步。"""
    sync_result = await execute_task(
        loop,
        lambda: plugin_manager.sync(
            startup_token,
            online_restore_plugins=online_restore_plugins,
        ),
        "插件同步到本地",
    )
    if sync_result is None:
        return False
    dependency_result = await (
        plugin_manager.async_install_plugin_missing_dependencies_with_status()
    )
    if dependency_result is None:
        return False
    if not isinstance(dependency_result, PluginDependencyInstallResult):
        logger.error("缺失依赖项安装返回了无效结果，跳过插件重新初始化")
        return False
    previous_statuses = plugin_manager.get_plugin_runtime_statuses()
    classification = plugin_manager.classify_plugins()
    plugin_manager.apply_plugin_dependency_classification(classification)
    if not dependency_result.success:
        logger.error("缺失依赖项安装未完成，将继续激活当前已就绪插件")
    changed_ids = await execute_task(
        loop,
        lambda: _activate_ready_plugins(
            plugin_manager,
            classification.ready,
            sync_result,
            previous_statuses,
        ),
        "插件运行态激活",
    )
    if changed_ids is None:
        return False

    if not changed_ids:
        logger.debug("没有新的插件进入可运行状态")
        return False

    for plugin_id in changed_ids:
        register_plugin_api(plugin_id)
    if dependency_result.success:
        logger.info(f"后台插件加载完成，共处理 {len(changed_ids)} 个插件")
    else:
        logger.warning(
            f"缺失依赖项仍未全部恢复，已激活 {len(changed_ids)} 个就绪插件"
        )
    return True


def _activate_ready_plugins(
    plugin_manager: PluginManager,
    ready_ids: tuple[str, ...],
    synced_ids: list[str],
    previous_statuses: dict[str, PluginRuntimeStatus],
) -> list[str]:
    """在线程池中完成插件导入和初始化，避免阻塞 Web 事件循环。"""
    running_ids = set(plugin_manager.running_plugins)
    synced = {
        _plugin_source_id(plugin_manager, plugin_id)
        for plugin_id in synced_ids
    }
    changed_ids: list[str] = []
    for plugin_id in ready_ids:
        source_id = _plugin_source_id(plugin_manager, plugin_id)
        dependency_recovered = (
            previous_statuses.get(plugin_id)
            is PluginRuntimeStatus.DEPENDENCY_PENDING
        )
        if plugin_id in running_ids and (source_id in synced or dependency_recovered):
            plugin_manager.reload_plugin(plugin_id)
            changed_ids.append(plugin_id)
            continue
        if plugin_id not in running_ids:
            plugin_manager.start(plugin_id)
            changed_ids.append(plugin_id)
    return changed_ids


def _plugin_source_id(plugin_manager: PluginManager, plugin_id: str) -> str:
    """把物理插件和虚拟实例归一到同一个源码身份。"""
    source_id = plugin_manager.get_plugin_source_id(plugin_id)
    try:
        return normalize_physical_plugin_id(source_id)
    except ValueError:
        return source_id.lower()


def _local_plugin_sources(plugin_manager: PluginManager) -> set[str]:
    """返回安装清单中存在本地仓候选的物理插件身份。"""
    installed = {
        normalize_physical_plugin_id(plugin_id)
        for plugin_id in (
            get_configured_system_config().get(SystemConfigKey.UserInstalledPlugins)
            or []
        )
    }
    candidates: set[str] = set()
    for plugin in plugin_manager.get_local_repo_plugins():
        try:
            source_id = normalize_physical_plugin_id(plugin.id)
        except ValueError:
            continue
        if source_id in installed:
            candidates.add(source_id)
    return candidates


async def quiesce_plugins(timeout: float = 240.0) -> bool:
    """封口插件变更并停用 handler，保留超时 Future 的运行所有权。"""
    plugin_manager = PluginManager.get_existing_instance()
    if plugin_manager is None:
        return True
    return await plugin_manager.quiesce_plugins(timeout=timeout)


async def quiesce_plugin_services(timeout: float = 240.0) -> bool:
    """在事件结算后有界执行旧插件 close、stop_service hook。"""
    plugin_manager = PluginManager.get_existing_instance()
    if plugin_manager is None:
        return True
    return await plugin_manager.quiesce_plugin_services(timeout=timeout)


def finalize_plugins() -> bool:
    """在事件屏障封口后卸载已停用 handler 的插件实例。"""
    plugin_manager = PluginManager.get_existing_instance()
    if plugin_manager is None:
        return True
    return bool(plugin_manager.finalize_plugins())


async def execute_task(loop, task_func, task_name):
    """
    执行后台任务；取消调用方时仍持有同步线程直到真实完成。
    """
    try:
        # loop 参数属于既有调用 ABI；同步执行改由 completion-aware 适配器持有，
        # 避免外层 Task 被取消后把仍在修改插件源码/依赖的线程伪装成已结束。
        del loop
        result = await run_in_threadpool_to_completion(task_func)
        if isinstance(result, PluginDependencyInstallResult):
            processed_count = len(result.missing)
        elif isinstance(result, list):
            processed_count = len(result)
        else:
            processed_count = 0
        if processed_count:
            logger.debug(f"{task_name} 已完成，共处理 {processed_count} 个项目")
        else:
            logger.debug(f"没有新的 {task_name} 需要处理")
        return result
    except Exception as e:
        logger.error(f"{task_name} 时发生错误：{e}", exc_info=True)
        return None


def init_plugins():
    """
    初始化插件
    """
    plugin_manager = PluginManager()
    if not plugin_manager.reopen_plugins():
        raise RuntimeError("上一应用生命周期的插件后台服务仍未收敛")
    classification = plugin_manager.classify_plugins()
    plugin_manager.apply_plugin_dependency_classification(classification)
    plugin_manager.set_plugin_settling(True)
    deferred_sources = _local_plugin_sources(plugin_manager)
    immediate_ready = [
        plugin_id
        for plugin_id in classification.ready
        if _plugin_source_id(plugin_manager, plugin_id) not in deferred_sources
    ]
    for plugin_id in immediate_ready:
        plugin_manager.start(plugin_id)
    register_plugin_api()
    plugin_manager.start_monitor(reopen=True)
    logger.info(
        "插件启动分类：立即加载=%s，等待本地同步=%s，等待依赖=%s，等待源码=%s",
        len(immediate_ready),
        len(classification.ready) - len(immediate_ready),
        len(classification.missing_dependencies),
        len(classification.missing_source),
    )


def stop_plugin_monitor(timeout: float = 5.0) -> bool:
    """封口已创建管理器的文件监控线程，并返回是否完成收口。"""
    plugin_manager = PluginManager.get_existing_instance()
    if plugin_manager is None:
        return True
    try:
        return bool(plugin_manager.close_monitor(timeout=timeout))
    except Exception as e:
        logger.error(f"停止插件文件监控时发生错误：{e}", exc_info=True)
        return False


def stop_plugins() -> bool:
    """停止已创建的插件监控和运行实例，不在停机阶段反向物化管理器。"""
    try:
        plugin_manager = PluginManager.get_existing_instance()
        if plugin_manager is None:
            return True
        monitor_stopped = True
        try:
            monitor_stopped = plugin_manager.stop_monitor()
        finally:
            plugin_manager.stop()
        return bool(monitor_stopped)
    except Exception as e:
        logger.error(f"停止插件时发生错误：{e}", exc_info=True)
        return False
    finally:
        reset_plugin_catalog_query()
        reset_plugin_release_service()
        reset_plugin_rating_service()
        reset_plugin_runtime_owners()
        reset_plugin_market_composition()
