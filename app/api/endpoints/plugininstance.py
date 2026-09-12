"""插件实例生命周期端点：分身创建与恢复、启停、彻底清理与卸载。

这些路由并入 ``plugin`` 路由器，路径与单文件时期完全一致；拆分只是让实例生命周期
与插件目录、市场、静态资源等关注点各自成篇。
"""

from typing import Annotated, Any, List

from fastapi import Depends, Query, Response

from app.api.dependencies.auth import get_current_active_superuser
from app.api.dependencies.plugin import get_plugin_config_command
from app.api.principal import ApiPrincipal
from app.api.response import (
    COLLECTION_TOTAL_HEADER,
    COLLECTION_TOTAL_OPENAPI_KEY,
    CompatibleCountParam,
    CompatiblePageParam,
    ResponseAPIRouter,
    resolve_compatible_pagination,
)
from app.application.commands import init_commands
from app.application.configuration import get_configured_system_config
from app.application.plugin.config import PluginConfigCommand, PluginPurgeScope
from app.application.plugin.folders import add_clone_to_plugin_folder, remove_plugin_from_folders
from app.application.plugin.routes import register_plugin_api, remove_plugin_api
from app.application.plugin.runtime import get_plugin_manager
from app.application.scheduling import remove_plugin_job, update_plugin_job
from app.runtime.log import logger
from app.schemas.exception import PluginMutationRejectedError
from app.schemas.plugin import PluginCloneOutcome as _SchemaPluginCloneOutcome
from app.schemas.plugin import PluginCloneRequest as _SchemaPluginCloneRequest
from app.schemas.plugin import (
    PluginInstanceEnabledRequest as _SchemaPluginInstanceEnabledRequest,
)
from app.schemas.plugin import PluginInstancePurgeOutcome as _SchemaPluginInstancePurgeOutcome
from app.schemas.plugin import PluginInstancePurgeRequest as _SchemaPluginInstancePurgeRequest
from app.schemas.plugin import PluginRestorableInstance as _SchemaPluginRestorableInstance
from app.schemas.response import Response as _SchemaResponse
from app.schemas.types import SystemConfigKey

router = ResponseAPIRouter()


def register_plugin(plugin_id: str) -> None:
    """
    注册一个插件相关的服务
    """
    # 注册插件服务
    update_plugin_job(plugin_id)
    # 注册菜单命令
    init_commands(plugin_id)
    # 注册插件API
    register_plugin_api(plugin_id)


@router.get(  # type: ignore[misc]
    "/clone/{plugin_id}/restorable",
    summary="列出可恢复的已卸载分身",
    response_model=List[_SchemaPluginRestorableInstance],
    openapi_extra={COLLECTION_TOTAL_OPENAPI_KEY: True},
)
def plugin_restorable_instances(
    plugin_id: str,
    _: ApiPrincipal = Depends(get_current_active_superuser),
    page: CompatiblePageParam = None,
    count: CompatibleCountParam = None,
    response: Response = None,
) -> List[_SchemaPluginRestorableInstance]:
    """
    列出该插件名下已卸载、设置仍留存可被恢复的分身

    卸载只把启用位置假，配置、展示名与锚定版本都还留在那一行上。在册的分身不在
    此列——它们的配置正被使用，摆进恢复选择器只会让人误以为能把活着的实例再建一遍。

    未指定分页时返回完整清单：一个插件的历史分身数量有限，恢复选择器要一次看全。
    """
    plugin_manager = get_plugin_manager()
    plugin_id = _normalize_registered_plugin_id(plugin_manager, plugin_id)
    instances = [
        _SchemaPluginRestorableInstance(**item)
        for item in plugin_manager.get_restorable_plugin_instances(plugin_id)
    ]
    if response is not None:
        response.headers[COLLECTION_TOTAL_HEADER] = str(len(instances))
    if page is not None or count is not None:
        page, count = resolve_compatible_pagination(page, count)
        assert page is not None and count is not None
        return instances[(page - 1) * count : page * count]
    return instances


