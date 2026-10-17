"""
覆盖整理必须先完整落地新版本，失败及恢复时保留旧版本。
"""

import errno
import os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.application.transfer.execution import TransferExecutionState, TransferStepState
from app.application.transfer.workflow import (
    TransferPlanCheckpoint,
    TransferPlanItem,
    TransferPlanningInput,
)
from app.chain.transfer import execution as execution_module
from app.db.adapters.transfer.execution import TransactionalTransferExecutionRepository
from app.db.base import Base
from app.db.models.transferexecutionstep import TransferExecutionStep
from app.db.models.transferhistory import TransferHistory
from app.db.models.transferpending import TransferPending
from app.domain.context import MediaInfo
from app.domain.meta.metabase import MetaBase
from app.modules.filemanager.module import FileManagerModule
from app.modules.filemanager.storages.local import LocalStorage
from app.modules.filemanager.transhandler import TransHandler
from app.schemas.file import FileItem
from app.schemas.types import MediaType


class _ImmediateStepRunner:
    """
    直接执行步骤，保留测试可观察的持久操作顺序。
    """

    def __init__(self) -> None:
        """
        初始化操作记录。
        """
        self.kinds = []

    def run(self, *, phase, kind, payload, execute, observe):
        """
        记录意图并执行外部操作。
        """
        del phase, payload, observe
        self.kinds.append(kind)
        return execute()


def _checkpoint(source: Path, target: Path, *, transfer_type="copy", mode="always"):
    """
    构造使用真实本地文件的冻结单文件计划。
    """
    source_item = object.__new__(LocalStorage).get_item_strict(source).model_dump(mode="json")
    planning_input = TransferPlanningInput(source_fileitem=source_item)
    return TransferPlanCheckpoint(
        planning_input=planning_input, target_storage="local",
        root_target_path=target.parent.as_posix(), final_target_path=target.as_posix(),
        resolved_transfer_type=transfer_type, overwrite_mode=mode,
        items=(TransferPlanItem(
            sequence=0, source_fileitem=source_item,
            target_storage="local", target_path=target.as_posix(),
        ),),
    )


def _execute(checkpoint, monkeypatch, runner):
    """
    使用真实本地存储执行计划，隔离插件及进度展示。
    """
    storage = object.__new__(LocalStorage)
    handler = TransHandler()
    monkeypatch.setattr(handler, "_TransHandler__intercept_transfer", lambda **_kwargs: (True, ""))
    monkeypatch.setattr(LocalStorage, "_LocalStorage__should_show_progress", lambda *_args: False)
    return handler.execute_transfer_plan(
        checkpoint, meta=MetaBase("Movie.2026.mkv"),
        mediainfo=MediaInfo(type=MediaType.MOVIE, title="Movie", year="2026"),
        source_oper=storage, target_oper=storage, step_runner=runner,
    )


@pytest.mark.parametrize("durable", [False, True])
def test_failed_overwrite_keeps_complete_old_target(tmp_path, monkeypatch, durable):
    """
    磁盘不足时不能提前删除已存在的完整媒体文件。
    """
    source = tmp_path / "download.mkv"
    target = tmp_path / "Movie.2026.mkv"
    source.write_bytes(b"new-version")
    target.write_bytes(b"old-version")

    def disk_full(*_args, **_kwargs):
        """
        在真实本地复制边界模拟空间耗尽。
        """
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr("app.modules.filemanager.storages.local.fsproxy.copy", disk_full)
    runner = _ImmediateStepRunner() if durable else None
    checkpoint = _checkpoint(source, target)
    if durable:
        with pytest.raises(RuntimeError):
            _execute(checkpoint, monkeypatch, runner)
    else:
        assert not _execute(checkpoint, monkeypatch, runner).success

    assert target.read_bytes() == b"old-version"
    assert source.read_bytes() == b"new-version"


@pytest.mark.parametrize("durable", [False, True])
def test_latest_keeps_other_versions_when_materialization_fails(tmp_path, monkeypatch, durable):
    """
    latest 模式只能在新目标完整落地以后清理其它版本。
    """
    source = tmp_path / "downloads" / "download.mkv"
    source.parent.mkdir()
    target = tmp_path / "Movie.2026.2160p.mkv"
    previous = tmp_path / "Movie.2026.1080p.mkv"
    source.write_bytes(b"new-version")
    previous.write_bytes(b"old-version")
    monkeypatch.setattr(LocalStorage, "copy", lambda *_args: False)
    runner = _ImmediateStepRunner() if durable else None
    checkpoint = _checkpoint(source, target, mode="latest")
    if durable:
        with pytest.raises(RuntimeError):
            _execute(checkpoint, monkeypatch, runner)
    else:
        assert not _execute(checkpoint, monkeypatch, runner).success

    assert previous.read_bytes() == b"old-version"


