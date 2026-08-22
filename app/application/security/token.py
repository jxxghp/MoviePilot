"""与传输框架无关的令牌、密码和对称加密能力。"""

import base64
import datetime
import hashlib
import hmac
import importlib
import json
import os
import traceback
from datetime import timedelta
from typing import Any, Optional, Union

import bcrypt
import jwt
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from cryptography.fernet import Fernet

from app.application.configuration import TokenRuntimeConfig, get_token_runtime_config
from app.runtime.log import logger
from app.schemas.token import TokenPayload

BCRYPT_PASSWORD_MAX_BYTES = 72
BCRYPT_ROUNDS = 12
ALGORITHM = "HS256"


def _token_config() -> TokenRuntimeConfig:
    """读取启动快照；未装配时保留旧插件的独立调用兼容。"""
    try:
        return get_token_runtime_config()
    except RuntimeError:
        legacy_settings = importlib.import_module("app.runtime.config").settings
        return TokenRuntimeConfig(
            secret_key=legacy_settings.SECRET_KEY,
            resource_secret_key=legacy_settings.RESOURCE_SECRET_KEY,
            access_token_expire_minutes=legacy_settings.ACCESS_TOKEN_EXPIRE_MINUTES,
            resource_access_token_expire_seconds=(
                legacy_settings.RESOURCE_ACCESS_TOKEN_EXPIRE_SECONDS
            ),
        )


class PasswordTooLongError(ValueError):
    """密码的 UTF-8 字节长度超过 bcrypt 可安全处理的上限。"""


class TokenValidationError(ValueError):
    """令牌缺失、签名无效或用途不符合调用方要求。"""


def _encode_bcrypt_password(
    password: str,
    *,
    allow_legacy_truncation: bool = False,
) -> bytes:
    """编码 bcrypt 密码；仅验证既有哈希时允许按历史语义截断。"""
    password_bytes = password.encode("utf-8")
    if len(password_bytes) > BCRYPT_PASSWORD_MAX_BYTES:
        if allow_legacy_truncation:
            return password_bytes[:BCRYPT_PASSWORD_MAX_BYTES]
        raise PasswordTooLongError(
            f"密码 UTF-8 编码后不能超过 {BCRYPT_PASSWORD_MAX_BYTES} 字节"
        )
    return password_bytes


def create_access_token(
    userid: Union[str, Any],
    username: str,
    super_user: Optional[bool] = False,
    expires_delta: Optional[timedelta] = None,
    level: Optional[int] = 1,
    purpose: Optional[str] = "authentication",
) -> str:
    """创建带身份、权限等级和用途声明的 JWT 访问令牌。"""
    config = _token_config()
    if purpose == "resource":
        default_expire = timedelta(
            seconds=config.resource_access_token_expire_seconds
        )
        secret_key = config.resource_secret_key
    else:
        default_expire = timedelta(minutes=config.access_token_expire_minutes)
        secret_key = config.secret_key

    if expires_delta is not None:
        if expires_delta.total_seconds() <= 0:
            raise ValueError("过期时间必须为正数")
        expire = datetime.datetime.now(datetime.UTC) + expires_delta
    else:
        expire = datetime.datetime.now(datetime.UTC) + default_expire

    now = datetime.datetime.now(datetime.UTC)
    payload = {
        "exp": expire,
        "iat": now,
        "sub": str(userid),
        "username": username,
        "super_user": super_user,
        "level": level,
        "purpose": purpose,
    }
    return jwt.encode(payload, secret_key, algorithm=ALGORITHM)


def decode_access_token(
    token: str | None,
    purpose: str = "authentication",
) -> TokenPayload:
    """校验 JWT 签名和用途并返回框架无关的令牌载荷。"""
    config = _token_config()
    if not token:
        raise TokenValidationError(f"{purpose} token not found")
    secret_key = (
        config.resource_secret_key
        if purpose == "resource"
        else config.secret_key
    )
    try:
        payload = jwt.decode(token, secret_key, algorithms=[ALGORITHM])
        token_payload = TokenPayload(**payload)
        if token_payload.purpose != purpose:
            raise jwt.InvalidTokenError("令牌用途不匹配")
        return token_payload
    except (
        jwt.DecodeError,
        jwt.InvalidTokenError,
        jwt.ImmatureSignatureError,
    ) as error:
        raise TokenValidationError("token校验不通过") from error


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """验证既有 bcrypt 哈希，并保留超长历史密码的截断语义。"""
    try:
        return bcrypt.checkpw(
            _encode_bcrypt_password(
                plain_password,
                allow_legacy_truncation=True,
            ),
            hashed_password.encode("ascii"),
        )
    except (UnicodeEncodeError, ValueError):
        return False


def get_password_hash(password: str) -> str:
    """使用 ``$2b$`` 前缀和 cost 12 生成可持久化的 bcrypt 哈希。"""
    return bcrypt.hashpw(
        _encode_bcrypt_password(password),
        bcrypt.gensalt(rounds=BCRYPT_ROUNDS, prefix=b"2b"),
    ).decode("ascii")


def decrypt(data: bytes, key: bytes) -> Optional[bytes]:
    """使用 Fernet 解密二进制数据，失败时记录诊断并返回空值。"""
    try:
        return Fernet(key).decrypt(data)
    except Exception as error:
        logger.error(f"解密失败：{str(error)} - {traceback.format_exc()}")
        return None


def encrypt_message(message: str, key: bytes) -> str:
    """使用 Fernet 加密文本并返回可传输字符串。"""
    return Fernet(key).encrypt(message.encode()).decode()


def hash_sha256(message: str) -> str:
    """返回文本的 SHA-256 十六进制摘要。"""
    return hashlib.sha256(message.encode()).hexdigest()


def aes_decrypt(data: str, key: str) -> str:
    """按历史 AES-256-CBC 合同解密 Base64 文本。"""
    if not data:
        return ""
    raw_data = base64.b64decode(data)
    iv = raw_data[:16]
    encrypted = raw_data[16:]
    cipher = AES.new(key.encode("utf-8"), AES.MODE_CBC, iv)
    result = cipher.decrypt(encrypted)
    padding = result[-1]
    if padding < 1 or padding > AES.block_size:
        return ""
    return result[:-padding].decode("utf-8")


def aes_encrypt(data: str, key: str) -> str:
    """按历史 AES-256-CBC 合同加密文本并返回 Base64 字符串。"""
    if not data:
        return ""
    cipher = AES.new(key.encode("utf-8"), AES.MODE_CBC)
    padding = AES.block_size - len(data) % AES.block_size
    padded = data + chr(padding) * padding
    result = cipher.encrypt(padded.encode("utf-8"))
    return base64.b64encode(cipher.iv + result).decode("utf-8")


def nexusphp_encrypt(data_str: str, key: bytes) -> str:
    """生成 NexusPHP 兼容的 AES-CBC 加密载荷。"""
    iv = os.urandom(16)
    iv_base64 = base64.b64encode(iv)
    cipher = AES.new(key, AES.MODE_CBC, iv)
    ciphertext = cipher.encrypt(pad(data_str.encode(), AES.block_size))
    ciphertext_base64 = base64.b64encode(ciphertext)
    mac = hmac.new(
        key,
        msg=iv_base64 + ciphertext_base64,
        digestmod=hashlib.sha256,
    ).hexdigest()
    payload = json.dumps({
        "iv": iv_base64.decode(),
        "value": ciphertext_base64.decode(),
        "mac": mac,
        "tag": "",
    })
    return base64.b64encode(payload.encode()).decode()
