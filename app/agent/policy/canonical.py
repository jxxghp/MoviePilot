"""严格工具调用规范化，不读取设置值或执行工具实现。"""

import hashlib
import json
from typing import Any, Mapping

from pydantic import BaseModel, ValidationError

from app.agent.policy.contracts import (
    ActionPolicy,
    CanonicalInvocation,
    ToolRevision,
)
from app.agent.tools.impl._system_setting_utils import (
    list_setting_specs,
    resolve_setting_spec,
)


class CanonicalizationError(ValueError):
    """当前工具 schema 或静态前提无法产生严格调用时的稳定失败。"""


def _stable_json(value: Any) -> str:
    """生成拒绝 NaN 且保留 Unicode 的稳定紧凑 JSON。"""
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, ValidationError) as error:
        raise CanonicalizationError("调用参数无法规范化") from error


def _accepted_argument_names(args_schema: type[BaseModel]) -> set[str]:
    """返回严格入口可接受的字段名与字符串别名。"""
    accepted = set(args_schema.model_fields)
    for field in args_schema.model_fields.values():
        for alias in (field.alias, field.validation_alias):
            if isinstance(alias, str):
                accepted.add(alias)
    return accepted


def canonicalize_invocation(
    *,
    tool: Any,
    arguments: Mapping[str, Any],
    policy: ActionPolicy,
    tool_revision: ToolRevision,
) -> CanonicalInvocation:
    """使用当前 Pydantic schema 与静态设置定义生成不可变调用摘要。"""
    tool_name = str(getattr(tool, "name", "") or "")
    args_schema = getattr(tool, "args_schema", None)
    if not tool_name or not isinstance(args_schema, type) or not issubclass(
        args_schema, BaseModel
    ):
        raise CanonicalizationError("工具缺少严格 Pydantic 参数契约")
    raw_arguments = dict(arguments or {})
    unknown_arguments = set(raw_arguments) - _accepted_argument_names(args_schema)
    if unknown_arguments:
        raise CanonicalizationError("工具参数校验失败")
    try:
        validated = args_schema.model_validate(raw_arguments)
        normalized = validated.model_dump(
            mode="json",
            exclude_unset=False,
            exclude_none=False,
        )
        schema_json = _stable_json(args_schema.model_json_schema())
    except (TypeError, ValueError) as error:
        raise CanonicalizationError("工具参数校验失败") from error

    preconditions: tuple[tuple[str, str], ...] = ()
    setting_key = normalized.get("setting_key")
    if tool_name == "query_system_settings":
        if setting_key:
            spec = resolve_setting_spec(setting_key)
            if spec is None:
                raise CanonicalizationError("系统设置项不存在")
            specs = [spec]
        else:
            try:
                specs = list_setting_specs(
                    group=normalized.get("group"),
                    keyword=normalized.get("keyword"),
                )
            except ValueError as error:
                raise CanonicalizationError("系统设置选择器无效") from error
            if not specs:
                raise CanonicalizationError("系统设置选择器没有匹配项")
        preconditions = tuple(
            (
                "setting",
                f"{spec.source}:{spec.key}:{spec.group}",
            )
            for spec in specs
        )

    payload = {
        "canonical_version": "p1-g2a2-canonical-v1",
        "action_subtype": policy.effect.value,
        "arguments": normalized,
        "policy_version": policy.policy_version,
        "preconditions": preconditions,
        "schema_digest": hashlib.sha256(schema_json.encode("utf-8")).hexdigest(),
        "tool_name": tool_name,
        "tool_revision": {
            "factory": tool_revision.factory,
            "implementation": tool_revision.implementation,
            "plugin": tool_revision.plugin,
        },
    }
    canonical_json = _stable_json(payload)
    return CanonicalInvocation(
        tool_name=tool_name,
        arguments=normalized,
        canonical_json=canonical_json,
        digest=hashlib.sha256(canonical_json.encode("utf-8")).hexdigest(),
        policy_version=policy.policy_version,
        tool_revision=tool_revision,
        schema_digest=payload["schema_digest"],
        preconditions=preconditions,
    )


__all__ = ["CanonicalizationError", "canonicalize_invocation"]
