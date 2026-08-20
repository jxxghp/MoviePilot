"""扩展声明式注册的声明载体。

扩展经 ``provides_*`` 钩子交出的是声明而不是裸实现：声明的数据字段描述「提供了
什么」，``impl`` 字段携带「用什么提供」。这样拆分是为了让声明面在宿主换实现语言、
扩展改为独立进程时仍然成立——届时 ``impl`` 不参与序列化，其余字段原样成为握手
报文，契约校验从内省对象退化为校验声明数据。

判据见 docs/plugin-extension-architecture.md。
"""

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Dict, List, Mapping, Optional, Tuple

from app.schemas.types import ModuleType


@dataclass(frozen=True, slots=True)
class ExtensionDeclaration:
    """
    扩展声明的公共部分

    :param capabilities: 本声明承诺提供的能力方法名，供依赖匹配与契约校验取用
    :param impl: 实现该声明的对象，进程内直接使用；跨进程时不参与传输
    """

    capabilities: Tuple[str, ...] = ()
    impl: Optional[Any] = None


@dataclass(frozen=True, slots=True)
class StorageDeclaration(ExtensionDeclaration):
    """
    存储后端声明

    ``schema`` 是存储标识，同一标识重复登记以最新一次为准，因此扩展提供的标识
    与内建标识相同即构成覆盖。标识允许是普通字符串，不要求登记于内核枚举。

    该存储类型的专属配置界面二选一，与扩展自身的渲染模式对应：

    - ``config_form``：vuetify 模式，形状与 ``_PluginBase.get_form()`` 相同——
      组件树加默认数据二元组
    - ``config_component``：vue 模式，本扩展联邦远程中承载该界面的组件名，
      要求扩展的 ``get_render_mode()`` 返回 ``"vue"``

    两者互斥，同时给出视为意图不明，整条声明被拒；都不给出合法，表示该存储
    类型没有专属界面，前端沿用内建类型的渲染方式。界面归属这条声明，不归属
    声明它的扩展：扩展同时提供存储与其它能力时，各自的配置界面互不干扰，也
    不会读到扩展自身的 ``get_form()``。

    :param schema: 存储标识，例如 u115、alipan
    :param config_form: (组件树, 默认数据) 二元组，vuetify 模式；与
        ``config_component`` 互斥
    :param config_component: 联邦远程中的组件名，vue 模式；与 ``config_form`` 互斥
    """

    schema: str = ""
    config_form: Optional[Tuple[List[Dict[str, Any]], Dict[str, Any]]] = None
    config_component: Optional[str] = None


@dataclass(frozen=True, slots=True)
class AgentToolDeclaration(ExtensionDeclaration):
    """
    智能体工具声明

    ``name``/``description`` 是工具向宿主自报的标识与说明，作为声明数据独立于
    ``impl``：宿主换实现语言、扩展改为独立进程时，这两个字段随其余声明数据
    原样成为握手报文，``impl`` 不参与传输。

    :param name: 工具名，供 Agent 识别并调用
    :param description: 工具描述，供 Agent 判断何时调用该工具
    """

    name: str = ""
    description: str = ""


@dataclass(frozen=True, slots=True)
class ModuleDeclaration(ExtensionDeclaration):
    """
    模块方法表声明

    ``methods`` 是原 ``_PluginBase.get_module()`` 那张「方法名到实现」表的声明式
    版本，宿主按方法名把请求分发到其中的可调用对象。跨进程时该表退化为方法名
    清单：可调用对象本身不参与序列化，握手报文只带方法名，具体调用改由对端进程
    按同名方法自行响应。

    ``service_config`` 声明本模块归属的服务配置族，取值须是 ``SystemConfigKey``
    的成员值，例如 ``Downloaders``、``MediaServers``、``Notifications``；不归属
    任何服务族时留空。

    :param methods: 方法名到可调用对象的映射，跨进程时退化为方法名清单
    :param service_config: 服务配置键，声明本模块归属的服务族；不归属任何服务族时为空
    """

    methods: Mapping[str, Any] = MappingProxyType({})
    service_config: str = ""