@router.post(  # type: ignore[misc]
    "/instance/{instance_id}/enabled",
    summary="启用或停用插件实例",
    response_model=_SchemaResponse[None],
)
def set_plugin_instance_enabled(
    instance_id: str,
    request: _SchemaPluginInstanceEnabledRequest,
    _: ApiPrincipal = Depends(get_current_active_superuser),
) -> Any:
    """
    启用或停用一个实例，本体与分身共用这一个入口

    停用只把启用位置假：配置、展示信息与锚定版本原样留在那一行，再次启用即恢复，
    因而它与卸载是两件事——卸载会清掉锚定版本，彻底清理才删数据。
    """
    plugin_manager = get_plugin_manager()
    try:
        changed = plugin_manager.set_plugin_instance_enabled(instance_id, request.enabled)
    except PluginMutationRejectedError as error:
        return _SchemaResponse(success=False, message=str(error))
    if not changed:
        # 两条完整句子而不是拼接中文片段：片段会被当成占位值，翻译时落不到模式上
        return _SchemaResponse(
            success=False,
            message=(
                f"实例 {instance_id} 不存在或已处于启用状态"
                if request.enabled
                else f"实例 {instance_id} 不存在或已处于停用状态"
            ),
        )
    return _SchemaResponse(success=True)


@router.post(  # type: ignore[misc]
    "/instance/{instance_id}/purge",
    summary="按选定范围彻底清理插件实例",
    response_model=_SchemaResponse[_SchemaPluginInstancePurgeOutcome],
)
def purge_plugin_instance(
    instance_id: str,
    request: _SchemaPluginInstancePurgeRequest,
    _: ApiPrincipal = Depends(get_current_active_superuser),
    command: PluginConfigCommand = Depends(get_plugin_config_command),
) -> Any:
    """
    按用户逐项勾选的范围彻底清理一个实例的配置与数据

    与停用是两件事：停用只把启用位置假、设置原样留着且可随时恢复，这里才真正删除
    用户数据，因而范围完全由请求给定，一项没选直接拒绝。分身会连同实例行一并消失，
    本体的行则保留——它还承载着「这个插件应当装载」。
    """
    result = command.purge(
        instance_id,
        PluginPurgeScope(
            config=request.config,
            plugin_data=request.plugin_data,
            own_database=request.own_database,
            data_directory=request.data_directory,
        ),
    )
    if not result.success:
        return _SchemaResponse(success=False, message=result.message)
    return _SchemaResponse(
        success=True,
        data=_SchemaPluginInstancePurgeOutcome(
            purged=list(result.purged),
            instance_removed=result.instance_removed,
        ),
    )


@router.post(  # type: ignore[misc]
    "/clone/{plugin_id}",
    summary="创建插件分身",
    response_model=_SchemaResponse[_SchemaPluginCloneOutcome],
)
def clone_plugin(
    plugin_id: str,
    clone_data: _SchemaPluginCloneRequest,
    _: ApiPrincipal = Depends(get_current_active_superuser),
) -> Any:
    """
    创建插件分身
    """
    plugin_manager = get_plugin_manager()
    try:
        with plugin_manager.mutation(f"创建插件 {plugin_id} 分身"):
            success, message = plugin_manager.clone_plugin(
                plugin_id=plugin_id,
                suffix=clone_data.suffix,
                name=clone_data.name,
                description=clone_data.description,
                icon=clone_data.icon,
                pinned_version=clone_data.pinned_version,
                restore_previous=clone_data.restore_previous,
            )

            if success:
                # 分身此时已经创建并加载完成，后面只是补齐宿主注册。这一步失败
                # 不能报成「创建失败」：分身确实已经存在，用户照提示重试只会撞上
                # 「分身已存在」，真正的原因反而被这句话盖掉。
                try:
                    register_plugin(message)
                    # 将分身插件添加到原插件所在的文件夹中
                    add_clone_to_plugin_folder(plugin_id, message)
                except Exception as register_error:  # noqa: BLE001
                    logger.error(f"插件分身 {message} 已创建，注册宿主能力失败：{register_error}")
                    return _SchemaResponse(
                        success=False,
                        message=(
                            f"插件分身 {message} 已创建，但注册定时任务或路由失败："
                            f"{register_error}；请检查该分身配置后重载插件"
                        ),
                    )
                return _SchemaResponse(
                    success=True,
                    message="插件分身创建成功",
                    data=_SchemaPluginCloneOutcome(instance_id=message),
                )
            return _SchemaResponse(success=False, message=message)
    except Exception as e:
        logger.error(f"创建插件分身失败：{str(e)}")
        return _SchemaResponse(success=False, message=f"创建插件分身失败：{str(e)}")


def _normalize_registered_plugin_id(plugin_manager: Any, plugin_id: str) -> str:
    """按注册表的真实键归一化插件ID。

    停止走的是大小写不敏感解析，而已装清单、实例查询与配置删除都是精确比较。
    大小写不符时分身守卫、清单移除、配置与数据删除会被一路跳过，插件却真的被
    停掉，最后还回报成功。

    :param plugin_manager: 插件管理器
    :param plugin_id: 调用方传入的插件ID
    :return: 注册表中的规范插件ID，未注册时原样返回
    """
    return next(
        (
            registered
            for registered in plugin_manager.plugins
            if registered.casefold() == plugin_id.casefold()
        ),
        plugin_id,
    )


