"""
与传输框架无关的令牌、密码和对称加密能力。
"""

import base64
import datetime
import hashlib
import importlib
import os
import secrets
import threading
import traceback
from collections.abc import Callable, Sequence
from datetime import timedelta
from pathlib import Path
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
    """
    读取启动快照；未装配时保留旧插件的独立调用兼容。
    """
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
    """
    密码的 UTF-8 字节长度超过 bcrypt 可安全处理的上限。
    """


class TokenValidationError(ValueError):
    """
    令牌缺失、签名无效或用途不符合调用方要求。
    """


def _encode_bcrypt_password(
    password: str,
    *,
    allow_legacy_truncation: bool = False,
) -> bytes:
    """
    编码 bcrypt 密码；仅验证既有哈希时允许按历史语义截断。
    """
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
    agent_channel: Optional[str] = None,
    agent_channel_user: Optional[str] = None,
    agent_authorization_id: Optional[str] = None,
    agent_operation: Optional[str] = None,
    agent_method: Optional[str] = None,
    agent_path: Optional[str] = None,
) -> str:
    """
    创建带身份、权限等级和用途声明的 JWT 访问令牌。

    宿主 Agent 代渠道用户调用内部 API 时，可把发起会话的渠道及渠道用户标识写入签名声明；
    两项需同时提供且令牌用途为认证时才会写入，资源令牌不携带这组声明。

    :param userid: 令牌主体的 MoviePilot 用户 ID
    :param username: 令牌主体的用户名
    :param super_user: 是否为超级用户
    :param expires_delta: 自定义有效期；None 时按用途使用默认有效期
    :param level: 权限级别
    :param purpose: 令牌用途，authentication 或 resource
    :param agent_channel: 宿主 Agent 会话所属的消息渠道
    :param agent_channel_user: 发起该会话的用户在渠道内的原始标识
    :param agent_authorization_id: 统一渠道授权的不可变身份，存在时必须提供固定操作与路径
    :param agent_operation: 已通过授权检查的受控操作
    :param agent_method: 当前 API 请求的固定方法
    :param agent_path: 当前 API 请求的固定路径
    :return: 已签名的 JWT 字符串
    :raises ValueError: 自定义有效期不是正数
    """
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
    if purpose == "authentication" and agent_channel and agent_channel_user:
        payload["agent_channel"] = agent_channel
        payload["agent_channel_user"] = agent_channel_user
    if agent_authorization_id:
        if purpose != "authentication" or not all((agent_operation, agent_method, agent_path)):
            raise ValueError("渠道凭据必须限定到一次受控 API 操作")
        payload.update(agent_authorization_id=agent_authorization_id, agent_operation=agent_operation,
                       agent_method=agent_method, agent_path=agent_path)
    encoded: str = jwt.encode(payload, secret_key, algorithm=ALGORITHM)
    return encoded


def decode_access_token(
    token: str | None,
    purpose: str = "authentication",
) -> TokenPayload:
    """
    校验 JWT 签名和用途并返回框架无关的令牌载荷。
    """
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
    """
    验证既有 bcrypt 哈希，并保留超长历史密码的截断语义。
    """
    try:
        verified: bool = bcrypt.checkpw(
            _encode_bcrypt_password(
                plain_password,
                allow_legacy_truncation=True,
            ),
            hashed_password.encode("ascii"),
        )
        return verified
    except (UnicodeEncodeError, ValueError):
        return False


def get_password_hash(password: str) -> str:
    """
    使用 ``$2b$`` 前缀和 cost 12 生成可持久化的 bcrypt 哈希。
    """
    hashed: bytes = bcrypt.hashpw(
        _encode_bcrypt_password(password),
        bcrypt.gensalt(rounds=BCRYPT_ROUNDS, prefix=b"2b"),
    )
    return hashed.decode("ascii")


def decrypt(data: bytes, key: bytes) -> Optional[bytes]:
    """
    使用 Fernet 解密二进制数据，失败时记录诊断并返回空值。
    """
    try:
        decrypted: bytes = Fernet(key).decrypt(data)
        return decrypted
    except Exception as error:
        logger.error(f"解密失败：{str(error)} - {traceback.format_exc()}")
        return None


_CREDENTIAL_KEY_FILENAME = ".credential_key"
_credential_secret_lock = threading.Lock()
_credential_secret_cache: dict[Path, str] = {}


