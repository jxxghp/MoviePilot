import base64
import hashlib
import hmac
import json
import os
import traceback
from datetime import datetime, timedelta
from typing import Any, Union, Optional, Annotated
import jwt
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from fastapi import HTTPException, status, Depends, Header
from fastapi.security import OAuth2PasswordBearer
from passlib.context import CryptContext

from app import schemas
from app.core.config import settings
from cryptography.fernet import Fernet

from app.log import logger

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
ALGORITHM = "HS256"

# Token认证
reusable_oauth2 = OAuth2PasswordBearer(
    tokenUrl=f"{settings.API_V1_STR}/login/access-token"
)


def create_access_token(
        userid: Union[str, Any], username: str, super_user: bool = False,
        expires_delta: timedelta = None, level: int = 1
) -> str:
    if expires_delta:
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


def verify_token(token: str = Depends(reusable_oauth2)) -> schemas.TokenPayload:
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


def __get_token(token: str = None) -> str:
    """
    从请求URL中获取token
    """
    return token


def __get_apikey(apikey: str = None, x_api_key: Annotated[str | None, Header()] = None) -> str:
    """
    从请求URL中获取apikey
    """
    return apikey or x_api_key


def verify_apitoken(token: str = Depends(__get_token)) -> str:
    """
    通过依赖项使用token进行身份认证
    """
    if token != settings.API_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="token校验不通过"
        )
    return token


def verify_apikey(apikey: str = Depends(__get_apikey)) -> str:
    """
    通过依赖项使用apikey进行身份认证
    """
    if apikey != settings.API_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="apikey校验不通过"
        )
    return apikey


def verify_uri_token(token: str = Depends(__get_token)) -> str:
    """
    通过依赖项使用token进行身份认证
    """
    if not verify_token(token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="token校验不通过"
        )
    return token


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
