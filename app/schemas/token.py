
from pydantic import BaseModel


class Token(BaseModel):
    access_token: str
    token_type: str
    super_user: bool
    user_name: str
    avatar: str | None = None


class TokenPayload(BaseModel):
    # 用户ID
    sub: int | None = None
    # 用户名
    username: str | None = None
    # 超级用户
    super_user: bool | None = None
