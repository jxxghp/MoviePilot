"""3.0.38 插件实例版本绑定合并为单一锚定列。

Revision ID: f3b8c2d5e7a1
Revises: c4e1a7b9d2f6
Create Date: 2026-09-11
"""

import sqlalchemy as sa
from alembic import op

revision = "f3b8c2d5e7a1"
down_revision = "c4e1a7b9d2f6"
branch_labels = None
depends_on = None

_TABLE = "plugininstance"


def _column_names() -> set:
    """读取当前表已有列名，兼容重复升级和已由 create_all 建出当前模型的场景。"""
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(_TABLE)}


def upgrade() -> None:
    """用 pinned_version 取代 plugin_version + follow_current_version。

    旧结构要求两列保持一致：跟随当前版本时 plugin_version 必须被忽略，锚定时又
    必须与目标版本同步落盘。这个隐式不变量反复造成绑定落空，单列让非法状态根本
    无法表示——为空即跟随，非空即锚定。
    """
    columns = _column_names()
    if "pinned_version" not in columns:
        op.add_column(_TABLE, sa.Column("pinned_version", sa.String(length=64), nullable=True))

    if "plugin_version" in columns and "follow_current_version" in columns:
        # 只有明确不跟随当前版本的实例才带着锚定版本；其余一律为空
        op.get_bind().execute(
            sa.text(
                f"UPDATE {_TABLE} SET pinned_version = plugin_version "
                "WHERE follow_current_version = 0 AND plugin_version IS NOT NULL"
            )
        )

    with op.batch_alter_table(_TABLE) as batch:
        if "plugin_version" in columns:
            batch.drop_column("plugin_version")
        if "follow_current_version" in columns:
            batch.drop_column("follow_current_version")


def downgrade() -> None:
    """还原两列结构，并按锚定与否回填它们。"""
    columns = _column_names()
    if "plugin_version" not in columns:
        op.add_column(_TABLE, sa.Column("plugin_version", sa.String(length=64), nullable=True))
    if "follow_current_version" not in columns:
        op.add_column(
            _TABLE,
            sa.Column(
                "follow_current_version",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            ),
        )

    if "pinned_version" in columns:
        connection = op.get_bind()
        connection.execute(
            sa.text(
                f"UPDATE {_TABLE} SET plugin_version = pinned_version, "
                "follow_current_version = 0 WHERE pinned_version IS NOT NULL"
            )
        )
        connection.execute(
            sa.text(
                f"UPDATE {_TABLE} SET follow_current_version = 1 WHERE pinned_version IS NULL"
            )
        )
        with op.batch_alter_table(_TABLE) as batch:
            batch.drop_column("pinned_version")
