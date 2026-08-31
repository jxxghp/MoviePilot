"""从业务 OpenAPI 构建 moviepilot_api 的外部 MCP 输入合同。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping, Sequence


def _rewrite_schema_refs(
    schema: Mapping[str, Any],
    *,
    components: Mapping[str, Any],
    definitions: dict[str, Any],
) -> dict[str, Any]:
    """把 OpenAPI components 引用改写为独立 MCP schema 的 $defs 引用。"""
    reference = schema.get("$ref")
    if isinstance(reference, str) and reference.startswith("#/components/schemas/"):
        name = reference.rsplit("/", 1)[-1]
        if name not in definitions:
            definitions[name] = {}
            source = components.get(name)
            if not isinstance(source, Mapping):
                raise ValueError(f"OpenAPI 缺少请求模型: {name}")
            definitions[name] = _rewrite_schema_refs(
                source,
                components=components,
                definitions=definitions,
            )
        return {"$ref": f"#/$defs/{name}"}

    rewritten: dict[str, Any] = {}
    for key, value in schema.items():
        if isinstance(value, Mapping):
            rewritten[key] = _rewrite_schema_refs(
                value,
                components=components,
                definitions=definitions,
            )
        elif isinstance(value, list):
            rewritten[key] = [
                _rewrite_schema_refs(
                    item,
                    components=components,
                    definitions=definitions,
                )
                if isinstance(item, Mapping)
                else deepcopy(item)
                for item in value
            ]
        else:
            rewritten[key] = deepcopy(value)
    return rewritten


def _parameter_object_schema(
    parameters: Sequence[Mapping[str, Any]],
    *,
    location: str,
    components: Mapping[str, Any],
    definitions: dict[str, Any],
) -> dict[str, Any] | None:
    """把 OpenAPI path/query 参数投影为网关结构化对象。"""
    selected = [parameter for parameter in parameters if parameter.get("in") == location]
    if not selected:
        return None
    properties: dict[str, Any] = {}
    required: list[str] = []
    for parameter in selected:
        name = str(parameter["name"])
        raw_schema = parameter.get("schema")
        if not isinstance(raw_schema, Mapping):
            raw_schema = {}
        field_schema = _rewrite_schema_refs(
            raw_schema,
            components=components,
            definitions=definitions,
        )
        if parameter.get("description") and "description" not in field_schema:
            field_schema["description"] = parameter["description"]
        properties[name] = field_schema
        if parameter.get("required"):
            required.append(name)
    result: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        result["required"] = required
    return result


def _request_body_schema(
    operation: Mapping[str, Any],
    *,
    components: Mapping[str, Any],
    definitions: dict[str, Any],
) -> tuple[dict[str, Any] | None, bool]:
    """读取一个 OpenAPI operation 的 JSON 请求体及必填性。"""
    request_body = operation.get("requestBody")
    if not isinstance(request_body, Mapping):
        return None, False
    content = request_body.get("content")
    if not isinstance(content, Mapping):
        return None, bool(request_body.get("required"))
    media = content.get("application/json")
    if not isinstance(media, Mapping):
        media = next((item for item in content.values() if isinstance(item, Mapping)), None)
    raw_schema = media.get("schema") if isinstance(media, Mapping) else None
    if not isinstance(raw_schema, Mapping):
        return None, bool(request_body.get("required"))
    return (
        _rewrite_schema_refs(
            raw_schema,
            components=components,
            definitions=definitions,
        ),
        bool(request_body.get("required")),
    )


def _person_credits_operation(openapi: Mapping[str, Any]) -> dict[str, Any]:
    """合并四个来源端点为稳定 media.person.credits 网关合同。"""
    paths = openapi.get("paths", {})
    source_paths = {
        "douban": "/api/v1/douban/person/credits/{person_id}",
        "tmdb": "/api/v1/tmdb/person/credits/{person_id}",
        "bangumi": "/api/v1/bangumi/person/credits/{person_id}",
        "anilist": "/api/v1/anilist/person/credits/{person_id}",
    }
    for source_path in source_paths.values():
        if source_path not in paths:
            raise ValueError(f"OpenAPI 缺少人物作品端点: {source_path}")
    return {
        "summary": "读取人物作品",
        "parameters": [
            {
                "name": "source",
                "in": "path",
                "required": True,
                "schema": {"type": "string", "enum": list(source_paths)},
                "description": "人物数据来源。",
            },
            {
                "name": "person_id",
                "in": "path",
                "required": True,
                "schema": {"type": "integer"},
                "description": "来源原生人物 ID。",
            },
            {
                "name": "page",
                "in": "query",
                "required": False,
                "schema": {"type": "integer", "minimum": 1, "default": 1},
            },
            {
                "name": "count",
                "in": "query",
                "required": False,
                "schema": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
                "description": "Bangumi 与 AniList 支持的每页条数；其他来源忽略。",
            },
        ],
    }


def _resolve_openapi_operation(
    operation_id: str,
    route: Any,
    openapi: Mapping[str, Any],
) -> Mapping[str, Any]:
    """按固定路由读取 OpenAPI operation，并处理多来源合成路由。"""
    if operation_id == "media.person.credits":
        return _person_credits_operation(openapi)
    path_item = openapi.get("paths", {}).get(route.path)
    if not isinstance(path_item, Mapping):
        raise ValueError(f"OpenAPI 缺少 operation 路径: {operation_id} -> {route.path}")
    operation = path_item.get(str(route.method).lower())
    if not isinstance(operation, Mapping):
        raise ValueError(f"OpenAPI 缺少 operation 方法: {operation_id} -> {route.method} {route.path}")
    return operation


def _apply_operation_overrides(
    operation_id: str,
    query_schema: dict[str, Any] | None,
) -> None:
    """补充同一路由多 operation 时无法由 FastAPI 自动表达的语义约束。"""
    if operation_id != "media.person.search" or query_schema is None:
        return
    query_schema["properties"]["type"] = {
        "type": "string",
        "const": "person",
        "description": "人物搜索固定传 person。",
    }
    required = query_schema.setdefault("required", [])
    if "type" not in required:
        required.append("type")


def build_api_mcp_input_schema(
    *,
    openapi: Mapping[str, Any],
    routes: Mapping[str, Any],
    specs: Sequence[Any],
) -> dict[str, Any]:
    """构建 59 个白名单 operation 的完整 MCP oneOf 输入合同。"""
    components = openapi.get("components", {}).get("schemas", {})
    if not isinstance(components, Mapping):
        components = {}
    definitions: dict[str, Any] = {}
    spec_by_id = {spec.operation_id: spec for spec in specs}
    if set(spec_by_id) != set(routes):
        raise ValueError("API operation 策略与路由注册表不一致")

    branches: list[dict[str, Any]] = []
    for operation_id in sorted(routes):
        route = routes[operation_id]
        operation = _resolve_openapi_operation(operation_id, route, openapi)
        parameters = operation.get("parameters")
        if not isinstance(parameters, list):
            parameters = []
        path_schema = _parameter_object_schema(
            parameters,
            location="path",
            components=components,
            definitions=definitions,
        )
        query_schema = _parameter_object_schema(
            parameters,
            location="query",
            components=components,
            definitions=definitions,
        )
        _apply_operation_overrides(operation_id, query_schema)
        body_schema, body_required = _request_body_schema(
            operation,
            components=components,
            definitions=definitions,
        )

        properties: dict[str, Any] = {
            "operation_id": {"type": "string", "const": operation_id},
        }
        required = ["operation_id"]
        if path_schema is not None:
            properties["path_params"] = path_schema
            if path_schema.get("required"):
                required.append("path_params")
        if query_schema is not None:
            properties["query"] = query_schema
            if query_schema.get("required"):
                required.append("query")
        if body_schema is not None:
            properties["body"] = body_schema
            if body_required:
                required.append("body")

        summary = str(operation.get("summary") or operation.get("description") or operation_id)
        spec = spec_by_id[operation_id]
        branches.append(
            {
                "type": "object",
                "title": operation_id,
                "description": (
                    f"{summary} Method: {route.method}. Path: {route.path}. "
                    f"Effect: {spec.effect.value}."
                ),
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            }
        )

    schema: dict[str, Any] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "moviepilot_api",
        "type": "object",
        "description": "Select the oneOf branch matching operation_id and send exactly its documented fields.",
        "properties": {
            "operation_id": {"type": "string", "enum": sorted(routes)},
            "path_params": {"type": "object"},
            "query": {"type": "object"},
            "body": {"type": "object"},
        },
        "required": ["operation_id"],
        "oneOf": branches,
    }
    if definitions:
        schema["$defs"] = definitions
    return schema


__all__ = ["build_api_mcp_input_schema"]
