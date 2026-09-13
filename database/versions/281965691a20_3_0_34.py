"""3.0.34 插件实例描述符迁入独立表。

Revision ID: 281965691a20
Revises: b2d4f6a8c1e3
Create Date: 2026-09-02
"""

from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision = "281965691a20"
down_revision = "b2d4f6a8c1e3"
branch_labels = None
depends_on = None

_TABLE = "plugininstance"
_LEGACY_KEY = "PluginInstances"


def _id_column(dialect_name: str) -> sa.Column:
    """保持 PostgreSQL Identity 与 SQLite 整数主键的当前模型语义一致。"""
    if dialect_name == "postgresql":
        return sa.Column(
            "id",
            sa.Integer(),
            sa.Identity(start=1, cycle=True),
            nullable=False,
        )
    return sa.Column("id", sa.Integer(), nullable=False)


def _legacy_entries(connection: sa.engine.Connection) -> list[dict]:
    """读取旧 systemconfig 单键里的实例描述，兼容历史字典与列表两种载荷形态。

    :param connection: 当前迁移事务连接
    :return: 已补全 ``instance_id`` 的实例字典列表，损坏项直接跳过
    """
    systemconfig = sa.table(
        "systemconfig",
        sa.column("key", sa.String()),
        sa.column("value", sa.JSON()),
    )
    row = connection.execute(
        sa.select(systemconfig.c.value).where(systemconfig.c.key == _LEGACY_KEY)
    ).first()
    raw = row[0] if row else None
    if isinstance(raw, dict):
        entries = []
        for instance_id, payload in raw.items():
            if not isinstance(payload, dict) or not instance_id:
                continue
            merged = dict(payload)
            merged.setdefault("instance_id", instance_id)
            entries.append(merged)
        return entries
    if isinstance(raw, list):
        return [
            item
            for item in raw
            if isinstance(item, dict) and item.get("instance_id")
        ]
    return []


def upgrade() -> None:
    """建立插件实例描述符表，并把旧 systemconfig 单键的现有内容逐条搬入。

    实例描述符原本挤在 ``PluginInstances`` 这一个 JSON 单键里：改任何一个分身都要
    读出整份载荷再整份写回，条目数随分身数量增长，而表里没有任何一列说得出某一条
    属于哪个源插件。独立成表之后一实例一行，归属由 ``source_plugin_id`` 显式表达。

    本体与分身不设模式列，角色由 ``instance_id`` 是否等于 ``source_plugin_id`` 派生：
    模式列会是这个等式的冗余副本，两者一旦失步，同一行就会在不同读取口被判成不同角色。

    只搬迁，不删除原 systemconfig 键：原键留作回滚依据，运行期兜底导入也据此
    在表为空而旧键非空时补一次导入。
    """
    inspector = sa.inspect(op.get_bind())
    if _TABLE in inspector.get_table_names():
        return
    op.create_table(
        _TABLE,
        _id_column(op.get_bind().dialect.name),
        sa.Column("instance_id", sa.String(length=128), nullable=False),
        sa.Column("source_plugin_id", sa.String(length=128), nullable=False),
        sa.Column("plugin_name", sa.String(length=255), nullable=True),
        sa.Column("plugin_desc", sa.String(length=255), nullable=True),
        sa.Column("plugin_icon", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.Column("updated_at", sa.String(length=40), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("instance_id", name="uq_plugininstance_instance_id"),
    )
    op.create_index(
        "ix_plugininstance_source_plugin_id",
        _TABLE,
        ["source_plugin_id"],
    )

    connection = op.get_bind()
    entries = _legacy_entries(connection)
    if not entries:
        return
    table = sa.table(
        _TABLE,
        sa.column("instance_id", sa.String()),
        sa.column("source_plugin_id", sa.String()),
        sa.column("plugin_name", sa.String()),
        sa.column("plugin_desc", sa.String()),
        sa.column("plugin_icon", sa.String()),
        sa.column("created_at", sa.String()),
        sa.column("updated_at", sa.String()),
    )
    now = datetime.now(timezone.utc).isoformat()
    rows = []
    seen_instance_ids: set[str] = set()
    for entry in entries:
        instance_id = entry.get("instance_id")
        source_plugin_id = entry.get("source_plugin_id")
        if not instance_id or not source_plugin_id or instance_id in seen_instance_ids:
            continue
        seen_instance_ids.add(instance_id)
        rows.append({
            "instance_id": instance_id,
            "source_plugin_id": source_plugin_id,
            "plugin_name": entry.get("plugin_name"),
            "plugin_desc": entry.get("plugin_desc"),
            "plugin_icon": entry.get("plugin_icon"),
            "created_at": now,
            "updated_at": now,
        })
    if rows:
        connection.execute(table.insert(), rows)


def downgrade() -> None:
    """删除插件实例描述符表，不触碰原 systemconfig 单键。"""
    inspector = sa.inspect(op.get_bind())
    if _TABLE not in inspector.get_table_names():
        return
    op.drop_index("ix_plugininstance_source_plugin_id", table_name=_TABLE)
    op.drop_table(_TABLE)
