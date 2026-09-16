"""系统设置查询、合同发现和受控更新接口。"""

from typing import Annotated, Any, Optional, Union

from fastapi import Body, Depends, HTTPException, Query

from app.api.context import get_host_runtime
from app.api.dependencies.auth import get_current_active_superuser_async
from app.api.principal import ApiPrincipal
from app.api.response import ResponseAPIRouter
from app.application.configuration import (
    get_configured_system_config,
    get_runtime_settings,
)
from app.application.settings.service import (
    SystemSettingConflictError,
    SystemSettingsService,
)
from app.schemas.common import JsonObject as _SchemaJsonObject
from app.schemas.common import ValueData as _SchemaValueData
from app.schemas.response import Response as _SchemaResponse
from app.schemas.system import SystemSettingsUpdateRequest as _SchemaSystemSettingsUpdateRequest
from app.startup.composition.context import HostRuntime

router = ResponseAPIRouter()


def _settings_service(runtime: HostRuntime) -> SystemSettingsService:
    """基于当前宿主运行时构造系统设置应用服务。"""
    return SystemSettingsService(
        get_runtime_settings(),
        get_configured_system_config(),
        runtime.system.publish_config_changed,
    )


@router.get(  # type: ignore[misc]
    "/setting/{key}",
    summary="查询系统设置",
    response_model=_SchemaResponse[_SchemaValueData],
)
async def get_setting(
    key: str,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
) -> _SchemaResponse[Any]:
    """查询系统设置（仅管理员）。"""
    runtime_settings = get_runtime_settings()
    if runtime_settings.contains(key):
        value = runtime_settings.get(key)
    else:
        value = get_configured_system_config().get(key)
    return _SchemaResponse(success=True, data={"value": value})


@router.post(  # type: ignore[misc]
    "/setting/{key}",
    summary="更新系统设置",
    response_model=_SchemaResponse[None],
)
async def set_setting(
    key: str,
    value: Annotated[Union[list[Any], dict[str, Any], bool, int, str] | None, Body()] = None,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
    runtime: HostRuntime = Depends(get_host_runtime),
) -> _SchemaResponse[Any]:
    """更新系统设置（仅管理员）。"""
    result = await runtime.system.update_setting(key, value)
    return _SchemaResponse(success=result.success, message=result.message)


@router.get(  # type: ignore[misc]
    "/settings/catalog",
    summary="List system setting contracts",
    response_model=_SchemaResponse[_SchemaJsonObject],
)
async def catalog_settings(
    group: Annotated[
        Optional[str],
        Query(description="Setting contract group. Use all to list every registered setting."),
    ] = "all",
    keyword: Annotated[
        Optional[str],
        Query(description="Case-insensitive substring matched against key, group, label, or description."),
    ] = None,
    source: Annotated[
        Optional[str],
        Query(description="Optional source filter: settings or systemconfig."),
    ] = None,
    offset: Annotated[int, Query(ge=0, description="Zero-based result offset.")] = 0,
    limit: Annotated[int, Query(ge=1, le=500, description="Maximum contracts to return.")] = 100,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
    runtime: HostRuntime = Depends(get_host_runtime),
) -> _SchemaResponse[Any]:
    """分页返回轻量设置合同，供 Agent 建立可用设置索引。"""
    try:
        data = _settings_service(runtime).catalog(
            group=group,
            keyword=keyword,
            source=source,
            offset=offset,
            limit=limit,
        )
    except ValueError as error:
        return _SchemaResponse(success=False, message=str(error))
    return _SchemaResponse(success=True, data=data)


@router.get(  # type: ignore[misc]
    "/settings/describe/{setting_key}",
    summary="Describe one system setting contract",
    response_model=_SchemaResponse[_SchemaJsonObject],
)
async def describe_setting(
    setting_key: str,
    include_value: Annotated[
        bool,
        Query(description="Return the current value in addition to the complete contract."),
    ] = True,
    show_secrets: Annotated[
        bool,
        Query(description="Return unredacted secret values only when explicitly authorized."),
    ] = False,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
    runtime: HostRuntime = Depends(get_host_runtime),
) -> _SchemaResponse[Any]:
    """返回单项完整设置合同、当前值和 revision。"""
    try:
        data = _settings_service(runtime).describe(
            setting_key=setting_key,
            include_value=include_value,
            show_secrets=show_secrets,
        )
    except ValueError as error:
        return _SchemaResponse(success=False, message=str(error))
    return _SchemaResponse(success=True, data=data)


@router.get(  # type: ignore[misc]
    "/settings",
    summary="Discover or read registered system settings",
    response_model=_SchemaResponse[_SchemaJsonObject],
)
async def query_settings(
    setting_key: Annotated[
        Optional[str],
        Query(
            description=(
                "Exact setting key. Accepts Settings field names such as APP_DOMAIN or LLM_MODEL, "
                "SystemConfigKey values or enum names such as Downloaders or MediaServers, and "
                "aliases that resolve to one unique setting. Omit it to use list-style discovery."
            )
        ),
    ] = None,
    group: Annotated[
        Optional[str],
        Query(
            description=(
                "Discovery group used when setting_key is omitted. Use config.system.list for a complete, "
                "machine-readable group index; this compatibility endpoint accepts all registered catalog groups, "
                "including settings and systemconfig."
            )
        ),
    ] = "all",
    keyword: Annotated[
        Optional[str],
        Query(description="Case-insensitive substring used to discover matching keys, groups, or labels."),
    ] = None,
    include_values: Annotated[
        Optional[bool],
        Query(description="Return full values. Defaults to true for one exact key and false for discovery results."),
    ] = None,
    show_secrets: Annotated[
        bool,
        Query(description="Return unredacted secret values. Defaults to false and remains confirmation-protected."),
    ] = False,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
    runtime: HostRuntime = Depends(get_host_runtime),
) -> _SchemaResponse[Any]:
    """按登记元数据查询设置，并默认对敏感值递归脱敏。"""
    try:
        data = _settings_service(runtime).query(
            setting_key=setting_key,
            group=group,
            keyword=keyword,
            include_values=include_values,
            show_secrets=show_secrets,
        )
    except ValueError as error:
        return _SchemaResponse(success=False, message=str(error))
    return _SchemaResponse(success=True, data=data)


@router.post(  # type: ignore[misc]
    "/settings",
    summary="Update one registered system setting",
    response_model=_SchemaResponse[_SchemaJsonObject],
)
async def update_settings(
    payload: _SchemaSystemSettingsUpdateRequest,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
    runtime: HostRuntime = Depends(get_host_runtime),
) -> _SchemaResponse[Any]:
    """按替换、字典合并或列表项操作更新一个登记设置。"""
    try:
        data = await _settings_service(runtime).update(**payload.model_dump())
    except SystemSettingConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ValueError as error:
        return _SchemaResponse(success=False, message=str(error))
    return _SchemaResponse(success=True, message=data.get("message"), data=data)
