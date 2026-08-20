import re
from typing import Any, Optional

from pydantic import BaseModel


class CustomRule(BaseModel):
    """
    自定义规则项
    """
    # 规则ID
    id: Optional[str] = None
    # 名称
    name: Optional[str] = None
    # 包含
    include: Optional[str] = None
    # 排除
    exclude: Optional[str] = None
    # 大小范围（MB）
    size_range: Optional[str] = None
    # 最少做种人数
    seeders: Optional[str] = None
    # 发布时间
    publish_time: Optional[str] = None


class FilterRuleGroup(BaseModel):
    """
    过滤规则组
    """
    # 名称
    name: Optional[str] = None
    # 规则串
    rule_string: Optional[str] = None
    # 适用类媒体类型 None-全部 电影/电视剧
    media_type: Optional[str] = None
    # 适用媒体类别 None-全部 对应二级分类
    category: Optional[str] = None


# 一条规则项承载匹配条件的字段名，规则至少要给出其中之一才有筛选意义
RULE_CONDITION_FIELDS = ("include", "exclude", "size_range", "seeders", "publish_time")

# 规则ID的合法形状，与 `app.domain.filterrule.RuleParser` 的原子文法
# ``Combine(Word(alphas, alphanums) | (Word(nums) + Word(alphas, alphanums)))`` 等价：
# 可选的前导数字之后必须出现一个字母，其余位置为字母或数字。规则ID会作为原子进入
# 规则串的语法，不合本形状的ID在登记时看不出问题，要到用户把它写进规则组时才解析失败，
# 因此文法收在这里供领域解析器与扩展契约校验共用一份。
_RULE_ID_RE = re.compile(r"^[0-9]*[A-Za-z][A-Za-z0-9]*$")

# 规则ID文法的说明文案，供各处校验给出一致的违约描述
RULE_ID_GRAMMAR_HINT = (
    "规则ID只能由字母和数字组成，且必须以字母开头或形如「数字+字母」开头"
    "（例如 BLU、4K、1080P），不能含下划线、连字符、空格等其它字符，也不能是纯数字"
)

# 规则串中的操作符与优先级分隔符，其余连续字符段即规则ID原子
_RULE_STRING_OPERATORS = re.compile(r"[!&|()]")


def is_valid_rule_id(rule_id: Any) -> bool:
    """
    判断规则ID能否作为原子被规则串语法解析

    :param rule_id: 待判定的规则ID
    :return: 能被解析时为 True；非字符串、为空或含非法字符时为 False
    """
    return isinstance(rule_id, str) and bool(_RULE_ID_RE.match(rule_id))


def rule_string_violation(rule_string: Any) -> Optional[str]:
    """
    校验规则串能否被规则表达式语法解析

    逐层校验而不调用解析器：解析器在领域层，规则串的合法性判定要同时供领域层与
    扩展契约校验取用。校验覆盖三处会让解析在使用时才失败的输入——括号不配对、
    优先级层级为空、原子不合规则ID文法。

    :param rule_string: 待校验的规则串
    :return: 违反语法的描述；规则串合法时为 None
    """
    if not isinstance(rule_string, str) or not rule_string.strip():
        return "规则串缺失或为空"
    depth = 0
    for char in rule_string:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                return "规则串括号不配对"
    if depth:
        return "规则串括号不配对"
    for level in rule_string.split(">"):
        if not level.strip():
            return "规则串存在空的优先级层级"
        atoms = [atom.strip() for atom in _RULE_STRING_OPERATORS.split(level)]
        atoms = [atom for atom in atoms if atom]
        if not atoms:
            return f"优先级层级 {level.strip()!r} 不含任何规则ID"
        for atom in atoms:
            if not is_valid_rule_id(atom):
                return f"规则串中的 {atom!r} 不合规则ID文法：{RULE_ID_GRAMMAR_HINT}"
    return None
