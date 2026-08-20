"""存储授权 API 输出模型。"""

from typing import Optional

from pydantic import BaseModel, Field

from app.schemas.common import JsonData
from app.schemas.plugin import PluginRemoteInfo


class StorageConfigForm(BaseModel):
    """存储类型的专属配置界面。

    界面归属声明该存储类型的扩展，不归属某个插件——同一插件可能同时声明存储
    与其它能力，本模型只承载存储这一份，不掺入插件自身 ``get_form()`` 的内容。
    内建类型与未声明界面的扩展类型同样落在 ``available=False``，不视为异常。

    界面二选一，与声明方扩展的渲染模式对应：``conf``/``model`` 是 vuetify
    模式的组件树加默认数据；``component``/``remote`` 是 vue 模式下应加载的
    组件名与其所在联邦远程入口。``available`` 为 True 时两组字段恰好一组非空。
    """

    available: bool = Field(description="该存储类型是否有随声明登记的专属配置界面")
    conf: Optional[list[dict[str, JsonData]]] = Field(
        default=None, description="vuetify 模式的组件树，非 vuetify 模式时为 None"
    )
    model: Optional[dict[str, JsonData]] = Field(
        default=None, description="vuetify 模式的表单默认数据，非 vuetify 模式时为 None"
    )
    component: Optional[str] = Field(
        default=None, description="vue 模式下承载该界面的组件名，非 vue 模式时为 None"
    )
    remote: Optional[PluginRemoteInfo] = Field(
        default=None, description="vue 模式下组件所在的联邦远程入口，非 vue 模式时为 None"
    )


class StorageQrCodeData(BaseModel):
    """云存储扫码授权二维码。"""

    codeContent: Optional[str] = Field(default=None, description="二维码原始内容")
    codeUrl: Optional[str] = Field(default=None, description="二维码图片地址")


class StorageAuthUrlData(BaseModel):
    """云存储 OAuth 授权入口。"""

    authUrl: str = Field(description="授权地址")
    state: str = Field(description="授权状态校验值")


class StorageLoginStatusData(BaseModel):
    """云存储扫码或 OAuth 登录状态。"""

    status: int | str = Field(description="授权状态")
    tip: str = Field(description="状态提示")
