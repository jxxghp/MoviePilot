"""插件自有库的 Alembic 迁移执行。"""

from __future__ import annotations

from configparser import ConfigParser
from pathlib import Path

from alembic.command import upgrade
from alembic.config import Config

from app.db.plugin.container import PluginDatabaseHandle

__all__ = ["run_migrations"]


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
        upgrade(config, "head")
        return
    with handle.engine.connect() as connection:
        config.attributes["connection"] = connection
        upgrade(config, "head")
        connection.commit()
