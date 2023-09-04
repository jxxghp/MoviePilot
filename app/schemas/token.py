from typing import Optional

from pydantic import BaseModel


class Token(BaseModel):
    access_token: str
    token_type: str
    super_user: bool
    user_name: str
    avatar: Optional[str] = None


class TokenPayload(BaseModel):
    # 用户ID
    sub: Optional[int] = None
