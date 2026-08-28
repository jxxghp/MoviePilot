"""数据库备份与离线还原用例。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Callable, Protocol

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

    @property
    def valid(self) -> bool:
        """返回制品是否通过校验。"""

    @property
    def method(self) -> str:
        """返回本次校验使用的方法。"""

    @property
    def detail(self) -> str | None:
        """返回校验失败详情，无详情时返回空值。"""


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


class BackupArtifactStore(Protocol):
    """管理一个备份根目录内正式制品与临时文件的技术端口。"""

    def create_temporary(self, suffix: str) -> Path:
        """创建由当前存储实例拥有的私有临时文件。"""

    def publish(self, temporary: Path, name: str) -> Path:
        """把已校验临时文件原子发布为正式制品。"""

    def discard(self, temporary: Path) -> None:
        """清理当前操作拥有的未发布临时文件。"""

    def list(self) -> list[Path]:
        """按创建时间倒序返回当前根目录内的正式制品。"""

    def resolve(self, name: str) -> Path:
        """按受限文件名解析一个必须存在的正式制品。"""

    def delete(self, name: str) -> None:
        """删除一个已通过名称约束的正式制品。"""

    def available_name(
        self,
        *,
        db_type: str,
        created_at: datetime,
        suffix: str,
    ) -> str:
        """返回当前根目录中可用的正式制品文件名。"""

    def database_type(self, name: str) -> str:
        """从受管文件名读取数据库类型。"""

    def created_at(self, name: str) -> datetime:
        """从受管文件名读取制品创建时间。"""

    def size(self, name: str) -> int:
        """返回受管正式制品的字节数。"""


class BackupArtifactStoreFactory(Protocol):
    """按一次策略快照的根目录构造制品存储端口。"""

    def __call__(self, root: Path) -> BackupArtifactStore:
        """返回仅能访问指定备份根目录的制品存储。"""


class DatabaseBackupService:
    """管理单文件数据库备份及明确的离线还原操作。"""

    def __init__(
        self,
        *,
        backend: DatabaseBackupBackend,
        artifact_store_factory: BackupArtifactStoreFactory,
        policy_reader: Callable[[], BackupPolicy],
        clock: Callable[[], datetime] = datetime.now,
    ) -> None:
        self._backend = backend
        self._artifact_store_factory = artifact_store_factory
        self._policy_reader = policy_reader
        self._clock = clock
        self._create_lock = Lock()

    def create(self) -> BackupArtifact:
        """创建、校验并发布一个在线一致快照。"""
        if not self._create_lock.acquire(blocking=False):
            raise DatabaseBackupInProgressError("已有数据库备份任务正在执行")
        try:
            policy = self._policy_reader()
            files = self._artifact_store_factory(policy.root)
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

            artifact = self._artifact(files, path, created_at=created_at)
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
        files = self._artifact_store_factory(self._policy_reader().root)
        return tuple(self._artifact(files, path) for path in files.list())

    def verify(self, name: str) -> BackupVerification:
        """按文件名校验一个受管数据库备份。"""
        files = self._artifact_store_factory(self._policy_reader().root)
        path = files.resolve(name)
        self._require_matching_type(files, path)
        result = self._backend.verify(path)
        return BackupVerification(result.valid, result.method, result.detail)

    def delete(self, name: str) -> None:
        """删除一个受管数据库备份文件。"""
        self._artifact_store_factory(self._policy_reader().root).delete(name)

    def restore(self, name: str) -> BackupArtifact:
        """校验后将受管制品还原到当前 CLI 解析出的离线数据库目标。"""
        files = self._artifact_store_factory(self._policy_reader().root)
        path = files.resolve(name)
        self._require_matching_type(files, path)
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
        return self._artifact(files, path)

    def _prune(
        self,
        files: BackupArtifactStore,
        policy: BackupPolicy,
        *,
        keep: str,
    ) -> None:
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

    def _require_matching_type(
        self,
        files: BackupArtifactStore,
        path: Path,
    ) -> None:
        """拒绝把其他数据库类型的制品交给当前后端。"""
        db_type = files.database_type(path.name)
        if db_type != self._backend.db_type:
            raise ValueError(
                f"备份类型 {db_type} 与当前数据库类型 {self._backend.db_type} 不一致"
            )

    @staticmethod
    def _artifact(
        files: BackupArtifactStore,
        path: Path,
        *,
        created_at: datetime | None = None,
    ) -> BackupArtifact:
        """通过存储端口把受管路径投影为应用制品。"""
        return BackupArtifact(
            name=path.name,
            db_type=files.database_type(path.name),
            created_at=created_at or files.created_at(path.name),
            path=path,
            size=files.size(path.name),
        )
