"""
3.1.9 新增小组件专用只读凭据表，不迁移主登录凭据或生成默认授权。
"""

from alembic import op
import sqlalchemy as sa

revision = "4f62a73e81c9"
down_revision = "e4a8c7b29016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    为旧库建表；新 ORM 已建表时不重复创建，维持 fresh 启动幂等。
    """
    if sa.inspect(op.get_bind()).has_table("widgetregistration"):
        return
    op.create_table(
        "widgetregistration",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("registration_id", sa.String(36), nullable=False),
        sa.Column("installation_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="CASCADE"), nullable=False),
        sa.Column("owner", sa.String(32), nullable=False),
        sa.Column("secret_digest", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.UniqueConstraint("registration_id", name="uq_widgetregistration_registration"),
        sa.UniqueConstraint("user_id", "installation_id", name="uq_widgetregistration_installation"),
    )
    op.create_index("ix_widgetregistration_owner", "widgetregistration", ["owner"])


def downgrade() -> None:
    """
    仅删除小组件授权；不触碰用户、权限或其它业务历史。
    """
    if sa.inspect(op.get_bind()).has_table("widgetregistration"):
        op.drop_table("widgetregistration")
