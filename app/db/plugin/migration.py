"""插件自有库的 Alembic 迁移执行。"""

from __future__ import annotations

from configparser import ConfigParser
from pathlib import Path

from alembic.command import upgrade
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.engine import Connection

from app.db.plugin.container import PluginDatabaseHandle

__all__ = ["PluginMigrationCompatibilityError", "run_migrations"]


class PluginMigrationCompatibilityError(RuntimeError):
    """插件库里存在随包迁移树无法识别的 revision，不能按当前版本继续升级。"""


def _reject_unknown_revisions(
    connection: Connection,
    config: Config,
    directory: Path,
) -> None:
    """
    在执行升级前确认库里已记录的 revision 都能被随包迁移树识别。

    插件装回旧版本后，库里留着的是新版本写下的 revision，而随包回退的迁移树里没有这
    条记录。此时 ``upgrade`` 只抛一句不带插件名也不带目录的 ``Can't locate revision``，
    排障得靠猜是哪个插件、哪个库。这里在同一批连接上先把这个事实查清楚，换成指名道姓
    的拒绝：宁可拒绝建库，也绝不自动 ``downgrade``——降级脚本通常只负责删表删列，自动
    执行等于把用户在新版本里产生的数据直接抹掉，且没有回头路。
    :param connection: 已指向插件库（PostgreSQL 下已限定 schema）的连接
    :param config: 已设定 script_location 的 Alembic 配置
    :param directory: 插件随包提供的迁移脚本目录
    :raise PluginMigrationCompatibilityError: 库里存在迁移树不认识的 revision
    """
    known = {
        script.revision
        for script in ScriptDirectory.from_config(config).walk_revisions()
    }
    # 空库还没有 alembic_version 表，get_current_heads 返回空元组，首次建库不受影响
    unknown = sorted(
        revision
        for revision in MigrationContext.configure(connection).get_current_heads()
        if revision not in known
    )
    if not unknown:
        return
    raise PluginMigrationCompatibilityError(
        f"插件数据库当前 revision {'、'.join(unknown)} 不属于迁移脚本目录 {directory}，"
        "拒绝按当前插件版本继续升级；请换用包含该 revision 的插件版本，"
        "或由管理员完成明确的数据迁移，宿主不会自动降级数据库"
    )


def run_migrations(handle: PluginDatabaseHandle, directory: Path) -> None:
    """
    在句柄对应的库上把 Alembic 迁移跑到 head。

    SQLite 按库文件 URL 起独立连接；PostgreSQL 复用句柄已被 ``schema_translate_map``
    限定过的连接，迁移脚本的 env.py 必须从 ``context.config.attributes["connection"]``
    取用它，否则迁移会落在 public schema 而不是该插件的 schema。
    :param handle: 插件数据库句柄
    :param directory: 迁移脚本目录，须符合 Alembic script_location 布局
    :raise PluginMigrationCompatibilityError: 库里存在迁移树不认识的 revision
    """
    config = Config()
    # 关闭 ini 插值：迁移目录里出现的 % 不应被当成插值语法（与宿主 _build_alembic_config 一致）
    config.file_config = ConfigParser(interpolation=None)
    config.set_main_option("script_location", str(directory))
    if handle.owns_engine:
        config.set_main_option(
            "sqlalchemy.url",
            handle.engine.url.render_as_string(hide_password=False),
        )
        # 独占引擎下 alembic 自己建连接，兼容性检查用完即还，不与后续升级争同一把写锁
        with handle.engine.connect() as connection:
            _reject_unknown_revisions(connection, config, directory)
        upgrade(config, "head")
        return
    with handle.engine.connect() as connection:
        config.attributes["connection"] = connection
        _reject_unknown_revisions(connection, config, directory)
        upgrade(config, "head")
        connection.commit()
