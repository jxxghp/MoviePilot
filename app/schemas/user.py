from typing import Optional

from pydantic import BaseModel, Field


# Shared properties
class UserBase(BaseModel):
    # 用户名
    name: str
    # 邮箱，未启用
    email: Optional[str] = None
    # 状态
    is_active: Optional[bool] = True
    # 超级管理员
    is_superuser: bool = False
    # 头像
    avatar: Optional[str] = None
    # 是否开启二次验证
    is_otp: Optional[bool] = False
    # 权限
    permissions: Optional[dict] = Field(default_factory=dict)
    # 个性化设置
    settings: Optional[dict] = Field(default_factory=dict)

    class Config:
        orm_mode = True


# Properties to receive via API on creation
class UserCreate(UserBase):
    name: str
    email: Optional[str] = None
    password: Optional[str] = None
    settings: Optional[dict] = Field(default_factory=dict)
    permissions: Optional[dict] = Field(default_factory=dict)


# Properties to receive via API on update
class UserUpdate(UserBase):
    id: int
    name: str
    email: Optional[str] = None
    password: Optional[str] = None
    settings: Optional[dict] = Field(default_factory=dict)
    permissions: Optional[dict] = Field(default_factory=dict)


class UserInDBBase(UserBase):
    id: Optional[int] = None

    class Config:
        orm_mode = True


# Additional properties to return via API
class User(UserInDBBase):
    name: str
    email: Optional[str] = None


# Additional properties stored in DB
class UserInDB(UserInDBBase):
    hashed_password: str