@pytest.mark.parametrize("transfer_type", ["copy", "move", "link", "softlink"])
@pytest.mark.parametrize("durable", [False, True])
def test_safe_overwrite_preserves_transfer_modes(tmp_path, monkeypatch, transfer_type, durable):
    """
    覆盖时保留复制、移动、硬链接和软链接各自的文件语义。
    """
    source = tmp_path / "download.mkv"
    target = tmp_path / "Movie.2026.mkv"
    source.write_bytes(b"new-version")
    target.write_bytes(b"old-version")
    result = _execute(
        _checkpoint(source, target, transfer_type=transfer_type), monkeypatch,
        _ImmediateStepRunner() if durable else None,
    )

    assert result.success
    assert result.target_item.path == target.as_posix()
    assert target.read_bytes() == b"new-version"
    assert source.exists() is (transfer_type != "move")
    if transfer_type == "link":
        assert source.samefile(target)
    assert target.is_symlink() is (transfer_type == "softlink")


@pytest.fixture
def repository_factory():
    """
    为真实文件操作建立独立 SQLite 账本并绑定实际冻结计划。
    """
    engines = []

    def create(checkpoint):
        """
        创建带有效租约的单任务步骤仓储。
        """
        engine = create_engine("sqlite+pysqlite:///:memory:")
        engines.append(engine)
        Base.metadata.create_all(engine, tables=[
            TransferPending.__table__, TransferHistory.__table__, TransferExecutionStep.__table__,
        ])
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        planning_input = checkpoint.planning_input
        with factory() as session:
            session.add(TransferPending(
                task_id="safe-overwrite", storage="local", src_path=planning_input.source_fileitem["path"],
                created_at="2026-08-27 09:00:00", updated_at="2026-08-27 09:00:00", state="planned",
                input_version=1, planning_input=planning_input.to_payload(),
                input_fingerprint=planning_input.fingerprint,
                checkpoint_version=checkpoint.schema_version, checkpoint_payload=checkpoint.to_payload(),
                planned_at="2026-08-27 09:00:00", lease_owner="worker", lease_token="lease",
                lease_expires_at="2099-01-01 00:00:00.000000", heartbeat_at="2026-08-27 01:00:00.000000",
                attempt_count=1, execution_state="not_started", retry_generation=0,
                retry_count=0, settlement_revision=0,
            ))
            session.commit()
        return TransactionalTransferExecutionRepository(
            factory, local_clock=lambda: datetime(2026, 8, 27, 9, 30, 0),
            lease_clock=lambda: datetime(2026, 8, 27, 1, 30, 0, tzinfo=timezone.utc),
        )

    yield create
    for engine in engines:
        engine.dispose()


def _durable_runner(repository, checkpoint):
    """
    在每次重启后以同一个冻结计划重建真实持久 runner。
    """
    return execution_module._DurableTransferStepRunner(
        task_id="safe-overwrite", lease_token="lease",
        checkpoint_fingerprint=checkpoint.fingerprint, repository=repository,
    )


