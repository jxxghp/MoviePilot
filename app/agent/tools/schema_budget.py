"""外部 MCP ``tools/list`` 的工具 schema 体积预算。

主流 MCP 客户端会按自身预算有损压缩工具 schema。以 Codex 为例，规范化后超过
5000 字节的 schema 会依次执行「删除 description → 丢弃 ``$defs`` → 折叠深层复杂对象
→ 移除组合关键字」，其中 ``oneOf``/``anyOf``/``allOf`` 所在的节点会被整段替换为
``{}``。``moviepilot_api``、``downloader_operation``、``mediaserver_operation`` 的完整
合同远超该预算，于是客户端实际拿到一个没有 ``type`` 的空对象；严格校验的 Responses
上游随后以 ``schema must be a JSON Schema of 'type: "object"', got 'type: null'``
拒绝整个请求。

本模块给出 MCP 表面的预算投影：超出预算的工具 schema 保留顶层 ``type``/``required``
与顶层属性（含 operation/action 枚举），去掉根级组合关键字与 ``$defs``。逐 operation 的
完整合同继续由 ``GET /api/v1/mcp/tools/{tool_name}/schema`` 与调用失败回执提供，调用侧
校验行为不变。
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Mapping, Tuple

# 客户端预算普遍在 5 KB 级别；这里留出余量，避免投影结果紧贴客户端阈值。
MCP_TOOL_SCHEMA_BUDGET_BYTES = 4096

# 客户端压缩会移除这些关键字所在节点，投影时提前删除以避免整段丢失。
COMPOSITION_KEYWORDS = ("oneOf", "anyOf", "allOf")

# 定义表在客户端压缩中会被丢弃，投影时不再保留无法解析的本地引用。
DEFINITION_KEYWORDS = ("$defs", "definitions")

# 投影标记：客户端与测试据此识别本次返回的是预算投影而非完整合同。
PROJECTION_MARKER = "x-moviepilot-schema-projection"


def serialized_schema_bytes(schema: Any) -> int:
    """返回 schema 紧凑序列化后的字节数；无法序列化时按超出预算处理。"""
    try:
        payload = json.dumps(schema, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    except TypeError, ValueError:
        return MCP_TOOL_SCHEMA_BUDGET_BYTES + 1
    return len(payload.encode("utf-8"))


def schema_within_budget(schema: Any) -> bool:
    """判断 schema 是否在 MCP 预算内。"""
    return serialized_schema_bytes(schema) <= MCP_TOOL_SCHEMA_BUDGET_BYTES


def _project_schema_node(node: Any) -> Any:
    """递归删除组合关键字与定义表，保留其余合同信息。"""
    if isinstance(node, list):
        return [_project_schema_node(item) for item in node]
    if not isinstance(node, Mapping):
        return deepcopy(node)
    projected: dict[str, Any] = {}
    for key, value in node.items():
        if not isinstance(key, str) or key in COMPOSITION_KEYWORDS or key in DEFINITION_KEYWORDS:
            continue
        if key == "properties" and isinstance(value, Mapping):
            projected[key] = {name: _project_schema_node(prop) for name, prop in value.items() if isinstance(name, str)}
        elif key in {"items", "additionalProperties", "patternProperties"}:
            projected[key] = _project_schema_node(value)
        else:
            projected[key] = deepcopy(value)
    return projected


def project_schema_for_budget(schema: Mapping[str, Any]) -> dict[str, Any]:
    """把超出预算的 schema 投影为顶层对象合同。"""
    projected = _project_schema_node(schema)
    if not isinstance(projected, dict):  # pragma: no cover - 调用方已保证是对象
        projected = {}
    projected.setdefault("type", "object")
    projected[PROJECTION_MARKER] = {
        "mode": "compact",
        "budget_bytes": MCP_TOOL_SCHEMA_BUDGET_BYTES,
        "full_schema_endpoint": "/api/v1/mcp/tools/{tool_name}/schema",
        "reason": "client tool-schema compaction replaces composition keywords with an empty schema",
    }
    return projected


def project_tool_schema(schema: Any) -> Tuple[Any, bool]:
    """按预算返回 ``(schema, 是否投影)``；预算内 schema 原样返回。"""
    if not isinstance(schema, Mapping) or schema_within_budget(schema):
        return schema, False
    return project_schema_for_budget(schema), True


__all__ = [
    "COMPOSITION_KEYWORDS",
    "DEFINITION_KEYWORDS",
    "MCP_TOOL_SCHEMA_BUDGET_BYTES",
    "PROJECTION_MARKER",
    "project_schema_for_budget",
    "project_tool_schema",
    "schema_within_budget",
    "serialized_schema_bytes",
]
