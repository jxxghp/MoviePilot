"""插件名称解析器声明的契约校验。

解析环没有基类可继承——它是一个「收一份请求、交一份结果」的函数，因此契约按
调用形状判定：可调用、能接受单个位置参数、不是协程函数。识别是同步热路径，
协程实现会在调用点变成一个永不 await 的对象，必须在登记时就拒掉。
"""

from __future__ import annotations

import inspect
from typing import Any, Optional

from app.runtime.extensions.contract.declaration import (
    MetaParserDeclaration,
    declaration_impl,
    declaration_meta_parser_identity,
    declaration_meta_parser_priority,
)
from app.runtime.extensions.registry.meta_parser import (
    META_PARSER_ID_RE,
    is_meta_parser_id,
)

# 内省解析环调用签名时填入的占位实参，签名绑定只匹配形参不读取实参内容
_REQUEST_PROBE_ARGUMENT = object()


def meta_parser_declaration_violation(declaration: Any) -> Optional[str]:
    """
    校验名称解析器声明是否满足登记契约

    契约要求声明是 `MetaParserDeclaration` 实例、解析器标识合法、展示名称非空、
    ``priority`` 是整数、``impl`` 可调用且能接受单个位置参数且不是协程函数。任一
    不满足都拒绝登记，不留到识别时才失败。

    :param declaration: `MetaParserDeclaration` 实例
    :return: 违反契约的描述；声明合规时为 None
    """
    if not isinstance(declaration, MetaParserDeclaration):
        return f"{declaration!r} 不是 MetaParserDeclaration 实例"
    try:
        impl = declaration_impl(declaration)
        parser_id, name = declaration_meta_parser_identity(declaration)
        priority = declaration_meta_parser_priority(declaration)
    except Exception as error:
        return f"读取名称解析器声明出错：{error}"
    if not parser_id:
        return "未声明非空的解析器标识 parser_id"
    if not is_meta_parser_id(parser_id):
        return f"parser_id {parser_id!r} 不合法，须匹配 {META_PARSER_ID_RE.pattern}"
    if not name:
        return "未声明非空的展示名称 name"
    if not isinstance(priority, int) or isinstance(priority, bool):
        return f"priority {priority!r} 不是整数，无法作为默认顺序"
    return _invoke_violation(impl)


def _invoke_violation(impl: Any) -> Optional[str]:
    """
    校验解析环能否被宿主按 ``impl(请求对象)`` 同步调用

    :param impl: 声明携带的解析环实现
    :return: 违反契约的描述；调用形状成立时为 None
    """
    if impl is None or not callable(impl):
        return "impl 缺失或不可调用，宿主无从执行该解析环"
    target = impl if inspect.isroutine(impl) else getattr(impl, "__call__", impl)
    if inspect.iscoroutinefunction(target):
        return f"{impl!r} 是协程函数，名称识别是同步链路，无法执行"
    try:
        signature = inspect.signature(impl)
    except (TypeError, ValueError) as error:
        return f"{impl!r} 的调用签名无法内省：{error}"
    try:
        signature.bind(_REQUEST_PROBE_ARGUMENT)
    except TypeError as error:
        return f"{impl!r} 的调用签名不接受单个位置参数：{error}"
    return None
