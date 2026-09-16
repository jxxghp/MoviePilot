"""3.0.31 保存订阅搜索重试站点并解除存量批次的累计空等。"""

# Alembic 的 op 是运行期代理。
# pylint: disable=no-member

from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision = "a9c3e7f1b5d8"
down_revision = "f8a2c6e9b1d4"
branch_labels = None
depends_on = None

_TABLE = "subscriptionsearchtask"


def upgrade() -> None:
    """增加可恢复站点游标，只提前尚未执行的自动任务，不绕过冷却和用户停止。"""
    inspector = sa.inspect(op.get_bind())
    if _TABLE not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns(_TABLE)}
    if "pending_site_ids" in columns:
        return
    op.add_column(_TABLE, sa.Column("pending_site_ids", sa.JSON(none_as_null=True), nullable=True))
    tasks = sa.table(
        _TABLE,
        sa.column("source"), sa.column("state"), sa.column("phase"),
        sa.column("attempt_count"), sa.column("cancel_requested"), sa.column("available_at"),
    )
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    op.execute(
        tasks.update().where(
            tasks.c.source == "fallback", tasks.c.state == "queued", tasks.c.phase == "queued",
            tasks.c.attempt_count == 0, tasks.c.cancel_requested == 0, tasks.c.available_at > now,
        ).values(available_at=now)
    )


def downgrade() -> None:
    """移除站点游标；恢复旧版本后未完成任务仍可完整重试。"""
    inspector = sa.inspect(op.get_bind())
    if _TABLE in inspector.get_table_names():
        columns = {column["name"] for column in inspector.get_columns(_TABLE)}
        if "pending_site_ids" in columns:
            op.drop_column(_TABLE, "pending_site_ids")
