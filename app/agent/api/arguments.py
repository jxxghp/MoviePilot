"""按已生成的 API 输入合同规范实际请求，使相同写入使用稳定参数身份。"""

from copy import deepcopy
from typing import Any

from pydantic import TypeAdapter, ValidationError

from app.agent.policy.api import resolve_api_route

_SCALAR_ADAPTERS = {
    "boolean": TypeAdapter(bool),
    "integer": TypeAdapter(int),
    "number": TypeAdapter(float),
    "string": TypeAdapter(str),
}
_MISSING = object()


def _resolve_schema(schema: dict[str, Any], definitions: dict[str, Any]) -> dict[str, Any]:
    """只展开缓存合同的本地引用，拒绝循环引用和任何远程 schema。"""
    seen = set()
    while "$ref" in schema:
        reference = schema["$ref"]
        if not isinstance(reference, str) or not reference.startswith("#/$defs/") or reference in seen:
            raise ValueError("API 参数合同引用无效")
        seen.add(reference)
        name = reference.removeprefix("#/$defs/").replace("~1", "/").replace("~0", "~")
        target = definitions.get(name)
        if not isinstance(target, dict):
            raise ValueError("API 参数合同引用缺失")
        schema = {**target, **{key: value for key, value in schema.items() if key != "$ref"}}
    return schema


def _value_type(value: Any) -> str:
    """按 JSON 原始类型选择联合分支，布尔值不能误当成整数。"""
    return {
        type(None): "null", bool: "boolean", int: "integer", float: "number",
        str: "string", dict: "object", list: "array",
    }.get(type(value), "")


def _normalize_union(value: Any, schema: dict[str, Any], definitions: dict[str, Any], depth: int) -> Any:
    """优先选择原始类型匹配的联合，歧义时不猜测不同分支的默认值。"""
    alternatives = [_resolve_schema(item, definitions) for item in schema.get("oneOf", schema.get("anyOf", []))]
    exact = [item for item in alternatives if item.get("type") == _value_type(value)]
    candidates = []
    for option in exact or alternatives:
        try:
            candidates.append(_normalize_value(value, option, definitions, depth + 1))
        except (TypeError, ValueError):
            continue
    if not candidates:
        raise ValueError("API 参数不符合联合类型合同")
    if all(candidate == candidates[0] for candidate in candidates):
        return candidates[0]
    # 同一输入可合法落入多个不同对象分支时保留原值，避免合并不同业务意图。
    return deepcopy(value)


def _normalize_object(value: dict[str, Any], schema: dict[str, Any], definitions: dict[str, Any], depth: int) -> dict[str, Any]:
    """按模型字段补明确默认值；自由字典保留原内容，forbid 额外字段明确拒绝。"""
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return deepcopy(value)
    extra = schema.get("additionalProperties")
    unknown = set(value) - set(properties)
    if unknown and extra is False:
        raise ValueError("API 参数包含合同未声明的字段")
    normalized = {key: deepcopy(value[key]) for key in unknown} if extra is True or isinstance(extra, dict) else {}
    required = schema.get("required", [])
    for key, declaration in properties.items():
        field_schema = _resolve_schema(declaration, definitions)
        current = value.get(key, field_schema.get("default", _MISSING))
        if current is _MISSING:
            if key in required:
                raise ValueError("API 参数缺少必需字段")
            continue
        normalized[key] = _normalize_value(current, field_schema, definitions, depth + 1)
    return normalized


def _normalize_value(value: Any, declaration: dict[str, Any], definitions: dict[str, Any], depth: int = 0) -> Any:
    """递归规范有限深度的 JSON 值，类型转换与实际请求共用同一份参数。"""
    if depth > 32:
        raise ValueError("API 参数嵌套过深")
    schema = _resolve_schema(declaration, definitions)
    if "anyOf" in schema or "oneOf" in schema:
        return _normalize_union(value, schema, definitions, depth)
    kind = schema.get("type")
    # JSON 布尔值不是数字；尤其不能把路径中的 true 改成 ID 1 后执行写入。
    if kind in {"integer", "number"} and isinstance(value, bool):
        raise ValueError("API 数值参数不能使用布尔值")
    if kind == "object":
        if not isinstance(value, dict):
            raise ValueError("API 参数必须是对象")
        return _normalize_object(value, schema, definitions, depth)
    if kind == "array":
        if not isinstance(value, list):
            raise ValueError("API 参数必须是数组")
        return [_normalize_value(item, schema.get("items", {}), definitions, depth + 1) for item in value]
    if kind == "null" and value is not None:
        raise ValueError("API 参数必须为空值")
    adapter = _SCALAR_ADAPTERS.get(kind) if isinstance(kind, str) else None
    if adapter is not None:
        value = adapter.validate_python(value)
    if "const" in schema and value != schema["const"]:
        raise ValueError("API 参数常量不匹配")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError("API 参数枚举值无效")
    return deepcopy(value)


def canonical_api_arguments(arguments: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """先采用执行器真实 GET 参数投影，再按唯一 operation 合同规范请求。"""
    operation_id = arguments.get("operation_id")
    branch = next((item for item in schema.get("oneOf", [])
                   if item.get("properties", {}).get("operation_id", {}).get("const") == operation_id), None)
    route = resolve_api_route(str(operation_id or ""))
    if branch is None or route is None:
        raise ValueError("API 操作没有规范参数合同")
    values = deepcopy(arguments)
    if route.method == "GET" and isinstance(values.get("body"), dict):
        values["query"] = {**(values.get("query") or {}), **values.pop("body")}
    properties = branch.get("properties", {})
    # 网关公共 schema 的空容器只代表没有参数，不应被 operation 分支当作额外字段。
    for field in ("path_params", "query", "body"):
        if field not in properties and values.get(field) in (None, {}):
            values.pop(field, None)
        elif field in {"path_params", "query"} and field in properties:
            values.setdefault(field, {})
    try:
        normalized = _normalize_object(values, branch, schema.get("$defs", {}), 0)
    except ValidationError as error:
        raise ValueError("API 参数类型不符合输入合同") from error
    # ToolNode 会按网关公共 schema 注入这些缺省字段，指纹与 handler 实参保持相同。
    for key, default in (("path_params", {}), ("query", {}), ("body", None)):
        normalized.setdefault(key, default)
    return normalized
