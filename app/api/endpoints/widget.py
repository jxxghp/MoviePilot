"""
小组件受限只读快照与本用户授权管理；没有业务写操作或公开凭据列表。
"""

from uuid import UUID

from fastapi import Depends, HTTPException, Response

from app.api.dependencies.auth import get_current_active_user
from app.api.dependencies.widget import (
    WIDGET_CACHE_HEADERS,
    get_widget_auth_service,
    get_widget_grant,
    get_widget_snapshot_service,
    widget_http_error,
)
from app.api.principal import ApiPrincipal
from app.api.response import RAW_RESPONSE_OPENAPI_KEY, ResponseAPIRouter
from app.application.security.widget import WidgetAccessError, WidgetAuthService, WidgetGrant
from app.application.widget import WidgetSnapshotService
from app.runtime.execution import run_in_threadpool
from app.schemas.response import Response as ApiResponse
from app.schemas.widget import WidgetContext, WidgetRegistrationRequest, WidgetRegistrationResponse, WidgetSnapshot

router = ResponseAPIRouter()


@router.get("/context", summary="读取当前登录的小组件账户上下文", response_model=ApiResponse[WidgetContext])
def widget_context(
    response: Response,
    current_user: ApiPrincipal = Depends(get_current_active_user),
    service: WidgetAuthService = Depends(get_widget_auth_service),
) -> ApiResponse[WidgetContext]:
    """
    通过既有主登录核对不可变身份与当前权限，不登记安装或返回凭据清单。

    :param response: 禁止缓存当前账号的上下文
    :param current_user: 原认证确定的激活账号
    :param service: 当前宿主的只读授权上下文服务
    :return: 当前身份及可用内容，不含主登录或小组件密钥
    """
    response.headers.update(WIDGET_CACHE_HEADERS)
    try:
        result = service.context(current_user.id, current_user.notification_identity)
    except WidgetAccessError as error:
        widget_http_error(error)
    return ApiResponse(success=True, data=result)


@router.post(
    "/registrations", summary="启用或轮换本机小组件授权", response_model=ApiResponse[WidgetRegistrationResponse]
)
def register_widget(
    request: WidgetRegistrationRequest,
    response: Response,
    current_user: ApiPrincipal = Depends(get_current_active_user),
    service: WidgetAuthService = Depends(get_widget_auth_service),
) -> ApiResponse[WidgetRegistrationResponse]:
    """
    使用已有登录创建三十天只读凭据，同账号同安装再次登记原子撤销旧凭据。

    :param request: 仅包含本机随机安装标识
    :param response: 禁止缓存一次性明文凭据
    :param current_user: 由原认证系统确定的激活账号
    :param service: 当前宿主专用授权服务
    :return: 只在数据库提交成功后返回一次的新凭据
    """
    response.headers.update(WIDGET_CACHE_HEADERS)
    try:
        result = service.register(current_user.id, current_user.notification_identity, request.installationId)
    except WidgetAccessError as error:
        widget_http_error(error)
    return ApiResponse(success=True, data=result)


@router.delete("/registrations/{registration_id}", summary="移除本用户的小组件授权", response_model=ApiResponse[None])
def revoke_widget(
    registration_id: UUID,
    response: Response,
    current_user: ApiPrincipal = Depends(get_current_active_user),
    service: WidgetAuthService = Depends(get_widget_auth_service),
) -> ApiResponse[None]:
    """
    主登录只能撤销属于本人的单个登记，不能通过标识判断他人是否存在。

    :param registration_id: 服务端生成的随机登记标识
    :param response: 禁止缓存授权管理结果
    :param current_user: 原认证确定的激活账号
    :param service: 专用授权服务
    :return: 成功撤销的标准响应
    """
    response.headers.update(WIDGET_CACHE_HEADERS)
    try:
        service.revoke(current_user.id, current_user.notification_identity, registration_id)
    except WidgetAccessError as error:
        widget_http_error(error)
    return ApiResponse(success=True)


@router.delete("/registration", summary="用小组件凭据撤销自身", response_model=ApiResponse[None])
def revoke_widget_self(
    grant: WidgetGrant = Depends(get_widget_grant),
    service: WidgetAuthService = Depends(get_widget_auth_service),
) -> ApiResponse[None]:
    """
    仅删除该 Bearer 自身，不接受其它登记标识或续期参数。

    :param grant: Header 中专用凭据对应的最新授权
    :param service: 当前宿主授权服务
    :return: 成功撤销的标准响应
    """
    try:
        service.revoke_self(grant)
    except WidgetAccessError as error:
        widget_http_error(error)
    return ApiResponse(success=True)


@router.get(
    "/snapshot",
    summary="读取小组件最小只读快照",
    response_model=WidgetSnapshot,
    response_model_exclude_none=True,
    openapi_extra={RAW_RESPONSE_OPENAPI_KEY: True},
)
async def widget_snapshot(
    grant: WidgetGrant = Depends(get_widget_grant),
    service: WidgetSnapshotService = Depends(get_widget_snapshot_service),
    auth: WidgetAuthService = Depends(get_widget_auth_service),
) -> WidgetSnapshot:
    """
    专用 Bearer 只读本账号有权限的数据，来源失败独立标记且不泄露内部字段。

    :param grant: 当前用户及有限期专用授权
    :param service: 复用既有查询与业务链的最小投影
    :param auth: 返回前重新校验撤销、到期和权限，防止采样等待跨越撤权
    :return: 协议 v1 原始模型，完整响应不超过 32 KiB
    """
    try:
        result = await service.snapshot(grant)
        await run_in_threadpool(auth.revalidate, grant)
    except WidgetAccessError as error:
        widget_http_error(error)
    except ValueError as error:
        raise HTTPException(503, "小组件快照暂不可用", headers=WIDGET_CACHE_HEADERS) from error
    return result
