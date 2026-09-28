"""GitHub Token 授权接口及首次初始化专用授权接口。"""

from fastapi import Depends, HTTPException

from app.api.dependencies.auth import get_current_active_superuser_async, get_user_service
from app.api.dependencies.github import get_github_auth_service
from app.api.principal import ApiPrincipal
from app.api.response import ResponseAPIRouter
from app.application.github_auth import (
    GithubAuthError,
    GithubAuthService,
    GithubAuthStatus,
)
from app.application.security.user import UserService
from app.schemas.github import (
    GithubDeviceAuthPoll,
    GithubDeviceAuthPollRequest,
    GithubDeviceAuthStart,
    GithubManualTokenRequest,
    GithubTokenStatus,
)
from app.schemas.response import Response as _SchemaResponse

router = ResponseAPIRouter()
initialization_router = ResponseAPIRouter()


def _status_model(status: GithubAuthStatus) -> GithubTokenStatus:
    """将应用层状态投影为不含 Token 原文的 API 模型。"""
    return GithubTokenStatus(
        configured=status.configured,
        valid=status.valid,
        source=status.source,
        login=status.login,
        masked_token=status.masked_token,
        expires_at=status.expires_at,
        needs_reauthorization=status.needs_reauthorization,
    )


def _raise_auth_error(error: GithubAuthError) -> None:
    """把授权应用错误转换为稳定的客户端错误，不返回外部响应原文。"""
    raise HTTPException(status_code=400, detail=str(error)) from error


async def _ensure_initialization_open(service: UserService) -> None:
    """限制未认证授权接口只能在首次初始化窗口内使用。"""
    if await service.is_initialized():
        raise HTTPException(status_code=409, detail="系统已经完成初始化")


@router.get(  # type: ignore[misc]
    "/auth/status",
    summary="查询 GitHub Token 状态",
    response_model=_SchemaResponse[GithubTokenStatus],
)
async def get_auth_status(
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
    github_auth: GithubAuthService = Depends(get_github_auth_service),
) -> _SchemaResponse[GithubTokenStatus]:
    """返回当前管理员可见的 GitHub Token 脱敏状态。"""
    return _SchemaResponse(success=True, data=_status_model(await github_auth.status()))


@router.post(  # type: ignore[misc]
    "/auth/start",
    summary="启动 GitHub 设备授权",
    response_model=_SchemaResponse[GithubDeviceAuthStart],
)
async def start_auth(
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
    github_auth: GithubAuthService = Depends(get_github_auth_service),
) -> _SchemaResponse[GithubDeviceAuthStart]:
    """为已登录管理员申请 GitHub Device Flow 设备码。"""
    try:
        result = await github_auth.start_device_auth()
    except GithubAuthError as error:
        _raise_auth_error(error)
    return _SchemaResponse(
        success=True,
        data=GithubDeviceAuthStart(
            session_id=result.session_id,
            verification_uri=result.verification_uri,
            user_code=result.user_code,
            expires_in=result.expires_in,
            interval_seconds=result.interval_seconds,
        ),
    )


@router.post(  # type: ignore[misc]
    "/auth/poll",
    summary="轮询 GitHub 设备授权",
    response_model=_SchemaResponse[GithubDeviceAuthPoll],
)
async def poll_auth(
    payload: GithubDeviceAuthPollRequest,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
    github_auth: GithubAuthService = Depends(get_github_auth_service),
) -> _SchemaResponse[GithubDeviceAuthPoll]:
    """轮询管理员发起的 GitHub Device Flow。"""
    try:
        result = await github_auth.poll_device_auth(payload.session_id)
    except GithubAuthError as error:
        _raise_auth_error(error)
    status = _status_model(result.status) if result.status else None
    return _SchemaResponse(
        success=True,
        data=GithubDeviceAuthPoll(
            state=result.state,
            message=result.message,
            retry_after=result.retry_after,
            status=status,
        ),
    )


@router.post(  # type: ignore[misc]
    "/auth/manual",
    summary="保存手动 GitHub Token",
    response_model=_SchemaResponse[GithubTokenStatus],
)
async def save_manual_token(
    payload: GithubManualTokenRequest,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
    github_auth: GithubAuthService = Depends(get_github_auth_service),
) -> _SchemaResponse[GithubTokenStatus]:
    """保存兼容 PAT 的手动 GitHub Token，并返回脱敏状态。"""
    try:
        status = await github_auth.set_manual_token(payload.token)
    except GithubAuthError as error:
        _raise_auth_error(error)
    return _SchemaResponse(success=True, data=_status_model(status))


