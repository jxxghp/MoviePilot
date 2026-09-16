"""3.0.26 约束系统配置键非空且唯一。

Revision ID: b6e1f8a3c9d2
Revises: c8f2e6a1d4b9
Create Date: 2026-09-03
"""

# Alembic 的 op 是运行期代理，静态分析无法看到实际操作方法。
# pylint: disable=no-member

from typing import Any, Optional, cast

import sqlalchemy as sa
from alembic import op

revision = "b6e1f8a3c9d2"
down_revision = "c8f2e6a1d4b9"
branch_labels = None
depends_on = None

_TABLE = "systemconfig"
_UNIQUE_INDEX = "ux_systemconfig_key"
_LEGACY_INDEX = "ix_systemconfig_key"

_SYSTEM_CONFIG = sa.table(
    _TABLE,
    sa.column("id", sa.Integer()),
    sa.column("key", sa.String()),
)


def _table_exists() -> bool:
    """检查系统配置表是否存在。"""
    return _TABLE in set(sa.inspect(op.get_bind()).get_table_names())


def _key_column() -> Optional[dict[str, Any]]:
    """返回系统配置键列的反射信息。"""
    for column in sa.inspect(op.get_bind()).get_columns(_TABLE):
        if column["name"] == "key":
            return cast(dict[str, Any], column)
    return None


def _indexes() -> dict[str, dict[str, Any]]:
    """按名称返回系统配置表索引。"""
    return {
        str(index["name"]): index
        for index in sa.inspect(op.get_bind()).get_indexes(_TABLE)
        if index.get("name")
    }


def _drop_index_if_present(index_name: str) -> None:
    """存在时删除指定系统配置索引。"""
    if index_name in _indexes():
        op.drop_index(index_name, table_name=_TABLE)


def _clean_legacy_rows() -> None:
    """清理空键，并为每个有效键确定性保留最大 ID 记录。"""
    connection = op.get_bind()
    invalid_key = sa.or_(
        _SYSTEM_CONFIG.c.key.is_(None),
        sa.func.trim(_SYSTEM_CONFIG.c.key) == "",
    )
    connection.execute(_SYSTEM_CONFIG.delete().where(invalid_key))

    retained_ids = (
        sa.select(sa.func.max(_SYSTEM_CONFIG.c.id))
        .where(_SYSTEM_CONFIG.c.key.is_not(None))
        .group_by(_SYSTEM_CONFIG.c.key)
    )
    connection.execute(
        _SYSTEM_CONFIG.delete().where(
            _SYSTEM_CONFIG.c.id.not_in(retained_ids)
        )
    )


def _set_key_nullable(nullable: bool) -> None:
    """按目标状态修改系统配置键的可空约束。"""
    column = _key_column()
    if column is None:
        raise RuntimeError("systemconfig 表缺少迁移必需字段: key")
    if bool(column.get("nullable")) == nullable:
        return
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.alter_column(
            "key",
            existing_type=column["type"],
            existing_nullable=bool(column.get("nullable")),
            nullable=nullable,
        )


def upgrade() -> None:
    """清理旧数据并建立系统配置键的非空唯一索引。"""
    if not _table_exists():
        return

    _clean_legacy_rows()
    _drop_index_if_present(_LEGACY_INDEX)
    _set_key_nullable(False)

    current = _indexes().get(_UNIQUE_INDEX)
    if current is not None and (
        tuple(current.get("column_names") or ()) != ("key",)
        or not bool(current.get("unique"))
    ):
        op.drop_index(_UNIQUE_INDEX, table_name=_TABLE)
        current = None
    if current is None:
        op.create_index(
            _UNIQUE_INDEX,
            _TABLE,
            ["key"],
            unique=True,
        )


def downgrade() -> None:
    """恢复系统配置键可空和普通查询索引。"""
    if not _table_exists():
        return

    _drop_index_if_present(_UNIQUE_INDEX)
    _set_key_nullable(True)

    current = _indexes().get(_LEGACY_INDEX)
    if current is not None and (
        tuple(current.get("column_names") or ()) != ("key",)
        or bool(current.get("unique"))
    ):
        op.drop_index(_LEGACY_INDEX, table_name=_TABLE)
        current = None
    if current is None:
        op.create_index(
            _LEGACY_INDEX,
            _TABLE,
            ["key"],
            unique=False,
        )
