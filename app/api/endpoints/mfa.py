"""
MFA (Multi-Factor Authentication) API 端点
包含 OTP 和 PassKey 相关功能
"""
from datetime import timedelta
from typing import Any, Annotated, Optional

from app.helper.sites import SitesHelper
from fastapi import APIRouter, Depends, HTTPException, Body
from sqlalchemy.ext.asyncio import AsyncSession

from app import schemas
from app.core import security
from app.core.config import settings
from app.db import get_async_db
from app.db.models.passkey import PassKey
from app.db.models.user import User
from app.db.systemconfig_oper import SystemConfigOper
from app.db.user_oper import get_current_active_user, get_current_active_user_async
from app.helper.passkey import PassKeyHelper
from app.log import logger
from app.schemas.types import SystemConfigKey
from app.utils.otp import OtpUtils

router = APIRouter()

# ==================== 请求模型 ====================

class OtpVerifyRequest(schemas.BaseModel):
    """OTP验证请求"""
    uri: str
    otpPassword: str

class OtpDisableRequest(schemas.BaseModel):
    """OTP禁用请求"""
    password: str

class PassKeyDeleteRequest(schemas.BaseModel):
    """PassKey删除请求"""
    passkey_id: int
    password: str

# ==================== 通用 MFA 接口 ====================

@router.get('/status/{username}', summary='判断用户是否开启双重验证(MFA)', response_model=schemas.Response)
async def mfa_status(username: str, db: AsyncSession = Depends(get_async_db)) -> Any:
    """
    检查指定用户是否启用了任何双重验证方式（OTP 或 PassKey）
    """
    user: User = await User.async_get_by_name(db, username)
    if not user:
        return schemas.Response(success=False)
    
    # 检查是否启用了OTP
    has_otp = user.is_otp
    
    # 检查是否有PassKey
    has_passkey = bool(await PassKey.async_get_by_user_id(db=db, user_id=user.id))
    
    # 只要有任何一种验证方式，就需要双重验证
    return schemas.Response(success=(has_otp or has_passkey))


# ==================== OTP 相关接口 ====================

@router.post('/otp/generate', summary='生成 OTP 验证 URI', response_model=schemas.Response)
def otp_generate(
    current_user: Annotated[User, Depends(get_current_active_user)]
) -> Any:
    """生成 OTP 密钥及对应的 URI"""
    secret, uri = OtpUtils.generate_secret_key(current_user.name)
    return schemas.Response(success=secret != "", data={'secret': secret, 'uri': uri})


@router.post('/otp/verify', summary='绑定并验证 OTP', response_model=schemas.Response)
async def otp_verify(
    data: OtpVerifyRequest,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(get_current_active_user_async)
) -> Any:
    """验证用户输入的 OTP 码，验证通过后正式开启 OTP 验证"""
    if not OtpUtils.is_legal(data.uri, data.otpPassword):
        return schemas.Response(success=False, message="验证码错误")
    await current_user.async_update_otp_by_name(db, current_user.name, True, OtpUtils.get_secret(data.uri))
    return schemas.Response(success=True)


@router.post('/otp/disable', summary='关闭当前用户的 OTP 验证', response_model=schemas.Response)
async def otp_disable(
    data: OtpDisableRequest,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(get_current_active_user_async)
) -> Any:
    """关闭当前用户的 OTP 验证功能"""
    # 安全检查：如果存在 PassKey，不允许关闭 OTP
    has_passkey = bool(await PassKey.async_get_by_user_id(db=db, user_id=current_user.id))
    if has_passkey:
        return schemas.Response(
            success=False,
            message="您已注册通行密钥，为了防止域名配置变更导致无法登录，请先删除所有通行密钥再关闭 OTP 验证"
        )

    # 验证密码
    if not security.verify_password(data.password, str(current_user.hashed_password)):
        return schemas.Response(success=False, message="密码错误")
    await current_user.async_update_otp_by_name(db, current_user.name, False, "")
    return schemas.Response(success=True)


# ==================== PassKey 相关接口 ====================

class PassKeyRegistrationStart(schemas.BaseModel):
    """PassKey注册开始请求"""
    name: str = "通行密钥"


class PassKeyRegistrationFinish(schemas.BaseModel):
    """PassKey注册完成请求"""
    credential: dict
    challenge: str
    name: str = "通行密钥"


class PassKeyAuthenticationStart(schemas.BaseModel):
    """PassKey认证开始请求"""
    username: Optional[str] = None


class PassKeyAuthenticationFinish(schemas.BaseModel):
    """PassKey认证完成请求"""
    credential: dict
    challenge: str


