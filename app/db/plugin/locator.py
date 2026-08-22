"""插件数据库文件路径与 PostgreSQL schema 命名的定位逻辑，不持有任何状态。"""

from __future__ import annotations

from pathlib import Path
from typing import Tuple

from app.runtime.extensions.lifecycle.paths import plugin_instance_path

# 插件 SQLite 库文件的固定文件名；文件所在目录已经过 plugin_instance_path 按插件与
# 实例分段，文件名无需再编码插件标识
PLUGIN_DB_FILENAME = "plugin.db"
# SQLite WAL 模式下的边车文件后缀
SQLITE_SIDECAR_SUFFIXES: Tuple[str, ...] = ("-wal", "-shm")


def sqlite_db_path(plugin_id: str, instance_id: str) -> Path:
    """
    返回插件实例的 SQLite 库文件路径，父目录经 plugin_instance_path 创建。
    :param plugin_id: 插件标识
    :param instance_id: 插件实例标识
    :return: 库文件的绝对路径
    """
    directory = plugin_instance_path(plugin_id, instance_id, "db")
    return directory / PLUGIN_DB_FILENAME


def sqlite_sidecar_paths(db_path: Path) -> Tuple[Path, ...]:
    """
    返回给定库文件对应的 WAL/SHM 边车文件路径。
    :param db_path: 库文件路径
    :return: 边车文件路径元组，不保证文件存在
    """
    return tuple(db_path.with_name(db_path.name + suffix) for suffix in SQLITE_SIDECAR_SUFFIXES)


def postgres_schema_name(plugin_id: str, instance_id: str) -> str:
    """
    按插件与实例标识拼出 PostgreSQL schema 名。

    只保留小写字母、数字与下划线，避免插件标识中的字符污染标识符。
    :param plugin_id: 插件标识
    :param instance_id: 插件实例标识
    :return: 合法的 schema 名
    """
    raw = f"plugin_{plugin_id}_{instance_id}".lower()
    return "".join(character if character.isalnum() or character == "_" else "_" for character in raw)
