from datetime import timedelta
from typing import Any, List, Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import OAuth2PasswordRequestForm

from app import schemas
from app.chain.user import UserChain
from app.core import security
from app.log import logger
from app.core.config import settings
from app.db.systemconfig_oper import SystemConfigOper
from app.helper.sites import SitesHelper  # noqa
from app.helper.image import WallpaperHelper
from app.schemas.types import SystemConfigKey

router = APIRouter()


@router.post("/access-token", summary="获取token", response_model=schemas.Token)
def login_access_token(
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
    otp_password: Annotated[str | None, Form()] = None,
) -> Any:
    """
    获取认证Token
    """
    success, user_or_message = UserChain().user_authenticate(
        username=form_data.username, password=form_data.password, mfa_code=otp_password
    )

    if not success:
        # 如果是需要MFA验证，返回特殊标识
        if user_or_message == "MFA_REQUIRED":
            raise HTTPException(
                status_code=401,
                detail="需要双重验证，请提供验证码或使用通行密钥",
                headers={"X-MFA-Required": "true"},
            )
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    # 用户等级
    level = SitesHelper().auth_level
    # 是否显示配置向导
    show_wizard = (
        not SystemConfigOper().get(SystemConfigKey.SetupWizardState)
        and not settings.ADVANCED_MODE
    )
    return schemas.Token(
        access_token=security.create_access_token(
            userid=user_or_message.id,
            username=user_or_message.name,
            super_user=user_or_message.is_superuser,
            expires_delta=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
            level=level,
        ),
        token_type="bearer",
        super_user=user_or_message.is_superuser,
        user_id=user_or_message.id,
        user_name=user_or_message.name,
        avatar=user_or_message.avatar,
        level=level,
        permissions=user_or_message.permissions or {},
        wizard=show_wizard,
    )


@router.get("/oidc/enabled", summary="查询 OIDC 登录是否启用")
def oidc_enabled():
    """
    查询 OIDC 登录是否已启用（无需认证，供登录页面使用）
    """
    return schemas.Response(success=True, data={"enabled": security.is_oidc_enabled()})


@router.get("/oidc/authorize", summary="OIDC 登录授权跳转")
async def oidc_authorize(request: Request):
    """
    发起 OIDC 登录，生成 state 并重定向到 IdP 授权页面
    """
    if not security.is_oidc_enabled():
        raise HTTPException(status_code=400, detail="OIDC 登录未启用")

    # 生成 state 并存储
    state = security.generate_oidc_state()
    security.store_oidc_state(state, user_id=None, action="login")

    # 构建回调地址：优先使用配置的 OIDC_REDIRECT_URI，否则自动根据请求生成
    redirect_uri = settings.OIDC_REDIRECT_URI or str(request.url_for("oidc_callback"))

    # 构建授权 URL 并重定向
    authorize_url = await security.build_oidc_authorize_url(
        redirect_uri=redirect_uri, state=state
    )
    return RedirectResponse(url=authorize_url)


