"""插件远程命令声明的契约校验。

命令声明由两部分组成：一部分是数据（命令词、展示名称、分类、参数描述、附加数据），
另一部分是实现。每一条校验都对应命令链路上一处确定的失败点——命令词文法对应分发时的
精确查表与渠道菜单的批量注册，实现可调用对应命令被触发时的那次调用。放到登记时拒绝，
就不会等到用户真的敲这条命令、或整批菜单注册失败时才暴露。
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from app.runtime.extensions.contract.declaration import (
    declaration_command_data,
    declaration_command_identity,
    declaration_command_override,
    declaration_command_presentation,
    declaration_command_show,
    declaration_impl,
)
from app.schemas.command import command_word_violation


def command_declaration_violation(declaration: Any) -> Optional[str]:
    """
    校验命令声明是否满足登记契约

    契约要求：命令词非空且合命令词文法；展示名称非空（渠道菜单按它渲染按钮文案）；
    实现可调用；分类与参数描述是字符串；附加数据是字符串键的字典；菜单展示开关与接管
    内建命令的意图是布尔值。任一不满足都拒绝登记，不留到用户敲这条命令时才失败。

    接管内建命令的意图必须是布尔值而不接受真值转换：它决定同名内建命令是被接管还是
    保持生效，取值含糊会让一次笔误静默改变用户手打某个内建命令的结果。

    :param declaration: `CommandDeclaration` 实例，或插件直接交出的描述字典
    :return: 违反契约的描述；声明合规时为 None
    """
    try:
        cmd, name = declaration_command_identity(declaration)
        category, args_description = declaration_command_presentation(declaration)
        data = declaration_command_data(declaration)
        show = declaration_command_show(declaration)
        overrides_builtin = declaration_command_override(declaration)
        impl = declaration_impl(declaration)
    except Exception as error:
        return f"读取命令声明出错：{error}"
    if not cmd:
        return "未声明非空的命令词 cmd"
    violation = command_word_violation(cmd)
    if violation:
        return violation
    if not name:
        return "未声明非空的命令展示名称 name"
    if impl is declaration or not callable(impl):
        return "未声明可调用的命令实现 impl"
    for field, value in (("category", category), ("args_description", args_description)):
        if value is not None and not isinstance(value, str):
            return f"字段 {field} 必须是字符串，实际是 {type(value).__name__}"
    for field, value in (("show", show), ("overrides_builtin", overrides_builtin)):
        if value is not None and not isinstance(value, bool):
            return f"字段 {field} 必须是布尔值，实际是 {type(value).__name__}"
    return _data_violation(data)


def _data_violation(data: Any) -> Optional[str]:
    """
    校验附加数据的形状

    附加数据会随命令上下文一并交给实现，也要能作为纯数据跨出进程边界，因此只接受
    字符串键的字典。

    :param data: 声明的附加数据原始值
    :return: 违反契约的描述；取值为空或形状合法时为 None
    """
    if data is None:
        return None
    if not isinstance(data, Mapping):
        return f"字段 data 必须是字典，实际是 {type(data).__name__}"
    for key in data:
        if not isinstance(key, str):
            return f"字段 data 的键必须是字符串，实际含 {type(key).__name__}"
    return None
