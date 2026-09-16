"""3.0.35 插件配置迁入插件实例表。

Revision ID: c4e1a7b9d2f6
Revises: 281965691a20
Create Date: 2026-09-11
"""

import json
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision = "c4e1a7b9d2f6"
down_revision = "281965691a20"
branch_labels = None
depends_on = None

_TABLE = "plugininstance"
_LEGACY_PREFIX = "plugin."


def _table_names(connection) -> set:
    """读取当前数据库已有表名。"""
    return set(sa.inspect(connection).get_table_names())


def _column_names(connection) -> set:
    """读取实例表已有列名；表不存在时为空集。"""
    if _TABLE not in _table_names(connection):
        return set()
    return {column["name"] for column in sa.inspect(connection).get_columns(_TABLE)}


def _decode(value):
    """把 JSON 列还原为 Python 值，兼容驱动直接返回原始字符串的情形。"""
    if isinstance(value, (str, bytes, bytearray)):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return value
    return value


def upgrade() -> None:
    """把每实例的业务参数收拢到它自己那一行上。

    插件配置此前寄存在 systemconfig 的 plugin.<实例ID> 单键下：键由插件 ID 决定、
    条目数随安装量增长，混在系统设置里会把主程序自己的设置项淹掉；而且那个键存的
    其实是实例 ID，表里没有任何一列说得出它属于哪个插件，想列出某插件的全部实例
    配置就只能靠字符串前缀去猜。

    配置与实例身份同存一行，而不是另起一张同键的表：展示信息与业务参数都是这个
    实例的设置，同一个生命周期，分表只会让「建分身、删分身」退化成两张表之间的
    协调问题。
    """
    connection = op.get_bind()
    now = datetime.now(timezone.utc).isoformat()
    columns = _column_names(connection)
    if not columns:
        return

    if "config_data" not in columns:
        op.add_column(_TABLE, sa.Column("config_data", sa.JSON(), nullable=True))

    if "systemconfig" not in _table_names(connection):
        return

    existing = {
        row[0]
        for row in connection.execute(
            sa.text(f"SELECT instance_id FROM {_TABLE}")
        ).fetchall()
    }
    legacy = connection.execute(
        sa.text("SELECT key, value FROM systemconfig WHERE key LIKE :pattern"),
        {"pattern": f"{_LEGACY_PREFIX}%"},
    ).fetchall()
    migrated_keys = []
    for key, value in legacy:
        instance_id = key[len(_LEGACY_PREFIX):]
        if not instance_id:
            continue
        payload = json.dumps(_decode(value))
        if instance_id in existing:
            connection.execute(
                sa.text(
                    f"UPDATE {_TABLE} SET config_data = :config_data, updated_at = :now "
                    "WHERE instance_id = :instance_id"
                ),
                {"config_data": payload, "instance_id": instance_id, "now": now},
            )
        else:
            # 没有实例行的只可能是本体自身：分身的行在建分身时就已落盘
            connection.execute(
                sa.text(
                    f"INSERT INTO {_TABLE} "
                    "(instance_id, source_plugin_id, config_data, created_at, updated_at) "
                    "VALUES (:instance_id, :instance_id, :config_data, :now, :now)"
                ),
                {"instance_id": instance_id, "config_data": payload, "now": now},
            )
            existing.add(instance_id)
        migrated_keys.append(key)

    # 同一份配置不能在两处各留一份，否则读取端按哪个都不对
    for key in migrated_keys:
        connection.execute(
            sa.text("DELETE FROM systemconfig WHERE key = :key"),
            {"key": key},
        )


def downgrade() -> None:
    """把业务参数搬回 systemconfig 原键，并移除新增列，回退路径上不丢配置。"""
    connection = op.get_bind()
    columns = _column_names(connection)
    if "config_data" not in columns:
        return

    if "systemconfig" in _table_names(connection):
        rows = connection.execute(
            sa.text(
                f"SELECT instance_id, config_data FROM {_TABLE} WHERE config_data IS NOT NULL"
            )
        ).fetchall()
        for instance_id, config_data in rows:
            key = f"{_LEGACY_PREFIX}{instance_id}"
            exists = connection.execute(
                sa.text("SELECT 1 FROM systemconfig WHERE key = :key"),
                {"key": key},
            ).fetchone()
            if not exists:
                connection.execute(
                    sa.text("INSERT INTO systemconfig (key, value) VALUES (:key, :value)"),
                    {"key": key, "value": json.dumps(_decode(config_data))},
                )

    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_column("config_data")