def _read_existing_secret_file(key_file: Path) -> Optional[str]:
    """
    读取已存在的持久化凭据密钥文件，长度不足时忽略。
    """
    try:
        if not key_file.is_file():
            return None
        existing = key_file.read_text(encoding="utf-8").strip()
        return existing if len(existing) >= 16 else None
    except OSError:
        return None


def _ensure_persistent_secret_file(config_dir: Path) -> Optional[str]:
    """
    在配置目录下读取或原子创建实例级持久化凭据主密钥文件。
    """
    resolved_dir = config_dir.resolve()
    cached = _credential_secret_cache.get(resolved_dir)
    if cached:
        return cached
    with _credential_secret_lock:
        cached = _credential_secret_cache.get(resolved_dir)
        if cached:
            return cached
        key_file = resolved_dir / _CREDENTIAL_KEY_FILENAME
        existing = _read_existing_secret_file(key_file)
        if existing:
            _credential_secret_cache[resolved_dir] = existing
            return existing
        generated = secrets.token_urlsafe(32)
        try:
            resolved_dir.mkdir(parents=True, exist_ok=True)
            fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(generated)
            _credential_secret_cache[resolved_dir] = generated
            return generated
        except FileExistsError:
            raced = _read_existing_secret_file(key_file)
            if raced:
                _credential_secret_cache[resolved_dir] = raced
            return raced
        except OSError:
            return None


def _safe_read_setting(setting_getter: Callable[[str], Any], key: str) -> Any:
    """
    调用单参数设置读取器，缺失属性或键时返回 None。
    """
    try:
        return setting_getter(key)
    except (AttributeError, KeyError, TypeError):
        return None


def resolve_persistent_credential_secret(setting_getter: Callable[[str], Any]) -> str:
    """
    解析跨重启持久化的实例凭据加密种子；无配置目录替身时回退运行时密钥。
    """
    config_path = _safe_read_setting(setting_getter, "CONFIG_PATH")
    if isinstance(config_path, Path):
        persisted = _ensure_persistent_secret_file(config_path)
        if persisted:
            return persisted
    fallback = (
        _safe_read_setting(setting_getter, "SECRET_KEY")
        or _safe_read_setting(setting_getter, "RESOURCE_SECRET_KEY")
        or "moviepilot"
    )
    return str(fallback)


def derive_domain_fernet_keys(
    domain: str,
    setting_getter: Callable[[str], Any],
) -> list[bytes]:
    """
    按业务域派生首选持久化 Fernet 密钥及历史会话兼容回退密钥列表。
    """
    candidates = [
        resolve_persistent_credential_secret(setting_getter),
        str(_safe_read_setting(setting_getter, "SECRET_KEY") or ""),
        str(_safe_read_setting(setting_getter, "RESOURCE_SECRET_KEY") or ""),
        "moviepilot",
    ]
    keys: list[bytes] = []
    seen: set[str] = set()
    for secret in candidates:
        if not secret or secret in seen:
            continue
        seen.add(secret)
        digest = hashlib.sha256(f"{domain}:{secret}".encode("utf-8")).digest()
        keys.append(base64.urlsafe_b64encode(digest))
    return keys


def decrypt_with_keys(data: bytes, keys: Sequence[bytes]) -> Optional[bytes]:
    """
    依次尝试候选 Fernet 密钥解密密文，避免回退探测期间输出误报错误日志。
    """
    for key in keys:
        try:
            decrypted: bytes = Fernet(key).decrypt(data)
            return decrypted
        except Exception:
            continue
    return None


def encrypt_message(message: str, key: bytes) -> str:
    """
    使用 Fernet 加密文本并返回可传输字符串。
    """
    encrypted: str = Fernet(key).encrypt(message.encode()).decode()
    return encrypted


def hash_sha256(message: str) -> str:
    """
    返回文本的 SHA-256 十六进制摘要。
    """
    return hashlib.sha256(message.encode()).hexdigest()


def aes_decrypt(data: str, key: str) -> str:
    """
    按历史 AES-256-CBC 合同解密 Base64 文本。
    """
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
    """
    按历史 AES-256-CBC 合同加密文本并返回 Base64 字符串。
    """
    if not data:
        return ""
    cipher = AES.new(key.encode("utf-8"), AES.MODE_CBC)
    # CBC 按字节分块；先编码再填充，保留 ASCII 密文并支持多字节文本。
    encoded = data.encode("utf-8")
    result = cipher.encrypt(pad(encoded, AES.block_size))
    return base64.b64encode(bytes(cipher.iv) + result).decode("utf-8")
