"""3.0.39 专辑订阅记录已接受资源中的音轨事实。"""

import sqlalchemy as sa
from alembic import op

revision = "c8e4a1f7b2d6"
down_revision = "b7d1e4a9c206"
branch_labels = None
depends_on = None

_TABLE = "subscribe"
_COLUMN = "downloaded_tracks"


def _table_exists(connection) -> bool:
    """判断订阅表是否已经由基础模型创建。"""
    return _TABLE in sa.inspect(connection).get_table_names()


def _column_names(connection) -> set[str]:
    """读取订阅表现有列名，兼容表尚未创建或模型已包含当前列的场景。"""
    inspector = sa.inspect(connection)
    if not _table_exists(connection):
        return set()
    return {column["name"] for column in inspector.get_columns(_TABLE)}


def upgrade() -> None:
    """增加专辑分批下载所需的音轨事实列，旧订阅从空事实开始累计。"""
    connection = op.get_bind()
    if _table_exists(connection) and _COLUMN not in _column_names(connection):
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.JSON(), nullable=True))


def downgrade() -> None:
    """删除专辑音轨事实列，回退后活动订阅进度将丢失。"""
    connection = op.get_bind()
    if _table_exists(connection) and _COLUMN in _column_names(connection):
        op.drop_column(_TABLE, _COLUMN)
