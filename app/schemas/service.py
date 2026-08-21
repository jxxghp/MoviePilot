"""服务实例 API 输出模型。"""

from typing import Optional

from pydantic import BaseModel, Field

from app.schemas.common import JsonData
from app.schemas.plugin import PluginRemoteInfo


class ServiceConfigForm(BaseModel):
    """服务实例类型的专属配置界面。

    界面归属声明该类型的扩展，不归属某个插件——同一插件可能同时声明服务实例
    与其它能力，本模型只承载该服务类型这一份，不掺入插件自身 ``get_form()``
    的内容。内建类型与未声明界面的扩展类型同样落在 ``available=False``，
    不视为异常。

    界面二选一，与声明方扩展的渲染模式对应：``conf``/``model`` 是 vuetify
    模式的组件树加默认数据；``component``/``remote`` 是 vue 模式下应加载的
    组件名与其所在联邦远程入口。``available`` 为 True 时两组字段恰好一组非空。

    ``multi_instance`` 与界面无关，回答的是「该类型能配几份」：为 False 时该
    类型只接受一份配置，前端据此不提供新增第二份的入口。

    ``config_schema`` 同样与界面无关，回答的是「该类型的配置是什么形状」。它与
    ``available`` 互不牵连：声明了契约却没有专属界面时 ``available`` 仍为 False，
    前端据契约生成默认表单；有专属界面时契约照常下发，供前端做提交前校验。
    """

    available: bool = Field(description="该服务类型是否有随声明登记的专属配置界面")
    name: Optional[str] = Field(
        default=None, description="该服务类型的展示名称，未登记该类型时为 None"
    )
    multi_instance: bool = Field(
        default=True, description="用户能否为该服务类型配置多份，未登记该类型时为 True"
    )
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
    config_schema: Optional[dict[str, JsonData]] = Field(
        default=None,
        description="该服务类型配置内容的契约，JSON Schema 受控子集；未声明契约时为 None",
    )