def _uninstall_one_plugin(plugin_manager: Any, plugin_id: str) -> None:
    """执行单个插件或分身的完整卸载序列。

    本体与分身共用同一串动作，级联卸载因此不会与单体卸载产生行为漂移。
    调用方需自行持有 ``mutation`` 上下文并处理异常。

    :param plugin_manager: 插件管理器
    :param plugin_id: 已归一化的插件ID
    """
    virtual_instance = plugin_manager.get_plugin_instance(plugin_id)
    config_oper = get_configured_system_config()
    # 删除已安装信息
    install_plugins = config_oper.get(SystemConfigKey.UserInstalledPlugins) or []
    for plugin in install_plugins:
        if plugin == plugin_id:
            install_plugins.remove(plugin)
            break
    config_oper.set(SystemConfigKey.UserInstalledPlugins, install_plugins)
    # 移除插件API
    remove_plugin_api(plugin_id)
    # 移除插件服务
    remove_plugin_job(plugin_id)
    # 删除必须晚于停止：停机钩子会重建刚删的自有库；停止同时注销插件类，故删除一律按 force
    plugin_manager.stop(plugin_id)
    # 分身与本体一致：卸载只移除实例身份，配置与业务数据保留。本体重装后设置能
    # 自动回来，分身的复原动作是在「创建分身」里按 ID 取回；要彻底清空另有「重置」
    # 入口，以及卸载弹窗里的可选清除范围。
    if virtual_instance:
        plugin_manager.delete_plugin_instance(plugin_id)
    else:
        # 卸载的是源插件本体：它的版本绑定另有一条记录，删实例的入口只认
        # 分身，不清会留下带着钉版本、日志等级与默认目标置位的孤儿记录，
        # 重装同名插件时被静默继承。
        plugin_manager.delete_plugin_host_binding(plugin_id)
    # 从插件文件夹中移除该插件
    remove_plugin_from_folders(plugin_id)
    # 移除插件
    plugin_manager.remove_plugin(plugin_id)


@router.delete(  # type: ignore[misc]
    "/{plugin_id}", summary="卸载插件", response_model=_SchemaResponse[None]
)
def uninstall_plugin(
    plugin_id: str,
    _: ApiPrincipal = Depends(get_current_active_superuser),
    cascade: Annotated[
        bool,
        Query(description="卸载源插件本体时是否一并卸载它的全部分身"),
    ] = False,
) -> Any:
    """
    卸载插件

    ``cascade`` 默认关闭：级联是破坏性放大操作，不能因为新增参数就让既有调用方
    的行为悄悄改变。开启时先逐个卸载分身、任一失败立即中止且本体不动，全部成功
    后才卸本体——反过来会留下指向不存在源码的孤儿分身。

    级联跑在单个 ``mutation`` 上下文内，但这**不是数据库事务**：配置键、业务数据、
    自有库文件与插件目录分属不同存储，已删内容无法回滚。真实保证只有两条——不会
    出现「本体没了、分身还在」，以及中途失败时本体与剩余分身保持完整、可重试。
    """
    plugin_manager = get_plugin_manager()
    plugin_id = _normalize_registered_plugin_id(plugin_manager, plugin_id)
    try:
        with plugin_manager.mutation(f"卸载插件 {plugin_id}"):
            virtual_instance = plugin_manager.get_plugin_instance(plugin_id)
            source_instances = plugin_manager.get_plugin_source_instances(plugin_id)
            if not virtual_instance and source_instances:
                if not cascade:
                    instance_ids = "、".join(
                        item.instance_id for item in source_instances
                    )
                    return _SchemaResponse(
                        success=False,
                        message=f"请先卸载该插件的分身：{instance_ids}",
                    )
                for instance in source_instances:
                    clone_id = _normalize_registered_plugin_id(
                        plugin_manager,
                        instance.instance_id,
                    )
                    try:
                        _uninstall_one_plugin(plugin_manager, clone_id)
                    except Exception as clone_error:  # noqa: BLE001
                        logger.error(f"级联卸载分身 {clone_id} 失败：{clone_error}")
                        return _SchemaResponse(
                            success=False,
                            message=(
                                f"卸载分身 {clone_id} 失败：{clone_error}；"
                                f"插件 {plugin_id} 与剩余分身未被改动，可重试"
                            ),
                        )
            _uninstall_one_plugin(plugin_manager, plugin_id)
            return _SchemaResponse(success=True)
    except PluginMutationRejectedError as error:
        return _SchemaResponse(success=False, message=str(error))
