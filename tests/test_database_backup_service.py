"""
验证数据库备份服务的发布、保留、并发控制及文件访问边界。
"""

from __future__ import annotations

import stat
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Event

import pytest

from app.adapters.system.backup.files import BackupFiles
from app.application.backup import (
    BackupCheck,
    BackupPolicy,
    DatabaseBackupInProgressError,
    DatabaseBackupService,
)
from version import APP_VERSION


@dataclass(frozen=True, slots=True)
class _Check:
    """
    备份后端测试返回的校验结果。

    :param valid: 备份是否通过校验
    :param method: 校验方式标识，默认表示测试校验
    :param detail: 校验补充信息；None 表示没有补充信息
    """
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
        """
        在指定路径写入固定快照内容，供服务发布流程使用。

        :param destination: 待写入的临时备份文件路径
        """
        destination.write_bytes(b"database snapshot")

    def verify(self, artifact: Path) -> BackupCheck:
        """
        按预设有效性和文件内容生成校验结果。

        :param artifact: 待读取的备份文件
        :return: 仅在开关允许且内容匹配时通过的校验结果
        """
        return _Check(self.valid and artifact.read_bytes() == b"database snapshot")

    def restore(self, artifact: Path) -> None:
        """
        记录请求恢复的文件，不改写活动数据库。

        :param artifact: 需要恢复的备份文件路径
        """
        self.restored = artifact


class _BlockingBackend(_Backend):
    """
    让首个创建停在后端写入阶段，以验证共享服务的并发约束。
    """

    def __init__(self) -> None:
        super().__init__()
        self.started = Event()
        self.release = Event()

    def create(self, destination: Path) -> None:
        """
        等待释放事件后写入快照，超过两秒未释放则报告测试超时。

        :param destination: 待写入的临时备份文件路径
        :raises TimeoutError: 测试未在两秒内释放创建操作
        """
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
        artifact_store_factory=BackupFiles,
        policy_reader=lambda: BackupPolicy(root, retention_days, max_count),
        clock=lambda: now or datetime(2026, 8, 19, 13, 45, 26),
    )


def test_service_builds_artifact_store_from_current_policy_root(tmp_path: Path) -> None:
    """
    每次操作都应把最新策略根目录交给注入的存储工厂。
    """
    roots: list[Path] = []

    def build_store(root: Path) -> BackupFiles:
        """
        记录应用传入的根目录并返回真实受限文件适配器。
        """
        roots.append(root)
        return BackupFiles(root)

    service = DatabaseBackupService(
        backend=_Backend(),
        artifact_store_factory=build_store,
        policy_reader=lambda: BackupPolicy(tmp_path),
        clock=lambda: datetime(2026, 8, 19, 13, 45, 26),
    )

    artifact = service.create()

    assert roots == [tmp_path]
    assert artifact.path.parent == tmp_path


def test_create_publishes_one_readable_private_file(tmp_path: Path) -> None:
    """
    成功备份只发布一个完整文件，并限制目录与文件访问权限。
    """
    artifact = _service(tmp_path).create()

    assert artifact.name == f"moviepilot_{APP_VERSION}_sqlite_20260819_134526.db"
    assert artifact.path.read_bytes() == b"database snapshot"
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700
    assert stat.S_IMODE(artifact.path.stat().st_mode) == 0o600
    assert not list(tmp_path.glob("*.partial"))


def test_failed_verification_does_not_publish_artifact(tmp_path: Path) -> None:
    """
    校验失败时不发布备份，也不阻塞下一次成功创建。
    """
    backend = _Backend(valid=False)
    service = _service(tmp_path, backend=backend)
    with pytest.raises(RuntimeError, match="数据库备份校验失败"):
        service.create()

    assert list(tmp_path.iterdir()) == []
    backend.valid = True
    assert service.create().path.is_file()


def test_concurrent_create_is_rejected_without_waiting(tmp_path: Path) -> None:
    """
    已有备份正在创建时，新的创建请求立即被拒绝。
    """
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
    """
    同秒创建多个备份时通过短序号后缀避免文件名冲突。
    """
    service = _service(tmp_path)

    first = service.create()
    second = service.create()

    assert first.name == f"moviepilot_{APP_VERSION}_sqlite_20260819_134526.db"
    assert second.name == f"moviepilot_{APP_VERSION}_sqlite_20260819_134526_1.db"


