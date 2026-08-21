"""插件工作流动作声明的契约校验。"""

from __future__ import annotations

import inspect
from typing import Any, Mapping, Optional

from app.runtime.extensions.declaration import (
    declaration_action_identity,
    declaration_action_impl,
    declaration_action_kwargs,
    declaration_service_instance_requirement,
)
from app.runtime.extensions.service_instance_requirement import (
    service_instance_requirement_violation,
)


def action_declaration_violation(declaration: Any) -> Optional[str]:
    """
    校验工作流动作声明是否满足登记契约

    契约要求实现可调用且能接收首个位置参数（工作流固定以 ActionContext 实例
    作为首个实参调用）、声明非空的动作标识 action_id 与展示名称 name；声明了
    kwargs 时须是映射。四项中任一不满足都拒绝登记，不留到调用时才失败。

    声明了 requires_service_instance 时只判它的形状，不判该能力标签有没有登记成
    服务族；判据见 `app.runtime.extensions.service_instance_requirement`。

    :param declaration: `ActionDeclaration` 实例，或插件直接交出的描述字典
    :return: 违反契约的描述；声明合规时为 None
    """
    try:
        impl = declaration_action_impl(declaration)
        action_id, name = declaration_action_identity(declaration)
        kwargs = declaration_action_kwargs(declaration)
        requirement = declaration_service_instance_requirement(declaration)
    except Exception as error:
        return f"读取工作流动作声明出错：{error}"
    if not callable(impl):
        return "实现函数缺失或不可调用"
    if not _accepts_leading_positional(impl):
        return f"{impl!r} 必须能接收首个位置参数 context"
    if not action_id:
        return "未声明非空的动作标识 action_id"
    if not name:
        return "未声明非空的动作展示名称 name"
    if kwargs is not None and not isinstance(kwargs, Mapping):
        return "kwargs 必须是映射"
    return service_instance_requirement_violation(requirement)


def _accepts_leading_positional(impl: Any) -> bool:
    """
    判断可调用对象是否能接收首个位置参数

    :param impl: 待判定的可调用对象
    :return: 能接收至少一个位置参数，或签名无法内省时为 True（内省失败不构成违约）
    """
    try:
        signature = inspect.signature(impl)
    except (TypeError, ValueError):
        return True
    for parameter in signature.parameters.values():
        if parameter.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.VAR_POSITIONAL,
        ):
            return True
    return False