# 可声明服务实例的能力标签取值集合，即宿主按「一份配置扇出一个具名实例」消费的服务族
SERVICE_INSTANCE_CAPABILITIES: Tuple[str, ...] = (
    ModuleType.Downloader.value,
    ModuleType.MediaServer.value,
    ModuleType.Notification.value,
)


@dataclass(frozen=True, slots=True)
class ServiceInstanceDeclaration(ExtensionDeclaration):
    """
    可配置服务实例类型声明

    服务实例与其它扩展点的区别在于「有没有」不是终点：用户在设置页里按类型新建
    任意多个具名实例，每个实例带自己的一份配置。因此声明描述的是**类型**，宿主
    按该类型下的每条用户配置构造一个实例。

    ``capability`` 是该类型属于哪一族服务的语义标签，取值须属于
    `SERVICE_INSTANCE_CAPABILITIES`。下载器、媒体服务器与消息通知共用这一条声明，
    差异只在该标签：三者的取用链是同一条——同一张服务实例表，按「能力标签加类型
    标识」取用，形状没有区别，因此不按业务族拆成三个钩子，差异作为参数声明出来。

    构造方式二选一，宿主对该类型下的每条用户配置执行其一：

    - ``impl``：实现类，宿主按 ``impl(name=配置名, **配置内容)`` 构造，要求构造
      签名能接受关键字 ``name``
    - ``factory``：可调用对象，宿主按 ``factory(配置对象)`` 构造，配置对象即该族
      配置模型的一条记录，怎么落到实例上由扩展自行决定

    两者互斥，同时给出视为意图不明，整条声明被拒；都不给出同样被拒，宿主无从
    构造实例。二者都是进程内快路径，跨进程时均不参与序列化。

    该服务类型的专属配置界面二选一，字段语义与 ``StorageDeclaration`` 相同：

    - ``config_form``：vuetify 模式，(组件树, 默认数据) 二元组
    - ``config_component``：vue 模式，本扩展联邦远程中承载该界面的组件名，
      要求扩展的 ``get_render_mode()`` 返回 ``"vue"``

    两者互斥，同时给出视为意图不明，整条声明被拒；都不给出合法，表示该类型
    没有专属界面，前端沿用内建类型的渲染方式。界面归属这条声明，不归属声明
    它的扩展。

    :param capability: 能力标签，取值须属于 `SERVICE_INSTANCE_CAPABILITIES`
    :param type: 类型标识，与该族配置模型的 ``type`` 字段取值对应，例如 qbittorrent
    :param name: 类型展示名称
    :param factory: 接收单条服务配置并返回实例的可调用对象；与 ``impl`` 互斥
    :param config_form: (组件树, 默认数据) 二元组，vuetify 模式；与
        ``config_component`` 互斥
    :param config_component: 联邦远程中的组件名，vue 模式；与 ``config_form`` 互斥
    """

    capability: str = ""
    type: str = ""
    name: str = ""
    factory: Optional[Any] = None
    config_form: Optional[Tuple[List[Dict[str, Any]], Dict[str, Any]]] = None
    config_component: Optional[str] = None


@dataclass(frozen=True, slots=True)
class AuthProviderDeclaration(ExtensionDeclaration):
    """
    登录认证提供方声明

    ``id``/``name``/``icon`` 是该登录入口向宿主自报的展示信息，缺省时分别回落为
    ``plugin:<实例键>``、插件展示名、无图标；``enabled`` 默认为 True。vue 模式下
    登录入口渲染组件固定为 ``AuthPage``，由宿主联邦机制原样注入，与旧写法语义
    一致，不受本声明字段影响。

    该认证提供方的专属配置界面二选一，字段语义与 ``StorageDeclaration`` 相同：

    - ``config_form``：vuetify 模式，(组件树, 默认数据) 二元组
    - ``config_component``：vue 模式，本扩展联邦远程中承载该界面的组件名，
      要求扩展的 ``get_render_mode()`` 返回 ``"vue"``

    两者互斥，同时给出视为意图不明，整条声明被拒；都不给出合法，表示该认证
    提供方没有专属配置界面。界面归属这条声明，不归属声明它的扩展。

    :param id: 提供方标识，缺省时回落为 ``plugin:<实例键>``
    :param name: 展示名称，缺省时回落为插件展示名
    :param icon: 展示图标
    :param enabled: 是否启用，默认为 True
    :param config_form: (组件树, 默认数据) 二元组，vuetify 模式；与
        ``config_component`` 互斥
    :param config_component: 联邦远程中的组件名，vue 模式；与 ``config_form`` 互斥
    """

    id: Optional[str] = None
    name: Optional[str] = None
    icon: Optional[str] = None
    enabled: bool = True
    config_form: Optional[Tuple[List[Dict[str, Any]], Dict[str, Any]]] = None
    config_component: Optional[str] = None


