from typing import Any, Dict, Optional

from fastapi import Depends, Request, Response
from fastapi.responses import HTMLResponse

from app import schemas
from app.api.response import ResponseAPIRouter
from app.agent.llm import LLMProviderManager, render_auth_result_html
from app.db.models import User
from app.api.deps import get_current_active_superuser_async

router = ResponseAPIRouter()


@router.post(
    "/manage",
    summary="LLM提供商统一管理",
    response_model=schemas.Response[Dict[str, Any]],
)
async def manage_provider(
        request: Request,
        payload: schemas.ManageRequest,
        _: User = Depends(get_current_active_superuser_async),
):
    """
    LLM 提供商统一管理入口：前端上送 target/action/params 原样透传，
    端点不定义任何提供商特定的名称、参数或响应字段；
    OAuth 回跳地址由具名回调路由统一构造后注入动作参数
    """
    params = dict(payload.params)
    params.setdefault(
        "callback_url",
        str(request.url_for("llm_provider_auth_callback", provider_id=payload.target)),
    )
    result = await LLMProviderManager().provider_manage(
        payload.target, payload.action, **params
    )
    return schemas.Response(
        success=bool(result.get("success")),
        message=result.get("message"),
        data=result.get("data"),
    )


@router.get(
    "/provider-auth/callback/{provider_id}",
    summary="LLM提供商OAuth回调",
    response_class=Response,
    name="llm_provider_auth_callback",
    response_model=None,
    responses={
        200: {
            "description": "OAuth 授权结果页面",
            "content": {"text/html": {"schema": {"type": "string"}}},
        }
    },
)
async def llm_provider_auth_callback(
    provider_id: str,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    error_description: Optional[str] = None,
):
    """
    处理需要浏览器回跳的 OAuth provider。
    """
    success, message = await LLMProviderManager().handle_chatgpt_callback(
        provider_id,
        code,
        state,
        error,
        error_description,
    )
    return HTMLResponse(content=render_auth_result_html(success, message))
