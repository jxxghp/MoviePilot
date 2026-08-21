"""插件模块声明的契约校验。

模块声明只描述方法表。归属哪一族可配置服务、按用户配置扇出多少个具名实例，由
服务实例声明承担——两件事共用一个入口会让宿主分不清该走方法名分发还是实例扇出。

「本插件是一个媒体数据源」同样不由本声明表达：数据源的展示信息与实现必须在同一条
`MediaSourceDeclaration` 里给全，否则宿主聚合不出来源列表，也无从按 source 路由。
"""

from __future__ import annotations

from typing import Any, Optional

from app.runtime.extensions.declaration import declaration_methods
from app.runtime.extensions.plugin.method_table import method_table_violation


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