@dataclass(frozen=True, slots=True)
class MediaSourceDeclaration(ExtensionDeclaration):
    """
    媒体数据源声明

    识别、搜索、图片与 NFO 刮削的实际实现仍由 ``provides_modules()``/``get_module()``
    按契约方法名分发；本声明只承载数据源自身的展示信息，供宿主聚合成来源列表。

    :param media_source: 规范媒体来源标识，须能被 ``MediaSource`` 解析——内置常量
        或形如 ``[a-z][a-z0-9._-]{0,63}`` 的插件扩展标识
    :param name: 数据源展示名称
    :param media_types: 支持的媒体类型；留空时由消费方按自身默认值处理
    """

    media_source: str = ""
    name: str = ""
    media_types: Tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ActionDeclaration(ExtensionDeclaration):
    """
    工作流动作声明

    ``impl`` 是动作的实现函数：首个位置参数固定为 ``ActionContext`` 实例，返回
    ``(执行状态, 更新后的 ActionContext)`` 二元组，与既有 ``get_actions()`` 对
    实现函数的要求一致。

    :param action_id: 动作标识，工作流按此标识调用该动作
    :param name: 动作展示名称
    :param kwargs: 调用该动作实现时附加传递的静态参数
    """

    action_id: str = ""
    name: str = ""
    kwargs: Mapping[str, Any] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class DashboardDeclaration(ExtensionDeclaration):
    """
    仪表盘声明

    声明的是「有哪些仪表盘、长什么样」；「当前该显示什么数据」仍由带参数的
    ``get_dashboard(key, **kwargs)`` 在每次请求时实时取用，两者不是一回事。

    该仪表盘的专属界面二选一，与 ``StorageDeclaration.config_form``/
    ``config_component`` 同一套语义：``config_form`` 是 vuetify 模式下的
    （组件树, 默认数据）二元组，``config_component`` 是 vue 模式下本扩展联邦
    远程中承载该仪表盘的组件名，要求扩展的 ``get_render_mode()`` 返回 ``"vue"``。
    两者互斥，同时给出视为意图不明，整条声明被拒；都不给出合法，表示该仪表盘
    没有随声明附带的初始界面。

    :param key: 仪表盘 key，在插件实例范围内唯一；单仪表盘插件可留空，代表
        插件的默认仪表盘
    :param name: 仪表盘展示名称
    :param config_form: (组件树, 默认数据) 二元组，vuetify 模式；与
        ``config_component`` 互斥
    :param config_component: 联邦远程中的组件名，vue 模式；与 ``config_form`` 互斥
    """

    key: str = ""
    name: str = ""
    config_form: Optional[Tuple[List[Dict[str, Any]], Dict[str, Any]]] = None
    config_component: Optional[str] = None


def declaration_schema(declaration: Any) -> Optional[str]:
    """
    读取声明自报的存储标识

    :param declaration: 存储声明
    :return: 存储标识；声明不带标识时为 None
    """
    schema = getattr(declaration, "schema", None)
    if isinstance(schema, str) and schema.strip():
        return schema.strip()
    return None


def declaration_config_form(
    declaration: Any,
) -> Optional[Tuple[List[Dict[str, Any]], Dict[str, Any]]]:
    """
    读取声明自带的配置界面

    :param declaration: 存储声明
    :return: (组件树, 默认数据) 二元组；声明未带配置界面时为 None
    """
    return getattr(declaration, "config_form", None)


