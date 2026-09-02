"""插件数据库文件路径与 PostgreSQL schema 名的解析，不持有任何状态。"""

from __future__ import annotations

import hashlib
from pathlib import Path

from app.runtime.settings import get_runtime_setting

__all__ = [
    "plugin_schema_name",
    "sqlite_database_path",
    "sqlite_sidecar_paths",
]

# 库文件名固定：所在目录已按插件标识分段，文件名无需再编码插件身份
DATABASE_FILENAME = "plugin.db"
# SQLite WAL 模式下与库文件同生共死的边车文件后缀
SIDECAR_SUFFIXES = ("-wal", "-shm")
# PostgreSQL 标识符上限 63 字节，超出部分会被静默截断成另一个 schema
SCHEMA_NAME_MAX_LENGTH = 63
# 归一改写了插件标识时用于区分两个插件的哈希长度
SCHEMA_NAME_HASH_LENGTH = 8


def sqlite_database_path(plugin_id: str) -> Path:
    """
    返回插件 SQLite 库文件路径。

    落在与 ``_PluginBase.get_data_path()`` 完全相同的插件数据目录下，插件库因此随插件
    数据一起被备份、迁移和删除，不会形成第二份需要单独维护的持久化根。本函数不创建
    目录：``ensure`` 在插件什么都没声明时不得凭空产生目录。
    :param plugin_id: 插件标识
    :return: 库文件绝对路径
    """
    return Path(get_runtime_setting("PLUGIN_DATA_PATH")) / plugin_id / DATABASE_FILENAME


def sqlite_sidecar_paths(db_path: Path) -> tuple[Path, ...]:
    """
    返回库文件对应的 WAL/SHM 边车文件路径。
    :param db_path: 库文件路径
    :return: 边车路径元组，不保证文件存在
    """
    return tuple(db_path.with_name(db_path.name + suffix) for suffix in SIDECAR_SUFFIXES)


def plugin_schema_name(plugin_id: str) -> str:
    """
    按插件标识拼出 PostgreSQL schema 名。

    只保留 ASCII 小写字母、数字与下划线，避免插件标识中的字符逃逸成标识符片段，也让
    名字的字符数与字节数一致。归一是多对一的：``My-Plugin``、``My_Plugin`` 与
    ``my_plugin`` 会折叠到同一个名字，而卸载一个插件执行的是 ``DROP SCHEMA ...
    CASCADE``，折叠意味着删掉另一个插件的全部数据。
    因此只要归一改写过标识，就追加插件标识的哈希把它们重新分开；超长标识同样追加哈希
    再截断，截断本身也是一次折叠。
    :param plugin_id: 插件标识
    :return: 合法且与插件标识一一对应的 schema 名
    """
    raw = f"plugin_{plugin_id}"
    sanitized = "".join(
        character
        if (character.isascii() and character.isalnum()) or character == "_"
        else "_"
        for character in raw.lower()
    )
    if sanitized == raw and len(sanitized) <= SCHEMA_NAME_MAX_LENGTH:
        return sanitized
    digest = hashlib.sha1(
        plugin_id.encode("utf-8"),
        usedforsecurity=False,
    ).hexdigest()[:SCHEMA_NAME_HASH_LENGTH]
    suffix = f"_{digest}"
    return f"{sanitized[:SCHEMA_NAME_MAX_LENGTH - len(suffix)]}{suffix}"
