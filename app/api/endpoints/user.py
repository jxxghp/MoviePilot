import base64
import re
from typing import Annotated, Any, List, Union

from fastapi import APIRouter, Body, Depends, HTTPException, UploadFile, File, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app import schemas
from app.core import security
from app.core.security import get_password_hash
from app.db import get_async_db
from app.db.models.user import User
from app.db.user_oper import (
    get_current_active_superuser_async,
    get_current_active_user_async,
    get_current_active_user,
    UserOper,
)
from app.db.userconfig_oper import UserConfigOper
from app.core.config import settings
from app.log import logger
from app.helper.sites import SitesHelper  # noqa

router = APIRouter()


@router.get("/", summary="所有用户", response_model=List[schemas.User])
async def list_users(
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(get_current_active_superuser_async),
) -> Any:
    """
    查询用户列表
    """
    return await current_user.async_list(db)


@router.post("/", summary="新增用户", response_model=schemas.Response)
async def create_user(
    *,
    db: AsyncSession = Depends(get_async_db),
    user_in: schemas.UserCreate,
    current_user: User = Depends(get_current_active_superuser_async),
) -> Any:
    """
    新增用户
    """
    user = await current_user.async_get_by_name(db, name=user_in.name)
    if user:
        return schemas.Response(success=False, message="用户已存在")
    user_info = user_in.model_dump()
    if user_info.get("password"):
        user_info["hashed_password"] = get_password_hash(user_info["password"])
        user_info.pop("password")
    user = await User(**user_info).async_create(db)
    return schemas.Response(success=True if user else False)


@router.put("/", summary="更新用户", response_model=schemas.Response)
async def update_user(
    *,
    db: AsyncSession = Depends(get_async_db),
    user_in: schemas.UserUpdate,
    current_user: User = Depends(get_current_active_superuser_async),
) -> Any:
    """
    更新用户
    """
    user_info = user_in.model_dump()
    if user_info.get("password"):
        # 正则表达式匹配密码包含字母、数字、特殊字符中的至少两项
        pattern = r"^(?![a-zA-Z]+$)(?!\d+$)(?![^\da-zA-Z\s]+$).{6,50}$"
        if not re.match(pattern, user_info.get("password")):
            return schemas.Response(
                success=False,
                message="密码需要同时包含字母、数字、特殊字符中的至少两项，且长度大于6位",
            )
        user_info["hashed_password"] = get_password_hash(user_info["password"])
        user_info.pop("password")
    user = await current_user.async_get_by_id(db, user_id=user_info["id"])
    user_name = user_info.get("name")
    if not user_name:
        return schemas.Response(success=False, message="用户名不能为空")
    # 新用户名去重
    users = await current_user.async_list(db)
    for u in users:
        if u.name == user_name and u.id != user_info["id"]:
            return schemas.Response(success=False, message="用户名已被使用")
    if not user:
        return schemas.Response(success=False, message="用户不存在")
    await user.async_update(db, user_info)
    return schemas.Response(success=True)


@router.get("/current", summary="当前登录用户信息", response_model=schemas.User)
async def read_current_user(
    current_user: User = Depends(get_current_active_user_async),
) -> Any:
    """
    当前登录用户信息
    """
    return current_user


@router.post(
    "/avatar/{user_id}", summary="上传用户头像", response_model=schemas.Response
)
async def upload_avatar(
    user_id: int,
    db: AsyncSession = Depends(get_async_db),
    file: UploadFile = File(...),
    _: User = Depends(get_current_active_user_async),
):
    """
    上传用户头像
    """
    # 将文件转换为Base64
    file_base64 = base64.b64encode(file.file.read())
    # 更新到用户表
    user = await User.async_get(db, user_id)
    if not user:
        return schemas.Response(success=False, message="用户不存在")
    await user.async_update(db, {"avatar": f"data:image/ico;base64,{file_base64}"})
    return schemas.Response(success=True, message=file.filename)


@router.get("/config/{key}", summary="查询用户配置", response_model=schemas.Response)
def get_config(key: str, current_user: User = Depends(get_current_active_user)):
    """
    查询用户配置
    """
    value = UserConfigOper().get(username=current_user.name, key=key)
    return schemas.Response(success=True, data={"value": value})


@router.post("/config/{key}", summary="更新用户配置", response_model=schemas.Response)
def set_config(
    key: str,
    value: Annotated[Union[list, dict, bool, int, str] | None, Body()] = None,
    current_user: User = Depends(get_current_active_user),
):
    """
    更新用户配置
    """
    UserConfigOper().set(username=current_user.name, key=key, value=value)
    return schemas.Response(success=True)


@router.delete("/id/{user_id}", summary="删除用户", response_model=schemas.Response)
async def delete_user_by_id(
    *,
    db: AsyncSession = Depends(get_async_db),
    user_id: int,
    current_user: User = Depends(get_current_active_superuser_async),
) -> Any:
    """
    通过唯一ID删除用户
    """
    user = await current_user.async_get_by_id(db, user_id=user_id)
    if not user:
        return schemas.Response(success=False, message="用户不存在")
    await current_user.async_delete(db, user_id)
    return schemas.Response(success=True)


