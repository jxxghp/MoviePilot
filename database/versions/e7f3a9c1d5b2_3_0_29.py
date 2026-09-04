"""3.0.29 修复旧分类名称中的目录层级路径。"""

from collections.abc import Mapping

import sqlalchemy as sa
from alembic import op
from sqlalchemy.sql.selectable import TableClause

revision = "e7f3a9c1d5b2"
down_revision = "d8f2b6a4c1e7"
branch_labels = None
depends_on = None

_TABLE = "systemconfig"
_POLICY_KEY = "MediaClassificationPolicy"
_LEGACY_CATEGORY_PREFIXES = ("legacy.movie.", "legacy.tv.")
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


def _table_exists() -> bool:
    """检查系统配置表是否存在。"""
    return _TABLE in set(sa.inspect(op.get_bind()).get_table_names())


def _system_config_relation() -> TableClause:
    """构造只包含本次 JSON 数据修复所需字段的系统配置关系。"""
    return sa.table(
        _TABLE,
        sa.column("id", sa.Integer()),
        sa.column("key", sa.String()),
        sa.column("value", sa.JSON()),
    )


def _path_segment_is_invalid(segment: str) -> bool:
    """判断分类路径段是否包含目录穿越或跨平台非法文件名。"""
    if not segment or segment != segment.strip():
        return True
    if segment in {".", ".."} or segment.endswith((".", " ")):
        return True
    if any(character in _ILLEGAL_PATH_CHARACTERS for character in segment):
        return True
    if any(ord(character) < 32 for character in segment):
        return True
    return segment.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES


def _safe_legacy_path(name: str) -> list[str] | None:
    """把可安全还原的旧分类名转换成目录路径段。"""
    segments = name.split("/")
    if (
        not segments
        or len(segments) > _MAX_CATEGORY_DEPTH
        or any(len(segment) > _MAX_CATEGORY_SEGMENT_LENGTH or _path_segment_is_invalid(segment) for segment in segments)
        or len("/".join(segments)) > _MAX_CATEGORY_PATH_LENGTH
    ):
        return None
    return segments


def _repair_category(category: object) -> tuple[object, bool]:
    """仅修复首版 legacy 影视分类的单段斜杠路径。"""
    if not isinstance(category, Mapping):
        return category, False
    category_id = category.get("id")
    name = category.get("name")
    path = category.get("path")
    if not (
        isinstance(category_id, str)
        and category_id.startswith(_LEGACY_CATEGORY_PREFIXES)
        and isinstance(name, str)
        and "/" in name
        and isinstance(path, list)
        and path == [name]
    ):
        return category, False
    repaired_path = _safe_legacy_path(name)
    if repaired_path is None:
        return category, False
    repaired = dict(category)
    repaired["path"] = repaired_path
    return repaired, True


def _repair_policy_snapshot(snapshot: object) -> tuple[object, bool]:
    """修复一个活动或历史策略快照中的 legacy 分类路径。"""
    if not isinstance(snapshot, Mapping):
        return snapshot, False
    categories = snapshot.get("categories")
    if not isinstance(categories, list):
        return snapshot, False
    repaired_categories: list[object] = []
    changed = False
    for category in categories:
        repaired, category_changed = _repair_category(category)
        repaired_categories.append(repaired)
        changed = changed or category_changed
    if not changed:
        return snapshot, False
    repaired_snapshot = dict(snapshot)
    repaired_snapshot["categories"] = repaired_categories
    return repaired_snapshot, True


def _repair_policy_state(value: object) -> tuple[object, bool]:
    """修复策略状态中的活动版本和有限历史版本。"""
    if not isinstance(value, Mapping):
        return value, False
    repaired_state = dict(value)
    changed = False

    active, active_changed = _repair_policy_snapshot(value.get("active"))
    if active_changed:
        repaired_state["active"] = active
        changed = True

    history = value.get("history")
    if isinstance(history, list):
        repaired_history: list[object] = []
        history_changed = False
        for snapshot in history:
            repaired, snapshot_changed = _repair_policy_snapshot(snapshot)
            repaired_history.append(repaired)
            history_changed = history_changed or snapshot_changed
        if history_changed:
            repaired_state["history"] = repaired_history
            changed = True

    return (repaired_state, True) if changed else (value, False)


def _repair_stored_policy() -> None:
    """回写数据库中已迁移策略的安全路径段，保持 revision 和目录字符串不变。"""
    relation = _system_config_relation()
    connection = op.get_bind()
    rows = (
        connection.execute(sa.select(relation.c.id, relation.c.value).where(relation.c.key == _POLICY_KEY))
        .mappings()
        .all()
    )
    for row in rows:
        repaired, changed = _repair_policy_state(row["value"])
        if not changed:
            continue
        connection.execute(relation.update().where(relation.c.id == row["id"]).values(value=repaired))


def upgrade() -> None:
    """修复已完成迁移的分类策略，避免旧斜杠名称触发路径安全错误。"""
    if _table_exists():
        _repair_stored_policy()


def downgrade() -> None:
    """数据修复不可逆，降级不恢复为原有的不安全路径表示。"""
    pass
