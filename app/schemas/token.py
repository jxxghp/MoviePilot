from typing import Optional

from pydantic import BaseModel, Field


class Token(BaseModel):
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
    permissions: Optional[dict] = Field(default_factory=dict)


class TokenPayload(BaseModel):
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
