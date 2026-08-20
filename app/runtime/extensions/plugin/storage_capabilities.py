"""插件存储声明的契约校验。

存储基类 ``StorageBase`` 定义在 ``app.modules._base.storage``，而本模块所在的
``app.runtime`` 层依赖矩阵禁止反向引用 ``app.modules``；子类判定改走 MRO 上的
模块与限定名比对，不依赖 import。
"""

from __future__ import annotations

import inspect
from typing import Any, Optional

from app.runtime.extensions.declaration import (
    declaration_config_component,
    declaration_config_form,
    declaration_impl,
    declaration_schema,
)
from app.runtime.extensions.plugin.config_interface import config_interface_violation

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


def storage_declaration_violation(
    declaration: Any, *, render_mode: Optional[str] = None
) -> Optional[str]:
    """
    校验存储声明是否满足登记契约

    契约要求实现是类、派生自存储基类 ``StorageBase``、其抽象方法已全部落地、
    声明了非空的存储标识；配置界面二选一——``config_form``（vuetify 组件树加
    默认数据）与 ``config_component``（vue 模式下从本扩展联邦远程加载的组件名）
    不可同时声明，给出 ``config_form`` 时形状须合法，给出 ``config_component``
    时声明方的渲染模式须为 vue。任一不满足都拒绝登记，不留到调用时才失败。

    :param declaration: `StorageDeclaration` 实例，或插件直接交出的实现类
    :param render_mode: 声明该存储的扩展当前的渲染模式；为 None 时跳过
        ``config_component`` 与渲染模式的一致性校验
    :return: 违反契约的描述；声明合规时为 None
    """
    try:
        impl = declaration_impl(declaration)
        schema = declaration_schema(declaration)
        config_form = declaration_config_form(declaration)
        config_component = declaration_config_component(declaration)
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
    return config_interface_violation(config_form, config_component, render_mode=render_mode)
