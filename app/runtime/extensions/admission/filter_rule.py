"""插件筛选规则与筛选规则组声明的契约校验。

筛选规则是数据不是实现，因此契约校验的对象不是类型与抽象方法，而是「这份数据被
规则引擎消费时会不会炸」。每一条校验都对应规则引擎里一处确定的失败点：正则编译、
数值转换、规则串解析。放到登记时拒绝，就不会等到每颗种子逐条匹配时才失败。
"""

from __future__ import annotations

import re
from typing import Any, Optional

from app.runtime.extensions.contract.declaration import (
    declaration_filter_rule_conditions,
    declaration_filter_rule_group_identity,
    declaration_filter_rule_group_scope,
    declaration_filter_rule_identity,
)
from app.schemas.rule import (
    RULE_CONDITION_FIELDS,
    RULE_ID_GRAMMAR_HINT,
    is_valid_rule_id,
    rule_string_violation,
)


def filter_rule_declaration_violation(declaration: Any) -> Optional[str]:
    """
    校验筛选规则声明是否满足登记契约

    契约要求：规则标识非空且合规则ID文法（标识会作为原子进入规则串语法，不合文法的
    标识在登记时看不出问题，要到用户把它写进规则组时才解析失败）；展示名称非空；
    条件字段都是字符串；至少给出一个条件；正则能编译、数值区间能转换。任一不满足都
    拒绝登记，不留到逐条匹配时才失败。

    :param declaration: `FilterRuleDeclaration` 实例，或插件直接交出的描述字典
    :return: 违反契约的描述；声明合规时为 None
    """
    try:
        rule_id, name = declaration_filter_rule_identity(declaration)
        conditions = declaration_filter_rule_conditions(declaration)
    except Exception as error:
        return f"读取筛选规则声明出错：{error}"
    if not rule_id:
        return "未声明非空的规则标识 rule_id"
    if not is_valid_rule_id(rule_id):
        return f"规则标识 {rule_id!r} 不合文法：{RULE_ID_GRAMMAR_HINT}"
    if not name:
        return "未声明非空的规则展示名称 name"
    for field in RULE_CONDITION_FIELDS:
        value = conditions.get(field)
        if value is not None and not isinstance(value, str):
            return f"条件字段 {field} 必须是字符串，实际是 {type(value).__name__}"
    if not any((conditions.get(field) or "").strip() for field in RULE_CONDITION_FIELDS):
        return f"未声明任何匹配条件，至少要给出 {'/'.join(RULE_CONDITION_FIELDS)} 之一"
    for field in ("include", "exclude"):
        violation = _regex_violation(field, conditions.get(field))
        if violation:
            return violation
    return (
        _size_range_violation(conditions.get("size_range"))
        or _seeders_violation(conditions.get("seeders"))
        or _publish_time_violation(conditions.get("publish_time"))
    )


def filter_rule_group_declaration_violation(declaration: Any) -> Optional[str]:
    """
    校验筛选规则组声明是否满足登记契约

    契约要求：组名非空（四个使用场景保存的就是组名，用户按它引用整组规则）；规则串
    非空且能被规则表达式语法解析——括号配对、优先级层级非空、每个原子都合规则ID文法；
    适用范围字段都是字符串。任一不满足都拒绝登记。

    规则串引用的规则标识是否存在不在校验范围内：规则组可以引用内建规则、用户自定义
    规则，或另一个插件提供的规则，这些在登记这条声明时未必都已就位。

    :param declaration: `FilterRuleGroupDeclaration` 实例，或插件直接交出的描述字典
    :return: 违反契约的描述；声明合规时为 None
    """
    try:
        name, rule_string = declaration_filter_rule_group_identity(declaration)
        media_type, category = declaration_filter_rule_group_scope(declaration)
    except Exception as error:
        return f"读取筛选规则组声明出错：{error}"
    if not name:
        return "未声明非空的规则组名称 name"
    if not rule_string:
        return "未声明非空的规则串 rule_string"
    violation = rule_string_violation(rule_string)
    if violation:
        return violation
    for field, value in (("media_type", media_type), ("category", category)):
        if value is not None and not isinstance(value, str):
            return f"适用范围字段 {field} 必须是字符串，实际是 {type(value).__name__}"
    return None


def _regex_violation(field: str, value: Optional[str]) -> Optional[str]:
    """
    校验正则条件能否编译

    :param field: 条件字段名
    :param value: 条件取值
    :return: 违反契约的描述；取值为空或能编译时为 None
    """
    if not value:
        return None
    try:
        re.compile(value)
    except re.error as error:
        return f"条件字段 {field} 的正则 {value!r} 无法编译：{error}"
    return None


def _size_range_violation(value: Optional[str]) -> Optional[str]:
    """
    校验大小范围条件的取值形状

    :param value: 大小范围取值
    :return: 违反契约的描述；取值为空或形状合法时为 None
    """
    if not value:
        return None
    text = value.strip()
    hint = "size_range 形如 1024-4096、>1024 或 <4096，单位为 MB"
    if "-" in text:
        parts = text.split("-")
        if len(parts) != 2:
            return f"条件字段 {hint}，实际是 {value!r}"
        return None if _all_floats(parts) else f"条件字段 {hint}，实际是 {value!r}"
    if text.startswith((">", "<")):
        return None if _all_floats([text[1:]]) else f"条件字段 {hint}，实际是 {value!r}"
    return f"条件字段 {hint}，实际是 {value!r}"


def _seeders_violation(value: Optional[str]) -> Optional[str]:
    """
    校验做种人数条件的取值形状

    :param value: 做种人数取值
    :return: 违反契约的描述；取值为空或为整数字符串时为 None
    """
    if not value:
        return None
    if not value.strip().isdigit():
        return f"条件字段 seeders 必须是整数，实际是 {value!r}"
    return None


def _publish_time_violation(value: Optional[str]) -> Optional[str]:
    """
    校验发布时间条件的取值形状

    :param value: 发布时间取值
    :return: 违反契约的描述；取值为空或形状合法时为 None
    """
    if not value:
        return None
    parts = value.strip().split("-")
    if len(parts) > 2 or not _all_floats(parts):
        return f"条件字段 publish_time 形如 60 或 60-1440，单位为分钟，实际是 {value!r}"
    return None


def _all_floats(parts: list) -> bool:
    """
    判断字符串序列是否都能转换成浮点数

    :param parts: 待判定的字符串序列
    :return: 全部能转换且序列非空时为 True
    """
    if not parts:
        return False
    for part in parts:
        try:
            float(part.strip())
        except (TypeError, ValueError):
            return False
    return True
