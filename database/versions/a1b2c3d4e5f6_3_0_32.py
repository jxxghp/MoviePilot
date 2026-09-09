"""3.0.32 增加整理失败的阶段、恢复动作、重试和下载器清理状态。"""

import sqlalchemy as sa
from alembic import op

revision = "a1b2c3d4e5f6"
down_revision = "a9c3e7f1b5d8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """兼容已有数据库，补充失败闭环的可见状态字段。"""
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("transferhistory")}
    additions = (
        ("retry_count", sa.Integer(), "失败重试次数"),
        ("auto_paused", sa.Boolean(), "自动整理暂停状态"),
        ("failure_stage", sa.String(), "失败阶段"),
        ("recovery_action", sa.String(), "恢复动作"),
        ("cleanup_status", sa.String(), "下载器清理状态"),
        ("cleanup_error", sa.String(), "下载器清理错误"),
    )
    for column_name, column_type, _description in additions:
        if column_name not in columns:
            if column_name == "auto_paused":
                op.add_column(
                    "transferhistory",
                    sa.Column(
                        column_name,
                        column_type,
                        nullable=False,
                        server_default=sa.false(),
                    ),
                )
            else:
                op.add_column("transferhistory", sa.Column(column_name, column_type, nullable=True))


def downgrade() -> None:
    """移除失败闭环字段，保留原有整理历史数据。"""
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("transferhistory")}
    for column_name in (
        "cleanup_error",
        "cleanup_status",
        "recovery_action",
        "failure_stage",
        "auto_paused",
        "retry_count",
    ):
        if column_name in columns:
            op.drop_column("transferhistory", column_name)