def declaration_config_component(declaration: Any) -> Optional[str]:
    """
    读取声明自带的 vue 模式配置界面组件名

    :param declaration: 存储声明
    :return: 组件名；未声明或为空白时为 None
    """
    return _declared_text(declaration, "config_component")


def declaration_auth_provider_fields(declaration: Any) -> Optional[Dict[str, Any]]:
    """
    读取认证提供方声明的展示字段

    兼容插件直接交出字段字典而不包 `AuthProviderDeclaration` 的写法：此时字典即
    声明本身，字段原样返回；此时无法声明专属配置界面，因为字典没有
    ``config_form``/``config_component`` 属性可读。

    :param declaration: `AuthProviderDeclaration` 实例，或插件直接交出的字段字典
    :return: 含 id/name/icon/enabled 等展示字段的字典；声明形状不合法时为 None
    """
    if isinstance(declaration, Mapping):
        return dict(declaration)
    if isinstance(declaration, AuthProviderDeclaration):
        fields: Dict[str, Any] = {"enabled": declaration.enabled}
        if declaration.id:
            fields["id"] = declaration.id
        if declaration.name:
            fields["name"] = declaration.name
        if declaration.icon:
            fields["icon"] = declaration.icon
        return fields
    return None


def declaration_agent_tool_identity(declaration: Any) -> Tuple[Optional[str], Optional[str]]:
    """
    读取声明自报的工具名与描述

    :param declaration: 智能体工具声明，或插件直接交出的实现类
    :return: (工具名, 工具描述) 二元组；对应字段缺失、非字符串或为空白时该位为 None
    """
    return _declared_text(declaration, "name"), _declared_text(declaration, "description")


def _declared_text(declaration: Any, field: str) -> Optional[str]:
    """
    读取声明对象上的非空字符串字段

    :param declaration: 声明对象
    :param field: 字段名
    :return: 去除首尾空白后的字符串；字段缺失、非字符串或全为空白时为 None
    """
    value = getattr(declaration, field, None)
    return value.strip() if isinstance(value, str) and value.strip() else None


def declaration_methods(declaration: Any) -> Optional[Mapping[str, Any]]:
    """
    读取声明的方法表

    兼容插件直接交出方法表字典而不包 `ModuleDeclaration` 的写法：此时方法表
    即声明本身。

    :param declaration: `ModuleDeclaration` 实例，或插件直接交出的方法表字典
    :return: 方法名到可调用对象的映射；取不到时为 None
    """
    if isinstance(declaration, Mapping):
        return declaration
    methods = getattr(declaration, "methods", None)
    return methods if isinstance(methods, Mapping) else None


def declaration_impl(declaration: Any) -> Optional[Any]:
    """
    读取声明携带的实现对象

    兼容扩展直接交出实现类而非声明对象的写法：此时实现即声明本身。

    :param declaration: 扩展声明或实现对象
    :return: 实现对象；取不到时为 None
    """
    if declaration is None:
        return None
    impl = getattr(declaration, "impl", None)
    return impl if impl is not None else declaration


def _declared_field(declaration: Any, field: str) -> Any:
    """
    读取声明字段的原始值，兼容属性对象与映射两种载体

    媒体数据源、工作流动作与仪表盘的兼容旧写法是插件直接交出描述字典而非
    声明对象，字典没有属性访问，须按载体类型分别取值。

    :param declaration: 声明对象，或插件直接交出的描述字典
    :param field: 字段名
    :return: 字段原始值；字段缺失时为 None
    """
    if isinstance(declaration, Mapping):
        return declaration.get(field)
    return getattr(declaration, field, None)


def _declared_field_text(declaration: Any, field: str) -> Optional[str]:
    """
    读取声明字段的非空字符串值，兼容属性对象与映射两种载体

    :param declaration: 声明对象，或插件直接交出的描述字典
    :param field: 字段名
    :return: 去除首尾空白后的字符串；字段缺失、非字符串或全为空白时为 None
    """
    value = _declared_field(declaration, field)
    return value.strip() if isinstance(value, str) and value.strip() else None