@router.delete("/name/{user_name}", summary="删除用户", response_model=schemas.Response)
async def delete_user_by_name(
    *,
    db: AsyncSession = Depends(get_async_db),
    user_name: str,
    current_user: User = Depends(get_current_active_superuser_async),
) -> Any:
    """
    通过用户名删除用户
    """
    user = await current_user.async_get_by_name(db, name=user_name)
    if not user:
        return schemas.Response(success=False, message="用户不存在")
    await current_user.async_delete(db, user.id)
    return schemas.Response(success=True)


@router.get("/{username}", summary="用户详情", response_model=schemas.User)
async def read_user_by_name(
    username: str,
    current_user: User = Depends(get_current_active_user_async),
    db: AsyncSession = Depends(get_async_db),
) -> Any:
    """
    查询用户详情
    """
    user = await current_user.async_get_by_name(db, name=username)
    if not user:
        raise HTTPException(
            status_code=404,
            detail="用户不存在",
        )
    if user == current_user:
        return user
    if not current_user.is_superuser:
        raise HTTPException(status_code=400, detail="用户权限不足")
    return user


# ==================== OIDC 绑定/解绑端点 ====================


@router.get("/oidc/status", summary="查询 OIDC 绑定状态")
async def oidc_status(
    current_user: User = Depends(get_current_active_user_async),
) -> Any:
    """
    查询当前用户 OIDC 绑定状态
    """
    # 脱敏显示：前4位 + ***
    masked_sub = None
    if current_user.openid_sub:
        sub = current_user.openid_sub
        masked_sub = (sub[:6] + "***") if len(sub) > 6 else (sub + "***")

    return schemas.Response(
        success=True,
        data={
            "bound": bool(current_user.openid_sub),
            "enabled": security.is_oidc_enabled(),
            "masked_sub": masked_sub,
        },
    )


@router.get("/oidc/bind/authorize", summary="OIDC 绑定授权跳转")
async def oidc_bind_authorize(
    request: Request,
    db: AsyncSession = Depends(get_async_db),
    access_token: str = None,
):
    """
    发起 OIDC 绑定，生成 state（关联当前用户ID）并重定向到 IdP 授权页面

    支持通过 access_token 查询参数或 Authorization: Bearer 头部传入 JWT
    绑定流程复用登录回调地址 /login/oidc/callback，通过 state 中的 action 区分
    """
    current_user = None

    # 优先从 Authorization: Bearer 头部获取令牌
    auth_header = request.headers.get("Authorization", "")
    bearer_token = None
    if auth_header.startswith("Bearer "):
        bearer_token = auth_header[7:]

    token = bearer_token or access_token

    if token:
        # 弹窗场景：通过 query 参数或 Bearer 头传入 JWT
        try:
            import jwt as jwt_lib
            from app.core.security import ALGORITHM
            payload = jwt_lib.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])
            token_payload = schemas.TokenPayload(**payload)
            if token_payload.purpose != "authentication":
                raise HTTPException(status_code=403, detail="令牌用途不匹配")
            user = await User.async_get(db, rid=token_payload.sub)
            if not user:
                raise HTTPException(status_code=403, detail="用户不存在")
            if not user.is_active:
                raise HTTPException(status_code=403, detail="用户未激活")
            current_user = user
        except jwt_lib.DecodeError:
            raise HTTPException(status_code=401, detail="无效的访问令牌")
        except jwt_lib.InvalidTokenError:
            raise HTTPException(status_code=401, detail="无效的访问令牌")

    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    if not security.is_oidc_enabled():
        raise HTTPException(status_code=400, detail="OIDC 未启用")

    if current_user.openid_sub:
        raise HTTPException(status_code=400, detail="当前用户已绑定 OIDC 账号")

    # 生成 state 并标记为绑定操作，关联当前用户ID
    state = security.generate_oidc_state()
    security.store_oidc_state(state, user_id=current_user.id, action="bind")

    # 复用登录回调地址，通过 state 区分登录和绑定
    redirect_uri = settings.OIDC_REDIRECT_URI or settings.MP_DOMAIN("/api/v1/login/oidc/callback") or str(request.url_for("oidc_callback"))

    # 构建授权 URL 并重定向
    authorize_url = await security.build_oidc_authorize_url(
        redirect_uri=redirect_uri, state=state
    )
    return RedirectResponse(url=authorize_url)




@router.post("/oidc/unbind", summary="解绑 OIDC 账号", response_model=schemas.Response)
async def oidc_unbind(
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(get_current_active_user_async),
) -> Any:
    """
    解绑当前用户的 OIDC 账号
    """
    if not current_user.openid_sub:
        return schemas.Response(success=False, message="当前用户未绑定 OIDC 账号")

    user_name = current_user.name
    await current_user.async_update(db, {"openid_sub": None})
    logger.info(f"用户 {user_name} 已解绑 OIDC 账号")
    return schemas.Response(success=True)
