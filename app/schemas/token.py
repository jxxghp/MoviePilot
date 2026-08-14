from typing import Optional

from pydantic import BaseModel, Field

from app.schemas.user import UserPermissions


class MfaChallenge(BaseModel):
    """密码认证通过后需要继续完成的二次验证信息。"""

    # 可用的二次验证方式
    mfa_methods: list[str] = Field(default_factory=list)


class Token(BaseModel):
    """OAuth2 登录成功后返回的访问令牌。"""

    # 令牌
    access_token: str
    # 令牌类型
    token_type: str
    # 超级用户
    super_user: bool
    # 用户ID
    user_id: int
    # 用户名
    user_name: str
    # 头像
    avatar: Optional[str] = None
    # 权限级别
    level: int = 1
    # 详细权限
    permissions: Optional[UserPermissions] = Field(default_factory=dict)
    # 是否显示配置向导
    wizard: Optional[bool] = None


class TokenPayload(BaseModel):
    """访问令牌中携带的用户身份与授权信息。"""

    # 用户ID
    sub: Optional[int] = None
    # 用户名
    username: Optional[str] = None
    # 超级用户
    super_user: Optional[bool] = None
    # 权限级别
    level: Optional[int] = None
    # 令牌用途 authentication\resource
    purpose: Optional[str] = None
