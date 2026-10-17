"""
外部 MCP ``tools/list`` 的 schema 预算投影（显式 opt-in）。

主流 MCP 客户端会对工具 schema 做有损压缩。以 Codex 为例，规范化后超过 5000 字节的
schema 会依次执行「删除 description → 丢弃 ``$defs`` → 折叠深层复杂对象 → 移除组合
关键字」，其中 ``oneOf``/``anyOf``/``allOf`` 所在的节点会被整段替换为 ``{}``。
``moviepilot_api`` 等工具的完整合同远超该预算，且组合分支位于根节点，压缩后客户端
只会拿到一个没有 ``type`` 的空对象；严格校验的上游随后以
``schema must be a JSON Schema of 'type: "object"', got 'type: null'`` 拒绝整个请求，
使一次会话里所有工具调用都失败。

本模块提供显式 opt-in 的紧凑投影（``X-MCP-Schema-Mode: compact``）：把序列化后超过
客户端压缩阈值的工具 schema 投影为顶层对象合同——保留 ``type``/``required`` 与顶层
属性（含 operation/action 枚举），去掉各级组合关键字、``$defs`` 与指向它们的失效
本地引用，并把引用已删除分支的说明改写为不依赖分支的直接描述（无法改写的整条移除）。
投影是 best-effort：它不承诺把结果压到阈值以内（``moviepilot_api`` 保留全部 231 个
operation 的枚举本身就约占 5 KB），而是保证根节点不再是组合节点，客户端的后续压缩
不会再把整份 schema 替换为空对象。逐 operation 的完整合同继续由
``GET /api/v1/mcp/tools/{tool_name}/schema`` 与调用失败回执提供，调用侧校验行为不变；
默认（``full``）模式不投影，保持既有完整合同。
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Mapping, Optional
from urllib.parse import unquote

# Codex 等客户端对工具 schema 触发逐级压缩的字节阈值；超过时 compact 模式才会投影。
MCP_CLIENT_SCHEMA_COMPACTION_BYTES = 5000

# 客户端压缩会把这些组合关键字所在节点整段替换为空对象，投影时提前删除。
COMPOSITION_KEYWORDS = ("oneOf", "anyOf", "allOf")

# 定义表在客户端压缩中会被丢弃，投影时不再保留无法解析的本地引用。
DEFINITION_KEYWORDS = ("$defs", "definitions")

# 投影会删除的关键字：指向它们的本地引用在投影后无法解析，必须一并去掉。
PROJECTED_KEYWORDS = (*COMPOSITION_KEYWORDS, *DEFINITION_KEYWORDS)

# 本地引用关键字；与 PROJECTED_KEYWORDS 组合判断指针是否已经失效。
REFERENCE_KEYWORDS = ("$ref", "$dynamicRef")

# 只遍历 schema 位置，避免把属性名、默认值或枚举中的同名业务字段当作关键字。
SCHEMA_MAPPING_KEYWORDS = {"properties", "patternProperties", "dependentSchemas", "dependencies"}
SCHEMA_VALUE_KEYWORDS = {
    "items", "prefixItems", "additionalItems", "additionalProperties",
}

# 投影标记：客户端与测试据此识别本次返回的是紧凑投影而非完整合同。
PROJECTION_MARKER = "x-moviepilot-schema-projection"

# 投影会删除根级组合分支，引用这些分支的说明必须同步改写为直接描述。
DESCRIPTION_REWRITES = {
    '选择与 operation_id 匹配的 oneOf 分支，仅发送该分支声明的字段。集合分支还通过 x-moviepilot-collection 说明额外返回的集合元数据。': (
        '选择 operation_id，仅发送该操作声明的字段；集合操作还通过 x-moviepilot-collection 说明额外返回的集合元数据。'
    ),
    '白名单中的精确操作标识，用于选择对应 oneOf 分支。': (
        '白名单中的精确操作标识。'
    ),
    '仅替换选定操作固定路由占位符的资源身份；名称和类型必须匹配对应 oneOf 分支。': (
        '仅替换选定操作固定路由占位符的资源标识；名称和类型以该操作的契约为准。'
    ),
    '通过查询字符串发送的筛选和控制值；名称、类型、默认值和枚举必须匹配所选操作的 oneOf 分支。': (
        '通过查询字符串发送的筛选和控制值；名称、类型、默认值和枚举以该操作的契约为准。'
    ),
    '选定操作的请求值；必须匹配对应 oneOf 分支，标量与对象请求体不能互换。': (
        '选定操作的请求值；标量与对象请求体不能互换。'
    ),
    # 兼容缓存或外部客户端仍持有的旧英文元数据；键是需要精确匹配的历史输入。
    (
        'Select the oneOf branch matching operation_id and send exactly its documented fields. Collectio'
        'n branches also describe their additive output metadata in x-moviepilot-collection.'
    ): (
        '选择 operation_id，仅发送该操作声明的字段；集合操作还通过 x-moviepilot-collection 说明额外返回的集合元数据。'
    ),
    'Exact allowlisted MoviePilot operation ID selecting this oneOf branch.': (
        '白名单中的精确操作标识。'
    ),
    (
        "Resource identities inserted only into the selected operation's fixed route placeholders. Use t"
        'he exact names and types in its oneOf branch.'
    ): (
        '仅替换选定操作固定路由占位符的资源标识；名称和类型以该操作的契约为准。'
    ),
    (
        'Filters and control values sent in the query string. Use the exact names, types, defaults, and '
        "enums in the selected operation's oneOf branch."
    ): (
        '通过查询字符串发送的筛选和控制值；名称、类型、默认值和枚举以该操作的契约为准。'
    ),
    (
        'Request value for the selected operation. Match the exact operation oneOf branch; scalar and ob'
        'ject request bodies are not interchangeable.'
    ): (
        '选定操作的请求值；标量与对象请求体不能互换。'
    ),
}


def serialized_schema_bytes(schema: Any) -> int:
    """
    返回 schema 紧凑序列化后的字节数；无法序列化时按超出阈值处理。
    """
    try:
        payload = json.dumps(schema, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError):
        return MCP_CLIENT_SCHEMA_COMPACTION_BYTES + 1
    return len(payload.encode("utf-8"))


def schema_within_client_budget(schema: Any) -> bool:
    """
    判断 schema 是否在客户端压缩阈值以内。
    """
    return serialized_schema_bytes(schema) <= MCP_CLIENT_SCHEMA_COMPACTION_BYTES


def _rewrite_description(description: str) -> Optional[str]:
    """
    改写引用已删除组合分支的说明；无法改写的引用直接移除。
    """
    rewrite = DESCRIPTION_REWRITES.get(description)
    if rewrite is not None:
        return rewrite
    if any(keyword in description for keyword in COMPOSITION_KEYWORDS):
        return None
    return description


def _targets_projected_keyword(pointer: Any, removed_paths: set[tuple[str, ...]]) -> bool:
    """
    按实际删除位置识别失效 JSON Pointer，同时保留名称恰好为关键字的属性引用。
    """
    if not isinstance(pointer, str) or not pointer.startswith("#/"):
        return False
    parts = tuple(part.replace("~1", "/").replace("~0", "~") for part in unquote(pointer[2:]).split("/"))
    return any(parts[:length] in removed_paths for length in range(1, len(parts) + 1))


def _project_schema_node(
    node: Any,
    path: tuple[str, ...],
    removed_paths: set[tuple[str, ...]],
    references: list[tuple[dict[str, Any], str, Any]],
) -> Any:
    """
    只递归 schema 位置，并记录删除位置及引用，供整棵投影完成后清理前向引用。
    """
    if isinstance(node, list):
        return [
            _project_schema_node(item, (*path, str(index)), removed_paths, references)
            for index, item in enumerate(node)
        ]
    if not isinstance(node, Mapping):
        return deepcopy(node)
    projected: dict[str, Any] = {}
    for key, value in node.items():
        if not isinstance(key, str):
            continue
        child_path = (*path, key)
        if key in PROJECTED_KEYWORDS:
            removed_paths.add(child_path)
            continue
        if key == "description" and isinstance(value, str):
            rewritten = _rewrite_description(value)
            if rewritten is not None:
                projected[key] = rewritten
            continue
        if key in REFERENCE_KEYWORDS:
            references.append((projected, key, value))
        if key in SCHEMA_MAPPING_KEYWORDS and isinstance(value, Mapping):
            projected[key] = {
                name: _project_schema_node(prop, (*child_path, name), removed_paths, references)
                for name, prop in value.items()
                if isinstance(name, str)
            }
        elif key in SCHEMA_VALUE_KEYWORDS:
            projected[key] = _project_schema_node(value, child_path, removed_paths, references)
        else:
            projected[key] = deepcopy(value)
    return projected


def project_schema_for_budget(schema: Mapping[str, Any]) -> dict[str, Any]:
    """
    把超出客户端阈值的 schema 投影为顶层对象合同。
    """
    removed_paths: set[tuple[str, ...]] = {(key,) for key in PROJECTED_KEYWORDS}
    references: list[tuple[dict[str, Any], str, Any]] = []
    projected = _project_schema_node(schema, (), removed_paths, references)
    for node, key, pointer in references:
        if _targets_projected_keyword(pointer, removed_paths):
            del node[key]
    # 调用方已保证输入是对象，此分支仅作防御性兜底。
    if not isinstance(projected, dict):  # pragma: no cover
        projected = {}
    projected.setdefault("type", "object")
    projected[PROJECTION_MARKER] = {
        "mode": "compact",
        "reason": "client schema compaction replaces composition keywords with an empty schema",
        "full_schema_endpoint": "/api/v1/mcp/tools/{tool_name}/schema",
    }
    return projected


def project_tool_schema(schema: Any) -> tuple[Any, bool]:
    """
    按客户端压缩阈值返回 ``(schema, 是否投影)``；阈值内 schema 原样返回。
    """
    if not isinstance(schema, Mapping) or schema_within_client_budget(schema):
        return schema, False
    return project_schema_for_budget(schema), True


__all__ = [
    "COMPOSITION_KEYWORDS",
    "DEFINITION_KEYWORDS",
    "DESCRIPTION_REWRITES",
    "MCP_CLIENT_SCHEMA_COMPACTION_BYTES",
    "PROJECTION_MARKER",
    "project_schema_for_budget",
    "project_tool_schema",
    "schema_within_client_budget",
    "serialized_schema_bytes",
]
