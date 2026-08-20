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

from app.schemas.rule import RULE_CONDITION_FIELDS
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


# 可声明服务实例的能力标签取值集合，即宿主按「一份配置扇出一个具名实例」消费的服务族
SERVICE_INSTANCE_CAPABILITIES: Tuple[str, ...] = (
    ModuleType.Downloader.value,
    ModuleType.MediaServer.value,
    ModuleType.Notification.value,
)


@dataclass(frozen=True, slots=True)
class ModuleDeclaration(ExtensionDeclaration):
    """
    模块方法表声明

    ``methods`` 是原 ``_PluginBase.get_module()`` 那张「方法名到实现」表的声明式
    版本，宿主按方法名把请求分发到其中的可调用对象。跨进程时该表退化为方法名
    清单：可调用对象本身不参与序列化，握手报文只带方法名，具体调用改由对端进程
    按同名方法自行响应。

    本声明只描述方法表。按用户配置扇出多个具名服务实例是另一回事，由
    `ServiceInstanceDeclaration` 承担——两者混在一条声明里会让「提供一批方法」与
    「提供一族可配置实例」共用同一个入口，而宿主对二者的装载路径本就不同。

    :param methods: 方法名到可调用对象的映射，跨进程时退化为方法名清单
    """

    methods: Mapping[str, Any] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class ServiceInstanceDeclaration(ExtensionDeclaration):
    """
    可配置服务实例类型声明

    服务实例与其它扩展点的区别在于「有没有」不是终点：用户在设置页里按类型新建
    具名实例，每个实例带自己的一份配置。因此声明描述的是**类型**，宿主按该类型
    下的每条用户配置构造一个实例。

    ``multi_instance`` 回答「用户能为这个类型配几份」：为 True 时该类型下有几条
    用户配置就有几个具名实例，为 False 时该类型只认一份配置。取值由声明表达而不
    由服务族推定——同一族里两种都存在，例如认证器接第三方站点单点登录时全局只有
    一份配置，接媒体服务器单点登录时需要每台服务器一份。

    该字段与「扩展本体是否分身」正交，两者回答的不是同一个问题：分身是扩展自己
    按 ``plugin_id@instance_id`` 扇出的多个行为体，各自独立运行、各自持有配置；
    ``multi_instance`` 描述的是本类型的配置列表允许有几条记录，与声明它的扩展建
    了几个分身无关。一个只建了默认分身的扩展照样可以提供多实例类型，一个建了多
    个分身的扩展提供的类型也可以只认一份配置。

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
    :param multi_instance: 用户能否为该类型配置多份，默认为 True
    :param factory: 接收单条服务配置并返回实例的可调用对象；与 ``impl`` 互斥
    :param config_form: (组件树, 默认数据) 二元组，vuetify 模式；与
        ``config_component`` 互斥
    :param config_component: 联邦远程中的组件名，vue 模式；与 ``config_form`` 互斥
    """

    capability: str = ""
    type: str = ""
    name: str = ""
    multi_instance: bool = True
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
class MetaParserDeclaration(ExtensionDeclaration):
    """
    名称解析器声明

    ``impl`` 是解析环的实现：接收一个 `MetaParseRequest`，交回本环认为成立的
    `ParsedMeta`，返回 None 即本次不认领。宿主按用户排定的顺序把各环串成管道，
    每一环拿到的是上游累积出的结果，因此下游既能补空位，也能改写上游填错的字段——
    代价是宿主为每个字段记录来源与被覆盖前的取值。

    解析环只能贡献，拿不到「继续或中断」的开关：一环抛异常只跳过这一环，整条
    链继续，内建解析保证门面永远返回可用结果。

    ``priority`` 只是该解析器初次出现在顺序表里的默认位置。实际顺序取用户排定的
    持久配置——顺序即语义，谁先跑决定谁的结果被覆盖，这种选择不能由用户看不见的
    声明值或登记先后决定。

    :param parser_id: 解析器标识，取值须形如 ``[A-Za-z0-9][A-Za-z0-9._-]{0,63}``；
        同一扩展的多个分身各声明一次即多个各自成立的解析环，宿主按实例键为其分别
        编号，因此标识只需在声明它的实例内唯一
    :param name: 解析器展示名称，供用户在顺序配置里辨认
    :param priority: 默认顺序，数值越小越靠前，仅在用户尚未排到该解析器时生效
    """

    parser_id: str = ""
    name: str = ""
    priority: int = 0


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
class FilterRuleDeclaration(ExtensionDeclaration):
    """
    筛选规则声明

    规则是纯数据：五个条件字段的形状与用户自定义规则 `CustomRule` 完全相同，宿主把
    声明投影成同一形状后并入运行期规则集，因此规则引擎（含 Rust 快路）分辨不出一条
    规则来自内建、插件还是用户。本声明不携带 ``impl``——判定逻辑仍由宿主的规则引擎
    执行，扩展只提供参数。

    ``rule_id`` 会作为原子进入规则串的语法，必须合规则ID文法，否则用户把它写进规则组
    时才会解析失败；契约校验在登记时即拒绝不合文法的标识。

    五个条件字段至少要给出一个：一条不带任何条件的规则对每颗种子都判定通过，等同于
    没有这条规则，声明它多半是笔误而不是意图。

    :param rule_id: 规则标识，作为原子出现在规则串中，须合规则ID文法
    :param name: 规则展示名称
    :param include: 包含项正则
    :param exclude: 排除项正则
    :param size_range: 大小范围（MB），形如 ``1024-4096``、``>1024``、``<4096``
    :param seeders: 最少做种人数
    :param publish_time: 发布时间（分钟），形如 ``60`` 或 ``60-1440``
    """

    rule_id: str = ""
    name: str = ""
    include: Optional[str] = None
    exclude: Optional[str] = None
    size_range: Optional[str] = None
    seeders: Optional[str] = None
    publish_time: Optional[str] = None


