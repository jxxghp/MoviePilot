from typing import Optional

from pydantic import BaseModel, Field, ConfigDict
from typing_extensions import TypedDict

from app.schemas.common import JsonData


class UserPermissions(TypedDict, total=False):
    """用户分类权限及可动态扩展的功能级权限。"""

    discovery: bool  # 发现功能分类权限
    search: bool  # 资源搜索分类权限
    subscribe: bool  # 订阅管理分类权限
    manage: bool  # 系统管理分类权限
    admin: bool  # 管理员入口标识，实际授权仍由超级用户身份决定
    features: dict[str, bool]  # 功能键到启用状态的映射


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
    permissions: Optional[UserPermissions] = Field(default_factory=dict)
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
    permissions: Optional[UserPermissions] = Field(default_factory=dict)


# Properties to receive via API on update
class UserUpdate(UserBase):
    """更新用户时接收的完整资料。"""

    id: int
    name: str
    email: Optional[str] = None
    password: Optional[str] = None
    settings: Optional[dict[str, JsonData]] = Field(default_factory=dict)
    permissions: Optional[UserPermissions] = Field(default_factory=dict)


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
    # 该远程入口所属实例运行的插件版本号，插件未声明 plugin_version 时为空
    version: Optional[str] = Field(default=None, description="插件版本号")
    # 按版本区分的联邦远程标识，格式为 `{id}#{version}`；无版本信息时与 id 相同，
    # 用途见 app.schemas.plugin.PluginRemoteInfo.remote_key
    remote_key: Optional[str] = Field(default=None, description="按版本区分的联邦远程标识")


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
    # 该认证方式的专属配置界面，归属声明它的那条声明而非扩展自身；二选一，
    # 与声明方扩展的渲染模式对应，都不给表示该认证方式没有专属界面
    config_form: Optional[list[dict[str, JsonData]]] = Field(
        default=None, description="vuetify 模式的组件树，非 vuetify 模式时为 None"
    )
    config_component: Optional[dict[str, JsonData]] = Field(
        default=None, description="vue 模式下应加载的组件名与其所在联邦远程入口"
    )
