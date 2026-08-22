"""数据库治理能力的宿主组合根。"""

from __future__ import annotations

from app.adapters.system.backup.database import (
    PostgreSQLBackupBackend,
    SQLiteBackupBackend,
)
from app.application.backup import BackupPolicy, DatabaseBackupService
from app.application.database import (
    DatabaseGovernance,
    DatabaseHealthService,
    configure_database_governance,
)
from app.application.maintenance import (
    DataCleanupService,
    read_cleanup_policy,
)
from app.db.engine import get_engine
from app.db.health import probe_database
from app.db.maintenance import DatabaseCleanupRepository
from app.db.session import SessionFactory
from app.runtime.config import settings


def build_database_governance() -> DatabaseGovernance:
    """以缓存同步引擎为事实源构造一个完整数据库治理门面。"""
    engine = get_engine()
    dialect = engine.dialect.name
    if dialect == "sqlite":
        backup_backend = SQLiteBackupBackend(engine)
    elif dialect == "postgresql":
        backup_backend = PostgreSQLBackupBackend(engine)
    else:
        raise RuntimeError(f"不支持的数据库类型：{dialect}")

    return DatabaseGovernance(
        health=DatabaseHealthService(probe_database),
        cleanup=DataCleanupService(
            repository=DatabaseCleanupRepository(session_factory=SessionFactory),
            policy_reader=read_cleanup_policy,
        ),
        backup=DatabaseBackupService(
            backend=backup_backend,
            policy_reader=read_backup_policy,
        ),
    )


def configure_database() -> None:
    """构造并登记宿主进程唯一的数据库治理门面。"""
    configure_database_governance(build_database_governance())


def read_backup_policy() -> BackupPolicy:
    """读取一次可热更新的数据库备份目录与保留策略。"""
    return BackupPolicy(
        root=settings.DATABASE_BACKUP_PATH,
        retention_days=settings.DB_BACKUP_RETENTION_DAYS,
        max_count=settings.DB_BACKUP_MAX_COUNT,
    )
