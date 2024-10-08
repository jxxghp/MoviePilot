import base64
import hashlib
import hmac
import json
import os
import traceback
from datetime import datetime, timedelta
from typing import Any, Union, Annotated, Optional

import jwt
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from cryptography.fernet import Fernet
from fastapi import HTTPException, status, Security
from fastapi.security import OAuth2PasswordBearer, APIKeyHeader, APIKeyQuery
from passlib.context import CryptContext

from app import schemas
from app.core.config import settings
from app.log import logger

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
ALGORITHM = "HS256"

# OAuth2PasswordBearer 用于 JWT Token 认证
oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl=f"{settings.API_V1_STR}/login/access-token"
)

# JWT TOKEN 通过 QUERY 认证
jwt_token_query = APIKeyQuery(name="token", auto_error=False, scheme_name="jwt_token_query")

# API TOKEN 通过 QUERY 认证
api_token_query = APIKeyQuery(name="token", auto_error=False, scheme_name="api_token_query")

# API KEY 通过 Header 认证
api_key_header = APIKeyHeader(name="X-API-KEY", auto_error=False, scheme_name="api_key_header")

# API KEY 通过 QUERY 认证
api_key_query = APIKeyQuery(name="apikey", auto_error=False, scheme_name="api_key_query")


def create_access_token(
        userid: Union[str, Any],
        username: str,
        super_user: bool = False,
        expires_delta: Optional[timedelta] = None,
        level: int = 1
) -> str:
    """
    创建 JWT 访问令牌，包含用户 ID、用户名、是否为超级用户以及权限等级
    :param userid: 用户的唯一标识符，通常是字符串或整数
    :param username: 用户名，用于标识用户的账户名
    :param super_user: 是否为超级用户，默认值为 False
    :param expires_delta: 令牌的有效期时长，如果不提供则使用默认过期时间
    :param level: 用户的权限级别，默认为 1
    :return: 编码后的 JWT 令牌字符串
    :raises ValueError: 如果 expires_delta 为负数
    """
    if expires_delta is not None:
        if expires_delta.total_seconds() <= 0:
            raise ValueError("过期时间必须为正数")
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(
            minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
        )

    to_encode = {
        "exp": expire,
        "sub": str(userid),
        "username": username,
        "super_user": super_user,
        "level": level
    }

    encoded_jwt = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


def __verify_token(token: str) -> schemas.TokenPayload:
    """
       使用 JWT Token 进行身份认证并解析 Token 的内容
       :param token: JWT 令牌，从请求的 Authorization 头部获取
       :return: 包含用户身份信息的 Token 负载数据
       :raises HTTPException: 如果令牌无效或解码失败，抛出 403 错误
       """
    try:
        payload = jwt.decode(
            token, settings.SECRET_KEY, algorithms=[ALGORITHM]
        )
        return schemas.TokenPayload(**payload)
    except (jwt.DecodeError, jwt.InvalidTokenError, jwt.ImmatureSignatureError):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="token校验不通过",
        )


def verify_token(token: str = Security(oauth2_scheme)) -> schemas.TokenPayload:
    """
    使用 JWT Token 进行身份认证并解析 Token 的内容
    :param token: JWT 令牌，从请求的 Authorization 头部获取
    :return: 包含用户身份信息的 Token 负载数据
    :raises HTTPException: 如果令牌无效或解码失败，抛出 403 错误
    """
    return __verify_token(token)


def verify_uri_token(token: str = Security(jwt_token_query)) -> schemas.TokenPayload:
    """
    使用 JWT Token 进行身份认证并解析 Token 的内容
    :param token: JWT 令牌，从请求的 Authorization 头部获取
    :return: 包含用户身份信息的 Token 负载数据
    :raises HTTPException: 如果令牌无效或解码失败，抛出 403 错误
    """
    return __verify_token(token)


def __get_api_token(
        token_query: Annotated[str | None, Security(api_token_query)] = None
) -> str:
    """
    从 URL 查询参数中获取 API Token
    :param token_query: 从 URL 中的 `token` 查询参数获取 API Token
    :return: 返回获取到的 API Token，若无则返回 None
    """
    return token_query


