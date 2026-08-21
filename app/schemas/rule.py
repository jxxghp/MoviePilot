import re
from typing import Any, List, Optional

from pydantic import BaseModel, Field

from app.schemas.common import JsonData


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


class FilterRuleLayer(BaseModel):
    """一条筛选规则或规则组在某一层的来源。

    三层的合并次序是内置 < 插件 < 用户，用户自定义永远赢；插件那一层还要指出是哪个
    插件的哪个分身，否则用户看到「来自插件」也不知道该去停用谁。
    """

    # 来源层：builtin 内置、plugin 插件、user 用户自定义
    layer: str
    # 插件实例键，形如 DemoPlugin@alt；内置与用户自定义层为 None
    owner: Optional[str] = None
    # 插件标识；内置与用户自定义层为 None
    extension_id: Optional[str] = None
    # 插件分身标识；内置与用户自定义层为 None
    instance_id: Optional[str] = None


class FilterRuleConflict(BaseModel):
    """一个标识被多个插件同时声明而使插件声明整体失效的呈现。

    规则是数据不是实现，两个插件各自的同名规则只是两套互不相干的语义争同一个名字，
    宿主无从裁决谁对，因此双方一并失效。用户据此知道该让哪一方改标识或停用谁。
    """

    # 声明该标识的插件标识，已排序
    plugins: List[str] = Field(default_factory=list)
    # 与 plugins 一一对应的插件实例键
    owners: List[str] = Field(default_factory=list)


class FilterRuleOrigin(BaseModel):
    """一个筛选规则标识或规则组名在运行期规则集中的来源分层。"""

    # 规则标识或规则组名
    id: str
    # 标识种类：rule 筛选规则、rule_group 筛选规则组
    kind: str
    # 该标识当前是否出现在运行期规则集中
    effective: bool = False
    # 交出当前生效定义的那一层
    source: Optional[FilterRuleLayer] = None
    # 被上层压住、当前不生效的下层来源，按内置到插件的次序排列
    shadowed: List[FilterRuleLayer] = Field(default_factory=list)
    # 该标识的插件声明因跨插件同名而整体失效时的详情
    conflict: Optional[FilterRuleConflict] = None
    # 当前生效的定义内容，不生效时为 None
    definition: JsonData = None


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