@dataclass(frozen=True, slots=True)
class FilterRuleGroupDeclaration(ExtensionDeclaration):
    """
    筛选规则组声明

    规则组把规则标识按布尔表达式与优先级组合成一套可整体引用的筛选方案，用户在搜索、
    订阅、洗版与默认规则四个场景里按 ``name`` 引用它。``rule_string`` 的书写顺序即
    优先级：``>`` 分隔的层级从高到低，同层内用 ``&``/``|``/``!`` 组合规则标识。

    ``name`` 既是标识也是展示名——四个场景保存的就是组名，两者不是可以分开的东西。

    :param name: 规则组名称，用户在四个场景里按此名称引用
    :param rule_string: 规则串，形如 ``CNSUB & 4K & !BLU > CNSUB & 1080P``
    :param media_type: 适用媒体类型，为空表示全部；取值为「电影」或「电视剧」
    :param category: 适用媒体类别，为空表示全部；取值为二级分类名
    """

    name: str = ""
    rule_string: str = ""
    media_type: Optional[str] = None
    category: Optional[str] = None


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


def declaration_meta_parser_identity(declaration: Any) -> Tuple[Optional[str], Optional[str]]:
    """
    读取声明自报的解析器标识与展示名称

    :param declaration: `MetaParserDeclaration` 实例
    :return: (解析器标识, 展示名称) 二元组；对应字段缺失、非字符串或为空白时该位为 None
    """
    return (
        _declared_field_text(declaration, "parser_id"),
        _declared_field_text(declaration, "name"),
    )


def declaration_meta_parser_priority(declaration: Any) -> Any:
    """
    读取声明自报的默认顺序取值

    按原值返回而不归一为整数：取值合法性由契约校验判定，此处先归一会把非整数的
    错误取值悄悄变成一个合法答案。

    :param declaration: `MetaParserDeclaration` 实例
    :return: priority 字段的原始值；字段缺失时为 None
    """
    return _declared_field(declaration, "priority")


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


def declaration_service_instance_multi_instance(declaration: Any) -> Any:
    """
    读取服务实例声明自报的实例数取值

    按原值返回而不归一为布尔：取值合法性由契约校验判定，此处先归一会把非布尔的
    错误取值悄悄变成一个合法答案，校验就再也看不见它。

    :param declaration: `ServiceInstanceDeclaration` 实例
    :return: multi_instance 字段的原始值；字段缺失时为 None
    """
    return _declared_field(declaration, "multi_instance")


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


def declaration_filter_rule_identity(
    declaration: Any,
) -> Tuple[Optional[str], Optional[str]]:
    """
    读取筛选规则声明自报的规则标识与展示名称

    :param declaration: `FilterRuleDeclaration` 实例，或插件直接交出的描述字典
    :return: (规则标识, 展示名称) 二元组；对应字段缺失、非字符串或为空白时该位为 None
    """
    return (
        _declared_field_text(declaration, "rule_id"),
        _declared_field_text(declaration, "name"),
    )


def declaration_filter_rule_conditions(declaration: Any) -> Dict[str, Any]:
    """
    读取筛选规则声明的全部匹配条件字段原始值

    按原值返回而不做归一：取值合法性由契约校验判定，此处先归一会把非字符串的错误
    取值悄悄变成一个合法答案，校验就再也看不见它。

    :param declaration: `FilterRuleDeclaration` 实例，或插件直接交出的描述字典
    :return: 条件字段名到原始值的字典，字段缺失时该项为 None
    """
    return {field: _declared_field(declaration, field) for field in RULE_CONDITION_FIELDS}


def declaration_filter_rule_group_identity(
    declaration: Any,
) -> Tuple[Optional[str], Optional[str]]:
    """
    读取筛选规则组声明自报的组名与规则串

    :param declaration: `FilterRuleGroupDeclaration` 实例，或插件直接交出的描述字典
    :return: (组名, 规则串) 二元组；对应字段缺失、非字符串或为空白时该位为 None
    """
    return (
        _declared_field_text(declaration, "name"),
        _declared_field_text(declaration, "rule_string"),
    )


def declaration_filter_rule_group_scope(
    declaration: Any,
) -> Tuple[Optional[str], Optional[str]]:
    """
    读取筛选规则组声明的适用范围

    :param declaration: `FilterRuleGroupDeclaration` 实例，或插件直接交出的描述字典
    :return: (适用媒体类型, 适用媒体类别) 二元组；对应字段缺失时该位为 None
    """
    return (
        _declared_field(declaration, "media_type"),
        _declared_field(declaration, "category"),
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
