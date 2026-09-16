"""整理计划检查点的 JSON 与分类快照编解码。"""

import json
from copy import deepcopy
from typing import Any, Optional, TypeAlias, Union

from app.application.classification.reference import (
    EffectiveClassificationSnapshot,
    persisted_classification_snapshot,
)

__all__ = ["EffectiveClassificationSnapshot"]

JSONValue: TypeAlias = Union[
    None,
    bool,
    int,
    float,
    str,
    list["JSONValue"],
    dict[str, "JSONValue"],
]

CLASSIFICATION_SNAPSHOT_FIELDS = (
    "category_id",
    "library_category",
    "classification_rule_id",
    "classification_policy_revision",
    "classification_source",
)


def copy_json_mapping(
    value: Optional[dict[str, JSONValue]],
) -> Optional[dict[str, JSONValue]]:
    """复制可选 JSON 对象，隔离检查点与调用方的可变状态。"""
    if value is None:
        return None
    return deepcopy(value)


def read_json_mapping(
    payload: dict[str, Any],
    key: str,
) -> Optional[dict[str, JSONValue]]:
    """读取可选 JSON 对象并拒绝不匹配的持久类型。"""
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"整理计划字段 {key} 必须是 JSON 对象")
    return deepcopy(value)


def read_json_tuple(
    payload: dict[str, Any],
    key: str,
) -> tuple[dict[str, JSONValue], ...]:
    """读取 JSON 对象数组并冻结为独立元组。"""
    value = payload.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"整理计划字段 {key} 必须是 JSON 对象数组")
    return tuple(deepcopy(item) for item in value)


def canonical_json(payload: dict[str, JSONValue]) -> str:
    """生成用于验证和指纹计算的确定性 JSON。"""
    try:
        return json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ValueError("整理计划只能包含有限 JSON 值") from error


def classification_snapshot_payload(
    snapshot: EffectiveClassificationSnapshot,
) -> dict[str, JSONValue]:
    """把最终分类快照投影为稳定的五字段 checkpoint 对象。"""
    return {
        "category_id": snapshot.category_id,
        "library_category": snapshot.path,
        "classification_rule_id": snapshot.rule_id,
        "classification_policy_revision": snapshot.policy_revision,
        "classification_source": snapshot.source,
    }


def read_classification_snapshot(
    payload: object,
    *,
    require_all_fields: bool,
) -> EffectiveClassificationSnapshot:
    """从 checkpoint 标量恢复安全分类快照，禁止路径穿越与描述性分类。"""
    if not isinstance(payload, dict):
        raise ValueError("整理计划分类快照必须是 JSON 对象")
    if require_all_fields and any(
        key not in payload for key in CLASSIFICATION_SNAPSHOT_FIELDS
    ):
        raise ValueError("整理计划分类快照缺少必需字段")
    if require_all_fields:
        for key in (
            "category_id",
            "library_category",
            "classification_rule_id",
            "classification_source",
        ):
            if payload.get(key) is not None and not isinstance(payload.get(key), str):
                raise ValueError(f"整理计划分类字段 {key} 必须是字符串或空值")
    raw_path = payload.get("library_category")
    if isinstance(raw_path, str) and (
        raw_path.startswith(("/", "\\")) or "\\" in raw_path
    ):
        raise ValueError("整理计划分类路径必须是 POSIX 相对路径")
    snapshot = persisted_classification_snapshot(
        category_id=payload.get("category_id"),
        category_path=raw_path,
        rule_id=payload.get("classification_rule_id"),
        policy_revision=payload.get("classification_policy_revision"),
        source=payload.get("classification_source"),
    )
    if raw_path not in (None, "", [], ()) and snapshot.path is None:
        raise ValueError("整理计划分类路径无效")
    if require_all_fields and raw_path not in (None, "") and raw_path != snapshot.path:
        raise ValueError("整理计划分类路径必须是规范 POSIX 相对路径")
    revision = payload.get("classification_policy_revision")
    if revision is not None and (
        isinstance(revision, bool)
        or not isinstance(revision, (int, str))
        or snapshot.policy_revision is None
    ):
        raise ValueError("整理计划分类策略版本必须是整数或空值")
    return snapshot


def legacy_classification_snapshot(
    payload: dict[str, Any],
) -> EffectiveClassificationSnapshot:
    """只从 v1 checkpoint 自身媒体载荷恢复分类，不读取当前活动策略。"""
    media_payload = payload.get("resolved_mediainfo")
    if not isinstance(media_payload, dict):
        invocation = payload.get("provider_invocation")
        if isinstance(invocation, dict):
            media_payload = invocation.get("mediainfo")
    if not isinstance(media_payload, dict):
        return EffectiveClassificationSnapshot()
    raw_classification = media_payload.get("classification")
    classification = raw_classification if isinstance(raw_classification, dict) else None
    effective = (
        classification.get("effective")
        if classification is not None
        and isinstance(classification.get("effective"), dict)
        else None
    )
    if isinstance(effective, dict):
        try:
            return read_classification_snapshot(
                {
                    "category_id": effective.get("category_id"),
                    "library_category": effective.get("category_path"),
                    "classification_rule_id": effective.get("rule_id"),
                    "classification_policy_revision": (
                        classification.get("policy_revision")
                        if classification is not None
                        else None
                    ),
                    "classification_source": effective.get("source"),
                },
                require_all_fields=False,
            )
        except ValueError:
            # v1 分类只是兼容辅助信息，历史脏值不能阻断冻结计划恢复。
            return EffectiveClassificationSnapshot()
    legacy_path = media_payload.get("library_category") or media_payload.get(
        "category"
    )
    try:
        return read_classification_snapshot(
            {
                "category_id": None,
                "library_category": legacy_path,
                "classification_rule_id": None,
                "classification_policy_revision": None,
                "classification_source": "legacy" if legacy_path else None,
            },
            require_all_fields=False,
        )
    except ValueError:
        # 保留 v1 原始 payload 和指纹，仅放弃不安全的兼容分类快照。
        return EffectiveClassificationSnapshot()
