"""扩展声明式注册的声明载体。

扩展经 ``provides_*`` 钩子交出的是声明而不是裸实现：声明的数据字段描述「提供了
什么」，``impl`` 字段携带「用什么提供」。这样拆分是为了让声明面在宿主换实现语言、
扩展改为独立进程时仍然成立——届时 ``impl`` 不参与序列化，其余字段原样成为握手
报文，契约校验从内省对象退化为校验声明数据。

判据见 docs/plugin-extension-architecture.md。
"""

from dataclasses import dataclass
from typing import Any, Optional, Tuple


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

    :param schema: 存储标识，例如 u115、alipan
    """

    schema: str = ""


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
