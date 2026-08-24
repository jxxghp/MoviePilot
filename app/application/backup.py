"""数据库备份与离线还原用例。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Callable, Protocol

from app.adapters.system.backup.files import BackupFiles
from app.runtime.log import logger


@dataclass(frozen=True, slots=True)
class BackupPolicy:
    """一次备份操作使用的目录与保留策略快照。"""

    root: Path
    retention_days: int = 0
    max_count: int = 0

    def __post_init__(self) -> None:
        if self.retention_days < 0 or self.max_count < 0:
            raise ValueError("数据库备份保留策略不能使用负数")


@dataclass(frozen=True, slots=True)
class BackupArtifact:
    """一个已完成校验并发布的数据库备份文件。"""

    name: str
    db_type: str
    created_at: datetime
    path: Path
    size: int


@dataclass(frozen=True, slots=True)
class BackupVerification:
    """数据库备份文件的基础内容校验结果。"""

    valid: bool
    method: str
    detail: str | None = None


class DatabaseBackupInProgressError(RuntimeError):
    """同一宿主进程已有数据库备份正在创建。"""


class BackupCheck(Protocol):
    """数据库适配器校验结果的结构合同。"""

    valid: bool
    method: str
    detail: str | None


class DatabaseBackupBackend(Protocol):
    """活动数据库创建、校验和离线还原所需的最小技术端口。"""

    db_type: str
    suffix: str

    def create(self, destination: Path) -> None:
        """把活动数据库的一致快照写入目标文件。"""

    def verify(self, artifact: Path) -> BackupCheck:
        """在不修改数据库的前提下校验备份文件。"""

    def restore(self, artifact: Path) -> None:
        """把已校验制品还原到离线目标数据库。"""


class DatabaseBackupService:
    """管理单文件数据库备份及明确的离线还原操作。"""

    def __init__(
        self,
        *,
        backend: DatabaseBackupBackend,
        policy_reader: Callable[[], BackupPolicy],
        clock: Callable[[], datetime] = datetime.now,
    ) -> None:
        self._backend = backend
        self._policy_reader = policy_reader
        self._clock = clock
        self._create_lock = Lock()

    def create(self) -> BackupArtifact:
        """创建、校验并发布一个在线一致快照。"""
        if not self._create_lock.acquire(blocking=False):
            raise DatabaseBackupInProgressError("已有数据库备份任务正在执行")
        try:
            policy = self._policy_reader()
            files = BackupFiles(policy.root)
            created_at = self._clock()
            name = files.available_name(
                db_type=self._backend.db_type,
                created_at=created_at,
                suffix=self._backend.suffix,
            )
            temporary = files.create_temporary(self._backend.suffix)
            try:
                self._backend.create(temporary)
                verification = self._backend.verify(temporary)
                if not verification.valid:
                    detail = f"：{verification.detail}" if verification.detail else ""
                    raise RuntimeError(
                        f"数据库备份校验失败（{verification.method}）{detail}"
                    )
                path = files.publish(temporary, name)
            except Exception:
                files.discard(temporary)
                raise

            artifact = self._artifact(path, created_at=created_at)
            self._prune(files, policy, keep=artifact.name)
            logger.info(
                f"数据库备份完成：文件={artifact.name}，类型={artifact.db_type}，"
                f"大小={artifact.size} bytes"
            )
            return artifact
        finally:
            self._create_lock.release()

    def list(self) -> tuple[BackupArtifact, ...]:
        """按创建时间倒序列出受管数据库备份文件。"""
        files = BackupFiles(self._policy_reader().root)
        return tuple(self._artifact(path) for path in files.list())

    def verify(self, name: str) -> BackupVerification:
        """按文件名校验一个受管数据库备份。"""
        path = BackupFiles(self._policy_reader().root).resolve(name)
        self._require_matching_type(path)
        result = self._backend.verify(path)
        return BackupVerification(result.valid, result.method, result.detail)

    def restore(self, name: str) -> BackupArtifact:
        """校验后将受管制品还原到当前 CLI 解析出的离线数据库目标。"""
        path = BackupFiles(self._policy_reader().root).resolve(name)
        self._require_matching_type(path)
        verification = self._backend.verify(path)
        if not verification.valid:
            detail = f"：{verification.detail}" if verification.detail else ""
            raise RuntimeError(
                f"数据库备份校验失败（{verification.method}）{detail}"
            )
        self._backend.restore(path)
        logger.info(
            f"数据库离线还原完成：文件={path.name}，类型={self._backend.db_type}"
        )
        return self._artifact(path)

    def _prune(self, files: BackupFiles, policy: BackupPolicy, *, keep: str) -> None:
        """新备份发布成功后按天数或份数清理同一目录中的旧文件。"""
        cutoff = (
            self._clock() - timedelta(days=policy.retention_days)
            if policy.retention_days > 0
            else None
        )
        for index, path in enumerate(files.list()):
            if path.name == keep:
                continue
            created_at = files.created_at(path.name)
            expired = cutoff is not None and created_at < cutoff
            exceeds_count = policy.max_count > 0 and index >= policy.max_count
            if expired or exceeds_count:
                files.delete(path.name)

    def _require_matching_type(self, path: Path) -> None:
        db_type = BackupFiles.database_type(path.name)
        if db_type != self._backend.db_type:
            raise ValueError(
                f"备份类型 {db_type} 与当前数据库类型 {self._backend.db_type} 不一致"
            )

    @staticmethod
    def _artifact(path: Path, *, created_at: datetime | None = None) -> BackupArtifact:
        return BackupArtifact(
            name=path.name,
            db_type=BackupFiles.database_type(path.name),
            created_at=created_at or BackupFiles.created_at(path.name),
            path=path,
            size=path.stat().st_size,
        )
