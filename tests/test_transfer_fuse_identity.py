"""
验证 FUSE 延迟更新状态时间时的整理清理与文件身份保护。
"""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import psutil
import pytest

from app.application.transfer.execution import (
    TransferExecutionCommand,
    TransferExecutionConflictError,
    TransferExecutionState,
    TransferManualReviewDecision,
    TransferStepState,
)
from app.chain.transfer.execution import _TransferManualReviewRequired
from app.db.adapters.transfer.admission import TransactionalTransferAdmissionRepository
from app.domain.context import MediaInfo
from app.domain.meta.metavideo import MetaVideo
from app.modules.filemanager.storages.local import LocalStorage
from app.modules.filemanager.transhandler import TransHandler
from app.schemas.exception import StorageQueryError
from app.schemas.file import FileItem
from app.schemas.types import MediaType
from tests.test_transfer_safe_overwrite import _checkpoint, _durable_runner
from tests.test_transfer_safe_overwrite import repository_factory as _repository_factory


@pytest.fixture
def repository_factory():
    """
    为完整整理流程提供临时 SQLite 步骤账本，并在用例后释放引擎。
    """
    yield from _repository_factory.__wrapped__()


@pytest.fixture
def fuse_mount(tmp_path, monkeypatch):
    """
    将用例临时目录报告为容器内的 zfuse 挂载，不触碰宿主实际挂载。
    """
    partitions = [SimpleNamespace(mountpoint=str(tmp_path.resolve()), fstype="zfuse.zfsv3")]
    query = Mock(return_value=partitions)
    monkeypatch.setattr(psutil, "disk_partitions", query)
    return query


def test_fuse_cleanup_allows_only_ctime_drift(tmp_path, fuse_mount, monkeypatch):
    """
    FUSE 同路径文件的完整身份一致时，单独变化的状态时间允许继续清理。
    """
    path = tmp_path / "source.mkv"
    path.write_bytes(b"movie")
    expected = object.__new__(LocalStorage).get_item_strict(path)
    current = expected.model_copy(update={"change_time_ns": expected.change_time_ns + 5_000_000_000})
    warning = Mock()
    monkeypatch.setattr("app.modules.filemanager.transhandler.logger.warning", warning)

    TransHandler._TransHandler__assert_file_identity(expected, current)

    fuse_mount.assert_called_once_with(all=True)
    assert "FUSE 状态时间已变化" in warning.call_args.args[0]


@pytest.mark.parametrize("change", [
    {"size": 6}, {"type": "dir"}, {"fileid": "other"}, {"fileid": None},
    {"modify_time_ns": 1}, {"modify_time_ns": None}, {"change_time_ns": None},
])
def test_fuse_cleanup_rejects_other_identity_changes(tmp_path, fuse_mount, change):
    """
    FUSE 的状态时间兼容不能接纳大小、类型、inode、修改时间变化或凭据丢失。
    """
    path = tmp_path / "source.mkv"
    path.write_bytes(b"movie")
    expected = object.__new__(LocalStorage).get_item_strict(path)
    current = expected.model_copy(update={"change_time_ns": expected.change_time_ns + 1, **change})

    with pytest.raises((TransferExecutionConflictError, StorageQueryError)):
        TransHandler._TransHandler__assert_file_identity(expected, current)

    fuse_mount.assert_not_called()


@pytest.mark.parametrize("missing", ["fileid", "modify_time_ns"])
def test_fuse_legacy_snapshot_without_strong_identity_keeps_ctime_check(tmp_path, fuse_mount, missing):
    """
    旧快照缺少稳定文件 ID 或纳秒修改时间时，状态时间变化仍需人工复核。
    """
    path = tmp_path / "source.mkv"
    path.write_bytes(b"movie")
    expected = object.__new__(LocalStorage).get_item_strict(path).model_copy(update={missing: None})
    current = expected.model_copy(update={"change_time_ns": expected.change_time_ns + 1})

    with pytest.raises(TransferExecutionConflictError, match="状态时间已变化"):
        TransHandler._TransHandler__assert_file_identity(expected, current)

    fuse_mount.assert_not_called()


