from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app import schemas
from app.core.auth_bridge import build_token_response, consume_plugin_auth_ticket
from app.core.plugin import PluginManager
from app.db.models.passkey import PassKey
from app.db.models.user import User

router = APIRouter()


class AuthExchangeRequest(BaseModel):
    """
    插件认证票据兑换请求。
    """

    ticket: str


def _system_auth_providers() -> list[dict[str, Any]]:
    """
    获取系统内建的匿名登录方式摘要。

    :return: 系统认证提供方列表
    """
    has_passkey = bool(PassKey.list(db=None))
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


@router.get("/providers", summary="查询登录认证提供方", response_model=list[dict])
def auth_providers() -> list[dict[str, Any]]:
    """
    查询系统和插件提供的登录认证入口。

    :return: 认证提供方摘要列表
    """
    providers = _system_auth_providers()
    providers.extend(PluginManager().get_plugin_auth_providers())
    return [provider for provider in providers if provider.get("enabled", True)]


@router.post("/exchange", summary="兑换插件认证登录票据", response_model=schemas.Token)
def auth_exchange(body: AuthExchangeRequest) -> schemas.Token:
    """
    将插件认证成功后生成的一次性票据兑换为系统 Token。

    :param body: 票据兑换请求
    :return: 标准登录 Token
    """
    ticket_data = consume_plugin_auth_ticket(body.ticket)
    if not ticket_data:
        raise HTTPException(status_code=401, detail="认证票据无效或已过期")

    user = User.get(db=None, rid=ticket_data.get("user_id"))
    if not user or not user.is_active:
        raise HTTPException(status_code=403, detail="用户不存在或已禁用")

    return build_token_response(user)