@router.get("/oidc/callback", summary="OIDC 登录/绑定回调", name="oidc_callback")
async def oidc_callback(
    request: Request,
    code: str = None,
    state: str = None,
    error: str = None,
    error_description: str = None,
):
    """
    OIDC 统一回调，验证 state，用 code 换 token。
    根据 state 中的 action 区分登录或绑定操作：
    - action="login": 查询绑定用户，生成 JWT 登录
    - action="bind": 将 openid_sub 绑定到指定用户
    通过 HTML 页面 + postMessage 将结果回传给弹窗的父窗口。
    """
    # 结果数据，将通过 postMessage 发送给父窗口
    result = {"type": "oidc_callback", "success": False}

    try:
        if not security.is_oidc_enabled():
            result["error"] = "oidc_disabled"
            result["message"] = "OIDC 登录未启用"
            return _oidc_callback_html(result)

        # IdP 返回了错误
        if error:
            logger.warning(f"OIDC 授权失败: {error} - {error_description}")
            result["error"] = "oidc_error"
            result["message"] = error_description or error
            return _oidc_callback_html(result)

        # 验证 state
        state_data = None
        if state:
            state_data = security.pop_oidc_state(state)

        if not state_data and not code:
            result["error"] = "oidc_invalid_state"
            result["message"] = "无效的授权状态"
            return _oidc_callback_html(result)

        # 提取 state 中的 action 和 user_id
        action = state_data.get("action", "login") if state_data else "login"
        bind_user_id = state_data.get("user_id") if state_data else None

        if not code:
            result["error"] = "oidc_no_code"
            result["message"] = "未获取到授权码"
            return _oidc_callback_html(result)

        # 用 code 换 token（回调地址须与授权时一致）
        redirect_uri = settings.OIDC_REDIRECT_URI or str(request.url_for("oidc_callback"))
        token_response = await security.oidc_exchange_code(code=code, redirect_uri=redirect_uri)
        oidc_access_token = token_response.get("access_token")
        if not oidc_access_token:
            result["error"] = "oidc_no_token"
            result["message"] = "未获取到访问令牌"
            return _oidc_callback_html(result)

        # 获取用户信息
        userinfo = await security.oidc_get_userinfo(oidc_access_token)
        openid_sub = userinfo.get("sub")
        if not openid_sub:
            result["error"] = "oidc_no_sub"
            result["message"] = "未获取到用户唯一标识"
            return _oidc_callback_html(result)

        # 根据 action 分别处理登录和绑定
        if action == "bind":
            # 绑定流程
            result["type"] = "oidc_bind_callback"
            return _handle_oidc_bind(result, openid_sub, bind_user_id)
        else:
            # 登录流程
            return _handle_oidc_login(result, openid_sub)

    except Exception as e:
        logger.error(f"OIDC 回调处理异常: {e}")
        result["error"] = "oidc_error"
        result["message"] = str(e)

    return _oidc_callback_html(result)


def _handle_oidc_login(result: dict, openid_sub: str) -> HTMLResponse:
    """
    处理 OIDC 登录：根据 openid_sub 查找绑定用户并生成 JWT
    """
    from app.db.user_oper import UserOper

    user_oper = UserOper()
    user = user_oper.get_by_openid_sub(openid_sub)

    if not user:
        logger.info(f"OIDC 用户 {openid_sub} 未绑定系统用户")
        result["error"] = "oidc_unbound"
        result["message"] = "该 OIDC 账号未绑定系统用户"
        return _oidc_callback_html(result)

    if not user.is_active:
        result["error"] = "user_inactive"
        result["message"] = "用户已被禁用"
        return _oidc_callback_html(result)

    # 生成 JWT
    level = SitesHelper().auth_level
    jwt_token = security.create_access_token(
        userid=user.id,
        username=user.name,
        super_user=user.is_superuser,
        expires_delta=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
        level=level,
    )

    # 判断是否显示配置向导
    show_wizard = (
        not SystemConfigOper().get(SystemConfigKey.SetupWizardState)
        and not settings.ADVANCED_MODE
    )

    result["success"] = True
    result["data"] = {
        "token": jwt_token,
        "super_user": bool(user.is_superuser),
        "user_id": user.id,
        "user_name": user.name,
        "avatar": user.avatar or "",
        "level": level,
        "permissions": user.permissions or {},
        "wizard": show_wizard,
    }

    return _oidc_callback_html(result)


