from typing import Any

from fastapi import APIRouter, Depends

from app import schemas
from app.core.security import verify_token
from app.helper.aliyun import AliyunHelper

router = APIRouter()


@router.get("/qrcode", summary="生成二维码内容", response_model=schemas.Response)
def qrcode(_: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    生成二维码
    """
    qrcode_data, errmsg = AliyunHelper().generate_qrcode()
    if qrcode_data:
        return schemas.Response(success=True, data=qrcode_data)
    return schemas.Response(success=False, message=errmsg)


@router.get("/check", summary="二维码登录确认", response_model=schemas.Response)
def check(ck: str, t: str, _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    if not ck or not t:
        return schemas.Response(success=False, message="参数错误")
    data, errmsg = AliyunHelper().check_login(ck, t)
    if data:
        return schemas.Response(success=True, data=data)
    return schemas.Response(success=False, message=errmsg)


@router.get("/userinfo", summary="查询用户信息", response_model=schemas.Response)
def userinfo(_: schemas.TokenPayload = Depends(verify_token)) -> Any:
    info = AliyunHelper().get_user_info()
    if info:
        return schemas.Response(success=True, data=info)
    return schemas.Response(success=False)