@pytest.mark.parametrize("crash_after", ["backup", "promote"])
def test_overwrite_recovers_after_rename_with_real_step_ledger(
        tmp_path, monkeypatch, repository_factory, crash_after,
):
    """
    备份或提升已生效但未记成功时，重启只恢复事实而不重做文件操作。
    """
    source = tmp_path / "download.mkv"
    target = tmp_path / "Movie.2026.mkv"
    source.write_bytes(b"new-version")
    target.write_bytes(b"old-version")
    checkpoint = _checkpoint(source, target, transfer_type="move")
    repository = repository_factory(checkpoint)
    real_rename = LocalStorage.rename
    copied = Mock(wraps=LocalStorage.copy)
    monkeypatch.setattr(LocalStorage, "copy", lambda self, *args: copied(self, *args))
    renamed = []
    crashed = False

    class ProcessStopped(BaseException):
        """
        模拟进程在外部操作完成后退出，绕过普通异常恢复。
        """

    def rename(self, fileitem, name):
        """
        执行真实 rename 后注入一次进程中断。
        """
        nonlocal crashed
        result = real_rename(self, fileitem, name)
        renamed.append(name)
        should_crash = name.startswith(".mp-backup-") if crash_after == "backup" else name == target.name
        if should_crash and not crashed:
            crashed = True
            raise ProcessStopped()
        return result

    monkeypatch.setattr(LocalStorage, "rename", rename)
    with pytest.raises(ProcessStopped):
        _execute(checkpoint, monkeypatch, _durable_runner(repository, checkpoint))
    assert next(tmp_path.glob(".mp-backup-*")).read_bytes() == b"old-version"
    assert source.read_bytes() == b"new-version"
    assert not target.exists() if crash_after == "backup" else target.read_bytes() == b"new-version"

    result = _execute(checkpoint, monkeypatch, _durable_runner(repository, checkpoint))
    assert result.success
    assert target.read_bytes() == b"new-version"
    assert not source.exists()
    assert len(renamed) == 2
    assert copied.call_count == 1
    assert not list(tmp_path.glob(".mp-*"))


@pytest.mark.parametrize("crash_after", ["backup", "promote"])
def test_unknown_rename_result_preserves_files_and_requires_manual_review(
        tmp_path, monkeypatch, repository_factory, crash_after,
):
    """
    中断后无法查询备份结果时不能重复覆盖或删除任何保留的文件。
    """
    source = tmp_path / "download.mkv"
    target = tmp_path / "Movie.2026.mkv"
    source.write_bytes(b"new-version")
    target.write_bytes(b"old-version")
    checkpoint = _checkpoint(source, target)
    repository = repository_factory(checkpoint)
    real_rename = LocalStorage.rename

    class ProcessStopped(BaseException):
        """
        保留 STARTED 账本状态以模拟真实重启。
        """

    def crash_after_backup(self, fileitem, name):
        """
        真实备份或提升完成后立即中断。
        """
        result = real_rename(self, fileitem, name)
        should_crash = name.startswith(".mp-backup-") if crash_after == "backup" else name == target.name
        if should_crash:
            raise ProcessStopped()
        return result

    monkeypatch.setattr(LocalStorage, "rename", crash_after_backup)
    with pytest.raises(ProcessStopped):
        _execute(checkpoint, monkeypatch, _durable_runner(repository, checkpoint))
    real_query = LocalStorage.get_item_strict

    def unavailable(self, path):
        """
        仅在备份观察边界模拟远端查询失败。
        """
        unavailable_path = path.name.startswith(".mp-backup-") if crash_after == "backup" else path == target
        if unavailable_path:
            raise OSError("storage unavailable")
        return real_query(self, path)

    monkeypatch.setattr(LocalStorage, "get_item_strict", unavailable)
    monkeypatch.setattr(LocalStorage, "rename", lambda *_args: pytest.fail("未知步骤不得重放"))
    with pytest.raises(execution_module._TransferManualReviewRequired):
        _execute(checkpoint, monkeypatch, _durable_runner(repository, checkpoint))
    assert repository.get_snapshot(task_id="safe-overwrite").state is TransferExecutionState.MANUAL_REVIEW
    assert next(tmp_path.glob(".mp-backup-*")).read_bytes() == b"old-version"
    new_path = next(tmp_path.glob(".mp-stage-*")) if crash_after == "backup" else target
    assert new_path.read_bytes() == b"new-version"
    assert source.read_bytes() == b"new-version"


@pytest.mark.parametrize("durable", [False, True])
def test_latest_cleans_other_versions_only_after_target_is_complete(tmp_path, monkeypatch, durable):
    """
    成功路径延后清理其它版本，复制过程中旧版本仍完整可读。
    """
    source = tmp_path / "downloads" / "download.mkv"
    source.parent.mkdir()
    source.write_bytes(b"new-version")
    target = tmp_path / "Movie.2026.2160p.mkv"
    previous = tmp_path / "Movie.2026.1080p.mkv"
    previous.write_bytes(b"old-version")
    real_copy = LocalStorage.copy

    def copy(self, *args):
        """
        在真实文件复制开始时验证旧版本仍然可用。
        """
        assert previous.read_bytes() == b"old-version"
        return real_copy(self, *args)

    monkeypatch.setattr(LocalStorage, "copy", copy)
    runner = _ImmediateStepRunner() if durable else None
    result = _execute(_checkpoint(source, target, mode="latest"), monkeypatch, runner)
    assert result.success
    assert target.read_bytes() == b"new-version"
    assert not previous.exists()
    assert source.read_bytes() == b"new-version"
    if runner:
        assert runner.kinds.index("materialize_target") < runner.kinds.index("delete_version_target")