def declaration_media_source_identity(declaration: Any) -> Tuple[Optional[str], Optional[str]]:
    """
    读取声明自报的数据源标识与展示名称

    :param declaration: `MediaSourceDeclaration` 实例，或插件直接交出的描述字典
    :return: (数据源标识, 展示名称) 二元组；对应字段缺失、非字符串或为空白时该位为 None
    """
    return (
        _declared_field_text(declaration, "media_source"),
        _declared_field_text(declaration, "name"),
    )


def declaration_media_types(declaration: Any) -> Optional[Tuple[Any, ...]]:
    """
    读取声明自报的支持媒体类型

    :param declaration: `MediaSourceDeclaration` 实例，或插件直接交出的描述字典
    :return: 媒体类型序列转换成的元组；字段缺失或不是序列时为 None
    """
    value = _declared_field(declaration, "media_types")
    return tuple(value) if isinstance(value, (list, tuple)) else None


def declaration_action_identity(declaration: Any) -> Tuple[Optional[str], Optional[str]]:
    """
    读取声明自报的动作标识与展示名称

    :param declaration: `ActionDeclaration` 实例，或插件直接交出的描述字典
    :return: (动作标识, 展示名称) 二元组；对应字段缺失、非字符串或为空白时该位为 None
    """
    return (
        _declared_field_text(declaration, "action_id"),
        _declared_field_text(declaration, "name"),
    )


def declaration_action_impl(declaration: Any) -> Any:
    """
    读取声明的动作实现函数

    兼容插件直接交出描述字典而不包 `ActionDeclaration` 的写法：字典形态复用
    ``get_actions()`` 返回项的 ``func`` 字段存放实现函数，与工作流实际消费的
    字段一致。

    :param declaration: `ActionDeclaration` 实例，或插件直接交出的描述字典
    :return: 实现函数；取不到时为 None
    """
    if isinstance(declaration, Mapping):
        return declaration.get("func")
    return getattr(declaration, "impl", None)


def declaration_action_kwargs(declaration: Any) -> Any:
    """
    读取声明自带的动作附加参数原始值

    :param declaration: `ActionDeclaration` 实例，或插件直接交出的描述字典
    :return: kwargs 字段的原始值；字段缺失时为 None
    """
    return _declared_field(declaration, "kwargs")


def declaration_service_instance_identity(
    declaration: Any,
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    读取服务实例声明自报的能力标签、类型标识与展示名称

    :param declaration: `ServiceInstanceDeclaration` 实例
    :return: (能力标签, 类型标识, 展示名称) 三元组；对应字段缺失、非字符串或
        为空白时该位为 None
    """
    return (
        _declared_field_text(declaration, "capability"),
        _declared_field_text(declaration, "type"),
        _declared_field_text(declaration, "name"),
    )


def declaration_service_instance_constructor(
    declaration: Any,
) -> Tuple[Optional[Any], Optional[Any]]:
    """
    读取服务实例声明的两条构造路径

    服务实例类型无法从裸实现推出能力标签与类型标识，因此两个字段都按原值读取，
    不套用 `declaration_impl` 的「实现即声明」回落。

    :param declaration: `ServiceInstanceDeclaration` 实例
    :return: (实现类, 实例工厂) 二元组；对应字段缺失时该位为 None
    """
    return (
        _declared_field(declaration, "impl"),
        _declared_field(declaration, "factory"),
    )


def declaration_dashboard_identity(declaration: Any) -> Tuple[Optional[str], Optional[str]]:
    """
    读取声明自报的仪表盘 key 与展示名称

    key 保留原始字符串（含空字符串），空字符串代表插件的默认仪表盘，与
    ``name`` 的「非空才有效」语义不同，不能共用同一条读取规则。

    :param declaration: `DashboardDeclaration` 实例，或插件直接交出的描述字典
    :return: (仪表盘 key, 展示名称) 二元组；key 非字符串时为 None，name 为空白时为 None
    """
    key = _declared_field(declaration, "key")
    return (key if isinstance(key, str) else None), _declared_field_text(declaration, "name")
