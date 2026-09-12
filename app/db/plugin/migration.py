"""插件自有库的 Alembic 迁移执行。"""

from __future__ import annotations

from configparser import ConfigParser
from pathlib import Path

from alembic.command import upgrade
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory

from app.db.plugin.container import PluginDatabaseHandle

__all__ = ["PluginMigrationCompatibilityError", "run_migrations"]


class PluginMigrationCompatibilityError(RuntimeError):
    """插件数据库 revision 不属于当前迁移树，不能安全切版。"""


def _check_migration_compatibility(connection, config: Config, directory: Path) -> None:
    """在执行目标迁移前确认数据库 revision 可由该迁移树识别。

    Alembic 的 ``upgrade head`` 不会主动降级数据库，但当插件切回旧版本时，旧
    迁移树会在导入阶段直接抛出一个难以定位的 ``Can't locate revision``。这里在
    已取得目标插件迁移声明、实际执行 Alembic upgrade 之前完成同一事实检查，明确
    告诉调用方需要兼容的迁移脚本或人工迁移；绝不尝试自动 downgrade 或猜测数据
    转换。插件生命周期仍按既有合同在 ``init_plugin`` 后读取声明，不能为了预检
    提前导入插件而改变这一 ABI。
    """
    script = ScriptDirectory.from_config(config)
    known_revisions = {
        revision.revision
        for revision in script.walk_revisions()
    }
    current_revisions = MigrationContext.configure(connection).get_current_heads()
    unknown = sorted(
        revision for revision in current_revisions if revision not in known_revisions
    )
    if unknown:
        current = ", ".join(unknown)
        raise PluginMigrationCompatibilityError(
            f"插件数据库当前 revision {current} 不属于迁移脚本 {directory}，"
            "拒绝切换到不兼容的插件版本；请提供包含该 revision 的兼容迁移脚本，"
            "或由管理员完成明确的数据迁移，不会自动降级数据库"
        )


def run_migrations(handle: PluginDatabaseHandle, directory: Path) -> None:
    """
    在句柄对应的库上把 Alembic 迁移跑到 head。

    SQLite 按库文件 URL 起独立连接；PostgreSQL 复用句柄已被 ``schema_translate_map``
    限定过的连接，迁移脚本的 env.py 必须从 ``context.config.attributes["connection"]``
    取用它，否则迁移会落在 public schema 而不是该插件的 schema。
    :param handle: 插件数据库句柄
    :param directory: 迁移脚本目录，须符合 Alembic script_location 布局
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
        with handle.engine.connect() as connection:
            _check_migration_compatibility(connection, config, directory)
        upgrade(config, "head")
        return
    with handle.engine.connect() as connection:
        config.attributes["connection"] = connection
        _check_migration_compatibility(connection, config, directory)
        upgrade(config, "head")
        connection.commit()
