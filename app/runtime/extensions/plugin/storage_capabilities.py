"""插件存储声明的契约校验。

存储基类 ``StorageBase`` 定义在 ``app.modules._base.storage``，而本模块所在的
``app.runtime`` 层依赖矩阵禁止反向引用 ``app.modules``；子类判定改走 MRO 上的
模块与限定名比对，不依赖 import。
"""

from __future__ import annotations

import inspect
from typing import Any, Optional

from app.runtime.extensions.declaration import declaration_impl, declaration_schema

# 存储基类的模块与限定名，用于在不引入反向依赖的前提下判定实现类的真实继承关系
_STORAGE_BASE_QUALIFIED_NAME = "app.modules._base.storage.StorageBase"


def _implements_storage_base(impl: Any) -> bool:
    """
    判断实现类是否派生自存储基类

    :param impl: 待判定的实现类
    :return: MRO 中存在与存储基类同源的类时为 True
    """
    for klass in getattr(impl, "__mro__", ()):
        if f"{klass.__module__}.{klass.__qualname__}" == _STORAGE_BASE_QUALIFIED_NAME:
            return True
    return False


def storage_declaration_violation(declaration: Any) -> Optional[str]:
    """
    校验存储声明是否满足登记契约

    契约要求实现是类、派生自存储基类 ``StorageBase``、其抽象方法已全部落地、
    且声明了非空的存储标识；四项中任一不满足都拒绝登记，不留到调用时才失败。

    :param declaration: `StorageDeclaration` 实例，或插件直接交出的实现类
    :return: 违反契约的描述；声明合规时为 None
    """
    try:
        impl = declaration_impl(declaration)
        schema = declaration_schema(declaration)
    except Exception as error:
        return f"读取存储声明出错：{error}"
    if not inspect.isclass(impl):
        return "impl 缺失或不是类"
    if not _implements_storage_base(impl):
        return f"{impl!r} 不是存储基类 StorageBase 的子类"
    unimplemented = getattr(impl, "__abstractmethods__", None)
    if unimplemented:
        return f"{impl!r} 未实现抽象方法：{sorted(unimplemented)}"
    if not schema:
        return "未声明非空的存储标识 schema"
    return None
