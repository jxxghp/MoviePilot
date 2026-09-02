"""插件已装版本查询与虚拟实例版本绑定切换接口。"""

from typing import Any

from fastapi import Depends

from app.api.dependencies.auth import get_current_active_superuser
from app.api.principal import ApiPrincipal
from app.api.response import ResponseAPIRouter
from app.application.plugin.runtime import get_plugin_manager
from app.schemas.exception import PluginMutationRejectedError
from app.schemas.plugin import PluginInstanceVersionUpdateRequest as _SchemaPluginInstanceVersionUpdateRequest
from app.schemas.plugin import PluginVersionOverview as _SchemaPluginVersionOverview
from app.schemas.plugin import PluginVersionRecycleOutcome as _SchemaPluginVersionRecycleOutcome
from app.schemas.response import Response as _SchemaResponse

router = ResponseAPIRouter()


@router.get(  # type: ignore[misc]
    "/versions/{plugin_id}",
    summary="查询插件已装版本与实例版本绑定",
    response_model=_SchemaResponse[_SchemaPluginVersionOverview],
)
def plugin_version_overview(
    plugin_id: str,
    _: ApiPrincipal = Depends(get_current_active_superuser),
) -> Any:
    """
    查询插件已装版本列表与各实例的版本绑定
    """
    try:
        overview = get_plugin_manager().get_plugin_version_overview(plugin_id)
    except LookupError as error:
        return _SchemaResponse(success=False, message=str(error))
    return _SchemaResponse(success=True, data=overview)


@router.put(  # type: ignore[misc]
    "/versions/{plugin_id}/{instance_id}",
    summary="设置插件实例的版本绑定",
    response_model=_SchemaResponse[None],
)
def set_plugin_instance_version(
    plugin_id: str,
    instance_id: str,
    update: _SchemaPluginInstanceVersionUpdateRequest,
    _: ApiPrincipal = Depends(get_current_active_superuser),
) -> Any:
    """
    设置指定插件实例的版本绑定，并完成一次停止再启动
    """
    plugin_manager = get_plugin_manager()
    try:
        overview = plugin_manager.get_plugin_version_overview(plugin_id)
    except LookupError as error:
        return _SchemaResponse(success=False, message=str(error))
    known_instance_ids = {item["instance_id"] for item in overview["instances"]}
    if instance_id not in known_instance_ids:
        return _SchemaResponse(success=False, message=f"插件实例 {instance_id} 不存在")
    success, message = plugin_manager.set_plugin_instance_version(
        instance_id,
        follow_current_version=update.follow_current_version,
        plugin_version=update.plugin_version,
    )
    return _SchemaResponse(
        success=success,
        message="版本切换成功" if success else message,
    )


@router.post(  # type: ignore[misc]
    "/versions/{plugin_id}/recycle",
    summary="回收插件不再引用的已装版本目录",
    response_model=_SchemaResponse[_SchemaPluginVersionRecycleOutcome],
)
def recycle_plugin_versions(
    plugin_id: str,
    _: ApiPrincipal = Depends(get_current_active_superuser),
) -> Any:
    """
    手动触发回收指定插件不再被引用、也不在最近版本窗口内的已装版本目录
    """
    try:
        outcome = get_plugin_manager().recycle_plugin_versions(plugin_id)
    except (LookupError, PluginMutationRejectedError) as error:
        return _SchemaResponse(success=False, message=str(error))
    return _SchemaResponse(success=True, data=outcome)
