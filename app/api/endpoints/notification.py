from typing import Any, Dict

from fastapi import Body, Depends

from app.api.dependencies.auth import get_current_active_superuser
from app.api.response import ResponseAPIRouter
from app.application.configuration import get_configured_system_config
from app.chain.notification import NotificationChain
from app.schemas.common import ManageRequest as _SchemaManageRequest
from app.schemas.response import Response as _SchemaResponse
from app.schemas.system import NotificationConf
from app.schemas.types import SystemConfigKey

router = ResponseAPIRouter()


def _normalize_configs(value: list[NotificationConf]) -> list[dict[str, Any]]:
    """规整通知配置身份和名称，拒绝会导致运行目录覆盖的输入。"""
    normalized: list[dict[str, Any]] = []
    names: set[str] = set()
    for config in value:
        name = (config.name or "").strip()
        if not name:
            raise ValueError("通知渠道名称不能为空")
        key = name.casefold()
        if key in names:
            raise ValueError(f"通知渠道名称重复：{name}")
        names.add(key)
        data = config.model_dump()
        data["name"] = name
        normalized.append(data)
    return normalized


@router.post(
    "/config",
    summary="保存通知渠道并同步登录缓存",
    response_model=_SchemaResponse[Dict[str, Any]],
)
async def save_config(
    configs: list[NotificationConf] = Body(...),
    _: object = Depends(get_current_active_superuser),
):
    """一次保存通知配置，并由渠道模块处理改名与删除缓存。"""
    try:
        current = _normalize_configs(configs)
        previous = get_configured_system_config().get(SystemConfigKey.Notifications) or []
        previous_models = []
        for item in previous:
            if not isinstance(item, dict):
                continue
            legacy = dict(item)
            if not legacy.get("id"):
                legacy["id"] = f"legacy-{legacy.get('type') or 'notification'}-{legacy.get('name') or 'unnamed'}"
            previous_models.append(NotificationConf(**legacy))
        previous_data = _normalize_configs(previous_models)
        result = NotificationChain().manage_channel(
            channel="WechatClawBot",
            action="reconcile_config",
            previous=previous_data,
            current=current,
        )
        if not result.get("success"):
            return _SchemaResponse(success=False, message=result.get("message"))
        saved = await get_configured_system_config().async_set(SystemConfigKey.Notifications, current)
        if saved is False:
            return _SchemaResponse(success=False, message="通知配置保存失败")
        return _SchemaResponse(success=True, message="通知配置保存成功", data={"value": current})
    except ValueError as error:
        return _SchemaResponse(success=False, message=str(error))


@router.post(
    "/manage",
    summary="通知渠道统一管理",
    response_model=_SchemaResponse[Dict[str, Any]],
)
def manage_channel(
    request: _SchemaManageRequest,
    _: object = Depends(get_current_active_superuser),
):
    """
    通知渠道统一管理入口

    端点层不定义任何渠道特定的名称与参数，
    渠道标识、管理动作与表单参数由前端上送并原样透传给渠道模块
    """
    result = NotificationChain().manage_channel(
        channel=request.target,
        action=request.action,
        **request.params,
    )
    return _SchemaResponse(
        success=bool(result.get("success")),
        message=result.get("message"),
        data=result.get("data"),
    )
