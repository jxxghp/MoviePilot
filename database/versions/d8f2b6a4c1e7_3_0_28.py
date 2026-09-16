"""3.0.28 为 durable transfer 检查点冻结最终分类快照。

Revision ID: d8f2b6a4c1e7
Revises: c9a4d7e2f1b6
Create Date: 2026-09-03
"""

# Alembic 的 op 是运行期代理，静态分析无法看到实际操作方法。
# pylint: disable=no-member

from collections.abc import Mapping, Sequence
from typing import Any, Optional

import sqlalchemy as sa
from alembic import op
from sqlalchemy.sql.selectable import TableClause

revision = "d8f2b6a4c1e7"
down_revision = "c9a4d7e2f1b6"
branch_labels = None
depends_on = None

_PENDING_TABLE = "transferpending"
_STEP_TABLE = "transferexecutionstep"
_LEGACY_CHECKPOINT_VERSION = 1
_CLASSIFIED_CHECKPOINT_VERSION = 2
_CLASSIFICATION_FIELDS = (
    "category_id",
    "library_category",
    "classification_rule_id",
    "classification_policy_revision",
    "classification_source",
)
_REQUIRED_PENDING_COLUMNS = {
    "id",
    "task_id",
    "checkpoint_version",
    "checkpoint_payload",
    "execution_state",
    "execution_payload",
}
_MAX_CATEGORY_DEPTH = 4
_MAX_CATEGORY_SEGMENT_LENGTH = 64
_MAX_CATEGORY_PATH_LENGTH = 240
_ILLEGAL_PATH_CHARACTERS = frozenset('<>:"/\\|?*')
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def _table_names() -> set[str]:
    """返回当前数据库中的数据表集合。"""
    return set(sa.inspect(op.get_bind()).get_table_names())


def _column_names(table_name: str) -> set[str]:
    """返回指定数据表的字段集合。"""
    return {
        str(column["name"])
        for column in sa.inspect(op.get_bind()).get_columns(table_name)
    }


def _validate_schema() -> None:
    """拒绝在缺少 durable transfer 契约的残缺数据库上改写计划身份。"""
    tables = _table_names()
    if _PENDING_TABLE not in tables or _STEP_TABLE not in tables:
        raise RuntimeError("缺少 durable transfer 数据表，拒绝迁移分类检查点")
    missing = _REQUIRED_PENDING_COLUMNS - _column_names(_PENDING_TABLE)
    if missing:
        raise RuntimeError(
            f"transferpending 缺少分类检查点迁移字段: {sorted(missing)}"
        )
    if "task_id" not in _column_names(_STEP_TABLE):
        raise RuntimeError("transferexecutionstep 缺少 task_id，拒绝迁移分类检查点")


def _pending_relation() -> TableClause:
    """构造迁移所需的最小待整理表关系。"""
    return sa.table(
        _PENDING_TABLE,
        sa.column("id", sa.Integer()),
        sa.column("task_id", sa.String(64)),
        sa.column("checkpoint_version", sa.Integer()),
        sa.column("checkpoint_payload", sa.JSON()),
        sa.column("execution_state", sa.String(32)),
        sa.column("execution_payload", sa.JSON()),
    )


def _step_relation() -> TableClause:
    """构造判断外部操作证据所需的最小步骤表关系。"""
    return sa.table(
        _STEP_TABLE,
        sa.column("task_id", sa.String(64)),
    )


def _step_task_ids() -> set[str]:
    """返回至少持久化过一个执行步骤的任务身份集合。"""
    steps = _step_relation()
    return {
        str(task_id)
        for task_id in op.get_bind().execute(
            sa.select(steps.c.task_id).distinct()
        ).scalars()
        if task_id
    }


def _has_execution_evidence(
    row: Mapping[str, Any],
    step_task_ids: set[str],
) -> bool:
    """判断检查点是否已有不能改变 operation identity 的执行证据。"""
    return (
        row["execution_state"] != "not_started"
        or row["execution_payload"] is not None
        or str(row["task_id"]) in step_task_ids
    )