@pytest.mark.parametrize("exists", [False, True])
def test_extra_file_overwrite_only_backs_up_an_existing_target(tmp_path, monkeypatch, exists):
    """
    字幕等附加文件首次整理无需备份，已有目标则沿用安全覆盖。
    """
    source = tmp_path / "download.srt"
    target = tmp_path / "Movie.2026.srt"
    source.write_bytes(b"new-subtitle")
    if exists:
        target.write_bytes(b"old-subtitle")
    result = _execute(_checkpoint(source, target), monkeypatch, _ImmediateStepRunner())
    assert result.success
    assert target.read_bytes() == b"new-subtitle"


@pytest.mark.parametrize("fails", [False, True])
def test_remote_download_with_same_source_name_never_writes_over_old_target(
        tmp_path, monkeypatch, fails,
):
    """
    网盘适配器按源文件名下载时，必须隔离目录后再安全替换已有目标。
    """
    target = tmp_path / "Movie.2026.mkv"
    target.write_bytes(b"old-version")
    source = FileItem(storage="alist", path="/downloads/Movie.2026.mkv", name=target.name,
                      type="file", size=11, modify_time=1.0, extension="mkv")
    source_oper = Mock()
    target_oper = object.__new__(LocalStorage)

    def download(*, fileitem, path):
        """
        保留旧版本验证成功及只写入部分内容后失败两种真实下载语义。
        """
        assert target.read_bytes() == b"old-version"
        assert path.parent == target.parent
        assert path != target.parent
        downloaded = path / fileitem.name
        downloaded.write_bytes(b"partial" if fails else b"new-version")
        return None if fails else downloaded

    def delete(_item):
        """
        移动源删除必须发生在最终新版本已完整可读以后。
        """
        assert target.read_bytes() == b"new-version"
        return True

    source_oper.download.side_effect = download
    source_oper.delete.side_effect = delete
    source_oper.get_item_strict.return_value = source
    monkeypatch.setattr(LocalStorage, "_LocalStorage__should_show_progress", lambda *_args: False)
    kwargs = dict(
        step_runner=None, source_fileitem=source.model_dump(mode="json"),
        target_storage="local", source_oper=source_oper, target_oper=target_oper,
        target_file=target, transfer_type="move",
    )
    if fails:
        with pytest.raises(RuntimeError):
            TransHandler._TransHandler__replace_target_with_steps(**kwargs)
        assert target.read_bytes() == b"old-version"
        source_oper.delete.assert_not_called()
    else:
        result, _ = TransHandler._TransHandler__replace_target_with_steps(**kwargs)
        assert result.path == target.as_posix()
        assert target.read_bytes() == b"new-version"
        source_oper.delete.assert_called_once()
    assert not list(tmp_path.glob(".mp-download-*"))


@pytest.mark.parametrize("transfer_type", ["copy", "move", "link", "softlink"])
def test_directory_leaf_overwrite_replays_from_real_ledger(
        tmp_path, monkeypatch, repository_factory, transfer_type,
):
    """
    目录叶目标也必须安全覆盖，重启不能根据替换后的存在性改变步骤身份。
    """
    source = tmp_path / "downloads" / "Movie.2026.mkv"
    source.parent.mkdir()
    source.write_bytes(b"new-version")
    target = tmp_path / "library" / "Movie.2026.mkv"
    target.parent.mkdir()
    target.write_bytes(b"old-version")
    checkpoint = _checkpoint(source, target, transfer_type=transfer_type)
    planning_input = TransferPlanningInput(source_fileitem=FileItem(
        storage="local", path=source.parent.as_posix(), type="dir", name=source.parent.name,
    ).model_dump(mode="json"))
    checkpoint = replace(checkpoint, planning_input=planning_input, final_target_path=target.parent.as_posix())
    repository = repository_factory(checkpoint)
    for _ in range(2):
        result = _execute(checkpoint, monkeypatch, _durable_runner(repository, checkpoint))
        assert result.success
    assert target.read_bytes() == b"new-version"
    assert source.exists() is (transfer_type != "move")
    assert target.is_symlink() is (transfer_type == "softlink")
    assert not list(target.parent.glob(".mp-*"))


