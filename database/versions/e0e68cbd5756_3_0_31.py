"""3.0.31 插件实例描述符增加默认调用目标标记。

Revision ID: e0e68cbd5756
Revises: 487f7e681955
Create Date: 2026-09-03
"""

import sqlalchemy as sa
from alembic import op

revision = "e0e68cbd5756"
down_revision = "487f7e681955"
branch_labels = None
depends_on = None

_TABLE = "plugininstance"
_DEFAULT_TARGET_INDEX = "ux_plugininstance_default_target"


def _column_names() -> set[str]:
    """读取当前表已有列名，兼容重复升级和已由 create_all 建出当前模型的场景。"""
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(_TABLE)}


def _index_names() -> set[str]:
    """读取当前表已有索引名，兼容重复升级和已由 create_all 建出当前模型的场景。"""
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(_TABLE)}


def upgrade() -> None:
    """增加默认调用目标标记列，并建出「同一源插件至多一个默认调用目标」的条件唯一索引。

    条件谓词按方言分别给出：布尔列与 ``True`` 比较，SQLite 编译为 ``IS 1``，
    PostgreSQL 编译为 ``IS true``；谓词整个丢失会退化成「每个源插件只能有一行
    实例」，把插件分身整个锁死，因此必须两个方言各给一份，不能只给一份共用。
    """
    columns = _column_names()
    if "is_default_target" not in columns:
        op.add_column(
            _TABLE,
            sa.Column(
                "is_default_target",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )
    if _DEFAULT_TARGET_INDEX not in _index_names():
        op.create_index(
            _DEFAULT_TARGET_INDEX,
            _TABLE,
            ["source_plugin_id"],
            unique=True,
            sqlite_where=sa.column("is_default_target", sa.Boolean()).is_(True),
            postgresql_where=sa.column("is_default_target", sa.Boolean()).is_(True),
        )


def downgrade() -> None:
    """删除条件唯一索引与默认调用目标标记列。"""
    if _DEFAULT_TARGET_INDEX in _index_names():
        op.drop_index(_DEFAULT_TARGET_INDEX, table_name=_TABLE)
    if "is_default_target" in _column_names():
        op.drop_column(_TABLE, "is_default_target")
