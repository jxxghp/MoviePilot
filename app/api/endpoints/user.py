import base64
import re
from typing import Annotated, Any, List, Union

from fastapi import Body, Depends, HTTPException, UploadFile, File

from app.schemas.common import FileNameData as _SchemaFileNameData
from app.schemas.common import ValueData as _SchemaValueData
from app.schemas.response import Response as _SchemaResponse
from app.schemas.user import User as _SchemaUser
from app.schemas.user import UserCreate as _SchemaUserCreate
from app.schemas.user import UserUpdate as _SchemaUserUpdate
from app.schemas.user import UserIdentityInfo as _SchemaUserIdentityInfo
from app.api.response import ResponseAPIRouter
from app.application.security.token import PasswordTooLongError, get_password_hash
from app.application.security.user import UserService
from app.application.security.identity import UserIdentityService
from app.api.deps import (
    get_current_active_superuser_async,
    get_current_active_user_async,
    get_current_active_user,
    get_user_service,
    get_user_identity_service,
)
from app.application.security.userconfig import get_configured_user_configuration

router = ResponseAPIRouter()


@router.get("/", summary="所有用户", response_model=List[_SchemaUser])
async def list_users(
    service: UserService = Depends(get_user_service),
    current_user: Any = Depends(get_current_active_superuser_async),
) -> Any:
    """
    查询用户列表
    """
    return await service.list()


@router.post("/", summary="新增用户", response_model=_SchemaResponse[None])
async def create_user(
    *,
    service: UserService = Depends(get_user_service),
    user_in: _SchemaUserCreate,
    current_user: Any = Depends(get_current_active_superuser_async),
) -> Any:
    """
    新增用户
    """
    user = await service.get_by_name(user_in.name)
    if user:
        return _SchemaResponse(success=False, message="用户已存在")
    user_info = user_in.model_dump()
    if user_info.get("password"):
        try:
            user_info["hashed_password"] = get_password_hash(user_info["password"])
        except PasswordTooLongError as error:
            return _SchemaResponse(success=False, message=str(error))
        user_info.pop("password")
    user = await service.create(user_info)
    return _SchemaResponse(success=True if user else False)


@router.put("/", summary="更新用户", response_model=_SchemaResponse[None])
async def update_user(
    *,
    service: UserService = Depends(get_user_service),
    user_in: _SchemaUserUpdate,
    current_user: Any = Depends(get_current_active_superuser_async),
) -> Any:
    """
    更新用户
    """
    user_info = user_in.model_dump()
    if user_info.get("password"):
        # 正则表达式匹配密码包含字母、数字、特殊字符中的至少两项
        pattern = r"^(?![a-zA-Z]+$)(?!\d+$)(?![^\da-zA-Z\s]+$).{6,50}$"
        if not re.match(pattern, user_info.get("password")):
            return _SchemaResponse(
                success=False,
                message="密码需要同时包含字母、数字、特殊字符中的至少两项，且长度大于6位",
            )
        try:
            user_info["hashed_password"] = get_password_hash(user_info["password"])
        except PasswordTooLongError as error:
            return _SchemaResponse(success=False, message=str(error))
        user_info.pop("password")
    user = await service.get_by_id(user_info["id"])
    user_name = user_info.get("name")
    if not user_name:
        return _SchemaResponse(success=False, message="用户名不能为空")
    # 新用户名去重
    users = await service.list()
    for u in users:
        if u.name == user_name and u.id != user_info["id"]:
            return _SchemaResponse(success=False, message="用户名已被使用")
    if not user:
        return _SchemaResponse(success=False, message="用户不存在")
    await service.update(user_info["id"], user_info)
    return _SchemaResponse(success=True)


@router.get("/current", summary="当前登录用户信息", response_model=_SchemaUser)
async def read_current_user(
    current_user: Any = Depends(get_current_active_user_async),
) -> Any:
    """
    当前登录用户信息
    """
    return current_user


