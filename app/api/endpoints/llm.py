from typing import Any, Dict, List, Optional, Union

from fastapi import Depends, Request, Response
from fastapi.responses import HTMLResponse

from app.schemas.common import ManageRequest as _SchemaManageRequest
from app.schemas.response import Response as _SchemaResponse
from app.api.response import ResponseAPIRouter
from app.db.models import User
from app.api.deps import get_current_active_superuser_async

router = ResponseAPIRouter()


def _get_llm_provider_manager_type() -> type:
    """在真实管理请求边界解析 provider 运行时。"""
    from app.agent.llm.provider import LLMProviderManager

    return LLMProviderManager


@router.post(
    "/manage",
    summary="LLM提供商统一管理",
    # 各动作 data 形态不一：目录查询返回列表，其余动作返回映射，
    # 须用具体联合类型声明，而非单一开放映射
    response_model=_SchemaResponse[Union[List[Dict[str, Any]], Dict[str, Any]]],
)
async def manage_provider(
        request: Request,
        payload: _SchemaManageRequest,
        _: User = Depends(get_current_active_superuser_async),
):
    """
    LLM 提供商统一管理入口：前端上送 target/action/params 原样透传，
    端点不定义任何提供商特定的名称、参数或响应字段；
    OAuth 回跳地址由具名回调路由统一构造后注入动作参数
    """
    params = dict(payload.params)
    # 目录类查询动作的 target 可为空，此时无需回跳地址；
    # 且 url_for 的路径参数不允许空值，必须先行防护
    if payload.target:
        params.setdefault(
            "callback_url",
            str(request.url_for("llm_provider_auth_callback", provider_id=payload.target)),
        )
    result = await _get_llm_provider_manager_type()().provider_manage(
        payload.target, payload.action, **params
    )
    return _SchemaResponse(
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
    success, message = await _get_llm_provider_manager_type()().handle_chatgpt_callback(
        provider_id,
        code,
        state,
        error,
        error_description,
    )
    from app.agent.llm.provider import render_auth_result_html

    return HTMLResponse(content=render_auth_result_html(success, message))