def __get_api_key(
        key_query: Annotated[str | None, Security(api_key_query)] = None,
        key_header: Annotated[str | None, Security(api_key_header)] = None
) -> str:
    """
    从 URL 查询参数或请求头部获取 API Key，优先使用 URL 参数
    :param key_query: URL 中的 `apikey` 查询参数
    :param key_header: 请求头中的 `X-API-KEY` 参数
    :return: 返回从 URL 或请求头中获取的 API Key，若无则返回 None
    """
    return key_query or key_header


def __verify_key(key: str, expected_key: str, key_type: str) -> str:
    """
    通用的 API Key 或 Token 验证函数
    :param key: 从请求中获取的 API Key 或 Token
    :param expected_key: 系统配置中的期望值，用于验证的 API Key 或 Token
    :param key_type: 键的类型（例如 "API_KEY" 或 "API_TOKEN"），用于错误消息
    :return: 返回校验通过的 API Key 或 Token
    :raises HTTPException: 如果校验不通过，抛出 401 错误
    """
    if key != expected_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"{key_type} 校验不通过"
        )
    return key


def verify_apitoken(token: str = Security(__get_api_token)) -> str:
    """
    使用 API Token 进行身份认证
    :param token: API Token，从 URL 查询参数中获取
    :return: 返回校验通过的 API Token
    """
    return __verify_key(token, settings.API_TOKEN, "API_TOKEN")


def verify_apikey(apikey: str = Security(__get_api_key)) -> str:
    """
    使用 API Key 进行身份认证
    :param apikey: API Key，从 URL 查询参数或请求头中获取
    :return: 返回校验通过的 API Key
    """
    return __verify_key(apikey, settings.API_TOKEN, "API_KEY")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


def decrypt(data: bytes, key: bytes) -> Optional[bytes]:
    """
    解密二进制数据
    """
    fernet = Fernet(key)
    try:
        return fernet.decrypt(data)
    except Exception as e:
        logger.error(f"解密失败：{str(e)} - {traceback.format_exc()}")
        return None


def encrypt_message(message: str, key: bytes):
    """
    使用给定的key对消息进行加密，并返回加密后的字符串
    """
    f = Fernet(key)
    encrypted_message = f.encrypt(message.encode())
    return encrypted_message.decode()


def hash_sha256(message):
    """
    对字符串做hash运算
    """
    return hashlib.sha256(message.encode()).hexdigest()


def aes_decrypt(data, key):
    """
    AES解密
    """
    if not data:
        return ""
    data = base64.b64decode(data)
    iv = data[:16]
    encrypted = data[16:]
    # 使用AES-256-CBC解密
    cipher = AES.new(key.encode('utf-8'), AES.MODE_CBC, iv)
    result = cipher.decrypt(encrypted)
    # 去除填充
    padding = result[-1]
    if padding < 1 or padding > AES.block_size:
        return ""
    result = result[:-padding]
    return result.decode('utf-8')


def aes_encrypt(data, key):
    """
    AES加密
    """
    if not data:
        return ""
    # 使用AES-256-CBC加密
    cipher = AES.new(key.encode('utf-8'), AES.MODE_CBC)
    # 填充
    padding = AES.block_size - len(data) % AES.block_size
    data += chr(padding) * padding
    result = cipher.encrypt(data.encode('utf-8'))
    # 使用base64编码
    return base64.b64encode(cipher.iv + result).decode('utf-8')


def nexusphp_encrypt(data_str: str, key):
    """
    NexusPHP加密
    """
    # 生成16字节长的随机字符串
    iv = os.urandom(16)
    # 对向量进行 Base64 编码
    iv_base64 = base64.b64encode(iv)
    # 加密数据
    cipher = AES.new(key, AES.MODE_CBC, iv)
    ciphertext = cipher.encrypt(pad(data_str.encode(), AES.block_size))
    ciphertext_base64 = base64.b64encode(ciphertext)
    # 对向量的字符串表示进行签名
    mac = hmac.new(key, msg=iv_base64 + ciphertext_base64, digestmod=hashlib.sha256).hexdigest()
    # 构造 JSON 字符串
    json_str = json.dumps({
        'iv': iv_base64.decode(),
        'value': ciphertext_base64.decode(),
        'mac': mac,
        'tag': ''
    })

    # 对 JSON 字符串进行 Base64 编码
    return base64.b64encode(json_str.encode()).decode()
