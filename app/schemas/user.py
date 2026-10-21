"""
用户账号、权限和第三方认证接口的数据结构。
"""

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field
from typing_extensions import TypedDict

from app.schemas.common import JsonData


class UserPermissions(TypedDict, total=False):
    """
    用户分类权限及可动态扩展的功能级权限。

    所有键均可省略，由授权策略按缺失权限处理。

    :ivar discovery: 是否允许访问发现功能
    :ivar search: 是否允许搜索资源
    :ivar subscribe: 是否允许管理订阅
    :ivar manage: 是否允许系统管理
    :ivar admin: 管理员入口标识，实际授权仍取决于超级用户身份
    :ivar features: 功能键到启用状态的映射
    """

    discovery: bool
    search: bool
    subscribe: bool
    manage: bool
    admin: bool
    features: dict[str, bool]


# 用户共有属性
class UserBase(BaseModel):
    """
    用户公共资料、权限和个性化设置。
    """

    name: str = Field(description="用户名")
    email: Optional[str] = Field(default=None, description="邮箱，未启用")
    is_active: Optional[bool] = Field(default=True, description="状态")
    is_superuser: bool = Field(default=False, description="超级管理员")
    avatar: Optional[str] = Field(default=None, description="头像")
    is_otp: Optional[bool] = Field(default=False, description="是否开启二次验证")
    permissions: Optional[UserPermissions] = Field(default_factory=UserPermissions, description="权限")
    settings: Optional[dict[str, JsonData]] = Field(default_factory=dict, description="个性化设置")

    model_config = ConfigDict(from_attributes=True)


# 创建用户时接收的属性
class UserCreate(UserBase):
    """
    创建用户时接收的资料和初始凭据。
    """

    name: str = Field(description="新用户的登录名")
    email: Optional[str] = Field(default=None, description="邮箱，未启用")
    password: Optional[str] = Field(default=None, description="新用户的初始密码")
    settings: Optional[dict[str, JsonData]] = Field(default_factory=dict, description="个性化设置")
    permissions: Optional[UserPermissions] = Field(default_factory=UserPermissions, description="权限")


# 更新用户时接收的属性
class UserUpdate(UserBase):
    """
    更新用户时接收的完整资料。
    """

    id: int = Field(description="记录标识")
    name: str = Field(description="修改后的用户登录名")
    email: Optional[str] = Field(default=None, description="邮箱，未启用")
    password: Optional[str] = Field(default=None, description="新密码，为空时保持当前密码")
    settings: Optional[dict[str, JsonData]] = Field(default_factory=dict, description="个性化设置")
    permissions: Optional[UserPermissions] = Field(default_factory=UserPermissions, description="权限")


class CurrentUserUpdate(BaseModel):
    """
    当前用户可自助更新的资料与通知身份设置。
    """

    email: Optional[str] = Field(default=None, description="邮箱")
    avatar: Optional[str] = Field(default=None, description="头像")
    settings: Optional[dict[str, JsonData]] = Field(default=None, description="个性化设置及通知身份绑定")
    password: Optional[str] = Field(default=None, description="新密码，为空时保持当前密码")

    model_config = ConfigDict(extra="forbid")


class UserInDBBase(UserBase):
    """
    包含数据库主键的用户公共记录。
    """

    id: Optional[int] = Field(default=None, description="记录标识")

    model_config = ConfigDict(from_attributes=True)


# 用户接口额外返回的属性
class User(UserInDBBase):
    """
    对 API 调用方公开的用户资料。
    """

    name: str = Field(description="显示名称")
    email: Optional[str] = Field(default=None, description="邮箱，未启用")


# 数据库存储的额外属性
class UserInDB(UserInDBBase):
    """
    包含密码哈希的内部用户记录。
    """

    hashed_password: str = Field(description="密码散列值，不是明文密码")


class AuthProviderRemote(BaseModel):
    """
    插件认证提供方的远程组件信息。
    """

    id: str = Field(description="记录标识")
    url: str = Field(description="访问地址")
    name: str = Field(description="显示名称")
    # 分身所属源插件 ID；前端联邦加载器按它定位共享的前端产物，本体为空。
    # 该字段必须在此声明：端点声明了 response_model，未声明的键会被 pydantic
    source_plugin_id: Optional[str] = Field(default=None, description="静默丢弃，投影层写进去也到不了前端")


class AuthProviderInfo(BaseModel):
    """
    匿名登录页可展示的认证提供方摘要。
    """

    id: str = Field(description="记录标识")
    type: str = Field(description="类型标识")
    name: str = Field(description="显示名称")
    enabled: bool = Field(default=True, description="是否启用")
    method: Optional[str] = Field(default=None, description="认证提供商支持的授权方式")
    icon: Optional[str] = Field(default=None, description="Vuetify 存储图标")
    component: Optional[str] = Field(default=None, description="认证界面组件名称")
    plugin_id: Optional[str] = Field(default=None, description="提供该认证能力的插件标识")
    remote: Optional[AuthProviderRemote] = Field(default=None, description="远程认证界面配置")
