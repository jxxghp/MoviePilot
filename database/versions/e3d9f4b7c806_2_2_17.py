"""2.2.17
新增待整理文件登记表

整理队列是纯内存的 queue.Queue，进程重启会让队列里的任务连同「这些文件还没
整理」这个事实一起蒸发，而已稳定落地的文件不会再产生任何监控事件，等于永久
漏件。该表只登记「存储 + 源文件路径」这一最小事实，供启动时回放。

Revision ID: e3d9f4b7c806
Revises: 7f5c1d2e3a4b
Create Date: 2026-08-10
"""

from alembic import op
import sqlalchemy as sa

revision = "e3d9f4b7c806"
down_revision = "7f5c1d2e3a4b"
branch_labels = None
depends_on = None


def _has_table(table_name: str) -> bool:
    """检查数据表是否已存在。"""
    inspector = sa.inspect(op.get_bind())
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    """
    创建待整理文件登记表。
    """
    if _has_table("transferpending"):
        return
    op.create_table(
        "transferpending",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("storage", sa.String, nullable=False),
        sa.Column("src_path", sa.String, nullable=False),
        sa.Column("created_at", sa.String),
    )
    # 同一个文件重复入队只保留一条，回放时不会重复送入整理链
    op.create_index(
        "ux_transferpending_storage_path",
        "transferpending",
        ["storage", "src_path"],
        unique=True,
    )


def downgrade() -> None:
    """
    删除待整理文件登记表。
    """
    if not _has_table("transferpending"):
        return
    op.drop_index("ux_transferpending_storage_path", table_name="transferpending")
    op.drop_table("transferpending")