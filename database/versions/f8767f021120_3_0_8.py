"""3.0.8
新增插件实例配置表、用户第三方身份绑定表与服务实例配置表

Revision ID: f8767f021120
Revises: 73370ce9bab7
Create Date: 2026-08-19
"""

import json

from alembic import op
import sqlalchemy as sa


revision = "f8767f021120"
down_revision = "73370ce9bab7"
branch_labels = None
depends_on = None

_SERVICECONFIG_DEFAULT_TARGET_COLUMN = "is_default_target"
_SERVICECONFIG_DEFAULT_TARGET_INDEX = "ux_serviceconfig_default_target"
# 内建类型的提供方保留值，与 app.db.models.serviceconfig.BUILTIN_PROVIDER 同值。
# 迁移是历史快照，常量自带副本而不是 import 模型：跟着当前代码一起演进会让旧库
# 重放出与当初不同的取值。
_BUILTIN_PROVIDER = "host:builtin"

# 三族实例配置在 systemconfig 上的存放键，以及各族由宿主消费、不进类型配置载荷的
# 实例级字段。同样是历史快照：这两份对照随宿主功能演进，import 当前代码会让旧库
# 重放出与当初不同的分列结果。
_SERVICE_FAMILIES = (
    ("downloader", "Downloaders", ("path_mapping",)),
    ("mediaserver", "MediaServers", ("sync_interval", "sync_libraries")),
    ("notification", "Notifications", ("switchs",)),
)

# 存储实例配置的族标识、在 systemconfig 上的存放键，以及裸令牌兼容指针在实例级宿主
# 载荷中的键。兼容指针是每个存储类型各一个，回答的是「存量路径 u115:/media 没写实例名
# 时落到哪一份」，不是默认调用目标，故不占用每族至多一行的默认调用目标列。
#
# 首轮搬迁写的是旧键名 is_default，之后由 _rename_storage_bare_token_field 改名。搬迁
# 是历史快照，一次写成新键名会让「已经搬过的库」与「新搬的库」走两条不同的路径，而改名
# 那一步无论如何都要为前者保留。
_STORAGE_CAPABILITY = "storage"
_STORAGE_CONFIG_KEY = "Storages"
_STORAGE_DEFAULT_INSTANCE_FIELD = "is_default"
_STORAGE_BARE_TOKEN_FIELD = "bare_token_target"


def _inspector() -> sa.Inspector:
    """返回使用当前迁移连接的数据库检查器。"""
    return sa.inspect(op.get_bind())


def _has_table(table_name: str) -> bool:
    """检查表是否存在。"""
    return table_name in _inspector().get_table_names()


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


def _id_column(dialect_name: str) -> sa.Column:
    """生成与当前 ORM 一致的自增主键定义。"""
    if dialect_name == "postgresql":
        return sa.Column(
            "id",
            sa.Integer(),
            sa.Identity(start=1, cycle=True),
            primary_key=True,
        )
    return sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True)


def upgrade() -> None:
    """建立插件实例配置表、用户身份绑定表与服务实例配置表，并搬迁存量服务实例配置与存储配置。

    唯一约束随建表一并声明：SQLite 不支持事后 ALTER TABLE 添加约束，只能在
    CREATE TABLE 时一次性带上；这张表没有需要兼容的历史结构，不必再额外处理
    「表已存在但约束缺失」的分支。
    """
    if not _has_table("pluginconfig"):
        dialect_name = op.get_bind().dialect.name
        op.create_table(
            "pluginconfig",
            _id_column(dialect_name),
            sa.Column("plugin_id", sa.String(), nullable=False),
            sa.Column("instance_id", sa.String(), nullable=False, server_default="default"),
            sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("log_level", sa.String(), nullable=True),
            sa.Column("log_expires_at", sa.DateTime(), nullable=True),
            sa.Column("config_data", sa.JSON(), nullable=True),
            sa.Column("plugin_version", sa.String(), nullable=True),
            sa.Column("follow_default_version", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.String(), nullable=True),
            sa.Column("updated_at", sa.String(), nullable=True),
            sa.UniqueConstraint("plugin_id", "instance_id", name="ux_pluginconfig_plugin_instance"),
        )
    if not _has_index("pluginconfig", "ix_pluginconfig_plugin_id"):
        op.create_index(
            "ix_pluginconfig_plugin_id",
            "pluginconfig",
            ["plugin_id"],
        )
    if not _has_index("pluginconfig", "ix_pluginconfig_plugin_id_plugin_version"):
        op.create_index(
            "ix_pluginconfig_plugin_id_plugin_version",
            "pluginconfig",
            ["plugin_id", "plugin_version"],
        )

    _create_useridentity_table()
    _create_serviceconfig_table()
    _migrate_service_configs()
    _migrate_storage_configs()
    _rename_storage_bare_token_field()