def _optional_text(value: object) -> tuple[Optional[str], bool]:
    """规范可空文本；类型异常时要求保留原 v1 检查点。"""
    if value is None:
        return None, True
    if not isinstance(value, str):
        return None, False
    return value.strip() or None, True


def _optional_revision(value: object) -> tuple[Optional[int], bool]:
    """规范可空策略版本，拒绝布尔值和不可解析文本。"""
    if value is None:
        return None, True
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None, False
    try:
        return int(value), True
    except ValueError:
        return None, False


def _path_segment_is_illegal(segment: str) -> bool:
    """判断路径段是否包含目录穿越或跨平台非法文件名。"""
    if segment in {".", ".."} or segment != segment.strip():
        return True
    if segment.endswith((".", " ")):
        return True
    if any(character in _ILLEGAL_PATH_CHARACTERS for character in segment):
        return True
    if any(ord(character) < 32 for character in segment):
        return True
    return segment.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES


def _optional_library_category(value: object) -> tuple[Optional[str], bool]:
    """把旧字符串或路径段数组规范为安全 POSIX 相对路径。"""
    if value is None:
        return None, True
    raw_segments: Sequence[object]
    if isinstance(value, str):
        if not value:
            return None, True
        if value.startswith(("/", "\\")) or "\\" in value:
            return None, False
        raw_segments = value.split("/")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        raw_segments = value
    else:
        return None, False
    segments: list[str] = []
    for raw_segment in raw_segments:
        if not isinstance(raw_segment, str):
            return None, False
        segment = raw_segment.strip()
        if segment:
            segments.append(segment)
    if not segments:
        return None, True
    if (
        len(segments) > _MAX_CATEGORY_DEPTH
        or any(
            len(segment) > _MAX_CATEGORY_SEGMENT_LENGTH
            or _path_segment_is_illegal(segment)
            for segment in segments
        )
        or len("/".join(segments)) > _MAX_CATEGORY_PATH_LENGTH
    ):
        return None, False
    return "/".join(segments), True


def _empty_classification_snapshot() -> dict[str, object]:
    """构造 v2 必须显式持久化的五字段空分类快照。"""
    return {field_name: None for field_name in _CLASSIFICATION_FIELDS}


