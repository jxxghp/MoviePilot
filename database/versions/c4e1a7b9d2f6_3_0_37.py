"""3.0.37 插件配置迁入插件实例表，实例角色改为派生。

Revision ID: c4e1a7b9d2f6
Revises: e0e68cbd5756
Create Date: 2026-09-11
"""

import json
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision = "c4e1a7b9d2f6"
down_revision = "e0e68cbd5756"
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


def _without_mode_constraint(connection) -> sa.Table:
    """反射实例表并摘掉限定 mode 取值的 CHECK 约束。

    SQLite 删列靠重建表：直接批量删列会把反射到的 CHECK 原样带进新表，而它引用的
    正是要删掉的那一列，重建当场失败。先把这条约束从反射结果里摘掉，重建出的新表
    自然就不再带它。
    """
    table = sa.Table(_TABLE, sa.MetaData(), autoload_with=connection)
    for constraint in list(table.constraints):
        if isinstance(constraint, sa.CheckConstraint) and "mode" in str(constraint.sqltext):
            table.constraints.discard(constraint)
    return table


def upgrade() -> None:
    """把每实例的配置收拢到实例行上，并让本体与分身的角色回归派生。

    插件配置此前寄存在 systemconfig 的 plugin.<实例ID> 单键下：键由插件 ID 决定、
    条目数随安装量增长，混在系统设置里会把主程序自己的设置项淹掉；而且那个键存的
    其实是实例 ID，表里没有任何一列说得出它属于哪个插件，想列出某插件的全部实例
    配置就只能靠字符串前缀去猜。

    配置与实例身份同存一行，而不是另起一张同键的表：展示信息、锚定版本、日志等级
    与业务参数都是这个实例的设置，同一个生命周期，分表只会让「卸载保留、重建恢复」
    退化成两张表之间的协调问题。

    mode 列同时删除：它是从 instance_id 是否等于 source_plugin_id 复制出来的冗余
    副本，两者一旦失步，同一行就会在不同读取口被判成不同角色。

    本体的装载判据一并归口到 is_enabled：安装清单 UserInstalledPlugins 退回去只
    回答「包在不在磁盘上」，不再兼任运行开关，因而每个已安装插件都要有一行启用中
    的本体记录。
    """
    connection = op.get_bind()
    now = datetime.now(timezone.utc).isoformat()
    columns = _column_names(connection)
    if not columns:
        return

    # 一、补齐承载配置与启用位所需的列
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
    if "config_data" not in columns:
        op.add_column(_TABLE, sa.Column("config_data", sa.JSON(), nullable=True))
    # 现存的每一行此刻都是在册且被装载着的实例，补列不得把它们整批判成未启用
    connection.execute(sa.text(f"UPDATE {_TABLE} SET is_enabled = 1"))

    # 二、把 systemconfig 里的插件配置搬到对应实例行上
    migrated_keys = []
    if "systemconfig" in _table_names(connection):
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
                        "(instance_id, source_plugin_id, is_enabled, is_default_target, "
                        "config_data, created_at, updated_at) "
                        "VALUES (:instance_id, :instance_id, 1, 0, :config_data, :now, :now)"
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

        # 三、为已安装但尚无本体行的插件补出启用中的本体行
        #
        # 本体的装载判据已经归口到 is_enabled，安装清单退回去只回答「包在不在磁盘
        # 上」。装了却从没存过配置的插件此前根本没有行，不补出来，升级后它们会因为
        # 「没有启用中的本体行」而整批不再加载。
        installed = connection.execute(
            sa.text("SELECT value FROM systemconfig WHERE key = 'UserInstalledPlugins'")
        ).scalar_one_or_none()
        for plugin_id in _decode(installed) or []:
            if not isinstance(plugin_id, str) or not plugin_id:
                continue
            if plugin_id in existing:
                continue
            connection.execute(
                sa.text(
                    f"INSERT INTO {_TABLE} "
                    "(instance_id, source_plugin_id, is_enabled, is_default_target, "
                    "created_at, updated_at) "
                    "VALUES (:instance_id, :instance_id, 1, 0, :now, :now)"
                ),
                {"instance_id": plugin_id, "now": now},
            )
            existing.add(plugin_id)

    # 四、删掉冗余的 mode 列及其取值约束
    if "mode" in columns:
        with op.batch_alter_table(_TABLE, copy_from=_without_mode_constraint(connection)) as batch:
            batch.drop_column("mode")


def downgrade() -> None:
    """还原 mode 列，把业务参数搬回 systemconfig，并移除新增列。"""
    connection = op.get_bind()
    columns = _column_names(connection)
    if not columns:
        return

    if "mode" not in columns:
        op.add_column(
            _TABLE,
            sa.Column(
                "mode",
                sa.String(length=16),
                nullable=False,
                server_default="virtual",
            ),
        )
        connection.execute(
            sa.text(
                f"UPDATE {_TABLE} SET mode = CASE "
                "WHEN instance_id = source_plugin_id THEN 'host' ELSE 'virtual' END"
            )
        )

    if "config_data" in columns and "systemconfig" in _table_names(connection):
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
        for name in ("config_data", "is_enabled"):
            if name in columns:
                batch.drop_column(name)
