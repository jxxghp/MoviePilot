"""自定义识别词结构化读写端点。"""

from typing import Any

from fastapi import Depends, HTTPException, status

from app.api.context import get_host_runtime
from app.api.dependencies.auth import get_current_active_superuser_async
from app.api.principal import ApiPrincipal
from app.api.response import ResponseAPIRouter
from app.application.configuration import (
    get_configured_system_config,
    get_runtime_settings,
)
from app.application.settings import SystemSettingConflictError, SystemSettingsService
from app.schemas.common import JsonObject
from app.schemas.response import Response
from app.schemas.system import CustomIdentifiersUpdateRequest
from app.schemas.types import SystemConfigKey
from app.startup.composition.context import HostRuntime

router = ResponseAPIRouter()


@router.get(  # type: ignore[misc]
    "/identifiers",
    summary="查询自定义识别词",
    response_model=Response[JsonObject],
)
async def query_custom_identifiers(
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
) -> Response[Any]:
    """返回完整的自定义识别词列表。"""
    identifiers = [
        item
        for item in (get_configured_system_config().get(SystemConfigKey.CustomIdentifiers) or [])
        if isinstance(item, str)
    ]
    return Response(
        success=True,
        data={"count": len(identifiers), "identifiers": identifiers},
    )


@router.post(  # type: ignore[misc]
    "/identifiers",
    summary="更新自定义识别词",
    response_model=Response[JsonObject],
)
async def update_custom_identifiers(
    payload: CustomIdentifiersUpdateRequest,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
    runtime: HostRuntime = Depends(get_host_runtime),
) -> Response[Any]:
    """完整替换自定义识别词，并拒绝基于过期快照的覆盖。"""
    identifiers = list(payload.identifiers)
    try:
        data = await SystemSettingsService(
            get_runtime_settings(),
            get_configured_system_config(),
            runtime.system.publish_config_changed,
        ).update(
            setting_key=SystemConfigKey.CustomIdentifiers.value,
            value=identifiers or None,
            expected_value=payload.expected_identifiers,
            enforce_expected_value=payload.expected_identifiers is not None,
        )
    except SystemSettingConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error
    data.update({"count": len(identifiers), "identifiers": identifiers})
    return Response(success=True, message=data.get("message"), data=data)
