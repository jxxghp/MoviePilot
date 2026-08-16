import asyncio
from types import SimpleNamespace

import bcrypt
import pytest

from app.api.endpoints import user as user_endpoint
from app.application.security.access import (
    PasswordTooLongError,
    get_password_hash,
    verify_password,
)


PASSLIB_BCRYPT_HASH = "$2b$12$6QiVIML7x3T.F/p6cuFjLuMvFumE1V4OZpvhGVgCwaSoBE7lHlMle"


def test_password_hash_uses_existing_bcrypt_contract():
    """新密码保持 $2b$、cost 12，并可由同一包装正确验证。"""
    hashed_password = get_password_hash("new-password")

    assert hashed_password.startswith("$2b$12$")
    assert verify_password("new-password", hashed_password) is True
    assert verify_password("wrong-password", hashed_password) is False


def test_verify_password_accepts_existing_passlib_bcrypt_hash():
    """不依赖 Passlib 时仍能验证既有 bcrypt 密码哈希。"""
    assert verify_password("existing-passlib-password", PASSLIB_BCRYPT_HASH) is True


def test_get_password_hash_rejects_more_than_72_utf8_bytes():
    """bcrypt 不得静默截断 UTF-8 编码后超过 72 字节的新密码。"""
    with pytest.raises(PasswordTooLongError, match="72 字节"):
        get_password_hash("a" * 73)

    with pytest.raises(PasswordTooLongError, match="72 字节"):
        get_password_hash("密" * 25)


def test_get_password_hash_accepts_exactly_72_utf8_bytes():
    """UTF-8 编码后恰好 72 字节的密码仍属于有效输入。"""
    password = "密" * 24

    assert verify_password(password, get_password_hash(password)) is True


def test_verify_password_preserves_legacy_long_password_access():
    """既有超长密码即使在多字节字符中间截断也应保持可登录。"""
    password = "a" * 70 + "密"
    hashed_password = bcrypt.hashpw(
        password.encode("utf-8")[:72], bcrypt.gensalt(rounds=4)
    ).decode("ascii")

    assert verify_password(password, hashed_password) is True


def test_verify_password_rejects_malformed_hash():
    """损坏的数据库哈希应按认证失败处理。"""
    assert verify_password("password", "not-a-bcrypt-hash") is False


class _CreateUserInput:
    """提供新增用户接口所需的最小输入契约。"""

    name = "new-user"

    @staticmethod
    def model_dump():
        """返回包含超长多字节密码的用户数据。"""
        return {
            "name": "new-user",
            "email": None,
            "password": "Ab1!" + "密" * 23,
            "is_active": True,
            "is_superuser": False,
            "avatar": None,
            "is_otp": False,
            "permissions": {},
            "settings": {},
        }


class _CurrentUser:
    """提供用户接口长度校验前需要的最小查询契约。"""

    @staticmethod
    async def async_get_by_name(_db, name):
        """模拟用户名尚未被使用。"""
        assert name == "new-user"
        return None


def test_create_user_returns_business_error_for_password_over_72_bytes():
    """新增用户遇到超长密码时应返回可读业务错误。"""
    response = asyncio.run(
        user_endpoint.create_user(
            db=SimpleNamespace(),
            user_in=_CreateUserInput(),
            current_user=_CurrentUser(),
        )
    )

    assert response.success is False
    assert response.message == "密码 UTF-8 编码后不能超过 72 字节"


def test_update_user_returns_business_error_for_password_over_72_bytes():
    """修改用户遇到超长密码时应返回可读业务错误。"""
    user_in = SimpleNamespace(
        model_dump=lambda: {
            "id": 1,
            "name": "user",
            "password": "Ab1!" + "密" * 23,
        }
    )

    response = asyncio.run(
        user_endpoint.update_user(
            db=SimpleNamespace(),
            user_in=user_in,
            current_user=SimpleNamespace(),
        )
    )

    assert response.success is False
    assert response.message == "密码 UTF-8 编码后不能超过 72 字节"
