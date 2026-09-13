"""插件分身的创建、恢复与可恢复清单接口。

这些路由并入 ``plugin`` 路由器，路径与单文件时期完全一致；单独成篇只是让分身创建与
插件目录、市场、静态资源等关注点各自分开，与 ``pluginfolder``/``pluginloglevel``/
``plugintarget`` 的切分方式一致。
"""

from typing import Any, List

from fastapi import Depends, Response

from app.api.dependencies.auth import get_current_active_superuser
from app.api.endpoints.plugin import register_plugin
from app.api.principal import ApiPrincipal
from app.api.response import (
    COLLECTION_TOTAL_HEADER,
    COLLECTION_TOTAL_OPENAPI_KEY,
    CompatibleCountParam,
    CompatiblePageParam,
    ResponseAPIRouter,
    resolve_compatible_pagination,
)
from app.application.plugin.folders import add_clone_to_plugin_folder
from app.application.plugin.runtime import get_plugin_manager
from app.runtime.log import logger
from app.schemas.plugin import PluginCloneOutcome as _SchemaPluginCloneOutcome
from app.schemas.plugin import PluginCloneRequest as _SchemaPluginCloneRequest
from app.schemas.plugin import PluginRestorableInstance as _SchemaPluginRestorableInstance
from app.schemas.response import Response as _SchemaResponse

router = ResponseAPIRouter()


@router.get(  # type: ignore[misc]
    "/clone/{plugin_id}/restorable",
    summary="列出可恢复的已停用分身",
    response_model=List[_SchemaPluginRestorableInstance],
    openapi_extra={COLLECTION_TOTAL_OPENAPI_KEY: True},
)
def plugin_restorable_instances(
    plugin_id: str,
    _: ApiPrincipal = Depends(get_current_active_superuser),
    page: CompatiblePageParam = None,
    count: CompatibleCountParam = None,
    response: Response = None,
) -> Any:
    """
    列出该插件名下已停用、设置仍留存可被恢复的分身

    停用只把启用位置假，业务参数与展示信息都还留在那一行上。启用中的分身不在此列
    ——它们的配置正被使用，摆进恢复选择器只会让人误以为能把一个活着的实例再建一遍。

    未指定分页时返回完整清单：一个插件的历史分身数量有限，恢复选择器要一次看全。
    """
    instances = [
        _SchemaPluginRestorableInstance(**item)
        for item in get_plugin_manager().get_restorable_plugin_instances(plugin_id)
    ]
    if response is not None:
        response.headers[COLLECTION_TOTAL_HEADER] = str(len(instances))
    if page is not None or count is not None:
        page, count = resolve_compatible_pagination(page, count)
        assert page is not None and count is not None
        return instances[(page - 1) * count: page * count]
    return instances


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

    不填后缀时由服务端分配一个最小可用序号，因而实例 ID 只能由回执给出；该后缀名下
    留有一个已停用的分身时，本次创建就是把那一行连同它的业务参数重新启用。
    """
    plugin_manager = get_plugin_manager()
    try:
        with plugin_manager.mutation(f"创建插件 {plugin_id} 分身"):
            success, message = plugin_manager.clone_plugin(
                plugin_id=plugin_id,
                suffix=clone_data.suffix,
                name=clone_data.name,
                description=clone_data.description,
                version=clone_data.version,
                icon=clone_data.icon,
                restore_previous=clone_data.restore_previous,
            )
            if not success:
                return _SchemaResponse(success=False, message=message)
            # 分身此时已经创建并加载完成，后面只是补齐宿主注册。这一步失败不能报成
            # 「创建失败」：分身确实已经存在，用户照提示重试只会撞上「分身已存在」，
            # 真正的原因反而被那句话盖掉。
            outcome = _SchemaPluginCloneOutcome(instance_id=message)
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
                    data=outcome,
                )
            return _SchemaResponse(success=True, message="插件分身创建成功", data=outcome)
    except Exception as e:
        logger.error(f"创建插件分身失败：{str(e)}")
        return _SchemaResponse(success=False, message=f"创建插件分身失败：{str(e)}")
