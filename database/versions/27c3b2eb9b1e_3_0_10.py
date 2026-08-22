"""3.0.10
插件配置迁入插件实例配置表，并登记插件实例的默认调用目标

Revision ID: 27c3b2eb9b1e
Revises: 58e7ce2cd0fd
Create Date: 2026-08-19
"""

import time

from alembic import op
import sqlalchemy as sa

from app.runtime.extensions.contract.instance import DEFAULT_INSTANCE_ID


revision = "27c3b2eb9b1e"
down_revision = "58e7ce2cd0fd"
branch_labels = None
depends_on = None

_KEY_PREFIX = "plugin."

_DEFAULT_TARGET_COLUMN = "is_default_target"
_DEFAULT_TARGET_INDEX = "ux_pluginconfig_default_target"


def _inspector() -> sa.Inspector:
    """返回使用当前迁移连接的数据库检查器。"""
    return sa.inspect(op.get_bind())


def _has_table(table_name: str) -> bool:
    """检查表是否存在。"""
    return table_name in _inspector().get_table_names()


def _has_column(table_name: str, column_name: str) -> bool:
    """检查表中是否存在指定列。"""
    if not _has_table(table_name):
        return False
    return column_name in {column["name"] for column in _inspector().get_columns(table_name)}


def _has_index(table_name: str, index_name: str) -> bool:
    """检查表索引或唯一约束是否存在。"""
    if not _has_table(table_name):
        return False
    inspector = _inspector()
    names = {index.get("name") for index in inspector.get_indexes(table_name)}
    names.update(
        constraint.get("name")
        for constraint in inspector.get_unique_constraints(table_name)
    )
    return index_name in names


def _default_target_predicate() -> sa.sql.ClauseElement:
    """返回默认调用目标条件索引的谓词表达式。"""
    return sa.column(_DEFAULT_TARGET_COLUMN, sa.Boolean()).is_(True)


