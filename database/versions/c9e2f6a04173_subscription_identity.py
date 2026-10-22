"""
为订阅完整身份增加并发唯一仲裁键，保留既有重复订阅及其运行事实。
"""

import hashlib
import json

import sqlalchemy as sa
from alembic import op

revision = "c9e2f6a04173"
down_revision = "b8d2f5a01962"
branch_labels = None
depends_on = None


def _identity_key(row) -> str | None:
    """
    使用本迁移冻结的身份算法，避免未来代码变化改变旧库升级结果。
    """
    if not row["media_source"] or not row["media_id"] or not row["type"] or row["username"] == "":
        return None
    identity = [row[field] for field in (
        "username", "type", "media_source", "media_id", "season", "episode_group", "music_type",
    )]
    if identity[1] == "音乐" and not identity[-1]:
        identity[-1] = "recording"
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def upgrade() -> None:
    """
    每个既有身份的首行持有唯一键，其余旧重复行原样保留，不合并下载进度或历史。
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "identity_key" not in {column["name"] for column in inspector.get_columns("subscribe")}:
        op.add_column("subscribe", sa.Column("identity_key", sa.String(64)))
    if "uq_subscribe_identity_key" in {index["name"] for index in inspector.get_indexes("subscribe")}:
        return
    table = sa.Table("subscribe", sa.MetaData(), autoload_with=bind)
    assigned = set()
    for row in bind.execute(sa.select(table).order_by(table.c.id)).mappings():
        key = _identity_key(row)
        canonical = key if key not in assigned else None
        if key:
            assigned.add(key)
        if row["identity_key"] != canonical:
            bind.execute(table.update().where(table.c.id == row["id"]).values(identity_key=canonical))
    op.create_index("uq_subscribe_identity_key", "subscribe", ["identity_key"], unique=True)


def downgrade() -> None:
    """
    只移除仲裁键，不删除或合并用户订阅数据。
    """
    inspector = sa.inspect(op.get_bind())
    if "uq_subscribe_identity_key" in {index["name"] for index in inspector.get_indexes("subscribe")}:
        op.drop_index("uq_subscribe_identity_key", table_name="subscribe")
    if "identity_key" in {column["name"] for column in inspector.get_columns("subscribe")}:
        op.drop_column("subscribe", "identity_key")