@pytest.mark.parametrize("resume", ["fresh", "restart", "manual_review"])
def test_move_cleans_fuse_source_after_upload_and_durable_resume(
        tmp_path, monkeypatch, repository_factory, fuse_mount, resume,
):
    """
    上传后仅源 ctime 延迟变化时，首次执行、重启及人工确认后重试均完成删除且不重复上传。
    """
    source, target = tmp_path / "source.mkv", tmp_path / "library" / "Movie.mkv"
    source.write_bytes(b"movie")
    target.parent.mkdir()
    local = object.__new__(LocalStorage)
    query = local.get_item_strict
    uploaded = False

    def query_source(path):
        """
        上传结束后回放底层单独更新源文件状态时间，大小和内容版本保持一致。
        """
        item = query(path)
        if item is not None and Path(path) == source and uploaded:
            return item.model_copy(update={"change_time_ns": item.change_time_ns + 5_000_000_000})
        return item

    def query_target(path):
        """
        以独立临时副本提供远端严格查询结果。
        """
        item = query(path)
        return item.model_copy(update={"storage": "remote"}) if item else None

    def upload(_folder, path, _name):
        """
        写入完整目标，再使源文件的后续查询体现状态时间变化。
        """
        nonlocal uploaded
        target.write_bytes(path.read_bytes())
        uploaded = True
        return query_target(target)

    remote = Mock()
    remote.get_folder.return_value = FileItem(storage="remote", path=str(target.parent), type="dir")
    remote.get_item_strict.side_effect = query_target
    remote.upload.side_effect = upload
    monkeypatch.setattr(local, "get_item_strict", query_source)
    checkpoint = _checkpoint(source, target, transfer_type="move", mode="never")
    checkpoint = replace(
        checkpoint, target_storage="remote",
        items=(replace(checkpoint.items[0], target_storage="remote"),),
    )
    repository = repository_factory(checkpoint)
    runner = _durable_runner(repository, checkpoint)
    handler = TransHandler()
    monkeypatch.setattr(handler, "_TransHandler__intercept_transfer", lambda **_kwargs: (True, ""))

    def execute(step_runner):
        """
        从正常计划执行入口运行真实步骤及源删除。
        """
        return handler.execute_transfer_plan(
            checkpoint, meta=MetaVideo("Movie.2026.mkv"),
            mediainfo=MediaInfo(type=MediaType.MOVIE, title="Movie", year="2026"),
            source_oper=local, target_oper=remote, step_runner=step_runner,
        )

    if resume == "restart":
        class ProcessStopped(BaseException):
            """
            模拟上传回执落账后、源删除开始前进程退出。
            """

        run = runner.run

        def stop_before_delete(**kwargs):
            """
            精确停在独立删除步骤之前，保留已成功的上传回执。
            """
            if kwargs["kind"] == "delete_move_source":
                raise ProcessStopped()
            return run(**kwargs)

        monkeypatch.setattr(runner, "run", stop_before_delete)
        with pytest.raises(ProcessStopped):
            execute(runner)
        assert source.exists() and target.read_bytes() == b"movie"
        runner = _durable_runner(repository, checkpoint)

    if resume == "manual_review":
        partitions = fuse_mount.return_value
        fuse_mount.return_value = []
        with pytest.raises(_TransferManualReviewRequired, match="状态时间已变化"):
            execute(runner)
        snapshot = repository.get_snapshot(task_id="safe-overwrite")
        assert snapshot.state is TransferExecutionState.MANUAL_REVIEW
        deletion = next(step for step in snapshot.steps if step.kind == "delete_move_source")
        resolved = TransferExecutionCommand(repository).resolve_manual_review(
            task_id="safe-overwrite", operation_id=deletion.operation_id,
            decision=TransferManualReviewDecision.NOT_APPLIED, actor="admin", reason="确认源和已上传目标完整",
        )
        assert resolved.state is TransferExecutionState.RETRY_WAIT
        claimed = TransactionalTransferAdmissionRepository(repository._session_factory).claim_task(
            task_id="safe-overwrite", owner_id="recovery", lease_seconds=120,
        )
        runner = type(runner)(
            task_id="safe-overwrite", lease_token=claimed.lease_token,
            checkpoint_fingerprint=checkpoint.fingerprint, repository=repository,
        )
        fuse_mount.return_value = partitions
        fuse_mount.reset_mock()

    result = execute(runner)

    assert result.success
    assert not source.exists()
    assert target.read_bytes() == b"movie"
    remote.upload.assert_called_once()
    deletion = next(step for step in repository.get_snapshot(task_id="safe-overwrite").steps
                    if step.kind == "delete_move_source")
    assert deletion.state is TransferStepState.SUCCEEDED
    fuse_mount.assert_called_once_with(all=True)