def test_safe_overwrite_steps_reject_free_paths_and_unfinished_predecessors(tmp_path):
    """
    账本仅接收冻结计划推导路径，且每个新增覆盖步骤都必须有已完成前驱。
    """
    source = tmp_path / "download.mkv"
    source.write_bytes(b"new")
    checkpoint = _checkpoint(source, tmp_path / "Movie.mkv", transfer_type="move")
    repository = TransactionalTransferExecutionRepository
    item = checkpoint.items[0]
    previous = [SimpleNamespace(
        phase="decision", kind="resolve_overwrite", state=TransferStepState.SUCCEEDED.value,
        intent_payload={"source": item.source_fileitem, "target_storage": item.target_storage,
                        "target_path": item.target_path}, result_payload={"over_flag": True},
    )]
    for phase, kind, payload in repository._safe_overwrite_intents(checkpoint, item):
        def validate(history, candidate):
            """
            核对某个候选意图的冻结计划归属与已完成前驱。
            """
            return repository._intent_belongs_to_checkpoint(
                checkpoint, phase=phase, kind=kind, payload=candidate, previous_steps=history,
            )
        assert not validate([], payload)
        assert validate(previous, payload)
        invalid = {**payload, ("target_path" if "target_path" in payload else "path"): "/unrelated.mkv"}
        assert not validate(previous, invalid)
        unfinished = [*previous[:-1], SimpleNamespace(**{
            **vars(previous[-1]), "state": TransferStepState.STARTED.value,
        })]
        assert not validate(unfinished, payload)
        previous.append(SimpleNamespace(
            phase=phase, kind=kind, intent_payload=payload,
            state=TransferStepState.SUCCEEDED.value, result_payload={"item": {}},
        ))


@pytest.mark.parametrize("transfer_type", ["copy", "move", "link", "softlink"])
def test_overwrite_never_deletes_a_source_that_is_also_the_target(tmp_path, monkeypatch, transfer_type):
    """
    原地整理不可把提升后的新目标当成移动源删除，也不能建立指向自身的软链接。
    """
    source = tmp_path / "Movie.2026.mkv"
    source.write_bytes(b"original")
    result = _execute(_checkpoint(source, source, transfer_type=transfer_type), monkeypatch, None)
    assert not result.success
    assert source.read_bytes() == b"original"
    assert not list(tmp_path.glob(".mp-*"))


def test_completed_promotion_does_not_allow_cleanup_when_new_target_becomes_unqueryable(
        tmp_path, monkeypatch, repository_factory,
):
    """
    提升已记成功但结算前重启，目标查询失败时仍保留源文件和旧备份。
    """
    source = tmp_path / "download.mkv"
    target = tmp_path / "Movie.2026.mkv"
    source.write_bytes(b"new-version")
    target.write_bytes(b"old-version")
    checkpoint = _checkpoint(source, target, transfer_type="move")
    repository = repository_factory(checkpoint)
    runner = _durable_runner(repository, checkpoint)
    real_run = runner.run

    class ProcessStopped(BaseException):
        """
        模拟提升成功已落账、源删除尚未开始时进程退出。
        """

    def stop_before_cleanup(**kwargs):
        """
        仅在清理源文件之前注入中断。
        """
        if kwargs["kind"] == "delete_overwrite_source":
            raise ProcessStopped()
        return real_run(**kwargs)

    monkeypatch.setattr(runner, "run", stop_before_cleanup)
    with pytest.raises(ProcessStopped):
        _execute(checkpoint, monkeypatch, runner)
    real_query = LocalStorage.get_item_strict

    def unavailable(self, path):
        """
        模拟重启后新目标所在挂载不可查询。
        """
        if path == target:
            raise OSError("target unavailable")
        return real_query(self, path)

    monkeypatch.setattr(LocalStorage, "get_item_strict", unavailable)
    with pytest.raises(execution_module._TransferManualReviewRequired):
        _execute(checkpoint, monkeypatch, _durable_runner(repository, checkpoint))
    assert repository.get_snapshot(task_id="safe-overwrite").state is TransferExecutionState.MANUAL_REVIEW
    assert target.read_bytes() == b"new-version"
    assert source.read_bytes() == b"new-version"
    assert next(tmp_path.glob(".mp-backup-*")).read_bytes() == b"old-version"


