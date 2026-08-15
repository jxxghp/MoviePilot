from typing import Optional

from fastapi import Depends

from app import schemas
from app.api.response import ResponseAPIRouter
from app.chain.message import MessageChain
from app.db.models import User
from app.api.deps import get_current_active_superuser

router = ResponseAPIRouter()

@router.get(
    "/wechatclawbot/status",
    summary="查询微信 ClawBot 登录状态",
    response_model=schemas.Response[schemas.WechatClawBotData],
)
def wechatclawbot_status(
    source: Optional[str] = None,
    fallback_source: Optional[str] = None,
    refresh_remote: bool = True,
    auto_generate_qrcode: bool = True,
    WECHATCLAWBOT_BASE_URL: Optional[str] = None,
    WECHATCLAWBOT_DEFAULT_TARGET: Optional[str] = None,
    WECHATCLAWBOT_ADMINS: Optional[str] = None,
    WECHATCLAWBOT_POLL_TIMEOUT: Optional[int] = None,
    _: User = Depends(get_current_active_superuser),
):
    """查询微信 ClawBot 登录状态和二维码。"""
    client, errmsg = MessageChain().get_wechatclawbot_client(
        source=source,
        fallback_source=fallback_source,
        WECHATCLAWBOT_BASE_URL=WECHATCLAWBOT_BASE_URL,
        WECHATCLAWBOT_DEFAULT_TARGET=WECHATCLAWBOT_DEFAULT_TARGET,
        WECHATCLAWBOT_ADMINS=WECHATCLAWBOT_ADMINS,
        WECHATCLAWBOT_POLL_TIMEOUT=WECHATCLAWBOT_POLL_TIMEOUT,
        allow_temporary=True,
    )
    if not client:
        return schemas.Response(success=False, message=errmsg)
    return schemas.Response(
        success=True,
        data=client.get_status(
            refresh_remote=refresh_remote,
            auto_generate_qrcode=auto_generate_qrcode,
        ),
    )


@router.post(
    "/wechatclawbot/refresh",
    summary="刷新微信 ClawBot 二维码",
    response_model=schemas.Response[schemas.WechatClawBotData],
)
def refresh_wechatclawbot_qrcode(
    source: Optional[str] = None,
    fallback_source: Optional[str] = None,
    WECHATCLAWBOT_BASE_URL: Optional[str] = None,
    WECHATCLAWBOT_DEFAULT_TARGET: Optional[str] = None,
    WECHATCLAWBOT_ADMINS: Optional[str] = None,
    WECHATCLAWBOT_POLL_TIMEOUT: Optional[int] = None,
    _: User = Depends(get_current_active_superuser),
):
    """刷新微信 ClawBot 二维码。"""
    client, errmsg = MessageChain().get_wechatclawbot_client(
        source=source,
        fallback_source=fallback_source,
        WECHATCLAWBOT_BASE_URL=WECHATCLAWBOT_BASE_URL,
        WECHATCLAWBOT_DEFAULT_TARGET=WECHATCLAWBOT_DEFAULT_TARGET,
        WECHATCLAWBOT_ADMINS=WECHATCLAWBOT_ADMINS,
        WECHATCLAWBOT_POLL_TIMEOUT=WECHATCLAWBOT_POLL_TIMEOUT,
        allow_temporary=True,
    )
    if not client:
        return schemas.Response(success=False, message=errmsg)
    result = client.refresh_qrcode()
    return schemas.Response(
        success=bool(result.get("success")),
        message=result.get("message"),
        data=result,
    )


@router.post(
    "/wechatclawbot/logout",
    summary="退出微信 ClawBot 登录",
    response_model=schemas.Response[schemas.WechatClawBotData],
)
def logout_wechatclawbot(
    source: Optional[str] = None,
    fallback_source: Optional[str] = None,
    WECHATCLAWBOT_BASE_URL: Optional[str] = None,
    WECHATCLAWBOT_DEFAULT_TARGET: Optional[str] = None,
    WECHATCLAWBOT_ADMINS: Optional[str] = None,
    WECHATCLAWBOT_POLL_TIMEOUT: Optional[int] = None,
    _: User = Depends(get_current_active_superuser),
):
    """退出微信 ClawBot 登录。"""
    client, errmsg = MessageChain().get_wechatclawbot_client(
        source=source,
        fallback_source=fallback_source,
        WECHATCLAWBOT_BASE_URL=WECHATCLAWBOT_BASE_URL,
        WECHATCLAWBOT_DEFAULT_TARGET=WECHATCLAWBOT_DEFAULT_TARGET,
        WECHATCLAWBOT_ADMINS=WECHATCLAWBOT_ADMINS,
        WECHATCLAWBOT_POLL_TIMEOUT=WECHATCLAWBOT_POLL_TIMEOUT,
        allow_temporary=True,
    )
    if not client:
        return schemas.Response(success=False, message=errmsg)
    result = client.logout()
    return schemas.Response(
        success=bool(result.get("success")),
        message=result.get("message"),
        data=result,
    )


@router.get(
    "/wechatclawbot/test",
    summary="测试微信 ClawBot 连通性",
    response_model=schemas.Response[None],
)
def test_wechatclawbot(
    source: Optional[str] = None,
    fallback_source: Optional[str] = None,
    WECHATCLAWBOT_BASE_URL: Optional[str] = None,
    WECHATCLAWBOT_DEFAULT_TARGET: Optional[str] = None,
    WECHATCLAWBOT_ADMINS: Optional[str] = None,
    WECHATCLAWBOT_POLL_TIMEOUT: Optional[int] = None,
    _: User = Depends(get_current_active_superuser),
):
    """测试微信 ClawBot 当前登录态是否可用。"""
    client, errmsg = MessageChain().get_wechatclawbot_client(
        source=source,
        fallback_source=fallback_source,
        WECHATCLAWBOT_BASE_URL=WECHATCLAWBOT_BASE_URL,
        WECHATCLAWBOT_DEFAULT_TARGET=WECHATCLAWBOT_DEFAULT_TARGET,
        WECHATCLAWBOT_ADMINS=WECHATCLAWBOT_ADMINS,
        WECHATCLAWBOT_POLL_TIMEOUT=WECHATCLAWBOT_POLL_TIMEOUT,
        allow_temporary=True,
    )
    if not client:
        return schemas.Response(success=False, message=errmsg)
    state, message = client.test_connection()
    return schemas.Response(success=state, message=message)


@router.post(
    "/wechatclawbot/migrate",
    summary="迁移微信 ClawBot 登录缓存",
    response_model=schemas.Response[None],
)
def migrate_wechatclawbot_cache(
    old_source: str,
    new_source: str,
    cleanup_old: bool = False,
    overwrite: bool = False,
    _: User = Depends(get_current_active_superuser),
):
    """在通知名称变更时迁移对应的微信 ClawBot 登录缓存。"""
    success, message = MessageChain().migrate_wechatclawbot_cache(
        old_name=old_source,
        new_name=new_source,
        cleanup_old=cleanup_old,
        overwrite=overwrite,
    )
    return schemas.Response(success=success, message=message)
