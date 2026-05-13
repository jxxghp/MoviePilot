from typing import Optional

from fastapi import APIRouter, Depends

from app import schemas
from app.core.module import ModuleManager
from app.db.models import User
from app.db.user_oper import get_current_active_superuser
from app.modules.wechatclawbot.wechatclawbot import WechatClawBot

router = APIRouter()


def _build_wechatclawbot_temp_client(
    source: Optional[str] = None,
    WECHATCLAWBOT_BASE_URL: Optional[str] = None,
    WECHATCLAWBOT_DEFAULT_TARGET: Optional[str] = None,
    WECHATCLAWBOT_ADMINS: Optional[str] = None,
    WECHATCLAWBOT_POLL_TIMEOUT: Optional[int] = None,
):
    """基于当前表单配置创建一个临时客户端，用于未保存时的扫码状态预览。"""
    source_name = str(source or "").strip()
    if not source_name:
        return None
    return WechatClawBot(
        name=source_name,
        WECHATCLAWBOT_BASE_URL=WECHATCLAWBOT_BASE_URL,
        WECHATCLAWBOT_DEFAULT_TARGET=WECHATCLAWBOT_DEFAULT_TARGET,
        WECHATCLAWBOT_ADMINS=WECHATCLAWBOT_ADMINS,
        WECHATCLAWBOT_POLL_TIMEOUT=WECHATCLAWBOT_POLL_TIMEOUT,
        auto_start_polling=False,
    )


def _get_wechatclawbot_client(
    source: Optional[str] = None,
    fallback_source: Optional[str] = None,
    WECHATCLAWBOT_BASE_URL: Optional[str] = None,
    WECHATCLAWBOT_DEFAULT_TARGET: Optional[str] = None,
    WECHATCLAWBOT_ADMINS: Optional[str] = None,
    WECHATCLAWBOT_POLL_TIMEOUT: Optional[int] = None,
    allow_temporary: bool = False,
):
    """获取已加载的微信 ClawBot 客户端，必要时退回到临时客户端。"""
    module = ModuleManager().get_running_module("WechatClawBotModule")
    source_name = str(source or "").strip() or None
    fallback_name = str(fallback_source or "").strip() or None

    if module:
        candidate_names = []
        for candidate in (fallback_name, source_name):
            if candidate and candidate not in candidate_names:
                candidate_names.append(candidate)

        if candidate_names:
            for candidate in candidate_names:
                config = module.get_config(candidate)
                if not config:
                    continue
                client = module.get_instance(config.name)
                if client:
                    return client, None
        else:
            client = module.get_instance()
            if client:
                return client, None

    if allow_temporary:
        temp_client = _build_wechatclawbot_temp_client(
            source=source_name or fallback_name,
            WECHATCLAWBOT_BASE_URL=WECHATCLAWBOT_BASE_URL,
            WECHATCLAWBOT_DEFAULT_TARGET=WECHATCLAWBOT_DEFAULT_TARGET,
            WECHATCLAWBOT_ADMINS=WECHATCLAWBOT_ADMINS,
            WECHATCLAWBOT_POLL_TIMEOUT=WECHATCLAWBOT_POLL_TIMEOUT,
        )
        if temp_client:
            return temp_client, None

    if source_name:
        return None, f"未找到名为 {source_name} 的微信 ClawBot 通知配置"
    return None, "微信 ClawBot 通知未启用或配置尚未保存，请先保存并启用当前渠道"


@router.get(
    "/wechatclawbot/status",
    summary="查询微信 ClawBot 登录状态",
    response_model=schemas.Response,
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
    client, errmsg = _get_wechatclawbot_client(
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
    response_model=schemas.Response,
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
    client, errmsg = _get_wechatclawbot_client(
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
    response_model=schemas.Response,
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
    client, errmsg = _get_wechatclawbot_client(
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
    response_model=schemas.Response,
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
    client, errmsg = _get_wechatclawbot_client(
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
    response_model=schemas.Response,
)
def migrate_wechatclawbot_cache(
    old_source: str,
    new_source: str,
    cleanup_old: bool = False,
    overwrite: bool = False,
    _: User = Depends(get_current_active_superuser),
):
    """在通知名称变更时迁移对应的微信 ClawBot 登录缓存。"""
    success, message = WechatClawBot.migrate_cached_state(
        old_name=old_source,
        new_name=new_source,
        cleanup_old=cleanup_old,
        overwrite=overwrite,
    )
    return schemas.Response(success=success, message=message)
