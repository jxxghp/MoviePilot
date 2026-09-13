"""插件实例默认调用目标的置位与清除接口。"""

from typing import Any

from fastapi import Depends, HTTPException
from sqlalchemy.exc import IntegrityError

from app.api.dependencies.auth import get_current_active_superuser
from app.api.principal import ApiPrincipal
from app.api.response import ResponseAPIRouter
from app.application.plugin.runtime import get_plugin_manager
from app.schemas.response import Response as _SchemaResponse

router = ResponseAPIRouter()


@router.put(  # type: ignore[misc]
    "/instances/{plugin_id}/{instance_id}/default_target",
    summary="设置插件实例的默认调用目标",
    response_model=_SchemaResponse[None],
)
def set_plugin_instance_default_target(
    plugin_id: str,
    instance_id: str,
    _: ApiPrincipal = Depends(get_current_active_superuser),
) -> Any:
    """
    设置指定插件实例为默认调用目标，并自动清除同插件的旧默认
    """
    try:
        matched = get_plugin_manager().set_plugin_instance_default_target(
            plugin_id, instance_id
        )
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except IntegrityError as error:
        # 「同一源插件至多一个默认目标」由条件唯一索引在库层强制。并发把不同实例
        # 各自设为默认时，后提交的那个会撞上索引；这是可重试的竞争，不是服务端故障。
        raise HTTPException(
            status_code=409,
            detail="默认调用目标正被并发修改，请重试",
        ) from error
    if not matched:
        raise HTTPException(status_code=404, detail=f"插件实例 {instance_id} 不存在")
    return _SchemaResponse(success=True)


@router.delete(  # type: ignore[misc]
    "/instances/{plugin_id}/{instance_id}/default_target",
    summary="清除插件实例的默认调用目标",
    response_model=_SchemaResponse[None],
)
def clear_plugin_instance_default_target(
    plugin_id: str,
    instance_id: str,
    _: ApiPrincipal = Depends(get_current_active_superuser),
) -> Any:
    """
    清除指定插件实例的默认调用目标置位，仅当当前置位的正是该实例时才动作，重复调用保持幂等
    """
    try:
        get_plugin_manager().clear_plugin_instance_default_target(plugin_id, instance_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return _SchemaResponse(success=True)
