import base64
import re
from typing import Annotated, Any, List, Union

from fastapi import Body, Depends, File, HTTPException, UploadFile

from app.api.dependencies.auth import (
    get_current_active_superuser_async,
    get_current_active_user_async,
    get_user_service,
)
from app.api.response import ResponseAPIRouter
from app.application.security.token import PasswordTooLongError, get_password_hash
from app.application.security.user import (
    LastActiveSuperuserError,
    UserNameConflictError,
    UserService,
)
from app.application.security.userconfig import get_configured_user_configuration
from app.schemas.common import FileNameData as _SchemaFileNameData
from app.schemas.common import ValueData as _SchemaValueData
from app.schemas.response import Response as _SchemaResponse
from app.schemas.user import CurrentUserUpdate as _SchemaCurrentUserUpdate
from app.schemas.user import User as _SchemaUser
from app.schemas.user import UserCreate as _SchemaUserCreate
from app.schemas.user import UserUpdate as _SchemaUserUpdate

router = ResponseAPIRouter()
_PASSWORD_PATTERN = re.compile(
    r"^(?![a-zA-Z]+$)(?!\d+$)(?![^\da-zA-Z\s]+$).{6,50}$"
)
_INVALID_PASSWORD_MESSAGE = "密码须为6至50位，并包含字母、数字、特殊字符中的至少两类"


def _prepare_password(user_info: dict[str, Any]) -> str | None:
    """按统一密码规则处理用户写入，并移除不会持久化的明文密码字段。"""
    password = user_info.pop("password", None)
    if not password:
        return None
    if not _PASSWORD_PATTERN.match(password):
        return _INVALID_PASSWORD_MESSAGE
    try:
        user_info["hashed_password"] = get_password_hash(password)
    except PasswordTooLongError as error:
        return str(error)
    return None


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
    user_info = user_in.model_dump()
    if user_info.get("password"):
        try:
            user_info["hashed_password"] = get_password_hash(user_info["password"])
        except PasswordTooLongError as error:
            return _SchemaResponse(success=False, message=str(error))
        user_info.pop("password")
    try:
        user = await service.create(user_info)
    except UserNameConflictError:
        return _SchemaResponse(success=False, message="用户已存在")
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
    password_error = _prepare_password(user_info)
    if password_error:
        return _SchemaResponse(success=False, message=password_error)
    user = await service.get_by_id(user_info["id"])
    user_name = user_info.get("name")
    if not user_name:
        return _SchemaResponse(success=False, message="用户名不能为空")
    if not user:
        return _SchemaResponse(success=False, message="用户不存在")
    try:
        await service.update(user_info["id"], user_info)
    except UserNameConflictError:
        return _SchemaResponse(success=False, message="用户名已被使用")
    except LastActiveSuperuserError:
        return _SchemaResponse(success=False, message="必须保留至少一个启用的超级管理员")
    return _SchemaResponse(success=True)


@router.put(  # type: ignore[misc]
    "/current", summary="更新当前用户资料", response_model=_SchemaUser
)
async def update_current_user(
    *,
    service: UserService = Depends(get_user_service),
    user_in: _SchemaCurrentUserUpdate,
    current_user: Any = Depends(get_current_active_user_async),
) -> Any:
    """仅更新当前用户允许自助维护的资料和通知身份设置。"""
    user_info = user_in.model_dump(exclude_unset=True)
    password_error = _prepare_password(user_info)
    if password_error:
        raise HTTPException(status_code=400, detail=password_error)
    user = await service.update(current_user.id, user_info)
    if user is None:
        raise HTTPException(
            status_code=401,
            detail="用户不存在",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


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
        raise HTTPException(status_code=403, detail="用户权限不足")

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
async def get_config(
    key: str,
    current_user: Any = Depends(get_current_active_user_async),
):
    """
    查询用户配置
    """
    value = get_configured_user_configuration().get(username=current_user.name, key=key)
    return _SchemaResponse(success=True, data={"value": value})


@router.post("/config/{key}", summary="更新用户配置", response_model=_SchemaResponse[None])
async def set_config(
    key: str,
    value: Annotated[Union[list, dict, bool, int, str] | None, Body()] = None,
    current_user: Any = Depends(get_current_active_user_async),
):
    """
    更新用户配置
    """
    await get_configured_user_configuration().async_set(
        username=current_user.name,
        key=key,
        value=value,
    )
    return _SchemaResponse(success=True)


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
    try:
        await service.delete(user_id)
    except LastActiveSuperuserError:
        return _SchemaResponse(success=False, message="必须保留至少一个启用的超级管理员")
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
    try:
        await service.delete(user.id)
    except LastActiveSuperuserError:
        return _SchemaResponse(success=False, message="必须保留至少一个启用的超级管理员")
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
        raise HTTPException(status_code=403, detail="用户权限不足")
    return user
