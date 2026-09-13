"""插件实例日志等级查询与设置接口。"""

from typing import Any

from fastapi import Depends, HTTPException

from app.api.dependencies.auth import get_current_active_superuser
from app.api.principal import ApiPrincipal
from app.api.response import ResponseAPIRouter
from app.application.plugin.runtime import get_plugin_manager
from app.schemas.plugin import PluginInstanceLogLevelOverview as _SchemaPluginInstanceLogLevelOverview
from app.schemas.plugin import PluginInstanceLogLevelUpdateRequest as _SchemaPluginInstanceLogLevelUpdateRequest
from app.schemas.response import Response as _SchemaResponse

router = ResponseAPIRouter()


@router.get(  # type: ignore[misc]
    "/loglevel/{plugin_id}",
    summary="查询插件全部实例的日志等级设置",
    response_model=_SchemaResponse[_SchemaPluginInstanceLogLevelOverview],
)
def plugin_instance_log_levels(
    plugin_id: str,
    _: ApiPrincipal = Depends(get_current_active_superuser),
) -> Any:
    """
    查询插件全部实例（含本体）当前的日志等级设置
    """
    try:
        levels = get_plugin_manager().get_plugin_instance_log_levels(plugin_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return _SchemaResponse(
        success=True,
        data={"plugin_id": plugin_id, "instances": levels},
    )


@router.put(  # type: ignore[misc]
    "/loglevel/{plugin_id}/{instance_id}",
    summary="设置插件实例的日志等级覆盖",
    response_model=_SchemaResponse[None],
)
def set_plugin_instance_log_level(
    plugin_id: str,
    instance_id: str,
    update: _SchemaPluginInstanceLogLevelUpdateRequest,
    _: ApiPrincipal = Depends(get_current_active_superuser),
) -> Any:
    """
    设置指定插件实例的日志等级覆盖，运行期立即生效，不随全局日志等级变更
    """
    try:
        get_plugin_manager().set_plugin_instance_log_level(
            plugin_id, instance_id, update.level, update.expires_at
        )
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return _SchemaResponse(success=True)


@router.delete(  # type: ignore[misc]
    "/loglevel/{plugin_id}/{instance_id}",
    summary="清除插件实例的日志等级覆盖",
    response_model=_SchemaResponse[None],
)
def clear_plugin_instance_log_level(
    plugin_id: str,
    instance_id: str,
    _: ApiPrincipal = Depends(get_current_active_superuser),
) -> Any:
    """
    清除指定插件实例的日志等级覆盖，立即回落全局等级，重复调用保持幂等
    """
    try:
        get_plugin_manager().clear_plugin_instance_log_level(plugin_id, instance_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return _SchemaResponse(success=True)
