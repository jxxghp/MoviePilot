"""3.0.25 插件实例描述符增加日志等级覆盖。

Revision ID: 487f7e681955
Revises: 281965691a20
Create Date: 2026-09-02
"""

import sqlalchemy as sa
from alembic import op

revision = "487f7e681955"
down_revision = "281965691a20"
branch_labels = None
depends_on = None

_TABLE = "plugininstance"


def _column_names() -> set[str]:
    """读取当前表已有列名，兼容重复升级和已由 create_all 建出当前模型的场景。"""
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(_TABLE)}


def upgrade() -> None:
    """增加日志等级覆盖列，缺省为空即跟随全局等级。"""
    columns = _column_names()
    if "log_level" not in columns:
        op.add_column(_TABLE, sa.Column("log_level", sa.String(length=16), nullable=True))
    if "log_expires_at" not in columns:
        op.add_column(_TABLE, sa.Column("log_expires_at", sa.String(length=40), nullable=True))


def downgrade() -> None:
    """删除日志等级覆盖列。"""
    columns = _column_names()
    if "log_expires_at" in columns:
        op.drop_column(_TABLE, "log_expires_at")
    if "log_level" in columns:
        op.drop_column(_TABLE, "log_level")
