from typing import Optional

from pydantic import BaseModel, Field, ConfigDict

from app.schemas.common import JsonData


# Shared properties
class UserBase(BaseModel):
    """用户公共资料、权限和个性化设置。"""

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
    permissions: Optional[dict[str, bool]] = Field(default_factory=dict)
    # 个性化设置
    settings: Optional[dict[str, JsonData]] = Field(default_factory=dict)

    model_config = ConfigDict(from_attributes=True)


# Properties to receive via API on creation
class UserCreate(UserBase):
    """创建用户时接收的资料和初始凭据。"""

    name: str
    email: Optional[str] = None
    password: Optional[str] = None
    settings: Optional[dict[str, JsonData]] = Field(default_factory=dict)
    permissions: Optional[dict[str, bool]] = Field(default_factory=dict)


# Properties to receive via API on update
class UserUpdate(UserBase):
    """更新用户时接收的完整资料。"""

    id: int
    name: str
    email: Optional[str] = None
    password: Optional[str] = None
    settings: Optional[dict[str, JsonData]] = Field(default_factory=dict)
    permissions: Optional[dict[str, bool]] = Field(default_factory=dict)


class UserInDBBase(UserBase):
    """包含数据库主键的用户公共记录。"""

    id: Optional[int] = None

    model_config = ConfigDict(from_attributes=True)


# Additional properties to return via API
class User(UserInDBBase):
    """对 API 调用方公开的用户资料。"""

    name: str
    email: Optional[str] = None


# Additional properties stored in DB
class UserInDB(UserInDBBase):
    """包含密码哈希的内部用户记录。"""

    hashed_password: str


class AuthProviderRemote(BaseModel):
    """插件认证提供方的远程组件信息。"""

    id: str
    url: str
    name: str


class AuthProviderInfo(BaseModel):
    """匿名登录页可展示的认证提供方摘要。"""

    id: str
    type: str
    name: str
    enabled: bool = True
    method: Optional[str] = None
    icon: Optional[str] = None
    component: Optional[str] = None
    plugin_id: Optional[str] = None
    remote: Optional[AuthProviderRemote] = None
