"""方法表形状的共用契约校验。

模块声明与媒体数据源声明都携带一张「方法名到实现」的表，两者对表本身的要求完全
相同：非空映射、键为非空字符串、值可调用。规则收在一处，两个校验器共用同一份判定。
"""

from __future__ import annotations

from typing import Any, Mapping, Optional


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
