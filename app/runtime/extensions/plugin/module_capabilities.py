"""插件模块声明的契约校验。

模块声明只描述方法表。归属哪一族可配置服务、按用户配置扇出多少个具名实例，由
服务实例声明承担——两件事共用一个入口会让宿主分不清该走方法名分发还是实例扇出。
"""

from __future__ import annotations

from typing import Any, Optional

from app.runtime.extensions.declaration import declaration_methods


def module_declaration_violation(declaration: Any) -> Optional[str]:
    """
    校验模块声明是否满足登记契约

    契约要求方法表是非空映射、键均为非空字符串、值均可调用。三项中任一不满足都
    拒绝登记，不留到调用时才失败。

    :param declaration: `ModuleDeclaration` 实例，或插件直接交出的方法表字典
    :return: 违反契约的描述；声明合规时为 None
    """
    try:
        methods = declaration_methods(declaration)
    except Exception as error:
        return f"读取模块声明出错：{error}"
    if not methods:
        return "methods 缺失或为空映射"
    for name, func in methods.items():
        if not isinstance(name, str) or not name.strip():
            return f"方法名 {name!r} 不是非空字符串"
        if not callable(func):
            return f"方法 {name!r} 对应的实现不可调用"
    return None
