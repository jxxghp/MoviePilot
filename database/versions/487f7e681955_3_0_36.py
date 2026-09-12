"""3.0.36 插件实例增加日志等级覆盖。

Revision ID: 487f7e681955
Revises: c4e1a7b9d2f6
Create Date: 2026-09-12
"""

import sqlalchemy as sa
from alembic import op

revision = "487f7e681955"
down_revision = "c4e1a7b9d2f6"
branch_labels = None
depends_on = None

_TABLE = "plugininstance"


def _column_names() -> set:
    """读取实例表已有列名；表不存在时为空集。

    兼容重复升级，以及表已由 create_all 按当前模型建出、两列本就存在的场景。
    """
    inspector = sa.inspect(op.get_bind())
    if _TABLE not in set(inspector.get_table_names()):
        return set()
    return {column["name"] for column in inspector.get_columns(_TABLE)}


def upgrade() -> None:
    """给实例行加上日志等级覆盖与失效时间，缺省为空即跟随全局等级。

    等级落在实例自己那一行，而不是另起一张按实例 ID 索引的表：它和业务参数一样是
    用户按实例设置的东西，同一个生命周期，实例被删时应当随行一起消失。
    """
    columns = _column_names()
    if not columns:
        return
    if "log_level" not in columns:
        op.add_column(_TABLE, sa.Column("log_level", sa.String(length=16), nullable=True))
    if "log_expires_at" not in columns:
        op.add_column(_TABLE, sa.Column("log_expires_at", sa.String(length=40), nullable=True))


def downgrade() -> None:
    """删除日志等级覆盖列；覆盖本就带失效时间，回退丢掉它不影响插件功能。"""
    columns = _column_names()
    if "log_expires_at" in columns:
        op.drop_column(_TABLE, "log_expires_at")
    if "log_level" in columns:
        op.drop_column(_TABLE, "log_level")
