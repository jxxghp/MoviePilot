"""
访问令牌、认证结果及令牌载荷的数据结构。
"""

from typing import Optional

from pydantic import BaseModel, Field

from app.schemas.user import UserPermissions


class MfaChallenge(BaseModel):
    """
    密码认证通过后需要继续完成的二次验证信息。
    """

    mfa_methods: list[str] = Field(default_factory=list, description="可用的二次验证方式")


class Token(BaseModel):
    """
    OAuth2 登录成功后返回的访问令牌。
    """

    access_token: str = Field(description="令牌")
    token_type: str = Field(description="令牌类型")
    super_user: bool = Field(description="超级用户")
    user_id: int = Field(description="用户ID")
    user_name: str = Field(description="用户名")
    avatar: Optional[str] = Field(default=None, description="头像")
    level: int = Field(default=1, description="权限级别")
    permissions: Optional[UserPermissions] = Field(default_factory=UserPermissions, description="详细权限")


class TokenPayload(BaseModel):
    """
    访问令牌中携带的用户身份与授权信息。
    """

    sub: Optional[int] = Field(default=None, description="用户ID")
    username: Optional[str] = Field(default=None, description="用户名")
    super_user: Optional[bool] = Field(default=None, description="超级用户")
    level: Optional[int] = Field(default=None, description="权限级别")
    purpose: Optional[str] = Field(default=None, description="令牌用途 authentication\\resource")
