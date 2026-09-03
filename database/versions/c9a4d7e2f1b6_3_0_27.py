"""3.0.27 增加分类稳定引用和历史执行快照字段。

Revision ID: c9a4d7e2f1b6
Revises: b6e1f8a3c9d2
Create Date: 2026-09-03
"""

# Alembic 的 op 是运行期代理，静态分析无法看到实际操作方法。
# pylint: disable=no-member

import sqlalchemy as sa
from alembic import op

revision = "c9a4d7e2f1b6"
down_revision = "b6e1f8a3c9d2"
branch_labels = None
depends_on = None

_STRING = "string"
_INTEGER = "integer"
_HISTORY_COLUMN_SPECS = (
    ("media_category_id", _STRING),
    ("classification_rule_id", _STRING),
    ("classification_policy_revision", _INTEGER),
    ("classification_source", _STRING),
)
_TABLE_COLUMN_SPECS = {
    "subscribe": (("media_category_id", _STRING),),
    "subscribehistory": _HISTORY_COLUMN_SPECS,
    "downloadhistory": _HISTORY_COLUMN_SPECS,
    "transferhistory": _HISTORY_COLUMN_SPECS,
}


def _existing_columns(table_name: str) -> set[str] | None:
    """返回现有字段集合；数据表不存在时返回空缺标记。"""
    inspector = sa.inspect(op.get_bind())
    if table_name not in set(inspector.get_table_names()):
        return None
    return {str(column["name"]) for column in inspector.get_columns(table_name)}


def _column(column_name: str, kind: str) -> sa.Column:
    """按稳定规格构造一次可空分类字段。"""
    column_type = sa.Integer() if kind == _INTEGER else sa.String()
    return sa.Column(column_name, column_type, nullable=True)


def upgrade() -> None:
    """为活动订阅和三类历史幂等增加分类持久化字段。"""
    for table_name, specs in _TABLE_COLUMN_SPECS.items():
        existing = _existing_columns(table_name)
        if existing is None:
            continue
        for column_name, kind in specs:
            if column_name in existing:
                continue
            op.add_column(table_name, _column(column_name, kind))
            existing.add(column_name)


def downgrade() -> None:
    """幂等移除本版本增加的分类持久化字段。"""
    for table_name, specs in reversed(tuple(_TABLE_COLUMN_SPECS.items())):
        existing = _existing_columns(table_name)
        if existing is None:
            continue
        removable = [column_name for column_name, _kind in reversed(specs) if column_name in existing]
        if not removable:
            continue
        with op.batch_alter_table(table_name) as batch_op:
            for column_name in removable:
                batch_op.drop_column(column_name)
