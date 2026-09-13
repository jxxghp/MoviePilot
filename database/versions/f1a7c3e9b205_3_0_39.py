"""3.0.39 合并目录整理批次与插件启用迁移分支。

Revision ID: f1a7c3e9b205
Revises: b7d1e4a9c206, d6f4b2a9c813
Create Date: 2026-09-13
"""

revision = "f1a7c3e9b205"
down_revision = ("b7d1e4a9c206", "d6f4b2a9c813")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """两个父迁移均已完成实际结构变更。"""


def downgrade() -> None:
    """降级到两个父 revision，不额外修改结构。"""
