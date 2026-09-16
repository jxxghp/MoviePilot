from typing import Any

from fastapi import Depends, HTTPException, Request, Response
from pydantic import BaseModel

from app.adapters.web.security.access import set_or_refresh_resource_token_cookie
from app.api.dependencies.auth import get_auth_service
from app.api.response import (
    RAW_RESPONSE_OPENAPI_KEY,
    CompatibleCountParam,
    CompatiblePageParam,
    ResponseAPIRouter,
)
from app.application.plugin.runtime import get_plugin_manager
from app.application.security.auth import AuthService, consume_plugin_auth_ticket
from app.schemas.token import Token as _SchemaToken
from app.schemas.token import TokenPayload as _SchemaTokenPayload
from app.schemas.user import AuthProviderInfo as _SchemaAuthProviderInfo

router = ResponseAPIRouter()


class AuthExchangeRequest(BaseModel):
    """
    插件认证票据兑换请求。
    """

    ticket: str


def _system_auth_providers(service: AuthService) -> list[dict[str, Any]]:
    """
    获取系统内建的匿名登录方式摘要。

    :return: 系统认证提供方列表
    """
    has_passkey = service.has_passkey()
    return [
        {
            "id": "system:passkey",
            "type": "system",
            "method": "passkey",
            "name": "通行密钥",
            "icon": "material-symbols:passkey",
            "enabled": has_passkey,
        }
    ]


@router.get(
    "/providers",
    summary="查询登录认证提供方",
    response_model=list[_SchemaAuthProviderInfo],
)
def auth_providers(service: AuthService = Depends(get_auth_service), page: CompatiblePageParam = None, count: CompatibleCountParam = None) -> list[dict[str, Any]]:
    """
    查询系统和插件提供的登录认证入口。

    :return: 认证提供方摘要列表
    """
    providers = _system_auth_providers(service)
    providers.extend(get_plugin_manager().get_plugin_auth_providers())
    return [provider for provider in providers if provider.get("enabled", True)]


@router.post(
    "/exchange",
    summary="兑换插件认证登录票据",
    response_model=_SchemaToken,
    openapi_extra={RAW_RESPONSE_OPENAPI_KEY: True},
)
def auth_exchange(
    request: Request,
    response: Response,
    body: AuthExchangeRequest,
    service: AuthService = Depends(get_auth_service),
) -> _SchemaToken:
    """
    将插件认证成功后生成的一次性票据兑换为系统 Token。

    :param body: 票据兑换请求
    :return: 标准登录 Token
    """
    ticket_data = consume_plugin_auth_ticket(body.ticket)
    if not ticket_data:
        raise HTTPException(
            status_code=401,
            detail="认证票据无效或已过期",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = service.get_user_by_id(ticket_data.get("user_id"))
    if not user or not user.is_active:
        raise HTTPException(
            status_code=401,
            detail="用户不存在或已禁用",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = service.build_token_response(user)
    set_or_refresh_resource_token_cookie(
        request,
        response,
        _SchemaTokenPayload(
            sub=user.id,
            username=user.name,
            super_user=user.is_superuser,
            level=token.level,
            purpose="authentication",
        ),
    )
    return token
