"""3.0.38 插件实例增加启用位，并把本体的装载判据搬到这一列上。

Revision ID: b7d1e4a9c206
Revises: e0e68cbd5756
Create Date: 2026-09-13
"""

import json
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision = "b7d1e4a9c206"
down_revision = "e0e68cbd5756"
branch_labels = None
depends_on = None

_TABLE = "plugininstance"
_INSTALLED_KEY = "UserInstalledPlugins"


def _table_names(connection) -> set:
    """读取当前数据库已有表名。"""
    return set(sa.inspect(connection).get_table_names())


def _column_names(connection) -> set:
    """读取实例表已有列名；表不存在时为空集。"""
    if _TABLE not in _table_names(connection):
        return set()
    return {column["name"] for column in sa.inspect(connection).get_columns(_TABLE)}


def _installed_plugin_ids(connection) -> list:
    """读取安装清单里的插件 ID，载荷不是字符串数组时按空清单处理。"""
    if "systemconfig" not in _table_names(connection):
        return []
    row = connection.execute(
        sa.text("SELECT value FROM systemconfig WHERE key = :key"),
        {"key": _INSTALLED_KEY},
    ).fetchone()
    if row is None or row[0] is None:
        return []
    value = row[0]
    if isinstance(value, (str, bytes, bytearray)):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return []
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def upgrade() -> None:
    """加上启用位，并把存量的分身与已安装插件本体一并登记为启用。

    存量行必须显式补成启用：这一列同时是本体的装载判据，列建出来默认为假而不回填，
    升级后全部插件都会变成「装着但永不加载」。分身按「行存在即在册」的旧语义一律置真；
    本体按安装清单补行并置真——清单在此之前一直兼任运行开关，两者在升级这一刻等价。
    """
    connection = op.get_bind()
    columns = _column_names(connection)
    if not columns:
        return

    if "is_enabled" not in columns:
        op.add_column(
            _TABLE,
            sa.Column(
                "is_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )

    # 布尔列写 Python 布尔而不是整数字面量：PostgreSQL 会拒绝 `= 1`，报
    # column is of type boolean but expression is of type integer
    connection.execute(
        sa.text(f"UPDATE {_TABLE} SET is_enabled = :enabled"),
        {"enabled": True},
    )

    now = datetime.now(timezone.utc).isoformat()
    existing = {
        row[0]
        for row in connection.execute(
            sa.text(f"SELECT instance_id FROM {_TABLE}")
        ).fetchall()
    }
    for plugin_id in _installed_plugin_ids(connection):
        if plugin_id in existing:
            continue
        connection.execute(
            sa.text(
                f"INSERT INTO {_TABLE} "
                "(instance_id, source_plugin_id, is_enabled, created_at, updated_at) "
                "VALUES (:plugin_id, :plugin_id, :enabled, :now, :now)"
            ),
            {"plugin_id": plugin_id, "enabled": True, "now": now},
        )
        existing.add(plugin_id)


def downgrade() -> None:
    """删除启用位；装载判据随之回落安装清单，停用状态无处表达因而丢弃。"""
    if "is_enabled" in _column_names(op.get_bind()):
        op.drop_column(_TABLE, "is_enabled")
