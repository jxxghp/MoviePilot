import re
from typing import Annotated, Optional

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app import schemas
from app.agent.llm import (
    LLMHelper,
    LLMProviderManager,
    LLMTestTimeout,
    render_auth_result_html,
)
from app.core.config import settings
from app.db.models import User
from app.db.user_oper import (
    get_current_active_superuser_async,
    get_current_active_user_async,
)
from app.log import logger

router = APIRouter()


class LlmTestRequest(BaseModel):
    """
    LLM 测试调用请求参数。
    """

    enabled: Optional[bool] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    thinking_level: Optional[str] = None
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    base_url_preset: Optional[str] = None
    user_agent: Optional[str] = None
    use_proxy: Optional[bool] = None


class LlmProviderAuthStartRequest(BaseModel):
    """
    LLM 提供商授权启动请求参数。
    """

    provider: str
    method: str


def _sanitize_llm_error(message: str, api_key: Optional[str] = None) -> str:
    """
    清理错误信息中的敏感字段，避免回显密钥。
    """
    if not message:
        return "LLM 没有返回任何内容"

    sanitized = message
    if api_key:
        sanitized = sanitized.replace(api_key, "***")
    sanitized = re.sub(
        r"(?i)(api[_-]?key\s*[:=]\s*)([^\s,;]+)",
        r"\1***",
        sanitized,
    )
    sanitized = re.sub(
        r"(?i)authorization\s*:\s*bearer\s+[^\s,;]+",
        "Authorization: ***",
        sanitized,
    )

    normalized_message = sanitized.lower().replace("_", "").replace(" ", "")
    if "str" in normalized_message and (
        "modeldump" in normalized_message
        or "setprivateattributes" in normalized_message
    ):
        return (
            "服务返回内容不是兼容的模型响应，请检查基础地址是否填写为 "
            "API Base URL，如果服务要求 /v1 等版本路径，请包含在基础地址中，"
            "不要填写网页地址或完整的 chat/completions 路径"
        )
    return sanitized


@router.get("/models", summary="获取LLM模型列表", response_model=schemas.Response)
async def get_llm_models(
    provider: str,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    base_url_preset: Optional[str] = None,
    user_agent: Optional[str] = None,
    use_proxy: Optional[bool] = None,
    force_refresh: Optional[bool] = False,
    _: User = Depends(get_current_active_user_async),
):
    """
    获取指定 provider 的模型目录。
    """
    try:
        provider_manager = LLMProviderManager()
        models = await LLMHelper().get_models(
            provider=provider,
            api_key=api_key,
            base_url=base_url,
            base_url_preset=base_url_preset,
            user_agent=user_agent,
            use_proxy=use_proxy,
            force_refresh=bool(force_refresh),
        )
        return schemas.Response(
            success=True,
            data={
                "provider": provider,
                "models": models,
                "auth_status": provider_manager.get_auth_status(provider),
            },
        )
    except Exception as err:
        return schemas.Response(
            success=False,
            message=_sanitize_llm_error(str(err), api_key),
        )


@router.get("/providers", summary="获取LLM提供商目录", response_model=schemas.Response)
async def get_llm_providers(
    _: User = Depends(get_current_active_user_async),
):
    """
    返回前端可直接渲染的 provider 目录。
    """
    try:
        providers = await LLMProviderManager().list_providers_async()
        return schemas.Response(success=True, data=providers)
    except Exception as err:
        return schemas.Response(success=False, message=str(err))


@router.post(
    "/provider-auth/start",
    summary="启动LLM提供商授权",
    response_model=schemas.Response,
)
async def start_llm_provider_auth(
    payload: LlmProviderAuthStartRequest,
    request: Request,
    _: User = Depends(get_current_active_superuser_async),
):
    """
    启动 provider 授权会话。
    """
    try:
        callback_url = None
        if payload.provider == "chatgpt" and payload.method == "browser_oauth":
            callback_url = str(
                request.url_for(
                    "llm_provider_auth_callback", provider_id=payload.provider
                )
            )
        result = await LLMProviderManager().start_auth(
            payload.provider,
            payload.method,
            callback_url,
        )
        return schemas.Response(success=True, data=result)
    except Exception as err:
        return schemas.Response(success=False, message=str(err))