@pytest.mark.parametrize("ancestor", [False, True])
def test_history_cleanup_preserves_new_target_through_local_symlink_alias(
        tmp_path, monkeypatch, repository_factory, ancestor,
):
    """
    历史目的经符号链接父目录指向新文件或其祖先时，不能再整体删除。
    """
    source, library = tmp_path / "download.mkv", tmp_path / "library"
    library.mkdir()
    target = library / "Movie.2026.mkv"
    source.write_bytes(b"new-version")
    target.write_bytes(b"old-version")
    alias = tmp_path / "old-library-view"
    alias.symlink_to(library, target_is_directory=True)
    cleanup_path = alias if ancestor else alias / target.name
    cleanup_item = FileItem(storage="local", path=cleanup_path.as_posix(),
                            type="dir" if ancestor else "file")
    checkpoint = _checkpoint(source, target, transfer_type="move")
    checkpoint = replace(checkpoint, planning_input=replace(checkpoint.planning_input, options={
        "cleanup_dest_fileitem": cleanup_item.model_dump(mode="json"),
    }))
    repository = repository_factory(checkpoint)
    storage = object.__new__(LocalStorage)
    cleanup = Mock(wraps=storage.delete)
    monkeypatch.setattr(TransHandler, "_TransHandler__intercept_transfer", lambda *_args, **_kwargs: (True, ""))
    monkeypatch.setattr(LocalStorage, "_LocalStorage__should_show_progress", lambda *_args: False)
    result = object.__new__(FileManagerModule).execute_transfer_plan(
        checkpoint, meta=MetaBase(target.name),
        mediainfo=MediaInfo(type=MediaType.MOVIE, title="Movie", year="2026"),
        source_oper=storage, target_oper=storage, cleanup_media_file=cleanup,
        step_runner=_durable_runner(repository, checkpoint),
    )
    assert result.success
    assert target.read_bytes() == b"new-version"
    assert not source.exists()
    cleanup.assert_not_called()
    assert not list(library.glob(".mp-*"))


@pytest.mark.parametrize("mutation", ["source", "target", "source_identity", "target_identity", "backup_identity"])
def test_completed_promotion_rechecks_live_files_before_destructive_cleanup(
        tmp_path, monkeypatch, repository_factory, mutation,
):
    """
    恢复已成功提升的账本时，源、目标或备份变更必须保留文件并等待复核。
    """
    source, target = tmp_path / "download.mkv", tmp_path / "Movie.2026.mkv"
    source.write_bytes(b"new-version")
    target.write_bytes(b"old-version")
    checkpoint = _checkpoint(source, target, transfer_type="move")
    repository = repository_factory(checkpoint)
    runner = _durable_runner(repository, checkpoint)
    real_run = runner.run

    class ProcessStopped(BaseException):
        """
        模拟提升结果已落账、源文件清理尚未开始时进程退出。
        """

    def stop_before_cleanup(**kwargs):
        """
        仅中断第一次源清理，保留真实 SQLite 步骤结果。
        """
        if kwargs["kind"] == "delete_overwrite_source":
            raise ProcessStopped()
        return real_run(**kwargs)

    monkeypatch.setattr(runner, "run", stop_before_cleanup)
    with pytest.raises(ProcessStopped):
        _execute(checkpoint, monkeypatch, runner)
    backup = next(tmp_path.glob(".mp-backup-*"))
    changed = source if mutation.startswith("source") else backup if mutation == "backup_identity" else target
    if mutation.endswith("identity"):
        before = changed.stat()
        replacement = tmp_path / "replacement.mkv"
        replacement.write_bytes(b"replacement")
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        replacement.replace(changed)
    else:
        changed.write_bytes(b"different-newer-payload" if mutation == "source" else b"partial")
    expected_source, expected_target, expected_backup = source.read_bytes(), target.read_bytes(), backup.read_bytes()
    with pytest.raises(execution_module._TransferManualReviewRequired):
        _execute(checkpoint, monkeypatch, _durable_runner(repository, checkpoint))
    assert repository.get_snapshot(task_id="safe-overwrite").state is TransferExecutionState.MANUAL_REVIEW
    # 备份单独被替换时源可能已经安全删除，但该新备份仍不得被旧任务删除。
    if mutation != "backup_identity":
        assert source.read_bytes() == expected_source
    assert target.read_bytes() == expected_target
    assert backup.read_bytes() == expected_backup