@router.post("/passkey/register/start", summary="开始注册 PassKey", response_model=schemas.Response)
def passkey_register_start(
    current_user: Annotated[User, Depends(get_current_active_user)]
) -> Any:
    """开始注册 PassKey - 生成注册选项"""
    try:
        # 安全检查：必须先启用 OTP
        if not current_user.is_otp:
            return schemas.Response(
                success=False,
                message="为了确保在域名配置错误时仍能找回访问权限，请先启用 OTP 验证码再注册通行密钥"
            )

        # 获取用户已有的PassKey
        existing_passkeys = PassKey.get_by_user_id(db=None, user_id=current_user.id)
        existing_credentials = [
            {
                'credential_id': pk.credential_id,
                'transports': pk.transports
            }
            for pk in existing_passkeys
        ] if existing_passkeys else None

        # 生成注册选项
        options_json, challenge = PassKeyHelper.generate_registration_options(
            user_id=current_user.id,
            username=current_user.name,
            display_name=current_user.settings.get('nickname') if current_user.settings else None,
            existing_credentials=existing_credentials
        )

        return schemas.Response(
            success=True,
            data={
                'options': options_json,
                'challenge': challenge
            }
        )
    except Exception as e:
        logger.error(f"生成PassKey注册选项失败: {e}")
        return schemas.Response(
            success=False,
            message=f"生成注册选项失败: {str(e)}"
        )


@router.post("/passkey/register/finish", summary="完成注册 PassKey", response_model=schemas.Response)
def passkey_register_finish(
    passkey_req: PassKeyRegistrationFinish,
    current_user: Annotated[User, Depends(get_current_active_user)]
) -> Any:
    """完成注册 PassKey - 验证并保存凭证"""
    try:
        # 验证注册响应
        credential_id, public_key, sign_count, aaguid = PassKeyHelper.verify_registration_response(
            credential=passkey_req.credential,
            expected_challenge=passkey_req.challenge
        )

        # 提取transports
        transports = None
        if 'response' in passkey_req.credential and 'transports' in passkey_req.credential['response']:
            transports = ','.join(passkey_req.credential['response']['transports'])

        # 保存到数据库
        passkey = PassKey(
            user_id=current_user.id,
            credential_id=credential_id,
            public_key=public_key,
            sign_count=sign_count,
            name=passkey_req.name or "通行密钥",
            aaguid=aaguid,
            transports=transports
        )
        passkey.create()

        logger.info(f"用户 {current_user.name} 成功注册PassKey: {passkey_req.name}")

        return schemas.Response(
            success=True,
            message="通行密钥注册成功"
        )
    except Exception as e:
        logger.error(f"注册PassKey失败: {e}")
        return schemas.Response(
            success=False,
            message=f"注册失败: {str(e)}"
        )


@router.post("/passkey/authenticate/start", summary="开始 PassKey 认证", response_model=schemas.Response)
def passkey_authenticate_start(
    passkey_req: PassKeyAuthenticationStart = Body(...)
) -> Any:
    """开始 PassKey 认证 - 生成认证选项"""
    try:
        existing_credentials = None
        
        # 如果指定了用户名，只允许该用户的PassKey
        if passkey_req.username:
            user = User.get_by_name(db=None, name=passkey_req.username)
            if not user:
                return schemas.Response(
                    success=False,
                    message="用户不存在"
                )
            
            existing_passkeys = PassKey.get_by_user_id(db=None, user_id=user.id)
            if not existing_passkeys:
                return schemas.Response(
                    success=False,
                    message="该用户未注册通行密钥"
                )
            
            existing_credentials = [
                {
                    'credential_id': pk.credential_id,
                    'transports': pk.transports
                }
                for pk in existing_passkeys
            ]

        # 生成认证选项
        options_json, challenge = PassKeyHelper.generate_authentication_options(
            existing_credentials=existing_credentials
        )

        return schemas.Response(
            success=True,
            data={
                'options': options_json,
                'challenge': challenge
            }
        )
    except Exception as e:
        logger.error(f"生成PassKey认证选项失败: {e}")
        return schemas.Response(
            success=False,
            message=f"生成认证选项失败: {str(e)}"
        )


