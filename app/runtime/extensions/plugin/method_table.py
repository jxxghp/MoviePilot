"""方法表形状的共用契约校验。

模块声明与媒体数据源声明都携带一张「方法名到实现」的表，两者对表本身的要求完全
相同：非空映射、键为非空字符串、值可调用。规则收在一处，两个校验器共用同一份判定。

`ExtensionDeclaration.capabilities` 的判定同样收在这里：它承诺的是方法名，指称对象
就是这张表，离开方法表这个字段没有可对照的东西。
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


def capability_promise_violation(
    capabilities: Any, methods: Any, *, field: str = "capabilities"
) -> Optional[str]:
    """
    校验能力承诺是否落在方法表的键集内

    承诺缺省即由方法表的键回答，因此 None 与空序列一律放行。承诺里出现方法表没有的
    名字时整条声明被拒并点名缺哪几个：那是一份宿主兑现不了的承诺，依赖它的调用方会
    按承诺挑中这条声明再落空。

    反向不作判定——方法表有、承诺没写不算违约：宿主挂载的是整张表，承诺写窄了只是
    作者自己少报了提供的能力，不影响别的声明。

    :param capabilities: 待校验的能力承诺原始值
    :param methods: 同一条声明的方法表，调用方已先行判定其形状
    :param field: 能力承诺在声明里的字段名，用于违约描述
    :return: 违反契约的描述；承诺合规时为 None
    """
    if capabilities is None:
        return None
    if isinstance(capabilities, (str, bytes)):
        return f"{field} {capabilities!r} 是单个字符串而不是方法名序列"
    if not isinstance(capabilities, (list, tuple, set, frozenset)):
        return f"{field} 必须是方法名序列，实际是 {type(capabilities).__name__}"
    promised = tuple(capabilities)
    if not promised:
        return None
    for name in promised:
        if not isinstance(name, str) or not name.strip():
            return f"{field} 中的 {name!r} 不是非空字符串"
    available = set(methods) if isinstance(methods, Mapping) else set()
    missing = sorted({name for name in promised if name not in available})
    if missing:
        return (
            f"{field} 承诺的 {missing} 不在方法表中，"
            f"当前方法表为 {sorted(available, key=str)}"
        )
    return None
