"""服务实例 API 输入输出模型。"""

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

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


class ServiceFamilyInfo(BaseModel):
    """一族服务在登记表中的元数据。

    族回答的是「这类服务长什么样」，族里有哪些类型由服务实例类型目录回答，两者不是
    同一个问题，因此各占一个模型。
    """

    capability: str = Field(description="能力标签，服务实例配置按它归族")
    name: str = Field(description="族的展示名称")
    distribution: str = Field(description="登记方的发行方式")
    owner: Optional[str] = Field(
        default=None, description="登记方的扩展实例键，宿主内建族为 None"
    )


class ServiceTypeInfo(BaseModel):
    """某族下一个可供新增配置的服务实例类型。

    ``multi_instance`` 决定配置列表上要不要给出新增第二份的入口；
    ``config_form_available`` 决定用登记方自带的界面还是按契约生成的默认表单，界面
    内容本身由配置界面端点按需取用，不随目录整批下发。
    """

    capability: str = Field(description="该类型所属服务族的能力标签")
    type: str = Field(description="类型标识，即该族配置模型的 type 取值")
    name: str = Field(description="类型展示名称")
    icon: Optional[str] = Field(default=None, description="类型展示图标，未声明时为 None")
    multi_instance: bool = Field(description="用户能否为该类型配置多份")
    config_form_available: bool = Field(
        description="该类型有没有随声明登记的专属配置界面"
    )
    config_schema: Optional[dict[str, JsonData]] = Field(
        default=None,
        description="该类型配置内容的契约，JSON Schema 受控子集；未声明契约时为 None",
    )
    provider: str = Field(description="提供该类型的扩展实例键")
    distribution: str = Field(description="提供方的发行方式")


class ServiceInstanceConfigInfo(BaseModel):
    """一条服务实例配置的下发形状。

    ``config`` 里的凭据一律已换成掩码，被掩码的字段路径列在 ``masked_fields``；提交
    更新时把掩码原样回传即表示该项未改动。

    ``type_available`` 按服务实例登记表当下有没有这个类型判定，而不是按 ``provider``：
    后者只是记账，用来在类型查不到时说出是哪个扩展没在场。
    """

    capability: str = Field(description="该配置所属服务族的能力标签")
    type: str = Field(description="类型标识")
    name: str = Field(description="实例名")
    enabled: bool = Field(description="该实例是否启用")
    config: dict[str, JsonData] = Field(
        default_factory=dict, description="类型专属配置载荷，凭据已掩码"
    )
    host_config: dict[str, JsonData] = Field(
        default_factory=dict, description="宿主消费的实例级字段载荷"
    )
    is_default_target: bool = Field(description="该实例是否为本族的默认调用目标")
    provider: str = Field(description="提供该类型的扩展实例键，内建类型为保留值")
    masked_fields: list[str] = Field(
        default_factory=list, description="已被掩码的字段路径，按嵌套层级逐层拼接"
    )
    type_available: bool = Field(
        description="该类型当前是否已登记，为 False 时这条配置产不出实例"
    )
    type_name: Optional[str] = Field(
        default=None, description="类型展示名称，类型未登记时为 None"
    )


class ServiceConfigProviderIssue(BaseModel):
    """一条提供方已消失的服务实例配置。

    ``reason`` 分开三种成因而不是笼统答一个「不可用」：三者的处置动作完全不同——装回
    插件、启用插件、以及查插件日志。第三种最难自查：用户看到插件是已启用、配置也好好
    地列在那里，唯独实例不存在。

    文案不在此处下发：本模型只给稳定的成因代码与身份，措辞由前端按当前语言渲染，
    免得同一句话在后端与前端各存一份、改一处就漂移。
    """

    capability: str = Field(description="该配置所属服务族的能力标签")
    type: str = Field(description="类型标识")
    name: str = Field(description="实例名")
    provider: str = Field(description="记账中提供该类型的扩展实例键")
    extension_id: str = Field(description="提供方所属的扩展标识")
    reason: str = Field(
        description=(
            "成因代码：not_installed 提供方未安装，disabled 已安装但未启用，"
            "start_failed 已启用但该类型没有登记成功"
        )
    )


class ServiceInstanceCandidateInfo(BaseModel):
    """一条可供扩展点选择的服务实例。

    不带 ``config``：候选列表是给用户挑选用的，而配置载荷里装着 token 与密码，随
    选择器下发即等于把凭据摊给每一个能编辑工作流的人。

    停用的实例照样列出并标注启用态：用户看到「配了但停用了」才知道该去启用哪一条，
    整个藏起来只会让人以为配置丢了。
    """

    type: str = Field(description="类型标识")
    name: str = Field(description="实例名，扩展点选中的即此取值")
    enabled: bool = Field(description="该实例是否已启用")
    is_default_target: bool = Field(description="该实例是否为本族的默认调用目标")


class ServiceInstanceSelection(BaseModel):
    """某个扩展点当前可选的服务实例，以及已选实例的现状。

    ``family_registered`` 与空候选列表分开回答两件事：族没登记意味着提供它的扩展不
    在场，处置是装回扩展；族登记了却没有候选意味着用户一份配置都还没建，处置是去
    设置页新建。合成一句「没有可选实例」会让这两种完全不同的处境看起来一样。

    ``issue`` 只给稳定的成因代码，措辞由前端按当前语言渲染，与「提供方已消失」那条
    通路同一记账口径——两者方向相反：那条回答「配置还在、提供方没了」，本字段回答
    「引用还在、被引用的实例没了」。
    """

    capability: str = Field(description="能力标签")
    family_registered: bool = Field(description="该能力标签当前是否为已登记的服务族")
    supports_default_target: bool = Field(
        description="该族有没有默认调用目标，为 False 时调用必须显式指定实例"
    )
    candidates: list[ServiceInstanceCandidateInfo] = Field(
        default_factory=list, description="可选实例列表，按类型标识与实例名升序"
    )
    selected: Optional[str] = Field(
        default=None, description="查询时给出的已选实例名，未给出时为 None"
    )
    issue: Optional[str] = Field(
        default=None,
        description=(
            "已选实例的失效成因代码：family_absent 该族未登记，instance_absent 实例配置"
            "已不存在，instance_disabled 实例配置已停用，type_excluded 实例类型不在声明"
            "收窄的范围内；引用成立时为 None"
        ),
    )


class ServiceInstanceConfigPayload(BaseModel):
    """服务实例配置的写入载荷，新增与更新共用。

    形状与该族配置模型一致：``config`` 归类型实现自己读，宿主消费的实例级字段
    （路径映射、场景开关、同步间隔等）平铺在顶层，因此本模型放行已声明字段之外的
    顶层键，由宿主按该族配置模型筛出自己要的那几个，其余丢弃。

    更新是整条替换而不是逐字段合并：未提交的字段按缺省取值落库。唯独凭据例外——
    回传掩码即保留库中原值，因此改一个端口号不必重新输入密码。

    ``default`` 不在本模型里：默认调用目标受「每族至多一个」的唯一索引管辖，置位与
    清位各有专用入口，混在配置写入里会让一次改端口号顺带把别人的默认置位清掉。
    """

    model_config = ConfigDict(extra="allow")

    type: Optional[str] = Field(
        default=None, description="类型标识；更新时取路径上的类型，本字段被忽略"
    )
    name: Optional[str] = Field(
        default=None, description="实例名；更新时给出不同取值即为改名"
    )
    enabled: Optional[bool] = Field(default=None, description="该实例是否启用")
    config: Optional[dict[str, JsonData]] = Field(
        default=None, description="类型专属配置载荷，凭据字段可回传掩码表示未改动"
    )
