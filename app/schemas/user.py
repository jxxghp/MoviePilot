
from pydantic import BaseModel


# Shared properties
class UserBase(BaseModel):
    # 用户名
    name: str
    # 邮箱，未启用
    email: str | None = None
    # 状态
    is_active: bool | None = True
    # 超级管理员
    is_superuser: bool = False
    # 头像
    avatar: str | None = None
    # 是否开启二次验证
    is_otp: bool | None = False


# Properties to receive via API on creation
class UserCreate(UserBase):
    name: str
    email: str | None = None
    password: str | None = None


# Properties to receive via API on update
class UserUpdate(UserBase):
    name: str
    password: str | None = None


class UserInDBBase(UserBase):
    id: int | None = None

    class Config:
        orm_mode = True


# Additional properties to return via API
class User(UserInDBBase):
    name: str
    email: str | None = None


# Additional properties stored in DB
class UserInDB(UserInDBBase):
    hashed_password: str