@router.post("/passkey/authenticate/finish", summary="完成 PassKey 认证", response_model=schemas.Token)
def passkey_authenticate_finish(
    passkey_req: PassKeyAuthenticationFinish
) -> Any:
    """完成 PassKey 认证 - 验证凭证并返回 token"""
    try:
        # 从credential中提取credential_id
        credential_id_raw = passkey_req.credential.get('id') or passkey_req.credential.get('rawId')
        if not credential_id_raw:
            raise HTTPException(status_code=400, detail="无效的凭证")

        # 标准化凭证ID
        credential_id = PassKeyHelper.standardize_credential_id(credential_id_raw)

        # 查找PassKey
        passkey = PassKey.get_by_credential_id(db=None, credential_id=credential_id)
        if not passkey:
            raise HTTPException(status_code=401, detail="通行密钥不存在或已失效")

        # 获取用户
        user = User.get_by_id(db=None, user_id=passkey.user_id)
        if not user or not user.is_active:
            raise HTTPException(status_code=401, detail="用户不存在或已禁用")

        # 验证认证响应
        success, new_sign_count = PassKeyHelper.verify_authentication_response(
            credential=passkey_req.credential,
            expected_challenge=passkey_req.challenge,
            credential_public_key=passkey.public_key,
            credential_current_sign_count=passkey.sign_count
        )

        if not success:
            raise HTTPException(status_code=401, detail="通行密钥验证失败")

        # 更新使用时间和签名计数
        passkey.update_last_used(db=None, sign_count=new_sign_count)

        logger.info(f"用户 {user.name} 通过PassKey认证成功")

        # 生成token
        level = SitesHelper().auth_level
        show_wizard = not SystemConfigOper().get(SystemConfigKey.SetupWizardState) and not settings.ADVANCED_MODE

        return schemas.Token(
            access_token=security.create_access_token(
                userid=user.id,
                username=user.name,
                super_user=user.is_superuser,
                expires_delta=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
                level=level
            ),
            token_type="bearer",
            super_user=user.is_superuser,
            user_id=user.id,
            user_name=user.name,
            avatar=user.avatar,
            level=level,
            permissions=user.permissions or {},
            wizard=show_wizard
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"PassKey认证失败: {e}")
        raise HTTPException(status_code=401, detail=f"认证失败: {str(e)}")


@router.get("/passkey/list", summary="获取当前用户的 PassKey 列表", response_model=schemas.Response)
def passkey_list(
    current_user: Annotated[User, Depends(get_current_active_user)]
) -> Any:
    """获取当前用户的所有 PassKey"""
    try:
        passkeys = PassKey.get_by_user_id(db=None, user_id=current_user.id)
        
        key_list = [
            {
                'id': pk.id,
                'name': pk.name,
                'created_at': pk.created_at.isoformat() if pk.created_at else None,
                'last_used_at': pk.last_used_at.isoformat() if pk.last_used_at else None,
                'aaguid': pk.aaguid,
                'transports': pk.transports
            }
            for pk in passkeys
        ] if passkeys else []

        return schemas.Response(
            success=True,
            data=key_list
        )
    except Exception as e:
        logger.error(f"获取PassKey列表失败: {e}")
        return schemas.Response(
            success=False,
            message=f"获取列表失败: {str(e)}"
        )


@router.post("/passkey/delete", summary="删除 PassKey", response_model=schemas.Response)
async def passkey_delete(
    data: PassKeyDeleteRequest,
    current_user: User = Depends(get_current_active_user_async)
) -> Any:
    """删除指定的 PassKey"""
    try:
        # 验证密码
        if not security.verify_password(data.password, str(current_user.hashed_password)):
            return schemas.Response(success=False, message="密码错误")

        success = PassKey.delete_by_id(db=None, passkey_id=data.passkey_id, user_id=current_user.id)
        
        if success:
            logger.info(f"用户 {current_user.name} 删除了PassKey: {data.passkey_id}")
            return schemas.Response(
                success=True,
                message="通行密钥已删除"
            )
        else:
            return schemas.Response(
                success=False,
                message="通行密钥不存在或无权删除"
            )
    except Exception as e:
        logger.error(f"删除PassKey失败: {e}")
        return schemas.Response(
            success=False,
            message=f"删除失败: {str(e)}"
        )


@router.post("/passkey/verify", summary="PassKey 二次验证", response_model=schemas.Response)
def passkey_verify_mfa(
    passkey_req: PassKeyAuthenticationFinish,
    current_user: Annotated[User, Depends(get_current_active_user)]
) -> Any:
    """使用 PassKey 进行二次验证（MFA）"""
    try:
        # 从credential中提取credential_id
        credential_id_raw = passkey_req.credential.get('id') or passkey_req.credential.get('rawId')
        if not credential_id_raw:
            return schemas.Response(
                success=False,
                message="无效的凭证"
            )

        # 标准化凭证ID
        credential_id = PassKeyHelper.standardize_credential_id(credential_id_raw)

        # 查找PassKey（必须属于当前用户）
        passkey = PassKey.get_by_credential_id(db=None, credential_id=credential_id)
        if not passkey or passkey.user_id != current_user.id:
            return schemas.Response(
                success=False,
                message="通行密钥不存在或不属于当前用户"
            )

        # 验证认证响应
        success, new_sign_count = PassKeyHelper.verify_authentication_response(
            credential=passkey_req.credential,
            expected_challenge=passkey_req.challenge,
            credential_public_key=passkey.public_key,
            credential_current_sign_count=passkey.sign_count
        )

        if not success:
            return schemas.Response(
                success=False,
                message="通行密钥验证失败"
            )

        # 更新使用时间和签名计数
        passkey.update_last_used(db=None, sign_count=new_sign_count)

        logger.info(f"用户 {current_user.name} 通过PassKey二次验证成功")

        return schemas.Response(
            success=True,
            message="二次验证成功"
        )
    except Exception as e:
        logger.error(f"PassKey二次验证失败: {e}")
        return schemas.Response(
            success=False,
            message=f"验证失败: {str(e)}"
        )