@router.delete(  # type: ignore[misc]
    "/auth/token",
    summary="清除 GitHub Token",
    response_model=_SchemaResponse[None],
)
async def clear_token(
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
    github_auth: GithubAuthService = Depends(get_github_auth_service),
) -> _SchemaResponse[None]:
    """清除管理员配置的 GitHub Token。"""
    try:
        await github_auth.clear_token()
    except GithubAuthError as error:
        _raise_auth_error(error)
    return _SchemaResponse(success=True)


@initialization_router.get(  # type: ignore[misc]
    "/status",
    summary="查询初始化阶段 GitHub Token 状态",
    response_model=_SchemaResponse[GithubTokenStatus],
)
async def get_initialization_auth_status(
    service: UserService = Depends(get_user_service),
    github_auth: GithubAuthService = Depends(get_github_auth_service),
) -> _SchemaResponse[GithubTokenStatus]:
    """在实例尚未初始化时返回 GitHub Token 脱敏状态。"""
    await _ensure_initialization_open(service)
    return _SchemaResponse(success=True, data=_status_model(await github_auth.status()))


@initialization_router.post(  # type: ignore[misc]
    "/start",
    summary="从初始化页启动 GitHub 设备授权",
    response_model=_SchemaResponse[GithubDeviceAuthStart],
)
async def start_initialization_auth(
    service: UserService = Depends(get_user_service),
    github_auth: GithubAuthService = Depends(get_github_auth_service),
) -> _SchemaResponse[GithubDeviceAuthStart]:
    """限制在首次初始化窗口内启动 GitHub Device Flow。"""
    await _ensure_initialization_open(service)
    try:
        result = await github_auth.start_device_auth()
    except GithubAuthError as error:
        _raise_auth_error(error)
    return _SchemaResponse(
        success=True,
        data=GithubDeviceAuthStart(
            session_id=result.session_id,
            verification_uri=result.verification_uri,
            user_code=result.user_code,
            expires_in=result.expires_in,
            interval_seconds=result.interval_seconds,
        ),
    )


@initialization_router.post(  # type: ignore[misc]
    "/poll",
    summary="从初始化页轮询 GitHub 设备授权",
    response_model=_SchemaResponse[GithubDeviceAuthPoll],
)
async def poll_initialization_auth(
    payload: GithubDeviceAuthPollRequest,
    service: UserService = Depends(get_user_service),
    github_auth: GithubAuthService = Depends(get_github_auth_service),
) -> _SchemaResponse[GithubDeviceAuthPoll]:
    """限制在首次初始化窗口内轮询 GitHub Device Flow。"""
    await _ensure_initialization_open(service)
    try:
        result = await github_auth.poll_device_auth(payload.session_id)
    except GithubAuthError as error:
        _raise_auth_error(error)
    status = _status_model(result.status) if result.status else None
    return _SchemaResponse(
        success=True,
        data=GithubDeviceAuthPoll(
            state=result.state,
            message=result.message,
            retry_after=result.retry_after,
            status=status,
        ),
    )


@initialization_router.post(  # type: ignore[misc]
    "/manual",
    summary="从初始化页保存手动 GitHub Token",
    response_model=_SchemaResponse[GithubTokenStatus],
)
async def save_initialization_manual_token(
    payload: GithubManualTokenRequest,
    service: UserService = Depends(get_user_service),
    github_auth: GithubAuthService = Depends(get_github_auth_service),
) -> _SchemaResponse[GithubTokenStatus]:
    """限制在首次初始化窗口内保存兼容 PAT 的手动 Token。"""
    await _ensure_initialization_open(service)
    try:
        status = await github_auth.set_manual_token(payload.token)
    except GithubAuthError as error:
        _raise_auth_error(error)
    return _SchemaResponse(success=True, data=_status_model(status))


@initialization_router.delete(  # type: ignore[misc]
    "/token",
    summary="从初始化页清除 GitHub Token",
    response_model=_SchemaResponse[None],
)
async def clear_initialization_token(
    service: UserService = Depends(get_user_service),
    github_auth: GithubAuthService = Depends(get_github_auth_service),
) -> _SchemaResponse[None]:
    """限制在首次初始化窗口内清除 GitHub Token。"""
    await _ensure_initialization_open(service)
    try:
        await github_auth.clear_token()
    except GithubAuthError as error:
        _raise_auth_error(error)
    return _SchemaResponse(success=True)
