"""扩展声明里的配置契约：JSON Schema 的一个受控子集，及其两向校验。

声明能说「这个类型的配置界面长什么样」（``config_form``）之外，还要能说「这个类型
的配置是什么形状」。前者是呈现，交给前端；后者是契约，宿主据此拒绝畸形配置，跨
进程时随声明原样成为握手报文的一部分。

取 JSON Schema 而不是自定义字段描述结构，是因为契约的消费方不止一个：宿主用它拒绝
写入、前端在没有 ``config_form`` 时用它生成默认表单、换实现语言后的宿主用它做同一
件事。标准词表在三侧都有现成实现，自定义结构要各写一遍。

取子集而不是全集，是因为宿主必须**真的**能判定：``$ref``/``allOf``/``if`` 这类关键字
一旦被悄悄忽略，校验就退化成一种虚假的安全感。因此关键字集合是封闭的，出现子集之外
的关键字即判整条声明不合契约，宿主永远不会面对自己评估不了的构造。

契约描述的是类型自己的配置内容，即该族配置模型 ``config`` 字段的形状，不描述 ``name``/
``type``/``enabled`` 这类由服务族持有的外壳字段——外壳属于宿主，类型没有理由描述它。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, Dict, List, Optional, Tuple

# 契约支持的取值类型，与 JSON 的类型体系一致
_SCHEMA_TYPES: Tuple[str, ...] = (
    "string",
    "integer",
    "number",
    "boolean",
    "array",
    "object",
)

# 各取值类型面向用户的中文名称
_TYPE_LABELS: Mapping[str, str] = {
    "string": "字符串",
    "integer": "整数",
    "number": "数字",
    "boolean": "布尔值",
    "array": "列表",
    "object": "对象",
}

# 与取值类型无关、任何字段都可声明的关键字
_COMMON_KEYWORDS: frozenset = frozenset({"type", "title", "description", "default", "enum"})

# 各取值类型专属的关键字，出现在其它类型上即判为不适用
_TYPE_KEYWORDS: Mapping[str, frozenset] = {
    "string": frozenset({"minLength", "maxLength", "pattern"}),
    "integer": frozenset({"minimum", "maximum"}),
    "number": frozenset({"minimum", "maximum"}),
    "boolean": frozenset(),
    "array": frozenset({"items", "minItems", "maxItems"}),
    "object": frozenset({"properties", "required", "additionalProperties"}),
}

# 契约根只能是对象，根上允许出现的关键字
_ROOT_KEYWORDS: frozenset = frozenset(
    {"type", "title", "description", "properties", "required", "additionalProperties"}
)


def config_schema_violation(
    schema: Any, *, reserved_property_names: Sequence[str] = ()
) -> Optional[str]:
    """
    校验一份配置契约本身是否落在受支持的子集内

    契约要求：整份数据可 JSON 序列化往返、根是 ``{"type": "object"}`` 形态、全部关键字
    落在子集内且与所声明的类型相符、``required`` 列出的字段都有对应的字段契约、每个
    ``default`` 满足它自己声明的约束。任一不满足即返回描述，调用方据此拒绝整条声明。

    :param schema: 声明携带的配置契约；为 None 表示未声明契约，不产生违约
    :param reserved_property_names: 由宿主自行填入、契约不得再声明的字段名
    :return: 违反契约的描述；契约合规或未声明时为 None
    """
    if schema is None:
        return None
    if not isinstance(schema, Mapping):
        return f"config_schema {schema!r} 不是对象，无法作为配置契约"
    serializable = _serializable_violation(schema)
    if serializable:
        return serializable
    if schema.get("type") != "object":
        return f"config_schema 的根 type 必须是 \"object\"，实际为 {schema.get('type')!r}"
    unknown = sorted(set(schema) - _ROOT_KEYWORDS)
    if unknown:
        return (
            f"config_schema 的根含不受支持的关键字 {unknown}，"
            f"可用关键字为 {sorted(_ROOT_KEYWORDS)}"
        )
    reserved = set(reserved_property_names) & set(_properties_of(schema))
    if reserved:
        return (
            f"config_schema 声明了由宿主填入的字段 {sorted(reserved)}，"
            f"该字段不属于类型自己的配置内容"
        )
    return _object_schema_violation(schema, path="")


def config_value_violations(schema: Any, value: Any) -> Tuple[str, ...]:
    """
    按配置契约校验一份配置内容，列出全部不合契约之处

    一次列全而不是首个即返回：用户填错的往往不止一处，逐个报错要改一次试一次。
    取值为 None 一律视为未提供——前端清空输入框会留下 null，把它按类型不符拒绝会
    让大量合法配置写不进去；必填字段为 None 仍按缺失判定。

    :param schema: 该类型声明的配置契约；为 None 表示未声明契约，不产生任何判定
    :param value: 待校验的配置内容
    :return: 面向用户的违约描述元组；全部合规或未声明契约时为空元组
    """
    if not isinstance(schema, Mapping):
        return ()
    if value is None:
        value = {}
    if not isinstance(value, Mapping):
        return (f"配置内容应为对象，实际为{_actual(value)}",)
    return tuple(_object_value_violations(schema, value, path=""))


def _serializable_violation(schema: Mapping[str, Any]) -> Optional[str]:
    """
    校验契约能否 JSON 序列化往返

    跨进程时契约原样成为握手报文，因此它必须是纯 JSON 数据。元组、集合、非字符串
    键这类只在进程内成立的形状能被 ``json.dumps`` 蒙混过关却在往返后变形，故按
    往返结果是否与原值相等判定，而不只看能否序列化。

    :param schema: 配置契约
    :return: 违反契约的描述；可往返时为 None
    """
    try:
        restored = json.loads(json.dumps(schema, allow_nan=False))
    except (TypeError, ValueError) as error:
        return f"config_schema 不能 JSON 序列化，无法跨进程传输：{error}"
    if restored != schema:
        return "config_schema 含 JSON 序列化后会变形的数据，无法跨进程传输"
    return None


def _properties_of(schema: Mapping[str, Any]) -> Dict[str, Any]:
    """
    取出对象契约的字段表

    :param schema: 对象契约
    :return: 字段名到字段契约的映射；未声明或形状不合法时为空字典
    """
    properties = schema.get("properties")
    return dict(properties) if isinstance(properties, Mapping) else {}


def _object_schema_violation(schema: Mapping[str, Any], *, path: str) -> Optional[str]:
    """
    校验一份对象契约的字段表、必填表与附加字段开关

    :param schema: 对象契约
    :param path: 该对象在契约中的位置，根为空串
    :return: 违反契约的描述；合规时为 None
    """
    label = f"字段 {path} " if path else "config_schema 根"
    properties = schema.get("properties")
    if properties is not None and not isinstance(properties, Mapping):
        return f"{label}的 properties 必须是对象"
    additional = schema.get("additionalProperties")
    if additional is not None and not isinstance(additional, bool):
        return f"{label}的 additionalProperties 必须是布尔值"
    declared = _properties_of(schema)
    required = schema.get("required")
    if required is not None:
        if not isinstance(required, list) or not all(
            isinstance(item, str) for item in required
        ):
            return f"{label}的 required 必须是字段名列表"
        missing = sorted(set(required) - set(declared))
        if missing:
            return f"{label}的 required 列出了未声明的字段 {missing}"
    for name, field_schema in declared.items():
        if not name or not isinstance(name, str):
            return f"{label}的 properties 含非法字段名 {name!r}"
        violation = _field_schema_violation(
            field_schema, path=f"{path}.{name}" if path else name
        )
        if violation:
            return violation
    return None


def _field_schema_violation(schema: Any, *, path: str) -> Optional[str]:
    """
    校验单个字段的契约：类型、关键字取值与默认值

    :param schema: 字段契约
    :param path: 字段在契约中的位置
    :return: 违反契约的描述；合规时为 None
    """
    if not isinstance(schema, Mapping):
        return f"字段 {path} 的契约必须是对象，实际为 {schema!r}"
    field_type = schema.get("type")
    if field_type not in _SCHEMA_TYPES:
        return (
            f"字段 {path} 的 type {field_type!r} 不受支持，"
            f"可用取值为 {list(_SCHEMA_TYPES)}"
        )
    allowed = _COMMON_KEYWORDS | _TYPE_KEYWORDS[field_type]
    unknown = sorted(set(schema) - allowed)
    if unknown:
        return (
            f"字段 {path} 含不适用于 {field_type} 的关键字 {unknown}，"
            f"可用关键字为 {sorted(allowed)}"
        )
    violation = _constraint_violation(schema, field_type, path=path)
    if violation:
        return violation
    if field_type == "object":
        nested = _object_schema_violation(schema, path=path)
        if nested:
            return nested
    if field_type == "array" and schema.get("items") is not None:
        nested = _field_schema_violation(schema["items"], path=f"{path}[]")
        if nested:
            return nested
    return _default_violation(schema, path=path)


def _constraint_violation(
    schema: Mapping[str, Any], field_type: str, *, path: str
) -> Optional[str]:
    """
    校验字段契约上各约束关键字的取值是否成立

    :param schema: 字段契约
    :param field_type: 字段声明的取值类型
    :param path: 字段在契约中的位置
    :return: 违反契约的描述；合规时为 None
    """
    enum = schema.get("enum")
    if enum is not None:
        if not isinstance(enum, list) or not enum:
            return f"字段 {path} 的 enum 必须是非空列表"
        mismatched = [item for item in enum if not _matches_type(item, field_type)]
        if mismatched:
            return f"字段 {path} 的 enum 含与 type {field_type} 不符的取值 {mismatched}"
    for keyword in ("minimum", "maximum"):
        bound = schema.get(keyword)
        if bound is not None and not _matches_type(bound, "number"):
            return f"字段 {path} 的 {keyword} 必须是数字"
    if _both_present(schema, "minimum", "maximum") and schema["minimum"] > schema["maximum"]:
        return f"字段 {path} 的 minimum 大于 maximum"
    for keyword in ("minLength", "maxLength", "minItems", "maxItems"):
        bound = schema.get(keyword)
        if bound is not None and (not _matches_type(bound, "integer") or bound < 0):
            return f"字段 {path} 的 {keyword} 必须是非负整数"
    for lower, upper in (("minLength", "maxLength"), ("minItems", "maxItems")):
        if _both_present(schema, lower, upper) and schema[lower] > schema[upper]:
            return f"字段 {path} 的 {lower} 大于 {upper}"
    pattern = schema.get("pattern")
    if pattern is not None:
        if not isinstance(pattern, str):
            return f"字段 {path} 的 pattern 必须是字符串"
        try:
            re.compile(pattern)
        except re.error as error:
            return f"字段 {path} 的 pattern 不是合法正则：{error}"
    return None


def _both_present(schema: Mapping[str, Any], lower: str, upper: str) -> bool:
    """
    判断一对上下界关键字是否都已给出

    :param schema: 字段契约
    :param lower: 下界关键字名
    :param upper: 上界关键字名
    :return: 两者都给出时为 True
    """
    return schema.get(lower) is not None and schema.get(upper) is not None


def _default_violation(schema: Mapping[str, Any], *, path: str) -> Optional[str]:
    """
    校验字段默认值是否满足该字段自己声明的约束

    默认值会被前端直接填进表单、被用户原样保存，一个违反自身约束的默认值等于预置了
    一份写不进去的配置。

    :param schema: 字段契约
    :param path: 字段在契约中的位置
    :return: 违反契约的描述；无默认值或默认值合规时为 None
    """
    if schema.get("default") is None:
        return None
    violations = _value_violations(schema, schema["default"], path=path)
    if violations:
        return f"字段 {path} 的 default 不满足自身约束：{violations[0]}"
    return None


def _object_value_violations(
    schema: Mapping[str, Any], value: Mapping[str, Any], *, path: str
) -> List[str]:
    """
    按对象契约校验一份对象取值

    :param schema: 对象契约
    :param value: 待校验的对象取值
    :param path: 该对象在配置中的位置，根为空串
    :return: 违约描述列表
    """
    violations: List[str] = []
    declared = _properties_of(schema)
    required = schema.get("required")
    for name in required if isinstance(required, list) else []:
        if value.get(name) is None:
            violations.append(f"字段 {_join(path, name)} 必填，但未提供")
    for name, field_schema in declared.items():
        item = value.get(name)
        if item is None:
            continue
        violations.extend(
            _value_violations(field_schema, item, path=_join(path, name))
        )
    if schema.get("additionalProperties") is False:
        for name in value:
            if name not in declared:
                violations.append(
                    f"字段 {_join(path, str(name))} 不在该类型的配置契约中"
                )
    return violations


def _value_violations(schema: Any, value: Any, *, path: str) -> List[str]:
    """
    按字段契约校验一个取值

    类型不符时直接返回，不再追问其余约束：类型都不对，长度与范围的报错只会淹没真正
    的原因。

    :param schema: 字段契约
    :param value: 待校验的取值
    :param path: 字段在配置中的位置
    :return: 违约描述列表
    """
    if not isinstance(schema, Mapping):
        return []
    field_type = schema.get("type")
    if field_type not in _SCHEMA_TYPES:
        return []
    if not _matches_type(value, field_type):
        return [
            f"字段 {path} 应为{_TYPE_LABELS[field_type]}，实际为{_actual(value)}"
        ]
    violations: List[str] = []
    enum = schema.get("enum")
    if isinstance(enum, list) and enum and value not in enum:
        violations.append(
            f"字段 {path} 只能取 {'、'.join(str(item) for item in enum)} 之一，"
            f"实际为{_actual(value)}"
        )
    if field_type == "string":
        violations.extend(_string_violations(schema, value, path=path))
    elif field_type in ("integer", "number"):
        violations.extend(_number_violations(schema, value, path=path))
    elif field_type == "array":
        violations.extend(_array_violations(schema, value, path=path))
    elif field_type == "object":
        violations.extend(_object_value_violations(schema, value, path=path))
    return violations


def _string_violations(
    schema: Mapping[str, Any], value: str, *, path: str
) -> List[str]:
    """
    校验字符串取值的长度与格式约束

    :param schema: 字段契约
    :param value: 待校验的字符串
    :param path: 字段在配置中的位置
    :return: 违约描述列表
    """
    violations: List[str] = []
    minimum = schema.get("minLength")
    maximum = schema.get("maxLength")
    if minimum is not None and len(value) < minimum:
        violations.append(f"字段 {path} 长度不能少于 {minimum} 个字符")
    if maximum is not None and len(value) > maximum:
        violations.append(f"字段 {path} 长度不能超过 {maximum} 个字符")
    pattern = schema.get("pattern")
    if isinstance(pattern, str):
        try:
            matched = re.search(pattern, value) is not None
        except re.error:
            matched = True
        if not matched:
            violations.append(f"字段 {path} 的格式不符合要求")
    return violations


def _number_violations(
    schema: Mapping[str, Any], value: Any, *, path: str
) -> List[str]:
    """
    校验数值取值的范围约束

    :param schema: 字段契约
    :param value: 待校验的数值
    :param path: 字段在配置中的位置
    :return: 违约描述列表
    """
    violations: List[str] = []
    minimum = schema.get("minimum")
    maximum = schema.get("maximum")
    if minimum is not None and value < minimum:
        violations.append(f"字段 {path} 不能小于 {minimum}，实际为 {value}")
    if maximum is not None and value > maximum:
        violations.append(f"字段 {path} 不能大于 {maximum}，实际为 {value}")
    return violations


def _array_violations(
    schema: Mapping[str, Any], value: List[Any], *, path: str
) -> List[str]:
    """
    校验列表取值的长度约束与元素契约

    :param schema: 字段契约
    :param value: 待校验的列表
    :param path: 字段在配置中的位置
    :return: 违约描述列表
    """
    violations: List[str] = []
    minimum = schema.get("minItems")
    maximum = schema.get("maxItems")
    if minimum is not None and len(value) < minimum:
        violations.append(f"字段 {path} 至少要有 {minimum} 项")
    if maximum is not None and len(value) > maximum:
        violations.append(f"字段 {path} 最多只能有 {maximum} 项")
    items = schema.get("items")
    if isinstance(items, Mapping):
        for index, item in enumerate(value):
            violations.extend(_value_violations(items, item, path=f"{path}[{index}]"))
    return violations


def _matches_type(value: Any, field_type: str) -> bool:
    """
    判断取值是否属于契约声明的类型

    布尔值在 Python 里是整数的子类，但在 JSON 的类型体系里不是数字，因此单独排除。

    :param value: 待判定的取值
    :param field_type: 契约声明的取值类型
    :return: 类型相符时为 True
    """
    if field_type == "boolean":
        return isinstance(value, bool)
    if field_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if field_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if field_type == "string":
        return isinstance(value, str)
    if field_type == "array":
        return isinstance(value, list)
    return isinstance(value, Mapping)


def _actual(value: Any) -> str:
    """
    描述一个实际取值，供违约提示指出用户到底填了什么

    :param value: 实际取值
    :return: 含类型名与取值的中文短语
    """
    if value is None:
        return "空值"
    for field_type in ("boolean", "integer", "number", "string", "array"):
        if _matches_type(value, field_type):
            return f"{_TYPE_LABELS[field_type]} {value!r}"
    if isinstance(value, Mapping):
        return "对象"
    return f"{type(value).__name__} {value!r}"


def _join(path: str, name: str) -> str:
    """
    拼接字段在配置中的位置

    :param path: 上级位置，根为空串
    :param name: 字段名
    :return: 点分位置串
    """
    return f"{path}.{name}" if path else name