def _upgrade_default_target_schema() -> None:
    """给插件实例配置表加上默认调用目标列与其条件唯一索引。

    条件唯一索引表达「每个插件至多一个默认调用目标」：只索引置位的行，同一插件
    因此可以有任意多行未置位、至多一行置位。部分索引的谓词是方言特性，SQLite 与
    PostgreSQL 各给一份，两边分别渲染成 ``IS 1`` 与 ``IS true``。
    """
    if not _has_table("pluginconfig"):
        return
    if not _has_column("pluginconfig", _DEFAULT_TARGET_COLUMN):
        op.add_column(
            "pluginconfig",
            sa.Column(
                _DEFAULT_TARGET_COLUMN,
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )
    if not _has_index("pluginconfig", _DEFAULT_TARGET_INDEX):
        op.create_index(
            _DEFAULT_TARGET_INDEX,
            "pluginconfig",
            ["plugin_id"],
            unique=True,
            sqlite_where=_default_target_predicate(),
            postgresql_where=_default_target_predicate(),
        )


def _downgrade_default_target_schema() -> None:
    """删除默认调用目标的条件唯一索引与列。"""
    if not _has_table("pluginconfig"):
        return
    if _has_index("pluginconfig", _DEFAULT_TARGET_INDEX):
        op.drop_index(_DEFAULT_TARGET_INDEX, table_name="pluginconfig")
    if _has_column("pluginconfig", _DEFAULT_TARGET_COLUMN):
        op.drop_column("pluginconfig", _DEFAULT_TARGET_COLUMN)


def _now() -> str:
    """返回与 PluginConfigOper 写入格式一致的当前时间字符串。"""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def _derive_is_enabled(config_data: object) -> bool:
    """
    从业务配置字典推导启用态。

    现存插件配置对启用开关的键名不统一，既有 ``enable`` 也有 ``enabled``——插件
    分身创建逻辑（``PluginManager.clone_plugin``）对两个键都做了清空，即为此留
    下的实测依据。两个键任一为真值即视为已启用。
    :param config_data: 插件业务配置字典
    :return: 推导出的启用态
    """
    if not isinstance(config_data, dict):
        return False
    return bool(config_data.get("enable")) or bool(config_data.get("enabled"))


def _systemconfig_table() -> sa.Table:
    """返回 systemconfig 表的核心层引用。"""
    return sa.table(
        "systemconfig",
        sa.column("id", sa.Integer()),
        sa.column("key", sa.String()),
        sa.column("value", sa.JSON()),
    )


def _pluginconfig_table() -> sa.Table:
    """返回 pluginconfig 表的核心层引用。"""
    return sa.table(
        "pluginconfig",
        sa.column("id", sa.Integer()),
        sa.column("plugin_id", sa.String()),
        sa.column("instance_id", sa.String()),
        sa.column("is_enabled", sa.Boolean()),
        sa.column("config_data", sa.JSON()),
        sa.column("follow_default_version", sa.Boolean()),
        sa.column(_DEFAULT_TARGET_COLUMN, sa.Boolean()),
        sa.column("created_at", sa.String()),
        sa.column("updated_at", sa.String()),
    )


def _migrate_plugin_config_rows() -> None:
    """把 systemconfig 中 plugin.<插件ID> 的行迁入 pluginconfig 默认实例，并删除原键。

    ``follow_default_version`` 与 ``is_default_target`` 显式写死（与 ``PluginConfig``
    模型的默认值一致），不依赖列上的 server_default——迁移用的是 ``sa.table()``
    构造的核心层引用，这类引用不携带列默认值，全新安装通过 ``create_all`` 建表时
    这两列也只在 ORM 层声明了默认值，未必有数据库端默认，省略后会在写入时触发
    NOT NULL 冲突。

    ``is_default_target`` 一律写 False，不把迁入的实例顺手认作默认调用目标：默认
    调用目标是用户的显式选择，迁移替用户选一个，与运行期「没指定就取第一个」的
    隐式回落是同一类错误，只是发生在建库那一刻。
    """
    systemconfig = _systemconfig_table()
    pluginconfig = _pluginconfig_table()
    connection = op.get_bind()
    rows = connection.execute(
        sa.select(systemconfig.c.id, systemconfig.c.key, systemconfig.c.value)
    ).mappings().all()
    now = _now()
    for row in rows:
        key = row["key"] or ""
        if not key.startswith(_KEY_PREFIX):
            continue
        plugin_id = key[len(_KEY_PREFIX):]
        if not plugin_id:
            continue
        config_data = row["value"]
        connection.execute(
            pluginconfig.insert().values(
                plugin_id=plugin_id,
                instance_id=DEFAULT_INSTANCE_ID,
                is_enabled=_derive_is_enabled(config_data),
                config_data=config_data,
                follow_default_version=True,
                is_default_target=False,
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(systemconfig.delete().where(systemconfig.c.id == row["id"]))


def upgrade() -> None:
    """补齐默认调用目标结构，并把 systemconfig 中 plugin.<插件ID> 的插件配置迁入默认实例行。

    结构变更与数据搬迁互不依赖：systemconfig 表缺席时数据搬迁无从谈起，但列与索引
    仍必须建出来，否则模型与库结构就此错位。
    """
    _upgrade_default_target_schema()
    if not _has_table("systemconfig") or not _has_table("pluginconfig"):
        return
    _migrate_plugin_config_rows()


def _has_non_default_instance_rows() -> bool:
    """检查插件实例配置表中是否存在非默认实例的行。"""
    pluginconfig = sa.table("pluginconfig", sa.column("instance_id", sa.String()))
    connection = op.get_bind()
    row = connection.execute(
        sa.select(pluginconfig.c.instance_id)
        .where(pluginconfig.c.instance_id != DEFAULT_INSTANCE_ID)
        .limit(1)
    ).first()
    return row is not None


def downgrade() -> None:
    """
    把默认实例的插件配置迁回 systemconfig，清空对应的实例配置行，并撤销默认调用目标结构。

    存在非默认实例的行时拒绝降级：systemconfig 的 plugin.<插件ID> 只能承载单一
    配置，降级会直接丢弃插件分身各自的配置。结构撤销放在数据搬迁之后，搬迁被拒时
    列与索引原样保留。
    """
    if not _has_table("pluginconfig") or not _has_table("systemconfig"):
        _downgrade_default_target_schema()
        return
    if _has_non_default_instance_rows():
        raise RuntimeError("pluginconfig 存在非默认实例数据，拒绝降级以避免丢失插件分身配置")

    systemconfig = _systemconfig_table()
    pluginconfig = _pluginconfig_table()
    connection = op.get_bind()
    rows = connection.execute(
        sa.select(pluginconfig.c.id, pluginconfig.c.plugin_id, pluginconfig.c.config_data)
    ).mappings().all()
    for row in rows:
        connection.execute(
            systemconfig.insert().values(
                key=f"{_KEY_PREFIX}{row['plugin_id']}",
                value=row["config_data"],
            )
        )
        connection.execute(pluginconfig.delete().where(pluginconfig.c.id == row["id"]))
    _downgrade_default_target_schema()
