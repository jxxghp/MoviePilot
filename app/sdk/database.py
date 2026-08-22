"""插件可使用的数据库备份只读门面与插件自有库句柄类型。

``PluginDatabaseHandle`` 是 ``_PluginBase.get_plugin_database()`` 交回的对象，持有插件实例
专属的引擎与会话工厂；插件自有表的读写会话都从它取。
"""

from app.application.backup import BackupArtifact, BackupVerification
from app.application.database import get_database_governance as _get_database_governance
from app.db.plugin import PluginDatabaseHandle


__all__ = [
    "BackupArtifact",
    "BackupVerification",
    "PluginDatabaseHandle",
    "create_backup",
    "list_backups",
    "verify_backup",
]


def create_backup() -> BackupArtifact:
    """在宿主管理目录中创建当前数据库的一致备份。"""
    return _get_database_governance().create_backup()


def list_backups() -> tuple[BackupArtifact, ...]:
    """列出宿主管理目录中的数据库备份。"""
    return _get_database_governance().list_backups()


def verify_backup(name: str) -> BackupVerification:
    """按文件名校验一个宿主管理的数据库备份。"""
    return _get_database_governance().verify_backup(name)