def _media_payload(checkpoint_payload: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    """按运行时优先级读取 checkpoint 自身冻结的媒体载荷。"""
    resolved = checkpoint_payload.get("resolved_mediainfo")
    if isinstance(resolved, Mapping):
        return resolved
    invocation = checkpoint_payload.get("provider_invocation")
    if isinstance(invocation, Mapping):
        provider_media = invocation.get("mediainfo")
        if isinstance(provider_media, Mapping):
            return provider_media
    return None


def _classification_snapshot(
    checkpoint_payload: Mapping[str, Any],
) -> tuple[dict[str, object], bool]:
    """只从持久媒体载荷投影最终分类，绝不读取当前策略或 metadata_category。"""
    media_payload = _media_payload(checkpoint_payload)
    if media_payload is None:
        return _empty_classification_snapshot(), True
    classification = media_payload.get("classification")
    effective = (
        classification.get("effective")
        if isinstance(classification, Mapping)
        else None
    )
    if isinstance(effective, Mapping):
        category_id, category_valid = _optional_text(effective.get("category_id"))
        category_path, path_valid = _optional_library_category(
            effective.get("category_path")
        )
        rule_id, rule_valid = _optional_text(effective.get("rule_id"))
        source, source_valid = _optional_text(effective.get("source"))
        policy_revision, revision_valid = _optional_revision(
            classification.get("policy_revision")
        )
        if not all((
            category_valid,
            path_valid,
            rule_valid,
            source_valid,
            revision_valid,
        )):
            return _empty_classification_snapshot(), False
        return {
            "category_id": category_id,
            "library_category": category_path,
            "classification_rule_id": rule_id,
            "classification_policy_revision": policy_revision,
            "classification_source": source,
        }, True
    legacy_path, path_valid = _optional_library_category(
        media_payload.get("library_category")
        or media_payload.get("category")
    )
    if not path_valid:
        return _empty_classification_snapshot(), False
    snapshot = _empty_classification_snapshot()
    snapshot["library_category"] = legacy_path
    snapshot["classification_source"] = "legacy" if legacy_path else None
    return snapshot, True


def _upgrade_payload(payload: object) -> Optional[dict[str, Any]]:
    """把可安全解释的 v1 计划复制为带显式分类快照的 v2 计划。"""
    if not isinstance(payload, Mapping) or payload.get("schema_version") != 1:
        return None
    snapshot, valid = _classification_snapshot(payload)
    if not valid:
        return None
    upgraded = dict(payload)
    upgraded["schema_version"] = _CLASSIFIED_CHECKPOINT_VERSION
    upgraded["classification_snapshot"] = snapshot
    return upgraded


def _downgrade_payload(payload: object) -> dict[str, Any]:
    """移除 v2 分类快照并恢复 v1 版本标记。"""
    if not isinstance(payload, Mapping) or payload.get("schema_version") != 2:
        raise RuntimeError("v2 整理检查点载荷无效，拒绝降级")
    downgraded = dict(payload)
    downgraded["schema_version"] = _LEGACY_CHECKPOINT_VERSION
    downgraded.pop("classification_snapshot", None)
    return downgraded


def upgrade() -> None:
    """无执行证据时把 v1 检查点幂等升级为分类快照 v2。"""
    _validate_schema()
    pending = _pending_relation()
    step_task_ids = _step_task_ids()
    rows = op.get_bind().execute(
        sa.select(
            pending.c.id,
            pending.c.task_id,
            pending.c.checkpoint_payload,
            pending.c.execution_state,
            pending.c.execution_payload,
        ).where(
            pending.c.checkpoint_version == _LEGACY_CHECKPOINT_VERSION
        )
    ).mappings().all()
    for row in rows:
        if _has_execution_evidence(row, step_task_ids):
            continue
        upgraded = _upgrade_payload(row["checkpoint_payload"])
        if upgraded is None:
            continue
        op.get_bind().execute(
            pending.update()
            .where(pending.c.id == row["id"])
            .values(
                checkpoint_version=_CLASSIFIED_CHECKPOINT_VERSION,
                checkpoint_payload=upgraded,
            )
        )


def downgrade() -> None:
    """仅降级无执行证据的 v2；任何执行证据都会原子阻止降级。"""
    _validate_schema()
    pending = _pending_relation()
    step_task_ids = _step_task_ids()
    rows = op.get_bind().execute(
        sa.select(
            pending.c.id,
            pending.c.task_id,
            pending.c.checkpoint_payload,
            pending.c.execution_state,
            pending.c.execution_payload,
        ).where(
            pending.c.checkpoint_version == _CLASSIFIED_CHECKPOINT_VERSION
        )
    ).mappings().all()
    blocked = [
        str(row["task_id"])
        for row in rows
        if _has_execution_evidence(row, step_task_ids)
    ]
    if blocked:
        raise RuntimeError(
            "存在执行证据的 v2 整理检查点，拒绝降级: "
            f"{', '.join(sorted(blocked))}"
        )
    downgraded_rows = [
        (row["id"], _downgrade_payload(row["checkpoint_payload"]))
        for row in rows
    ]
    for row_id, payload in downgraded_rows:
        op.get_bind().execute(
            pending.update()
            .where(pending.c.id == row_id)
            .values(
                checkpoint_version=_LEGACY_CHECKPOINT_VERSION,
                checkpoint_payload=payload,
            )
        )
