from pathlib import Path
from typing import Dict, List, Optional

from app.runtime.compat.diagnostics import (
    configure_legacy_import_diagnostics,
    scan_plugin_legacy_imports,
)
from app.runtime.compat.plugin_version_readiness import scan_plugin_version_readiness
from app.runtime.compat.resource_imports import scan_plugin_resource_imports
from app.application.plugin.routes import register_plugin_api
from app.runtime.config import global_vars
from app.runtime.config import settings
from app.runtime.extensions.plugin_manager import (
    PluginManager,
    configure_plugin_catalog_factory,
    configure_plugin_install_reporter,
    _configure_plugin_instance_persistence,
    _configure_plugin_instance_version_binding,
    configure_plugin_legacy_import_services,
    _configure_plugin_multi_version_probe,
    configure_plugin_resource_import_preparer,
    _configure_plugin_version_switch_notifier,
    configure_site_auth_level_provider,
)
from app.runtime.extensions.contract.dependency import (
    PluginDependencyInstallResult,
)
from app.application.messaging.message import MessageHelper
from app.application.plugin.catalog import PluginCatalogService
from app.application.plugin.data import DeletePluginDataCommand
from app.adapters.external.plugin.client import PluginMarketClient
from app.runtime.extensions.admission.instance_selection import (
    PluginInstanceTarget,
    configure_plugin_instance_targets,
)
from app.runtime.extensions.lifecycle.storage import (
    PluginStorage,
    configure_plugin_storage,
)
from app.runtime.extensions.lifecycle.system import (
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
from app.adapters.system.plugin.dependency import (
    PluginDependencyInstaller,
    describe_version_dependency_conflicts,
    find_version_dependency_conflicts,
    read_requirement_specifiers,
)
from app.adapters.system.plugin.manifest import dependency_manifest_status
from app.adapters.system.plugin.package import PluginPackageManager
from app.runtime.extensions.lifecycle.layout import (
    plugin_version_dirs,
    plugin_version_from_dir_name,
)
from app.adapters.system.host import SystemUtils
from app.db.oper.plugindata import PluginDataOper
from app.db.oper.pluginconfig import PluginConfigOper
from app.db.oper.systemconfig import SystemConfigOper
from app.db.session import SessionFactory
from app.db.uow import SqlAlchemyUnitOfWork
from app.runtime.extensions.contract.instance import DEFAULT_INSTANCE_ID
from app.runtime.log import (
    configure_plugin_log_dir_resolver,
    logger,
    set_plugin_instance_log_level,
)
from app.foundation.version import compare_version
from app.schemas.plugin import PluginRuntimeStatus
from app.schemas.types import SystemConfigKey


async def _async_write_plugin_config(key, value):
    """通过数据库操作器异步保存插件运行时配置。"""
    return await SystemConfigOper().async_set(key, value)


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


def _list_plugin_instance_targets(plugin_id: str) -> List[PluginInstanceTarget]:
    """列出插件全部实例在调用目标解析中所需的状态。

    :param plugin_id: 插件标识
    :return: 实例状态列表，一条实例配置都没有时为空列表
    """
    return [
        PluginInstanceTarget(
            instance_id=row.instance_id,
            is_enabled=bool(row.is_enabled),
            is_default_target=bool(row.is_default_target),
        )
        for row in PluginConfigOper().list_by_plugin(plugin_id)
    ]


def _write_plugin_instance_config(plugin_id: str, value) -> None:
    """写入插件默认实例配置行的业务配置。"""
    PluginConfigOper().upsert(plugin_id, DEFAULT_INSTANCE_ID, {"config_data": value})


async def _async_write_plugin_instance_config(plugin_id: str, value) -> None:
    """异步写入插件默认实例配置行的业务配置。"""
    await PluginConfigOper().async_upsert(plugin_id, DEFAULT_INSTANCE_ID, {"config_data": value})


def _delete_plugin_instance_config(plugin_id: str) -> bool:
    """删除插件默认实例配置行。"""
    return PluginConfigOper().delete_instance(plugin_id, DEFAULT_INSTANCE_ID)


def _upsert_plugin_instance_config_row(plugin_id: str, instance_id: str, config: dict) -> None:
    """按插件标识与实例标识写入或更新一行配置。"""
    PluginConfigOper().upsert(plugin_id, instance_id, {"config_data": config})


def _delete_plugin_instance_config_row(plugin_id: str, instance_id: str) -> bool:
    """按插件标识与实例标识删除一行配置，返回是否命中记录。"""
    return PluginConfigOper().delete_instance(plugin_id, instance_id)


def _delete_plugin_instance_data_rows(plugin_id: str, instance_id: str) -> None:
    """删除指定插件实例的全部业务数据，不影响其余实例。"""
    PluginDataOper().del_data(plugin_id, instance_id=instance_id)


def _read_plugin_instance_version(plugin_id: str, instance_id: str):
    """读取插件实例已生效的版本与版本跟随开关。

    :param plugin_id: 插件标识
    :param instance_id: 实例标识
    :return: `(已生效版本, 是否跟随默认实例)`；该实例没有配置行时为 None
    """
    row = PluginConfigOper().get(plugin_id, instance_id)
    if row is None:
        return None
    return row.plugin_version, bool(row.follow_default_version)


def _write_plugin_instance_version(plugin_id: str, instance_id: str, version: str) -> None:
    """把实例本次成功启动的版本登记为其已生效版本。

    :param plugin_id: 插件标识
    :param instance_id: 实例标识
    :param version: 本次生效的版本号
    """
    PluginConfigOper().upsert(plugin_id, instance_id, {"plugin_version": version})


def _write_plugin_instance_follow_default(
    plugin_id: str, instance_id: str, follow: bool
) -> None:
    """写入实例的版本跟随开关。

    :param plugin_id: 插件标识
    :param instance_id: 实例标识
    :param follow: 是否跟随默认实例的版本
    """
    PluginConfigOper().upsert(
        plugin_id, instance_id, {"follow_default_version": bool(follow)}
    )


def _notify_plugin_version_switch(title: str, text: str) -> None:
    """把插件版本切换失败投递为系统消息。

    :param title: 消息标题
    :param text: 消息正文
    """
    MessageHelper().put(text, title=title, role="system")


def plugin_multi_version_blockers(plugin_id: str, source_dirs: List[Path]) -> List[str]:
    """检查插件源码是否允许多版本并存，给出阻断原因。

    两类写法使插件无法同时跑两个版本：自引用绝对导入在版本化目录下必然
    ``ModuleNotFoundError``；在宿主共享声明基类上定义的模型会让两个版本映射到同名表，
    导入第二个版本时直接冲突。
    :param plugin_id: 插件目录名
    :param source_dirs: 待检查的插件源码目录，不存在的目录按无命中处理
    :return: 阻断原因列表，为空表示允许多版本并存
    """
    blockers: List[str] = []
    for source_dir in source_dirs:
        readiness = scan_plugin_version_readiness(plugin_id, Path(source_dir))
        blockers.extend(
            f"存在自引用绝对导入：{hit.file}:{hit.line} {hit.statement}；{hit.suggestion}"
            for hit in readiness.self_referential_imports
        )
        blockers.extend(
            f"在宿主共享声明基类上定义模型 {hit.class_name}：{hit.file}:{hit.line}"
            for hit in readiness.shared_base_models
        )
    return blockers


def plugin_version_coexistence_rejection(
    plugin_id: str,
    new_version: str,
    new_source_dir: Path,
    installed_versions: Dict[str, Path],
) -> Optional[str]:
    """判定待装版本能否与该插件已装的其它版本并存。

    先看插件写法是否允许多版本，再对两个版本共同依赖的包求版本约束交集，
    交集为空即判定不可并存，把故障从运行期提前到安装时明确拒绝。
    :param plugin_id: 插件ID
    :param new_version: 待装版本号
    :param new_source_dir: 待装版本的源码目录
    :param installed_versions: 已装版本号到其源码目录的映射
    :return: 拒绝说明；允许并存时为 None
    """
    blockers = plugin_multi_version_blockers(
        plugin_id.lower(), [Path(new_source_dir), *installed_versions.values()]
    )
    if blockers:
        return (
            f"插件 {plugin_id} 的写法不支持多版本并存，拒绝安装第二个版本："
            + "；".join(blockers)
        )
    new_requirements = read_requirement_specifiers(new_source_dir)
    for installed_version, installed_dir in sorted(installed_versions.items()):
        conflicts = find_version_dependency_conflicts(
            read_requirement_specifiers(installed_dir), new_requirements
        )
        if conflicts:
            return describe_version_dependency_conflicts(
                installed_version, new_version, conflicts
            )
    return None


def _prepare_legacy_plugin_import(*, plugin_id: str, plugin_dir: Path) -> None:
    """在执行旧插件顶层代码前准备其静态导入所需的宿主资源。"""
    for capability_id in scan_plugin_resource_imports(plugin_id, plugin_dir):
        acquire_managed_resource(
            capability_id,
            reason="legacy_plugin_import",
        )


def _resolve_plugin_instance_log_dir(plugin_id: str, instance_id: str) -> Path:
    """
    按插件实例持久化目录推导其日志目录：与业务数据目录同级的 logs 子目录。

    `app.runtime.log` 是依赖叶节点，不直接导入插件目录定位模块；这里是组合根，
    由它经 `plugin_instance_path` 完成插件标识与实例标识的路径分段校验。
    :param plugin_id: 插件标识
    :param instance_id: 实例标识
    :return: 该实例的日志目录
    """
    from app.runtime.extensions.lifecycle.paths import plugin_instance_path
    return plugin_instance_path(plugin_id, instance_id, "data").parent / "logs"


def _seed_plugin_instance_log_levels() -> None:
    """
    把数据库中已配置的实例日志等级预热进日志模块的进程内缓存。

    日志模块的等级缓存只在进程内存里，进程重启后为空；这里在插件加载完成后
    按已知插件逐一读取实例配置行，把非空的 log_level 覆盖重新注入缓存，
    避免重启后临时调高的排障等级静默丢失、回落成全局等级。
    """
    manager = PluginManager()
    for plugin_id in list(manager.plugins):
        for row in PluginConfigOper().list_by_plugin(plugin_id):
            if not row.log_level:
                continue
            try:
                set_plugin_instance_log_level(
                    plugin_id, row.instance_id, row.log_level, row.log_expires_at
                )
            except ValueError:
                logger.warning(
                    f"插件 {plugin_id} 实例 {row.instance_id} 的日志等级配置非法，"
                    f"已跳过预热：{row.log_level}"
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
        lambda: SystemConfigOper().get(SystemConfigKey.UserInstalledPlugins) or []
    )
    configure_plugin_catalog_factory(_build_plugin_catalog)
    configure_plugin_system(PluginSystemServices(
        market=market_client,
        package=PluginPackageManager(
            plugin_helper,
            version_dirs=plugin_version_dirs,
            coexistence_checker=plugin_version_coexistence_rejection,
            version_name_resolver=plugin_version_from_dir_name,
        ),
        dependency=PluginDependencyInstaller(
            plugin_helper,
            installed_plugins_provider=lambda: SystemConfigOper().get(
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
        read=lambda key: SystemConfigOper().get(key),
        write=lambda key, value: SystemConfigOper().set(key, value),
        async_write=_async_write_plugin_config,
        delete=lambda key: SystemConfigOper().delete(key),
        delete_data=_delete_plugin_data,
        read_config=_read_plugin_instance_config,
        write_config=_write_plugin_instance_config,
        async_write_config=_async_write_plugin_instance_config,
        delete_config=_delete_plugin_instance_config,
        list_instances=_list_plugin_instance_ids,
    ))
    configure_plugin_instance_targets(_list_plugin_instance_targets)
    _configure_plugin_instance_persistence(
        upsert_config=_upsert_plugin_instance_config_row,
        delete_config=_delete_plugin_instance_config_row,
        delete_data=_delete_plugin_instance_data_rows,
    )
    _configure_plugin_instance_version_binding(
        read_binding=_read_plugin_instance_version,
        write_version=_write_plugin_instance_version,
        write_follow_default=_write_plugin_instance_follow_default,
    )
    _configure_plugin_version_switch_notifier(_notify_plugin_version_switch)
    _configure_plugin_multi_version_probe(plugin_multi_version_blockers)
    configure_plugin_log_dir_resolver(_resolve_plugin_instance_log_dir)


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
    plugin_manager = None
    try:
        configure_plugin_services()
        loop = global_vars.loop
        plugin_manager = PluginManager()
        plugin_manager.set_plugin_settling(True)

        sync_result = await execute_task(loop, plugin_manager.sync, "插件同步到本地")
        dependency_result = await execute_task(
            loop,
            plugin_manager.install_plugin_missing_dependencies_with_status,
            "缺失依赖项安装",
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

        # 预热本轮新进入运行态的实例日志等级覆盖
        _seed_plugin_instance_log_levels()
        for plugin_id in changed_ids:
            register_plugin_api(plugin_id)
        if dependency_result.success:
            logger.info(f"后台插件加载完成，共处理 {len(changed_ids)} 个插件")
        else:
            logger.warning(
                f"缺失依赖项仍未全部恢复，已激活 {len(changed_ids)} 个就绪插件"
            )
        return True
    except Exception as e:
        logger.error(f"插件初始化过程中出现异常: {e}")
        return False


def _activate_ready_plugins(
    plugin_manager: PluginManager,
    ready_ids: tuple[str, ...],
    synced_ids: list[str],
    previous_statuses: dict[str, PluginRuntimeStatus],
) -> list[str]:
    """在线程池中完成插件导入和初始化，避免阻塞 Web 事件循环。"""
    # 运行态按实例键登记，这里比对的是插件族，取按族去重后的插件ID
    running_ids = set(plugin_manager.get_running_plugin_ids())
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


async def execute_task(loop, task_func, task_name):
    """
    执行后台任务
    """
    try:
        result = await loop.run_in_executor(None, task_func)
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
    classification = plugin_manager.classify_plugins()
    plugin_manager.apply_plugin_dependency_classification(classification)
    plugin_manager.set_plugin_settling(True)
    for plugin_id in classification.ready:
        plugin_manager.start(plugin_id)
    # 预热已配置的实例日志等级覆盖，避免进程重启后临时调高的排障等级静默丢失
    _seed_plugin_instance_log_levels()
    register_plugin_api()
    plugin_manager.start_monitor()
    logger.info(
        f"插件启动分类：立即加载={len(classification.ready)}，"
        f"等待依赖={len(classification.missing_dependencies)}，"
        f"等待源码={len(classification.missing_source)}"
    )
    # 回收没有实例引用、不在保留窗口内的旧版本目录。只有插件加载完成后才能读到
    # 全部实例的版本绑定，因此固定放在启动流程末尾；安装流程不做这件事，避免
    # 与用户正打算回退到旧版本的意图冲突。失败不阻断启动，只记错误日志
    try:
        plugin_manager.recycle_plugin_versions()
    except Exception as err:
        logger.error(f"插件版本回收出错：{err}")


def stop_plugins():
    """
    停止插件
    """
    try:
        plugin_manager = PluginManager()
        try:
            plugin_manager.stop_monitor()
        finally:
            plugin_manager.stop()
    except Exception as e:
        logger.error(f"停止插件时发生错误：{e}", exc_info=True)
