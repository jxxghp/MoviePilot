"""
系统设置查询、合同发现和受控更新接口。
"""

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
    """
    基于当前宿主运行时构造系统设置应用服务。
    """
    return SystemSettingsService(
        get_runtime_settings(),
        get_configured_system_config(),
        runtime.system.publish_config_changed,
    )


@router.get(
    "/setting/{key}",
    summary="查询系统设置",
    response_model=_SchemaResponse[_SchemaValueData],
)
async def get_setting(
    key: str,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
) -> _SchemaResponse[Any]:
    """
    查询系统设置（仅管理员）。
    """
    runtime_settings = get_runtime_settings()
    if runtime_settings.contains(key):
        value = runtime_settings.get(key)
    else:
        value = get_configured_system_config().get(key)
    return _SchemaResponse(success=True, data={"value": value})


@router.post(
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
    """
    更新系统设置（仅管理员）。
    """
    result = await runtime.system.update_setting(key, value)
    return _SchemaResponse(success=result.success, message=result.message or "")


@router.get(
    "/settings/catalog",
    summary='列出系统设置契约',
    response_model=_SchemaResponse[_SchemaJsonObject],
)
async def catalog_settings(
    group: Annotated[
        Optional[str],
        Query(description='设置契约分组；使用 all 可列出全部已登记设置。'),
    ] = "all",
    keyword: Annotated[
        Optional[str],
        Query(description='匹配设置键、分组、标签或说明的子串，不区分大小写。'),
    ] = None,
    source: Annotated[
        Optional[str],
        Query(description='可选的来源筛选：settings 或 systemconfig。'),
    ] = None,
    offset: Annotated[int, Query(ge=0, description='结果偏移量，从 0 开始。')] = 0,
    limit: Annotated[int, Query(ge=1, le=500, description='最多返回的设置契约条数。')] = 100,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
    runtime: HostRuntime = Depends(get_host_runtime),
) -> _SchemaResponse[Any]:
    """
    分页返回轻量设置合同，供 Agent 建立可用设置索引。
    """
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


@router.get(
    "/settings/describe/{setting_key}",
    summary='查询单项系统设置契约',
    response_model=_SchemaResponse[_SchemaJsonObject],
)
async def describe_setting(
    setting_key: str,
    include_value: Annotated[
        bool,
        Query(description='是否在完整契约之外返回当前值。'),
    ] = True,
    show_secrets: Annotated[
        bool,
        Query(description='是否返回未脱敏的秘密值，仅可在明确授权后使用。'),
    ] = False,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
    runtime: HostRuntime = Depends(get_host_runtime),
) -> _SchemaResponse[Any]:
    """
    返回单项完整设置合同、当前值和 revision。
    """
    try:
        data = _settings_service(runtime).describe(
            setting_key=setting_key,
            include_value=include_value,
            show_secrets=show_secrets,
        )
    except ValueError as error:
        return _SchemaResponse(success=False, message=str(error))
    return _SchemaResponse(success=True, data=data)


@router.get(
    "/settings",
    summary='发现或读取已登记的系统设置',
    response_model=_SchemaResponse[_SchemaJsonObject],
)
async def query_settings(
    setting_key: Annotated[
        Optional[str],
        Query(
            description=(
                '精确设置键，接受 APP_DOMAIN 或 LLM_MODEL 等 Settings 字段名、Downloaders 或 MediaServers 等 Syst'
                'emConfigKey 值或枚举名，以及能唯一解析的别名；省略时按列表发现设置。'
            )
        ),
    ] = None,
    group: Annotated[
        Optional[str],
        Query(
            description=(
                '省略 setting_key 时使用的发现分组。调用 config.system.list 可获取完整、机器可读的分组索引；此兼容端点接受全部已登记分组，包括 '
                'settings 和 systemconfig。'
            )
        ),
    ] = "all",
    keyword: Annotated[
        Optional[str],
        Query(description='用于发现匹配设置键、分组或标签的子串，不区分大小写。'),
    ] = None,
    include_values: Annotated[
        Optional[bool],
        Query(description='是否返回完整值；查询单个精确设置键时默认为 True，发现设置列表时默认为 False。'),
    ] = None,
    show_secrets: Annotated[
        bool,
        Query(description='是否返回未脱敏的秘密值；默认为 False，启用时仍需通过确认保护。'),
    ] = False,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
    runtime: HostRuntime = Depends(get_host_runtime),
) -> _SchemaResponse[Any]:
    """
    按登记元数据查询设置，并默认对敏感值递归脱敏。
    """
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


@router.post(
    "/settings",
    summary='更新单项已登记系统设置',
    response_model=_SchemaResponse[_SchemaJsonObject],
)
async def update_settings(
    payload: _SchemaSystemSettingsUpdateRequest,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
    runtime: HostRuntime = Depends(get_host_runtime),
) -> _SchemaResponse[Any]:
    """
    按替换、字典合并或列表项操作更新一个登记设置。
    """
    try:
        data = await _settings_service(runtime).update(**payload.model_dump())
    except SystemSettingConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ValueError as error:
        return _SchemaResponse(success=False, message=str(error))
    return _SchemaResponse(success=True, message=data.get("message") or "", data=data)
