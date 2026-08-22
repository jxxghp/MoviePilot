"""插件模块声明的契约校验，以及方法表形状的共用判定。

模块声明只描述方法表。归属哪一族可配置服务、按用户配置扇出多少个具名实例，由
服务实例声明承担——两件事共用一个入口会让宿主分不清该走方法名分发还是实例扇出。

「本插件是一个媒体数据源」同样不由本声明表达：数据源的展示信息与实现必须在同一条
`MediaSourceDeclaration` 里给全，否则宿主聚合不出来源列表，也无从按 source 路由。

媒体数据源声明携带的实现表与模块声明的方法表形状相同，对表本身的要求也完全相同：
非空映射、键为非空字符串、值可调用。判定收在本模块，两个校验器共用同一份规则。
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from app.runtime.extensions.contract.declaration import declaration_methods


def method_table_violation(methods: Any, *, field: str = "methods") -> Optional[str]:
    """
    校验方法表是否满足登记契约

    :param methods: 待校验的方法表原始值
    :param field: 方法表在声明里的字段名，用于违约描述
    :return: 违反契约的描述；方法表合规时为 None
    """
    if methods is None:
        return f"{field} 缺失"
    if not isinstance(methods, Mapping):
        return f"{field} 必须是映射，实际是 {type(methods).__name__}"
    if not methods:
        return f"{field} 为空映射"
    for name, func in methods.items():
        if not isinstance(name, str) or not name.strip():
            return f"方法名 {name!r} 不是非空字符串"
        if not callable(func):
            return f"方法 {name!r} 对应的实现不可调用"
    return None


def module_declaration_violation(declaration: Any) -> Optional[str]:
    """
    校验模块声明是否满足登记契约

    契约要求方法表是非空映射、键均为非空字符串、值均可调用。三项中任一不满足都
    拒绝登记，不留到调用时才失败。声明提供的能力面即方法表的键，不另行声明。

    :param declaration: `ModuleDeclaration` 实例，或插件直接交出的方法表字典
    :return: 违反契约的描述；声明合规时为 None
    """
    try:
        methods = declaration_methods(declaration)
    except Exception as error:
        return f"读取模块声明出错：{error}"
    return method_table_violation(methods)
