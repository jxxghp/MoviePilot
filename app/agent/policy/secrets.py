"""Agent 设置工具与宿主回执共用的敏感字段身份判定。"""

import re
from typing import Any


_MAX_FIELD_NAME_CHARS = 256
_ACRONYM_BOUNDARY_PATTERN = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])")
_CAMEL_CASE_BOUNDARY_PATTERN = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SECRET_FIELD_NAMES = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "api_token",
        "auth_header",
        "authorization",
        "client_secret",
        "cookie",
        "passkey",
        "passwd",
        "password",
        "private_key",
        "pwd",
        "refresh_token",
        "secret",
        "secret_access_key",
        "secret_key",
        "token",
    }
)
_SECRET_FIELD_ENDINGS = tuple(f"_{name}" for name in _SECRET_FIELD_NAMES)
_SECRET_SETTING_NAMES = frozenset(
    {
        # CookieCloud 的用户 key 没有类型后缀，但与密码共同构成端到端加密凭据。
        "cookiecloud_key",
    }
)
_SECRET_SETTING_ENDINGS = (
    "_encrypt_key",
)


def _normalize_field_name(value: Any) -> str:
    """将短字段名规范化为 snake_case，非字符串不参与身份推导。"""
    if type(value) is not str:
        return ""
    text = value.strip()
    if len(text) > _MAX_FIELD_NAME_CHARS:
        text = text[-_MAX_FIELD_NAME_CHARS:]
    text = _ACRONYM_BOUNDARY_PATTERN.sub("_", text)
    text = _CAMEL_CASE_BOUNDARY_PATTERN.sub("_", text)
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def is_secret_setting_key(key: Any) -> bool:
    """按完整字段或类型后缀识别凭据，避免误伤 token 统计与过期配置。"""
    normalized = _normalize_field_name(key)
    if not normalized:
        return False
    return (
        normalized in _SECRET_FIELD_NAMES
        or normalized in _SECRET_SETTING_NAMES
        or normalized.endswith(_SECRET_FIELD_ENDINGS)
        or normalized.endswith(_SECRET_SETTING_ENDINGS)
    )


__all__ = ["is_secret_setting_key"]
