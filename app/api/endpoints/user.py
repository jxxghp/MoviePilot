import base64
import re
from typing import Any, List, Union

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.orm import Session

from app import schemas
from app.core.security import get_password_hash
from app.db import get_db
from app.db.models.user import User
from app.db.userauth import get_current_active_superuser, get_current_active_user
from app.db.userconfig_oper import UserConfigOper
from app.utils.otp import OtpUtils

router = APIRouter()


@router.get("/", summary="所有用户", response_model=List[schemas.User])
def read_users(
        db: Session = Depends(get_db),
        current_user: User = Depends(get_current_active_superuser),
) -> Any:
    """
    查询用户列表
    """
    users = current_user.list(db)
    return users


@router.post("/", summary="新增用户", response_model=schemas.Response)
def create_user(
        *,
        db: Session = Depends(get_db),
        user_in: schemas.UserCreate,
        current_user: User = Depends(get_current_active_superuser),
) -> Any:
    """
    新增用户
    """
    user = current_user.get_by_name(db, name=user_in.name)
    if user:
        return schemas.Response(success=False, message="用户已存在")
    user_info = user_in.dict()
    if user_info.get("password"):
        user_info["hashed_password"] = get_password_hash(user_info["password"])
        user_info.pop("password")
    user = User(**user_info)
    user.create(db)
    return schemas.Response(success=True)


@router.put("/", summary="更新用户", response_model=schemas.Response)
def update_user(
        *,
        db: Session = Depends(get_db),
        user_in: schemas.UserCreate,
        _: User = Depends(get_current_active_superuser),
) -> Any:
    """
    更新用户
    """
    user_info = user_in.dict()
    if user_info.get("password"):
        # 正则表达式匹配密码包含字母、数字、特殊字符中的至少两项
        pattern = r'^(?![a-zA-Z]+$)(?!\d+$)(?![^\da-zA-Z\s]+$).{6,50}$'
        if not re.match(pattern, user_info.get("password")):
            return schemas.Response(success=False,
                                    message="密码需要同时包含字母、数字、特殊字符中的至少两项，且长度大于6位")
        user_info["hashed_password"] = get_password_hash(user_info["password"])
        user_info.pop("password")
    user = User.get_by_name(db, name=user_info["name"])
    if not user:
        return schemas.Response(success=False, message="用户不存在")
    user.update(db, user_info)
    return schemas.Response(success=True)


@router.get("/current", summary="当前登录用户信息", response_model=schemas.User)
def read_current_user(
        current_user: User = Depends(get_current_active_user)
) -> Any:
    """
    当前登录用户信息
    """
    return current_user


@router.post("/avatar/{user_id}", summary="上传用户头像", response_model=schemas.Response)
def upload_avatar(user_id: int, db: Session = Depends(get_db), file: UploadFile = File(...),
                  _: User = Depends(get_current_active_user)):
    """
    上传用户头像
    """
    # 将文件转换为Base64
    file_base64 = base64.b64encode(file.file.read())
    # 更新到用户表
    user = User.get(db, user_id)
    if not user:
        return schemas.Response(success=False, message="用户不存在")
    user.update(db, {
        "avatar": f"data:image/ico;base64,{file_base64}"
    })
    return schemas.Response(success=True, message=file.filename)


@router.post('/otp/generate', summary='生成otp验证uri', response_model=schemas.Response)
def otp_generate(
        current_user: User = Depends(get_current_active_user)
) -> Any:
    secret, uri = OtpUtils.generate_secret_key(current_user.name)
    return schemas.Response(success=secret != "", data={'secret': secret, 'uri': uri})


@router.post('/otp/judge', summary='判断otp验证是否通过', response_model=schemas.Response)
def otp_judge(
        data: dict,
        db: Session = Depends(get_db),
        current_user: User = Depends(get_current_active_user)
) -> Any:
    uri = data.get("uri")
    otp_password = data.get("otpPassword")
    if not OtpUtils.is_legal(uri, otp_password):
        return schemas.Response(success=False, message="验证码错误")
    current_user.update_otp_by_name(db, current_user.name, True, OtpUtils.get_secret(uri))
    return schemas.Response(success=True)


@router.post('/otp/disable', summary='关闭当前用户的otp验证', response_model=schemas.Response)
def otp_disable(
        db: Session = Depends(get_db),
        current_user: User = Depends(get_current_active_user)
) -> Any:
    current_user.update_otp_by_name(db, current_user.name, False, "")
    return schemas.Response(success=True)


@router.get('/otp/{userid}', summary='判断当前用户是否开启otp验证', response_model=schemas.Response)
def otp_enable(userid: str, db: Session = Depends(get_db)) -> Any:
    user: User = User.get_by_name(db, userid)
    if not user:
        return schemas.Response(success=False, message="用户不存在")
    return schemas.Response(success=user.is_otp)


@router.get("/config/{key}", summary="查询用户配置", response_model=schemas.Response)
def get_config(key: str,
               current_user: User = Depends(get_current_active_user)):
    """
    查询用户配置
    """
    value = UserConfigOper().get(username=current_user.name, key=key)
    return schemas.Response(success=True, data={
        "value": value
    })


@router.post("/config/{key}", summary="更新用户配置", response_model=schemas.Response)
def set_config(key: str, value: Union[list, dict, bool, int, str] = None,
               current_user: User = Depends(get_current_active_user)):
    """
    更新用户配置
    """
    UserConfigOper().set(username=current_user.name, key=key, value=value)
    return schemas.Response(success=True)


@router.delete("/{user_name}", summary="删除用户", response_model=schemas.Response)
def delete_user(
        *,
        db: Session = Depends(get_db),
        user_name: str,
        current_user: User = Depends(get_current_active_superuser),
) -> Any:
    """
    删除用户
    """
    user = current_user.get_by_name(db, name=user_name)
    if not user:
        return schemas.Response(success=False, message="用户不存在")
    user.delete_by_name(db, user_name)
    return schemas.Response(success=True)


@router.get("/{user_id}", summary="用户详情", response_model=schemas.User)
def read_user_by_id(
        user_id: int,
        current_user: User = Depends(get_current_active_user),
        db: Session = Depends(get_db),
) -> Any:
    """
    查询用户详情
    """
    user = current_user.get(db, rid=user_id)
    if not user:
        raise HTTPException(
            status_code=404,
            detail="用户不存在",
        )
    if user == current_user:
        return user
    if not user.is_superuser:
        raise HTTPException(
            status_code=400,
            detail="用户权限不足"
        )
    return user
