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
