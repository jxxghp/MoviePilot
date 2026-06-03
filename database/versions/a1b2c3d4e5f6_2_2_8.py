"""2.2.8
为用户表新增 openid_sub 字段，支持 OIDC 登录绑定

Revision ID: a1b2c3d4e5f6
Revises: 1f0d2c3b4a5e
Create Date: 2026-06-03
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "a1b2c3d4e5f6"
down_revision = "1f0d2c3b4a5e"
branch_labels = None
depends_on = None


def _has_column(inspector: sa.Inspector, table_name: str, column_name: str) -> bool:
    if table_name not in inspector.get_table_names():
        return False
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def _has_index(inspector: sa.Inspector, table_name: str, index_name: str) -> bool:
    return any(index["name"] == index_name for index in inspector.get_indexes(table_name))


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if _has_column(inspector, "user", "openid_sub") is False:
        op.add_column("user", sa.Column("openid_sub", sa.String(), nullable=True))

    # 为 openid_sub 创建索引，加速按 OIDC 标识查询用户
    if _has_index(inspector, "user", "ix_user_openid_sub") is False:
        op.create_index("ix_user_openid_sub", "user", ["openid_sub"], unique=False)


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if _has_index(inspector, "user", "ix_user_openid_sub"):
        op.drop_index("ix_user_openid_sub", table_name="user")

    if _has_column(inspector, "user", "openid_sub"):
        op.drop_column("user", "openid_sub")
