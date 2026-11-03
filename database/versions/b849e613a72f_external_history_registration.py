"""
为外部整理登记增加可空来源和审计列，旧记录保持未记录来源。
"""

import sqlalchemy as sa
from alembic import op

revision = "b849e613a72f"
down_revision = "97ad7239c1e0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    幂等添加登记列，不重建或覆盖既有历史与源地址唯一索引。
    """
    inspector = sa.inspect(op.get_bind())
    if "transferhistory" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("transferhistory")}
    for name, kind in (("origin", sa.String(16)), ("registration", sa.JSON())):
        if name not in columns:
            op.add_column("transferhistory", sa.Column(name, kind, nullable=True))


def downgrade() -> None:
    """
    删除外部登记附加列，保留所有历史行及原有唯一约束。
    """
    inspector = sa.inspect(op.get_bind())
    if "transferhistory" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("transferhistory")}
    for name in ("registration", "origin"):
        if name in columns:
            op.drop_column("transferhistory", name)
