from pathlib import Path

from app.runtime.compat.diagnostics import (
    configure_legacy_import_diagnostics,
    scan_plugin_legacy_imports,
)
from app.runtime.compat.resource_imports import scan_plugin_resource_imports
from app.application.plugin.routes import register_plugin_api
from app.runtime.config import global_vars
from app.runtime.settings import RuntimeSettingsCompat

settings = RuntimeSettingsCompat()
from app.runtime.extensions.plugin_manager import (
    PluginManager,
    configure_plugin_catalog_factory,
    configure_plugin_install_reporter,
    configure_plugin_legacy_import_services,
    configure_plugin_resource_import_preparer,
    configure_site_auth_level_provider,
)
from app.runtime.execution import run_in_threadpool_to_completion
from app.runtime.extensions.plugin.dependency import PluginDependencyInstallResult
from app.application.plugin.catalog import PluginCatalogService
from app.application.plugin.data import DeletePluginDataCommand
from app.adapters.external.plugin.client import PluginMarketClient
from app.runtime.extensions.plugin.storage import (
    PluginStorage,
    configure_plugin_storage,
)
from app.runtime.extensions.plugin.system import (
    PluginSystemServices,
    configure_plugin_system,
)
from app.runtime.managed_resources import acquire_managed_resource
from app.application.site.sites import SitesHelper  # pylint: disable=import-error,no-name-in-module
from app.adapters.external.server import MoviePilotServerHelper
from app.adapters.external.market import (
    PluginHelper,
    VERSION_BACKWARD_COMPATIBLE_FLAGS,
    configure_installed_plugins_provider,
)
from app.adapters.system.plugin.dependency import PluginDependencyInstaller
from app.adapters.system.plugin.manifest import dependency_manifest_status
from app.adapters.system.plugin.package import PluginPackageManager
from app.adapters.system.host import SystemUtils
from app.db.oper.plugindata import PluginDataOper
from app.application.configuration import get_configured_system_config
from app.db.session import SessionFactory
from app.db.uow import SqlAlchemyUnitOfWork
from app.runtime.log import logger
from app.foundation.version import compare_version
from app.schemas.plugin import PluginRuntimeStatus
from app.schemas.exception import PluginMutationRejectedError
from app.schemas.types import SystemConfigKey


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


def _prepare_legacy_plugin_import(*, plugin_id: str, plugin_dir: Path) -> None:
    """在执行旧插件顶层代码前准备其静态导入所需的宿主资源。"""
    for capability_id in scan_plugin_resource_imports(plugin_id, plugin_dir):
        acquire_managed_resource(
            capability_id,
            reason="legacy_plugin_import",
        )


def configure_plugin_services() -> None:
    """把兼容诊断、远程上报和站点认证等级装配到插件管理器。"""
    plugin_helper = PluginHelper()
    market_client = PluginMarketClient(plugin_helper)
    configure_plugin_legacy_import_services(
        diagnostics_configurator=configure_legacy_import_diagnostics,
        import_scanner=scan_plugin_legacy_imports,
    )
    configure_plugin_resource_import_preparer(_prepare_legacy_plugin_import)
    configure_plugin_install_reporter(MoviePilotServerHelper.install_plugin_reg)
    configure_site_auth_level_provider(lambda: SitesHelper().auth_level)
    configure_installed_plugins_provider(
        lambda: get_configured_system_config().get(SystemConfigKey.UserInstalledPlugins) or []
    )
    configure_plugin_catalog_factory(_build_plugin_catalog)
    configure_plugin_system(PluginSystemServices(
        market=market_client,
        package=PluginPackageManager(plugin_helper),
        dependency=PluginDependencyInstaller(
            plugin_helper,
            installed_plugins_provider=lambda: get_configured_system_config().get(
                SystemConfigKey.UserInstalledPlugins
            ) or [],
            plugin_dir=Path(settings.ROOT_PATH) / "app" / "plugins",
        ),
        dependency_manifest_status=dependency_manifest_status,
        compatible_flags=lambda flag: (
            [flag] + VERSION_BACKWARD_COMPATIBLE_FLAGS.get(flag, [])
            if flag else []
        ),
        frozen=SystemUtils.is_frozen,
    ))
    configure_plugin_storage(PluginStorage(
        read=lambda key: get_configured_system_config().get(key),
        write=lambda key, value: get_configured_system_config().set(key, value),
        async_write=_async_write_plugin_config,
        delete=lambda key: get_configured_system_config().delete(key),
        delete_data=_delete_plugin_data,
    ))


def _build_plugin_catalog(manager: PluginManager) -> PluginCatalogService:
    """在组合根连接目录用例、市场客户端、持久化读取和插件 DTO 映射。"""
    client = PluginMarketClient()
    return PluginCatalogService(
        market_loader=client.get_plugins,
        async_market_loader=client.async_get_plugins,
        installed_plugins_provider=lambda: get_configured_system_config().get(
            SystemConfigKey.UserInstalledPlugins
        ) or [],
        plugin_mapper=manager._process_plugin_info,
        is_local_repo=PluginMarketClient.is_local_repo_url,
        version_compare=compare_version,
        warning=logger.warning,
        error=logger.error,
    )


async def sync_plugins() -> bool:
    """
    初始化安装插件，并动态注册后台任务及API
    """
    plugin_manager = None
    try:
        loop = global_vars.loop
        plugin_manager = PluginManager()
        with plugin_manager.mutation("启动后同步插件"):
            configure_plugin_services()
            plugin_manager.set_plugin_settling(True)
            return await _sync_plugins_admitted(plugin_manager, loop)
    except PluginMutationRejectedError as error:
        logger.warning(str(error))
        return False
    except Exception as e:
        logger.error(f"插件初始化过程中出现异常: {e}")
        return False


async def _sync_plugins_admitted(plugin_manager: PluginManager, loop) -> bool:
    """在一个 admission lease 内完成包、依赖、实例和动态路由同步。"""
    sync_result = await execute_task(loop, plugin_manager.sync, "插件同步到本地")
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
            sync_result or [],
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
    synced = set(synced_ids)
    changed_ids: list[str] = []
    for plugin_id in ready_ids:
        dependency_recovered = (
            previous_statuses.get(plugin_id)
            is PluginRuntimeStatus.DEPENDENCY_PENDING
        )
        if plugin_id in running_ids and (plugin_id in synced or dependency_recovered):
            plugin_manager.reload_plugin(plugin_id)
            changed_ids.append(plugin_id)
            continue
        if plugin_id not in running_ids:
            plugin_manager.start(plugin_id)
            changed_ids.append(plugin_id)
    return changed_ids


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
    configure_plugin_services()
    plugin_manager = PluginManager()
    if not plugin_manager.reopen_plugins():
        raise RuntimeError("上一应用生命周期的插件后台服务仍未收敛")
    classification = plugin_manager.classify_plugins()
    plugin_manager.apply_plugin_dependency_classification(classification)
    plugin_manager.set_plugin_settling(True)
    for plugin_id in classification.ready:
        plugin_manager.start(plugin_id)
    register_plugin_api()
    plugin_manager.start_monitor(reopen=True)
    logger.info(
        "插件启动分类：立即加载=%s，等待依赖=%s，等待源码=%s",
        len(classification.ready),
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