def _handle_oidc_bind(result: dict, openid_sub: str, bind_user_id: int) -> HTMLResponse:
    """
    处理 OIDC 绑定：将 openid_sub 绑定到指定用户
    """
    from app.db.user_oper import UserOper
    from app.db import get_db
    from app.db.models.user import User

    if not bind_user_id:
        result["error"] = "oidc_bind_error"
        result["message"] = "缺少绑定用户信息"
        return _oidc_callback_html(result)

    # 检查 openid_sub 是否已被其他用户绑定
    user_oper = UserOper()
    existing_user = user_oper.get_by_openid_sub(openid_sub)
    if existing_user:
        if existing_user.id == bind_user_id:
            result["error"] = "oidc_bind_error"
            result["message"] = "该 OIDC 账号已绑定到当前用户"
        else:
            result["error"] = "oidc_bind_error"
            result["message"] = "该 OIDC 账号已被其他用户绑定"
        return _oidc_callback_html(result)

    # 绑定 openid_sub 到用户
    db = next(get_db())
    try:
        user = User.get(db, rid=bind_user_id)
        if not user:
            result["error"] = "oidc_bind_error"
            result["message"] = "用户不存在"
            return _oidc_callback_html(result)

        if user.openid_sub:
            result["error"] = "oidc_bind_error"
            result["message"] = "当前用户已绑定其他 OIDC 账号"
            return _oidc_callback_html(result)

        user.update(db, {"openid_sub": openid_sub})
        logger.info(f"用户 {user.name} 成功绑定 OIDC 账号 {openid_sub}")
        result["success"] = True
        result["message"] = "OIDC 账号绑定成功"
    except Exception as e:
        logger.error(f"OIDC 绑定异常: {e}")
        result["error"] = "oidc_bind_error"
        result["message"] = str(e)
    finally:
        db.close()

    return _oidc_callback_html(result)


def _oidc_callback_html(result: dict) -> HTMLResponse:
    """
    生成 OIDC 回调 HTML 页面，通过 postMessage 将结果发送给父窗口并关闭自身
    """
    import json

    result_json = json.dumps(result, ensure_ascii=False)

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>OIDC Callback</title></head>
<body>
<script>
(function() {{
  var result = {result_json};
  if (window.opener && !window.opener.closed) {{
    result.type = 'oidc_callback';
    window.opener.postMessage(result, '*');
    window.close();
  }} else {{
    // 弹窗跨域重定向后 window.opener 可能为 null，跳转到前端 callback 页面完成登录
    var params = new URLSearchParams();
    if (result.success && result.data) {{
      params.set('token', result.data.token || '');
      params.set('super_user', result.data.super_user ? 'true' : 'false');
      params.set('user_id', result.data.user_id || '');
      params.set('user_name', result.data.user_name || '');
      params.set('avatar', result.data.avatar || '');
      params.set('level', result.data.level || '1');
      params.set('permissions', JSON.stringify(result.data.permissions || {{}}));
      params.set('wizard', result.data.wizard ? 'true' : 'false');
    }} else {{
      params.set('error', result.error || 'oidc_error');
      if (result.message) params.set('message', result.message);
    }}
    window.location.href = '/#/oidc/callback?' + params.toString();
  }}
}})();
</script>
</body>
</html>"""
    return HTMLResponse(content=html)


@router.get("/oidc/test", summary="测试 OIDC 连接")
async def oidc_test(_: schemas.TokenPayload = Depends(security.verify_token)):
    """
    测试 OIDC 提供商连接，获取发现文档并验证必要端点
    """
    if not settings.OIDC_ISSUER:
        return schemas.Response(success=False, message="OIDC 签发者 URL 未配置")
    try:
        discovery = await security.get_oidc_discovery()
        if not discovery.get("authorization_endpoint") or not discovery.get("token_endpoint"):
            return schemas.Response(success=False, message="发现文档缺少必要的端点（authorization_endpoint 或 token_endpoint）")
        return schemas.Response(success=True, message="OIDC 连接测试成功")
    except HTTPException as e:
        return schemas.Response(success=False, message=str(e.detail))
    except Exception as e:
        return schemas.Response(success=False, message=f"连接失败: {e}")


@router.get("/wallpaper", summary="登录页面电影海报", response_model=schemas.Response)
def wallpaper() -> Any:
    """
    获取登录页面电影海报
    """
    url = WallpaperHelper().get_wallpaper()
    if url:
        return schemas.Response(success=True, message=url)
    return schemas.Response(success=False)


@router.get("/wallpapers", summary="登录页面电影海报列表", response_model=List[str])
def wallpapers() -> Any:
    """
    获取登录页面电影海报
    """
    return WallpaperHelper().get_wallpapers()
