"""数据库健康、清理、备份与离线还原的统一应用门面。"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Optional, Protocol, TypeVar

if TYPE_CHECKING:
    from app.application.backup import (
        BackupArtifact,
        BackupVerification,
        DatabaseBackupService,
    )
    from app.application.maintenance import DataCleanupService


DatabaseProbe = Callable[[], Optional[str]]
T = TypeVar("T")


class AsyncDatabaseExecutor(Protocol):
    """让异步业务调用同步短事务而不阻塞事件循环。"""

    async def run(self, operation: Callable[[], T]) -> T:
        """等待数据库操作完成提交或回滚，并返回执行结果。"""
        ...


class DatabaseHealthService:
    """提供不暴露会话实现的数据库探测能力。"""

    def __init__(self, probe: DatabaseProbe) -> None:
        self._probe = probe

    def test(self) -> Optional[str]:
        """执行数据库探测，成功返回空值，失败返回说明。"""
        return self._probe()


class DatabaseGovernance:
    """向宿主入口提供唯一的数据库治理能力入口。"""

    def __init__(
        self,
        *,
        health: DatabaseHealthService,
        cleanup: DataCleanupService,
        backup: DatabaseBackupService,
    ) -> None:
        self._health = health
        self._cleanup = cleanup
        self._backup = backup

    def test(self) -> Optional[str]:
        """探测当前活动数据库。"""
        return self._health.test()

    def cleanup(
        self,
        *,
        batch_size: int | None = None,
        progress_callback: Callable[..., None] | None = None,
    ) -> dict[str, Any]:
        """按当前配置执行数据表清理。"""
        return self._cleanup.execute(
            batch_size=batch_size,
            progress_callback=progress_callback,
        )

    def create_backup(self) -> BackupArtifact:
        """创建一个当前活动数据库的一致备份。"""
        return self._backup.create()

    def list_backups(self) -> tuple[BackupArtifact, ...]:
        """列出受管数据库备份文件。"""
        return self._backup.list()

    def verify_backup(self, name: str) -> BackupVerification:
        """校验一个受管数据库备份文件。"""
        return self._backup.verify(name)

    def delete_backup(self, name: str) -> None:
        """删除一个受管数据库备份文件。"""
        self._backup.delete(name)

    def restore_backup(self, name: str) -> BackupArtifact:
        """在离线 CLI 进程中还原一个受管数据库备份。"""
        return self._backup.restore(name)


_DATABASE_GOVERNANCE: list[DatabaseGovernance] = []


def configure_database_governance(governance: DatabaseGovernance) -> None:
    """由启动组合根登记宿主唯一的数据库治理门面。"""
    _DATABASE_GOVERNANCE.clear()
    _DATABASE_GOVERNANCE.append(governance)


def get_database_governance() -> DatabaseGovernance:
    """返回启动阶段登记的数据库治理门面。"""
    if not _DATABASE_GOVERNANCE:
        raise RuntimeError("数据库治理服务尚未配置")
    return _DATABASE_GOVERNANCE[0]