@router.post(
    "/avatar/{user_id}",
    summary="上传用户头像",
    response_model=_SchemaResponse[_SchemaFileNameData],
)
async def upload_avatar(
    user_id: int,
    service: UserService = Depends(get_user_service),
    file: UploadFile = File(...),
    current_user: Any = Depends(get_current_active_user_async),
) -> _SchemaResponse:
    """
    上传用户头像
    """
    if current_user.id != user_id and not current_user.is_superuser:
        raise HTTPException(status_code=400, detail="用户权限不足")

    # 将文件转换为Base64
    file_base64 = base64.b64encode(file.file.read())
    # 更新到用户表
    user = await service.get_by_id(user_id)
    if not user:
        return _SchemaResponse(success=False, message="用户不存在")
    await service.update(user_id, {"avatar": f"data:image/ico;base64,{file_base64}"})
    return _SchemaResponse(success=True, data={"filename": file.filename})


@router.get(
    "/config/{key}",
    summary="查询用户配置",
    response_model=_SchemaResponse[_SchemaValueData],
)
def get_config(key: str, current_user: Any = Depends(get_current_active_user)):
    """
    查询用户配置
    """
    value = get_configured_user_configuration().get(username=current_user.name, key=key)
    return _SchemaResponse(success=True, data={"value": value})


@router.post("/config/{key}", summary="更新用户配置", response_model=_SchemaResponse[None])
def set_config(
    key: str,
    value: Annotated[Union[list, dict, bool, int, str] | None, Body()] = None,
    current_user: Any = Depends(get_current_active_user),
):
    """
    更新用户配置
    """
    get_configured_user_configuration().set(
        username=current_user.name,
        key=key,
        value=value,
    )
    return _SchemaResponse(success=True)


@router.get(
    "/identity/list",
    summary="获取当前用户的第三方身份绑定列表",
    response_model=_SchemaResponse[List[_SchemaUserIdentityInfo]],
)
def list_user_identities(
    current_user: Any = Depends(get_current_active_user),
    service: UserIdentityService = Depends(get_user_identity_service),
) -> Any:
    """
    获取当前用户绑定的全部第三方身份
    """
    identities = service.list_by_user_id(current_user.id)
    return _SchemaResponse(
        success=True,
        data=[
            {
                "id": identity.id,
                "provider": identity.provider,
                "external_id": identity.external_id,
                "display_name": identity.display_name,
                "created_at": identity.created_at.isoformat() if identity.created_at else None,
            }
            for identity in identities
        ],
    )


@router.delete(
    "/identity/{identity_id}",
    summary="解绑第三方身份",
    response_model=_SchemaResponse[None],
)
def unbind_user_identity(
    identity_id: int,
    current_user: Any = Depends(get_current_active_user),
    service: UserIdentityService = Depends(get_user_identity_service),
) -> Any:
    """
    解绑当前用户名下的第三方身份，不属于当前用户的绑定不会被解绑
    """
    if service.unbind(identity_id, current_user.id):
        return _SchemaResponse(success=True, message="已解绑")
    return _SchemaResponse(success=False, message="绑定不存在")


@router.delete("/id/{user_id}", summary="删除用户", response_model=_SchemaResponse[None])
async def delete_user_by_id(
    *,
    service: UserService = Depends(get_user_service),
    user_id: int,
    current_user: Any = Depends(get_current_active_superuser_async),
) -> Any:
    """
    通过唯一ID删除用户
    """
    user = await service.get_by_id(user_id)
    if not user:
        return _SchemaResponse(success=False, message="用户不存在")
    await service.delete(user_id)
    return _SchemaResponse(success=True)


@router.delete("/name/{user_name}", summary="删除用户", response_model=_SchemaResponse[None])
async def delete_user_by_name(
    *,
    service: UserService = Depends(get_user_service),
    user_name: str,
    current_user: Any = Depends(get_current_active_superuser_async),
) -> Any:
    """
    通过用户名删除用户
    """
    user = await service.get_by_name(user_name)
    if not user:
        return _SchemaResponse(success=False, message="用户不存在")
    await service.delete(user.id)
    return _SchemaResponse(success=True)


@router.get("/{username}", summary="用户详情", response_model=_SchemaUser)
async def read_user_by_name(
    username: str,
    current_user: Any = Depends(get_current_active_user_async),
    service: UserService = Depends(get_user_service),
) -> Any:
    """
    查询用户详情
    """
    user = await service.get_by_name(username)
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
