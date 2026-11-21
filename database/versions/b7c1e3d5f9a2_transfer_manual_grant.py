"""为持久整理任务增加单份待消费的人工身份修正授权。"""

from alembic import op
import sqlalchemy as sa

revision = "b7c1e3d5f9a2"
down_revision = "a7383c1d9e20"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    在旧整理任务表增加授权状态槽，并兼容 ORM 已创建最新结构的启动流程。
    """
    inspector = sa.inspect(op.get_bind())
    if "transferpending" not in inspector.get_table_names():
        return
    if "manual_identity_authorization" in {
        column["name"] for column in inspector.get_columns("transferpending")
    }:
        return
    op.add_column(
        "transferpending",
        sa.Column("manual_identity_authorization", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    """
    移除待消费授权槽，不触碰任务或历史记录。
    """
    inspector = sa.inspect(op.get_bind())
    if "transferpending" not in inspector.get_table_names():
        return
    if "manual_identity_authorization" not in {
        column["name"] for column in inspector.get_columns("transferpending")
    }:
        return
    op.drop_column("transferpending", "manual_identity_authorization")