@pytest.mark.parametrize("missing_evidence", ["source", "target"])
def test_overwrite_preserves_all_copies_when_cleanup_identity_evidence_is_missing(
        tmp_path, monkeypatch, repository_factory, missing_evidence,
):
    """
    旧快照或适配器缺少修改时间时，禁止把当前查询结果当作原文件凭据。
    """
    source, target = tmp_path / "download.mkv", tmp_path / "Movie.2026.mkv"
    source.write_bytes(b"new-version")
    target.write_bytes(b"old-version")
    checkpoint = _checkpoint(source, target, transfer_type="move")
    if missing_evidence == "source":
        frozen_source = {**checkpoint.items[0].source_fileitem, "modify_time": None}
        checkpoint = replace(checkpoint,
                             planning_input=replace(checkpoint.planning_input, source_fileitem=frozen_source),
                             items=(replace(checkpoint.items[0], source_fileitem=frozen_source),))
    else:
        real_query = LocalStorage.get_item_strict

        def query_without_time(self, path):
            """
            模拟目标适配器未提供可持久验证的修改时间。
            """
            item = real_query(self, path)
            return item.model_copy(update={"modify_time": None}) if item and path == target else item

        monkeypatch.setattr(LocalStorage, "get_item_strict", query_without_time)
    repository = repository_factory(checkpoint)
    with pytest.raises(execution_module._TransferManualReviewRequired):
        _execute(checkpoint, monkeypatch, _durable_runner(repository, checkpoint))
    assert repository.get_snapshot(task_id="safe-overwrite").state is TransferExecutionState.MANUAL_REVIEW
    assert source.read_bytes() == target.read_bytes() == b"new-version"
    assert next(tmp_path.glob(".mp-backup-*")).read_bytes() == b"old-version"


def test_reported_success_with_partial_stage_does_not_move_old_target(
        tmp_path, monkeypatch, repository_factory,
):
    """
    适配器返回成功但暂存文件不完整时，真实大小校验须阻止旧目标备份或删除。
    """
    source, target = tmp_path / "download.mkv", tmp_path / "Movie.2026.mkv"
    source.write_bytes(b"new-version")
    target.write_bytes(b"old-version")
    checkpoint = _checkpoint(source, target, transfer_type="move")
    repository = repository_factory(checkpoint)

    def partial_copy(_storage, _item, directory, name):
        """
        模拟远端 SDK 错把部分写入报告为成功。
        """
        (directory / name).write_bytes(b"partial")
        return True

    monkeypatch.setattr(LocalStorage, "copy", partial_copy)
    with pytest.raises(execution_module._TransferManualReviewRequired):
        _execute(checkpoint, monkeypatch, _durable_runner(repository, checkpoint))
    assert source.read_bytes() == b"new-version"
    assert target.read_bytes() == b"old-version"
    assert not list(tmp_path.glob(".mp-backup-*"))


def test_uncommitted_promotion_does_not_adopt_replaced_target_as_success_evidence(
        tmp_path, monkeypatch, repository_factory,
):
    """
    提升已生效但未记成功时，恢复须沿用暂存凭据，不能采纳后来被替换的文件。
    """
    source, target = tmp_path / "download.mkv", tmp_path / "Movie.2026.mkv"
    source.write_bytes(b"new-version")
    target.write_bytes(b"old-version")
    checkpoint = _checkpoint(source, target, transfer_type="move")
    repository = repository_factory(checkpoint)
    real_rename = LocalStorage.rename

    class ProcessStopped(BaseException):
        """
        模拟文件提升完成后、账本写入成功前退出。
        """

    def promote_then_stop(self, fileitem, name):
        """
        保留真实 rename 副作用，在最终提升操作后停止。
        """
        result = real_rename(self, fileitem, name)
        if name == target.name:
            raise ProcessStopped()
        return result

    monkeypatch.setattr(LocalStorage, "rename", promote_then_stop)
    with pytest.raises(ProcessStopped):
        _execute(checkpoint, monkeypatch, _durable_runner(repository, checkpoint))
    target.write_bytes(b"partial")
    with pytest.raises(execution_module._TransferManualReviewRequired):
        _execute(checkpoint, monkeypatch, _durable_runner(repository, checkpoint))
    assert repository.get_snapshot(task_id="safe-overwrite").state is TransferExecutionState.MANUAL_REVIEW
    assert source.read_bytes() == b"new-version"
    assert target.read_bytes() == b"partial"
    assert next(tmp_path.glob(".mp-backup-*")).read_bytes() == b"old-version"
