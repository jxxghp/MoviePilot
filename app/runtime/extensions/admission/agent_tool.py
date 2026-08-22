"""插件智能体工具声明的契约校验。

工具基类 ``MoviePilotTool`` 定义在 ``app.agent.tools.base``，而本模块所在的
``app.runtime`` 层依赖矩阵禁止反向引用 ``app.agent``；基类改由启动组合根经
``configure_agent_tool_base`` 注入，未注入时跳过继承项校验。
"""

from __future__ import annotations

import inspect
from typing import Any, Optional

from app.runtime.extensions.contract.declaration import declaration_agent_tool_identity, declaration_impl

# 智能体工具基类，由启动组合根注入；未注入前契约校验跳过继承项，仅校验其余各项
_agent_tool_base: Optional[type] = None


def configure_agent_tool_base(tool_base: type) -> None:
    """
    注入智能体工具基类，使契约校验能够判定实现类的真实继承关系

    :param tool_base: 智能体工具基类
    """
    global _agent_tool_base
    _agent_tool_base = tool_base


def _pydantic_field_default(impl: Any, field: str) -> Optional[str]:
    """
    读取 pydantic 模型字段的默认值

    pydantic 模型把字段默认值收进 ``model_fields``、不在类级别暴露为普通属性，
    插件直接交出工具类而不包声明对象时，只能经此读取 name/description 默认值。

    :param impl: 待读取的实现类
    :param field: 字段名
    :return: 非空默认值；取不到时为 None
    """
    model_fields = getattr(impl, "model_fields", None)
    if not isinstance(model_fields, dict):
        return None
    field_info = model_fields.get(field)
    default = getattr(field_info, "default", None)
    return default.strip() if isinstance(default, str) and default.strip() else None


def agent_tool_declaration_name(declaration: Any) -> Optional[str]:
    """
    读取智能体工具声明最终生效的工具名

    工具名优先取声明字段，声明未带时回落到实现类的 name 默认值，取值口径与契约
    校验一致。

    :param declaration: `AgentToolDeclaration` 实例，或插件直接交出的实现类
    :return: 工具名；声明与实现两侧都取不到时为 None
    """
    name, _description = declaration_agent_tool_identity(declaration)
    return name or _pydantic_field_default(declaration_impl(declaration), "name")


def agent_tool_declaration_violation(declaration: Any) -> Optional[str]:
    """
    校验智能体工具声明是否满足登记契约

    契约要求实现是类、继承自工具基类（基类未注入时跳过该项）、其抽象方法已全部
    落地、具备非空的工具名与描述、且实现了异步的 ``run`` 方法；五项中任一不满足
    都拒绝登记，不留到调用时才失败。

    :param declaration: `AgentToolDeclaration` 实例，或插件直接交出的实现类
    :return: 违反契约的描述；声明合规时为 None
    """
    try:
        impl = declaration_impl(declaration)
        name, description = declaration_agent_tool_identity(declaration)
    except Exception as error:
        return f"读取智能体工具声明出错：{error}"
    if not inspect.isclass(impl):
        return "impl 缺失或不是类"
    if _agent_tool_base is not None and not issubclass(impl, _agent_tool_base):
        return f"{impl!r} 不是工具基类 {_agent_tool_base.__name__} 的子类"
    unimplemented = getattr(impl, "__abstractmethods__", None)
    if unimplemented:
        return f"{impl!r} 未实现抽象方法：{sorted(unimplemented)}"
    name = name or _pydantic_field_default(impl, "name")
    description = description or _pydantic_field_default(impl, "description")
    if not name:
        return "未声明非空的工具名 name"
    if not description:
        return "未声明非空的工具描述 description"
    run = getattr(impl, "run", None)
    if not callable(run):
        return "缺少 run 方法"
    if not inspect.iscoroutinefunction(run):
        return "run 必须是异步方法"
    return None
