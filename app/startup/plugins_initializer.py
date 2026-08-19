from pathlib import Path

from app.runtime.compat.diagnostics import (
    configure_legacy_import_diagnostics,
    scan_plugin_legacy_imports,
)
from app.runtime.compat.resource_imports import scan_plugin_resource_imports
from app.runtime.config import global_vars
from app.runtime.config import settings
from app.runtime.extensions.plugin_manager import (
    PluginManager,
    configure_plugin_catalog_factory,
    configure_plugin_install_reporter,
    configure_plugin_legacy_import_services,
    configure_plugin_resource_import_preparer,
    configure_site_auth_level_provider,
)
from app.application.plugin.catalog import PluginCatalogService
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
from app.application.site.sites import SitesHelper  # pylint: disable=no-name-in-module
from app.adapters.external.server import MoviePilotServerHelper
from app.adapters.external.market import (
    PluginHelper,
    VERSION_BACKWARD_COMPATIBLE_FLAGS,
    configure_installed_plugins_provider,
)
from app.adapters.system.plugin.dependency import PluginDependencyInstaller
from app.adapters.system.plugin.package import PluginPackageManager
from app.adapters.system.host import SystemUtils
from app.db.oper.plugindata import PluginDataOper
from app.db.oper.pluginconfig import PluginConfigOper
from app.db.oper.systemconfig import SystemConfigOper
from app.runtime.extensions.instance import DEFAULT_INSTANCE_ID
from app.runtime.log import logger
from app.foundation.version import compare_version
from app.schemas.types import SystemConfigKey


async def _async_write_plugin_config(key, value):
    """通过数据库操作器异步保存插件运行时配置。"""
    return await SystemConfigOper().async_set(key, value)


def _read_plugin_instance_config(plugin_id: str, instance_id: str = DEFAULT_INSTANCE_ID):
    """读取插件某个实例配置行的业务配置。

    :param plugin_id: 插件标识
    :param instance_id: 实例标识，缺省为默认实例
    :return: 该实例的业务配置，未登记时为 None
    """
    row = PluginConfigOper().get(plugin_id, instance_id)
    return row.config_data if row else None


def _list_plugin_instance_ids(plugin_id: str) -> list:
    """列出插件已登记的全部实例标识。

    :param plugin_id: 插件标识
    :return: 实例标识列表，按默认实例优先、其余按标识升序排列
    """
    instance_ids = {row.instance_id for row in PluginConfigOper().list_by_plugin(plugin_id)}
    ordered = sorted(instance_ids - {DEFAULT_INSTANCE_ID})
    if DEFAULT_INSTANCE_ID in instance_ids:
        ordered.insert(0, DEFAULT_INSTANCE_ID)
    return ordered


def _write_plugin_instance_config(plugin_id: str, value) -> None:
    """写入插件默认实例配置行的业务配置。"""
    PluginConfigOper().upsert(plugin_id, DEFAULT_INSTANCE_ID, {"config_data": value})


async def _async_write_plugin_instance_config(plugin_id: str, value) -> None:
    """异步写入插件默认实例配置行的业务配置。"""
    await PluginConfigOper().async_upsert(plugin_id, DEFAULT_INSTANCE_ID, {"config_data": value})


def _delete_plugin_instance_config(plugin_id: str) -> bool:
    """删除插件默认实例配置行。"""
    return PluginConfigOper().delete_instance(plugin_id, DEFAULT_INSTANCE_ID)


def _prepare_legacy_plugin_import(*, plugin_id: str, plugin_dir: Path) -> None:
    """在执行旧插件顶层代码前准备其静态导入所需的宿主资源。"""
    for capability_id in scan_plugin_resource_imports(plugin_id, plugin_dir):
        acquire_managed_resource(
            capability_id,
            reason="legacy_plugin_import",
        )


def _configure_plugin_services() -> None:
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
        lambda: SystemConfigOper().get(SystemConfigKey.UserInstalledPlugins) or []
    )
    configure_plugin_catalog_factory(_build_plugin_catalog)
    configure_plugin_system(PluginSystemServices(
        market=market_client,
        package=PluginPackageManager(plugin_helper),
        dependency=PluginDependencyInstaller(
            plugin_helper,
            installed_plugins_provider=lambda: SystemConfigOper().get(
                SystemConfigKey.UserInstalledPlugins
            ) or [],
            plugin_dir=Path(settings.ROOT_PATH) / "app" / "plugins",
        ),
        compatible_flags=lambda flag: (
            [flag] + VERSION_BACKWARD_COMPATIBLE_FLAGS.get(flag, [])
            if flag else []
        ),
        frozen=SystemUtils.is_frozen,
    ))
    configure_plugin_storage(PluginStorage(
        read=lambda key: SystemConfigOper().get(key),
        write=lambda key, value: SystemConfigOper().set(key, value),
        async_write=_async_write_plugin_config,
        delete=lambda key: SystemConfigOper().delete(key),
        delete_data=lambda plugin_id: PluginDataOper().del_data(plugin_id),
        read_config=_read_plugin_instance_config,
        write_config=_write_plugin_instance_config,
        async_write_config=_async_write_plugin_instance_config,
        delete_config=_delete_plugin_instance_config,
        list_instances=_list_plugin_instance_ids,
    ))


def _build_plugin_catalog(manager: PluginManager) -> PluginCatalogService:
    """在组合根连接目录用例、市场客户端、持久化读取和插件 DTO 映射。"""
    client = PluginMarketClient()
    return PluginCatalogService(
        market_loader=client.get_plugins,
        async_market_loader=client.async_get_plugins,
        installed_plugins_provider=lambda: SystemConfigOper().get(
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
    try:
        _configure_plugin_services()
        loop = global_vars.loop
        plugin_manager = PluginManager()

        sync_result = await execute_task(loop, plugin_manager.sync, "插件同步到本地")
        resolved_dependencies = await execute_task(loop, plugin_manager.install_plugin_missing_dependencies,
                                                   "缺失依赖项安装")
        # 判断是否需要进行插件初始化
        if not sync_result and not resolved_dependencies:
            logger.debug("没有新的插件同步到本地或缺失依赖项需要安装")
            return False

        # 继续执行后续的插件初始化步骤
        logger.info("正在重新初始化插件")
        # 重新初始化插件
        plugin_manager.init_config()
        # 重新注册插件API
        register_plugin_api()
        logger.info("所有插件初始化完成")
        return True
    except Exception as e:
        logger.error(f"插件初始化过程中出现异常: {e}")
        return False


async def execute_task(loop, task_func, task_name):
    """
    执行后台任务
    """
    try:
        result = await loop.run_in_executor(None, task_func)
        if isinstance(result, list) and result:
            logger.debug(f"{task_name} 已完成，共处理 {len(result)} 个项目")
        else:
            logger.debug(f"没有新的 {task_name} 需要处理")
        return result
    except Exception as e:
        logger.error(f"{task_name} 时发生错误：{e}", exc_info=True)
        return []


def register_plugin_api():
    """
    插件启动后注册插件API
    """
    from app.api.endpoints import plugin
    plugin.register_plugin_api()


def init_plugins():
    """
    初始化插件
    """
    _configure_plugin_services()
    PluginManager().start()
    register_plugin_api()


def stop_plugins():
    """
    停止插件
    """
    try:
        plugin_manager = PluginManager()
        plugin_manager.stop()
        plugin_manager.stop_monitor()
    except Exception as e:
        logger.error(f"停止插件时发生错误：{e}", exc_info=True)
