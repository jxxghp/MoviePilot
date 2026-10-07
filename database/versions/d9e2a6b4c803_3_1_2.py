"""3.1.2 订阅搜索任务保存逐页进度与不含凭据的候选。"""

import sqlalchemy as sa
from alembic import op

revision = "d9e2a6b4c803"
down_revision = "c8e4a1f7b2d6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """兼容基础模型已建表的启动路径；进行中的旧任务从第一页开始。"""
    if "searchsession" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "searchsession", sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.String(40), nullable=False),
        sa.UniqueConstraint("task_id", name="uq_searchsession_task_id"),
    )


def downgrade() -> None:
    """回退会移除分页快照，下次搜索重新开始。"""
    if "searchsession" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("searchsession")