def _create_useridentity_table() -> None:
    """建立用户第三方身份绑定表及其唯一约束、外键与索引。

    ``user_id`` 外键带 ``ON DELETE CASCADE``：用户删除时数据库层级联删除其全部
    身份绑定行。``UniqueConstraint("provider", "external_id")`` 禁止同一第三方
    身份绑定到多个本项目用户，不对 ``(user_id, provider)`` 设唯一约束——同一用户
    允许绑定同一 provider 族下的多个实例（如两台媒体服务器）。唯一约束与外键随建表
    一并声明：SQLite 不支持事后 ALTER TABLE 添加约束，只能在 CREATE TABLE 时一次性带上。
    """
    if not _has_table("useridentity"):
        dialect_name = op.get_bind().dialect.name
        op.create_table(
            "useridentity",
            _id_column(dialect_name),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("provider", sa.String(), nullable=False),
            sa.Column("external_id", sa.String(), nullable=False),
            sa.Column("display_name", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(
                ["user_id"], ["user.id"],
                name="fk_useridentity_user_id",
                ondelete="CASCADE",
            ),
            sa.UniqueConstraint(
                "provider", "external_id", name="ux_useridentity_provider_external_id"
            ),
        )
    if not _has_index("useridentity", "ix_useridentity_user_id"):
        op.create_index("ix_useridentity_user_id", "useridentity", ["user_id"])


def _default_target_predicate() -> sa.sql.ClauseElement:
    """返回服务实例默认调用目标条件索引的谓词表达式。"""
    return sa.column(_SERVICECONFIG_DEFAULT_TARGET_COLUMN, sa.Boolean()).is_(True)


def _create_serviceconfig_table() -> None:
    """建立服务实例配置表及其唯一约束与索引。

    ``UniqueConstraint("capability", "type", "name")`` 不含 ``provider``：跨提供方
    生效才能保证扩展换标识重装后同名配置不会变成两条。唯一约束随建表一并声明，
    SQLite 不支持事后 ALTER TABLE 添加约束。

    该约束自带的索引已按最左前缀覆盖「按 capability 列出」与「按 (capability, type)
    取用」两类查询，因此另建的索引只有两条：``ix_serviceconfig_provider`` 按提供方
    筛出「提供方已消失」的配置；``ux_serviceconfig_default_target`` 是条件唯一索引，
    表达「每族至多一个默认调用目标」——只索引置位的行，同一族因而可以有任意多行未
    置位、至多一行置位。部分索引的谓词是方言特性，SQLite 与 PostgreSQL 各给一份，
    两边分别渲染成 ``IS 1`` 与 ``IS true``。
    """
    if not _has_table("serviceconfig"):
        dialect_name = op.get_bind().dialect.name
        op.create_table(
            "serviceconfig",
            _id_column(dialect_name),
            sa.Column("capability", sa.String(), nullable=False),
            sa.Column("type", sa.String(), nullable=False),
            sa.Column("name", sa.String(), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("config", sa.JSON(), nullable=True),
            sa.Column("host_config", sa.JSON(), nullable=True),
            sa.Column(
                _SERVICECONFIG_DEFAULT_TARGET_COLUMN,
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
            sa.Column(
                "provider",
                sa.String(),
                nullable=False,
                server_default=_BUILTIN_PROVIDER,
            ),
            sa.UniqueConstraint(
                "capability", "type", "name", name="ux_serviceconfig_capability_type_name"
            ),
        )
    if not _has_index("serviceconfig", "ix_serviceconfig_provider"):
        op.create_index("ix_serviceconfig_provider", "serviceconfig", ["provider"])
    if not _has_index("serviceconfig", _SERVICECONFIG_DEFAULT_TARGET_INDEX):
        op.create_index(
            _SERVICECONFIG_DEFAULT_TARGET_INDEX,
            "serviceconfig",
            ["capability"],
            unique=True,
            sqlite_where=_default_target_predicate(),
            postgresql_where=_default_target_predicate(),
        )


def _serviceconfig_table() -> sa.Table:
    """返回供数据搬迁使用的服务实例配置表定义。"""
    return sa.Table(
        "serviceconfig",
        sa.MetaData(),
        sa.Column("capability", sa.String()),
        sa.Column("type", sa.String()),
        sa.Column("name", sa.String()),
        sa.Column("enabled", sa.Boolean()),
        sa.Column("config", sa.JSON()),
        sa.Column("host_config", sa.JSON()),
        sa.Column(_SERVICECONFIG_DEFAULT_TARGET_COLUMN, sa.Boolean()),
        sa.Column("provider", sa.String()),
    )


def _service_config_rows(capability: str, value, host_fields: tuple) -> list:
    """把一族存放在 systemconfig 里的配置列表整形为服务实例配置表的行。

    脏数据按四条口径处置，四条都以「与切表前的运行期行为一致」为准，因此搬迁不会
    改变用户看到的实例：

    - 取不到名称或类型的条目丢弃。这类条目在切表前就不产出任何实例（扇出只认具名且
      类型一致的配置），也无从被显式指定；而表按 (capability, type, name) 定位一行，
      根本装不下没有身份的条目。
    - 同名条目后者覆盖前者。切表前扇出按名字建映射，后写入的同样覆盖先写入的，这里
      沿用同一条规则，用户看到的那一份不变。
    - ``default`` 为真的条目可能不止一条，取顺序上第一条。运行期原本就取首个默认标记，
      且同一份输入重复搬迁得到同一结果；不裁决则直接撞上「每族至多一个默认调用目标」
      的条件唯一索引。
    - ``provider`` 一律填内建保留值。存量配置不记提供方，无从还原；该列只用于把「没有
      这个类型」翻译成「由扩展 X 提供而 X 未启用」，填错只影响提示文案，不影响配置生效。

    :param capability: 族标识
    :param value: systemconfig 上该族的配置值
    :param host_fields: 该族由宿主消费的实例级字段名
    :return: 服务实例配置表的行
    """
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    if not isinstance(value, list):
        return []
    records = {}
    for conf in value:
        if not isinstance(conf, dict):
            continue
        name = conf.get("name")
        service_type = conf.get("type")
        if not isinstance(name, str) or not name.strip():
            continue
        if not isinstance(service_type, str) or not service_type.strip():
            continue
        name = name.strip()
        service_type = service_type.strip()
        host_config = {
            field: conf[field] for field in host_fields if conf.get(field) is not None
        }
        records[(service_type, name)] = {
            "capability": capability,
            "type": service_type,
            "name": name,
            "enabled": bool(conf.get("enabled")),
            "config": conf.get("config") or {},
            "host_config": host_config or None,
            _SERVICECONFIG_DEFAULT_TARGET_COLUMN: bool(conf.get("default")),
            "provider": _BUILTIN_PROVIDER,
        }
    default_seen = False
    for record in records.values():
        if not record[_SERVICECONFIG_DEFAULT_TARGET_COLUMN]:
            continue
        if default_seen:
            record[_SERVICECONFIG_DEFAULT_TARGET_COLUMN] = False
            continue
        default_seen = True
    return list(records.values())


def _migrate_service_configs() -> None:
    """把 systemconfig 上三族服务实例配置搬进服务实例配置表。

    表里已有任何一行就整体跳过：这一笔只负责首轮搬迁，重复搬迁会与用户此后在表上做的
    增删改叠加成重复行。跳过判据取「表非空」而不取「本 revision 是否跑过」，因为降级会
    连表一起删掉，再次升级时 alembic 的版本记录已经回退，只有数据本身能证明搬过没有。

    systemconfig 上那三个键只停写不删，搬迁不动它们：读路径改回去即完成回退。
    """
    if not _has_table("serviceconfig") or not _has_table("systemconfig"):
        return
    bind = op.get_bind()
    existing = bind.execute(
        sa.select(sa.func.count()).select_from(sa.table("serviceconfig"))
    ).scalar()
    if existing:
        return
    systemconfig = sa.Table(
        "systemconfig",
        sa.MetaData(),
        sa.Column("key", sa.String()),
        sa.Column("value", sa.JSON()),
    )
    rows = []
    for capability, config_key, host_fields in _SERVICE_FAMILIES:
        value = bind.execute(
            sa.select(systemconfig.c.value).where(systemconfig.c.key == config_key)
        ).scalar()
        rows.extend(_service_config_rows(capability, value, host_fields))
    if rows:
        op.bulk_insert(_serviceconfig_table(), rows)


def _storage_config_rows(value) -> list:
    """把存放在 systemconfig 里的存储配置列表整形为服务实例配置表的行。

    存量配置是一个存储类型一份，搬迁后成为该存储类型的具名实例，并标记为该类型的默认
    实例——裸令牌 ``u115`` 指向默认实例，不标记则所有存量路径 ``u115:/media`` 会整体
    失效。脏数据按三条口径处置：

    - 取不到存储类型的条目丢弃。表按 (族, 类型, 实例名) 定位一行，装不下没有类型的条目。
    - 取不到名称的条目以存储类型为实例名。存量名称是可空的展示名，缺了仍要有实例名。
    - 同一存储类型有多份时取顺序上第一份为默认实例。切表前按类型取配置返回的就是列表里
      的首个匹配项，取第一份即与切表前用户实际用到的那一份一致。

    :param value: systemconfig 上存储配置键的值
    :return: 服务实例配置表的行
    """
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    if not isinstance(value, list):
        return []
    records = {}
    for conf in value:
        if not isinstance(conf, dict):
            continue
        storage_type = conf.get("type")
        if not isinstance(storage_type, str) or not storage_type.strip():
            continue
        storage_type = storage_type.strip()
        name = conf.get("name")
        name = name.strip() if isinstance(name, str) and name.strip() else storage_type
        records[(storage_type, name)] = {
            "capability": _STORAGE_CAPABILITY,
            "type": storage_type,
            "name": name,
            "enabled": True,
            "config": conf.get("config") or {},
            "host_config": {_STORAGE_DEFAULT_INSTANCE_FIELD: False},
            _SERVICECONFIG_DEFAULT_TARGET_COLUMN: False,
            "provider": _BUILTIN_PROVIDER,
        }
    defaulted = set()
    for record in records.values():
        if record["type"] in defaulted:
            continue
        defaulted.add(record["type"])
        record["host_config"] = {_STORAGE_DEFAULT_INSTANCE_FIELD: True}
    return list(records.values())


def _migrate_storage_configs() -> None:
    """把 systemconfig 上的存储配置搬进服务实例配置表。

    跳过判据取「该族已有行」而不是「整张表非空」：三族服务实例配置与存储各搬各的，
    按整张表判定会让先搬的那一族把后搬的挡在门外。systemconfig 上的存储配置键只停写
    不删，搬迁不动它，读取端改回去即完成回退。
    """
    if not _has_table("serviceconfig") or not _has_table("systemconfig"):
        return
    bind = op.get_bind()
    serviceconfig = sa.table("serviceconfig", sa.column("capability", sa.String()))
    existing = bind.execute(
        sa.select(sa.func.count())
        .select_from(serviceconfig)
        .where(serviceconfig.c.capability == _STORAGE_CAPABILITY)
    ).scalar()
    if existing:
        return
    systemconfig = sa.Table(
        "systemconfig",
        sa.MetaData(),
        sa.Column("key", sa.String()),
        sa.Column("value", sa.JSON()),
    )
    value = bind.execute(
        sa.select(systemconfig.c.value).where(systemconfig.c.key == _STORAGE_CONFIG_KEY)
    ).scalar()
    rows = _storage_config_rows(value)
    if rows:
        op.bulk_insert(_serviceconfig_table(), rows)


def _rename_storage_bare_token_field() -> None:
    """把存储行宿主载荷里的旧键 is_default 改名为 bare_token_target。

    改的只是键名，取值原样搬过去：这个标记从来回答的就是「裸令牌落到哪一份」，旧名字
    让它看起来像默认调用目标，而默认调用目标另有专列且对存储也已启用，两者同名会被
    读成同一件事。

    逐行判定、逐行改写，已经是新键名的行跳过：本函数会随每次升级重放，只有「载荷里
    还带着旧键」这件事本身能证明这一行没搬过。两个键都在时以新键为准，旧键直接丢弃。

    :return: 无返回值
    """
    if not _has_table("serviceconfig"):
        return
    bind = op.get_bind()
    serviceconfig = sa.table(
        "serviceconfig",
        sa.column("id", sa.Integer()),
        sa.column("capability", sa.String()),
        sa.column("host_config", sa.JSON()),
    )
    rows = bind.execute(
        sa.select(serviceconfig.c.id, serviceconfig.c.host_config)
        .where(serviceconfig.c.capability == _STORAGE_CAPABILITY)
    ).all()
    for row_id, host_config in rows:
        if isinstance(host_config, str):
            try:
                host_config = json.loads(host_config)
            except ValueError:
                continue
        if not isinstance(host_config, dict) or _STORAGE_DEFAULT_INSTANCE_FIELD not in host_config:
            continue
        renamed = dict(host_config)
        legacy = renamed.pop(_STORAGE_DEFAULT_INSTANCE_FIELD)
        renamed.setdefault(_STORAGE_BARE_TOKEN_FIELD, bool(legacy))
        bind.execute(
            sa.update(serviceconfig)
            .where(serviceconfig.c.id == row_id)
            .values(host_config=renamed)
        )


def downgrade() -> None:
    """删除插件实例配置表、用户第三方身份绑定表与服务实例配置表，唯一约束与外键随建表内联声明，随表一并删除。"""
    if _has_table("pluginconfig"):
        if _has_index("pluginconfig", "ix_pluginconfig_plugin_id_plugin_version"):
            op.drop_index("ix_pluginconfig_plugin_id_plugin_version", table_name="pluginconfig")
        if _has_index("pluginconfig", "ix_pluginconfig_plugin_id"):
            op.drop_index("ix_pluginconfig_plugin_id", table_name="pluginconfig")
        op.drop_table("pluginconfig")

    if _has_table("useridentity"):
        if _has_index("useridentity", "ix_useridentity_user_id"):
            op.drop_index("ix_useridentity_user_id", table_name="useridentity")
        op.drop_table("useridentity")

    if _has_table("serviceconfig"):
        if _has_index("serviceconfig", _SERVICECONFIG_DEFAULT_TARGET_INDEX):
            op.drop_index(_SERVICECONFIG_DEFAULT_TARGET_INDEX, table_name="serviceconfig")
        if _has_index("serviceconfig", "ix_serviceconfig_provider"):
            op.drop_index("ix_serviceconfig_provider", table_name="serviceconfig")
        op.drop_table("serviceconfig")