def test_backup_name_uses_application_release_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    宿主备份文件名包含实际应用发行版本。
    """
    monkeypatch.setattr(
        "app.adapters.system.backup.files.get_app_version",
        lambda: "v4.2.1",
    )

    assert _service(tmp_path).create().name == "moviepilot_v4.2.1_sqlite_20260819_134526.db"


def test_backup_name_uses_plugin_target_and_version(tmp_path: Path) -> None:
    """
    插件备份文件名应记录插件 ID 与插件版本，而不是宿主版本。
    """
    service = DatabaseBackupService(
        backend=_Backend(),
        artifact_store_factory=BackupFiles,
        policy_reader=lambda: BackupPolicy(tmp_path),
        clock=lambda: datetime(2026, 8, 19, 13, 45, 26),
        target="DemoPlugin",
        version="1.2.3",
    )

    artifact = service.create()

    assert artifact.name == "DemoPlugin_v1.2.3_sqlite_20260819_134526.db"
    assert artifact.target == "DemoPlugin"


def test_plugin_backup_without_version_uses_target_without_version_segment(
    tmp_path: Path,
) -> None:
    """
    插件版本元数据缺失时仍应备份，并使用可解析的无版本文件名。
    """
    service = DatabaseBackupService(
        backend=_Backend(),
        artifact_store_factory=BackupFiles,
        policy_reader=lambda: BackupPolicy(tmp_path),
        clock=lambda: datetime(2026, 8, 19, 13, 45, 26),
        target="DemoPlugin",
        version=None,
    )

    artifact = service.create()

    assert artifact.name == "DemoPlugin_sqlite_20260819_134526.db"
    assert artifact.target == "DemoPlugin"
    assert BackupFiles.target(artifact.name) == "DemoPlugin"


def test_retention_applies_after_new_artifact_is_available(tmp_path: Path) -> None:
    """
    新备份发布后再按保留策略清理旧备份。
    """
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
    """
    列举忽略非托管文件，校验拒绝包含目录的名称。
    """
    artifact = _service(tmp_path).create()
    (tmp_path / "notes.txt").write_text("ignore", encoding="utf-8")

    assert [item.name for item in _service(tmp_path).list()] == [artifact.name]
    with pytest.raises(ValueError, match="文件名"):
        _service(tmp_path).verify("../user.db")


def test_legacy_backup_name_remains_managed(tmp_path: Path) -> None:
    """
    升级前生成的无代际前缀备份仍可列出、校验和删除。
    """
    legacy = tmp_path / "sqlite_20260818_030000.db"
    legacy.write_bytes(b"database snapshot")
    service = _service(tmp_path)

    assert [item.name for item in service.list()] == [legacy.name]
    assert service.verify(legacy.name).valid is True
    service.delete(legacy.name)
    assert legacy.exists() is False


def test_delete_removes_only_named_managed_backup(tmp_path: Path) -> None:
    """
    删除仅影响指定托管备份，保留其他文件并拒绝路径穿越。
    """
    service = _service(tmp_path)
    artifact = service.create()
    unmanaged = tmp_path / "notes.txt"
    unmanaged.write_text("keep", encoding="utf-8")

    service.delete(artifact.name)

    assert artifact.path.exists() is False
    assert unmanaged.exists() is True
    with pytest.raises(ValueError, match="文件名"):
        service.delete("../notes.txt")


def test_restore_requires_matching_database_type(tmp_path: Path) -> None:
    """
    恢复仅接受当前数据库类型的备份，并将文件交给对应后端。
    """
    backend = _Backend()
    service = _service(tmp_path, backend=backend)
    artifact = service.create()

    restored = service.restore(artifact.name)

    assert restored.name == artifact.name
    assert backend.restored == artifact.path

    postgres = tmp_path / "moviepilot_v3.0.0_postgresql_20260819_134526.dump"
    postgres.write_bytes(b"database snapshot")
    with pytest.raises(ValueError, match="当前数据库类型"):
        service.restore(postgres.name)
