from __future__ import annotations

import stat
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Event

import pytest

from app.application.backup import (
    BackupPolicy,
    DatabaseBackupInProgressError,
    DatabaseBackupService,
)


@dataclass(frozen=True, slots=True)
class _Check:
    valid: bool
    method: str = "test-check"
    detail: str | None = None


class _Backend:
    db_type = "sqlite"
    suffix = ".db"

    def __init__(self, *, valid: bool = True) -> None:
        self.valid = valid
        self.restored: Path | None = None

    def create(self, destination: Path) -> None:
        destination.write_bytes(b"database snapshot")

    def verify(self, artifact: Path) -> _Check:
        return _Check(self.valid and artifact.read_bytes() == b"database snapshot")

    def restore(self, artifact: Path) -> None:
        self.restored = artifact


class _BlockingBackend(_Backend):
    """让首个创建停在后端写入阶段，以验证共享服务的并发约束。"""

    def __init__(self) -> None:
        super().__init__()
        self.started = Event()
        self.release = Event()

    def create(self, destination: Path) -> None:
        self.started.set()
        if not self.release.wait(timeout=2):
            raise TimeoutError("测试未释放数据库备份")
        super().create(destination)


def _service(
    root: Path,
    *,
    backend: _Backend | None = None,
    now: datetime | None = None,
    retention_days: int = 0,
    max_count: int = 0,
) -> DatabaseBackupService:
    return DatabaseBackupService(
        backend=backend or _Backend(),
        policy_reader=lambda: BackupPolicy(root, retention_days, max_count),
        clock=lambda: now or datetime(2026, 8, 19, 13, 45, 26),
    )


def test_create_publishes_one_readable_private_file(tmp_path: Path) -> None:
    artifact = _service(tmp_path).create()

    assert artifact.name == "sqlite_20260819_134526.db"
    assert artifact.path.read_bytes() == b"database snapshot"
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700
    assert stat.S_IMODE(artifact.path.stat().st_mode) == 0o600
    assert not list(tmp_path.glob("*.partial"))


def test_failed_verification_does_not_publish_artifact(tmp_path: Path) -> None:
    backend = _Backend(valid=False)
    service = _service(tmp_path, backend=backend)
    with pytest.raises(RuntimeError, match="数据库备份校验失败"):
        service.create()

    assert list(tmp_path.iterdir()) == []
    backend.valid = True
    assert service.create().path.is_file()


def test_concurrent_create_is_rejected_without_waiting(tmp_path: Path) -> None:
    backend = _BlockingBackend()
    service = _service(tmp_path, backend=backend)

    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(service.create)
        assert backend.started.wait(timeout=1)
        with pytest.raises(DatabaseBackupInProgressError, match="正在执行"):
            service.create()
        backend.release.set()
        artifact = first.result(timeout=2)

    assert artifact.path.is_file()


def test_same_second_backups_receive_short_sequence_suffix(tmp_path: Path) -> None:
    service = _service(tmp_path)

    first = service.create()
    second = service.create()

    assert first.name == "sqlite_20260819_134526.db"
    assert second.name == "sqlite_20260819_134526_1.db"


def test_retention_applies_after_new_artifact_is_available(tmp_path: Path) -> None:
    old = _service(tmp_path, now=datetime(2026, 8, 1, 3, 0, 0)).create()
    current = _service(
        tmp_path,
        now=datetime(2026, 8, 19, 3, 0, 0),
        retention_days=7,
        max_count=1,
    ).create()

    assert current.path.exists()
    assert not old.path.exists()


def test_list_ignores_unmanaged_files_and_rejects_paths(tmp_path: Path) -> None:
    artifact = _service(tmp_path).create()
    (tmp_path / "notes.txt").write_text("ignore", encoding="utf-8")

    assert [item.name for item in _service(tmp_path).list()] == [artifact.name]
    with pytest.raises(ValueError, match="文件名"):
        _service(tmp_path).verify("../user.db")


def test_restore_requires_matching_database_type(tmp_path: Path) -> None:
    backend = _Backend()
    service = _service(tmp_path, backend=backend)
    artifact = service.create()

    restored = service.restore(artifact.name)

    assert restored.name == artifact.name
    assert backend.restored == artifact.path

    postgres = tmp_path / "postgresql_20260819_134526.dump"
    postgres.write_bytes(b"database snapshot")
    with pytest.raises(ValueError, match="当前数据库类型"):
        service.restore(postgres.name)