@router.get(
    "/provider-auth/{session_id}",
    summary="获取LLM提供商授权会话状态",
    response_model=schemas.Response,
)
async def get_llm_provider_auth_session(
    session_id: str,
    _: User = Depends(get_current_active_superuser_async),
):
    """
    查询授权会话状态。
    """
    try:
        result = LLMProviderManager().get_session_status(session_id)
        return schemas.Response(success=True, data=result)
    except Exception as err:
        return schemas.Response(success=False, message=str(err))


@router.post(
    "/provider-auth/{session_id}/poll",
    summary="轮询LLM提供商授权会话",
    response_model=schemas.Response,
)
async def poll_llm_provider_auth_session(
    session_id: str,
    _: User = Depends(get_current_active_superuser_async),
):
    """
    轮询 device code / OAuth 会话状态。
    """
    try:
        result = await LLMProviderManager().poll_auth_session(session_id)
        return schemas.Response(success=True, data=result)
    except Exception as err:
        return schemas.Response(success=False, message=str(err))


@router.delete(
    "/provider-auth/{provider_id}",
    summary="断开LLM提供商授权",
    response_model=schemas.Response,
)
async def delete_llm_provider_auth(
    provider_id: str,
    _: User = Depends(get_current_active_superuser_async),
):
    """
    删除已保存的 provider 授权信息。
    """
    try:
        await LLMProviderManager().clear_auth(provider_id)
        return schemas.Response(success=True)
    except Exception as err:
        return schemas.Response(success=False, message=str(err))


@router.get(
    "/provider-auth/callback/{provider_id}",
    summary="LLM提供商OAuth回调",
    response_class=HTMLResponse,
    name="llm_provider_auth_callback",
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


@router.post("/test", summary="测试LLM调用", response_model=schemas.Response)
async def llm_test(
    payload: Annotated[Optional[LlmTestRequest], Body()] = None,
    _: User = Depends(get_current_active_superuser_async),
):
    """
    使用传入配置或当前已保存配置执行一次最小 LLM 调用。
    """
    payload = payload or LlmTestRequest(
        enabled=settings.AI_AGENT_ENABLE,
        provider=settings.LLM_PROVIDER,
        model=settings.LLM_MODEL,
        thinking_level=settings.LLM_THINKING_LEVEL,
        api_key=settings.LLM_API_KEY,
        base_url=settings.LLM_BASE_URL,
        base_url_preset=settings.LLM_BASE_URL_PRESET,
        user_agent=settings.LLM_USER_AGENT,
        use_proxy=settings.LLM_USE_PROXY,
    )

    if not payload.provider:
        return schemas.Response(success=False, message="请配置LLM提供商和模型")
    if not payload.model or not payload.model.strip():
        return schemas.Response(success=False, message="请先配置 LLM 模型")

    data = {
        "provider": payload.provider,
        "model": payload.model,
    }
    if not payload.enabled:
        return schemas.Response(success=False, message="请先启用智能助手", data=data)

    if payload.provider not in {"chatgpt", "github-copilot"} and (
        not payload.api_key or not payload.api_key.strip()
    ):
        return schemas.Response(
            success=False,
            message="请先配置 LLM API Key",
            data=data,
        )

    try:
        result = await LLMHelper.test_current_settings(
            provider=payload.provider,
            model=payload.model,
            thinking_level=payload.thinking_level,
            api_key=payload.api_key,
            base_url=payload.base_url,
            base_url_preset=payload.base_url_preset,
            user_agent=payload.user_agent,
            use_proxy=payload.use_proxy,
        )
        if not result.get("reply_preview"):
            return schemas.Response(
                success=False,
                message="模型响应为空",
                data=result,
            )
        return schemas.Response(success=True, data=result)
    except (LLMTestTimeout, TimeoutError) as err:
        logger.warning(err)
        return schemas.Response(
            success=False,
            message="LLM 调用超时",
        )
    except Exception as err:
        return schemas.Response(
            success=False,
            message=_sanitize_llm_error(str(err), payload.api_key),
        )
