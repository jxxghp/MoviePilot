"""音乐识别失败的真实事务、接纳重放与零文件副作用重规划。"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.application.transfer.execution import (
    TransferExecutionConflictError,
    TransferExecutionState,
    TransferOperationObservation,
    TransferOperationObservationState,
    TransferStepResult,
)
from app.application.transfer.recovery import music_replanning_input
from app.application.transfer.workflow import (
    TransferLeaseLostError,
    TransferPlanCheckpoint,
    TransferPlanningInput,
    TransferPlanningStateError,
    TransferTask,
)
from app.chain.media import MediaChain
from app.chain.media.cache import AlbumDirectoryCache
from app.chain.transfer.execution import _DurableTransferStepRunner
from app.chain.transfer.music import defer_music_recognition, refresh_music_retry_context
from app.db.adapters.transfer.admission import TransactionalTransferAdmissionRepository
from app.db.adapters.transfer.execution import TransactionalTransferExecutionRepository
from app.db.models.transferexecutionstep import TransferExecutionStep
from app.db.models.transferhistory import TransferHistory
from app.db.models.transferpending import TransferPending
from app.db.models.transfersettlementreceipt import TransferSettlementReceipt
from app.domain.context import MusicInfo
from app.domain.meta.metamusic import MetaMusic
from app.modules.filemanager.storages.local import LocalStorage
from app.modules.filemanager.transhandler import TransHandler
from app.modules.musicbrainz import MusicBrainzModule
from app.schemas.transfer import TransferInfo
from app.schemas.types import MediaType
from tests.test_music_album_match import _release_detail
from tests.test_music_cue import _image_pair
from tests.test_music_resource_context import _audio_files
from tests.test_transfer_sync_extra_files import bind_empty_history_repositories, make_transfer_chain


@pytest.fixture
def store(tmp_path, monkeypatch):
    """用隔离SQLite及可控租约时钟验证跨实例、跨重启的状态行为。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'music-recovery.db'}")
    for model in (TransferHistory, TransferPending, TransferExecutionStep, TransferSettlementReceipt):
        model.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    admissions = TransactionalTransferAdmissionRepository(factory)
    executions = TransactionalTransferExecutionRepository(factory)
    clock = [datetime.now(timezone.utc)]
    monkeypatch.setattr(admissions, "_lease_now", lambda: clock[0])
    yield admissions, executions, factory, clock
    engine.dispose()


def _input(paths):
    """冻结真实音频范围及尚未成功的媒体识别结果。"""
    item = LocalStorage().get_item(paths[0])
    meta = MetaMusic(title="旧错误曲名", album="Album", artists=["Artist"])
    info = MusicInfo.from_meta(meta)
    info.raw_data["recognition"] = {"status": "service_error", "message": "识别服务暂时不可用"}
    return TransferPlanningInput(
        source_fileitem=item.model_dump(mode="json"), meta=meta.to_dict(), mediainfo=info.to_dict(),
        media_type=MediaType.MUSIC.value, requested_transfer_type="copy", need_scrape=False,
        options={"_meta_kind": "MetaMusic", "_mediainfo_kind": "MusicInfo", "music_recognition_scope": {
            "storage": "local", "directory": str(paths[0].parent), "main": paths[0].name,
            "files": [path.name for path in paths], "regions": None, "scripts": None,
        }},
    )


def _claim(admissions, planning):
    """准入并取得用于当前测试的真实租约。"""
    admitted = admissions.admit(storage="local", src_path=planning.source_fileitem["path"], planning_input=planning)
    claimed = admissions.claim_task(task_id=admitted.task_id, owner_id="test", lease_seconds=120)
    assert claimed is not None
    return claimed


def _state(factory, task_id):
    """从新Session读取状态，避免用ORM内存值冒充已提交的结果。"""
    with factory() as session:
        row = session.execute(select(TransferPending).where(TransferPending.task_id == task_id)).scalar_one()
        return row.state, row.execution_state, row.retry_count, row.checkpoint_payload, row.lease_token


def test_planning_backoff_survives_restart_and_obeys_due_time_and_budget(store, tmp_path, monkeypatch):
    """故障退避保持accepted且没有检查点；到期后新仓储实例可以claim，预算耗尽不再延期。"""
    admissions, executions, factory, clock = store
    planning = _input(_audio_files(tmp_path / "Artist - Album (2004)"))
    claimed = _claim(admissions, planning)

    assert admissions.defer_planning(task_id=claimed.task_id, lease_token=claimed.lease_token,
                                     error="unavailable", retry_after=30, max_retries=1)
    assert _state(factory, claimed.task_id) == ("accepted", "retry_wait", 1, None, None)
    assert executions.get_snapshot(task_id=claimed.task_id).steps == ()
    restarted = TransactionalTransferAdmissionRepository(factory)
    monkeypatch.setattr(restarted, "_lease_now", lambda: clock[0])
    assert restarted.claim_recoverable(owner_id="new", limit=10, lease_seconds=120) == []
    clock[0] += timedelta(seconds=31)
    next_claim = restarted.claim_recoverable(owner_id="new", limit=10, lease_seconds=120)[0]
    assert next_claim.planning_input == planning
    assert not restarted.defer_planning(task_id=claimed.task_id, lease_token=next_claim.lease_token,
                                        error="still unavailable", retry_after=30, max_retries=1)
    assert _state(factory, claimed.task_id)[2] == 1


def test_planning_backoff_rejects_stale_lease_and_existing_plan(store, tmp_path):
    """陈旧worker和已有计划均不能借识别故障重置执行状态。"""
    admissions, _executions, factory, _clock = store
    planning = _input(_audio_files(tmp_path / "Artist - Album (2004)"))
    claimed = _claim(admissions, planning)
    with pytest.raises(TransferLeaseLostError):
        admissions.defer_planning(task_id=claimed.task_id, lease_token="stale", error="unavailable", retry_after=30, max_retries=3)
    checkpoint = _rejection(planning)
    admissions.checkpoint_plan(task_id=claimed.task_id, lease_token=claimed.lease_token,
                              input_fingerprint=planning.fingerprint, checkpoint=checkpoint)
    with pytest.raises(TransferPlanningStateError):
        admissions.defer_planning(task_id=claimed.task_id, lease_token=claimed.lease_token,
                                  error="unavailable", retry_after=30, max_retries=3)
    assert _state(factory, claimed.task_id)[3] == checkpoint.to_payload()


@pytest.mark.parametrize("evidence", ["checkpoint_version", "history"])
def test_retry_budget_exhaustion_does_not_hide_existing_evidence(store, tmp_path, evidence):
    """即使重试预算为零，也必须先拒绝不完整计划或已有历史，不能直接写新拒绝计划。"""
    admissions, _executions, factory, _clock = store
    planning = _input(_audio_files(tmp_path / "Artist - Album (2004)"))
    claimed = _claim(admissions, planning)
    with factory() as session:
        if evidence == "checkpoint_version":
            row = session.execute(select(TransferPending)).scalar_one()
            row.checkpoint_version = 2
        else:
            session.add(TransferHistory(src=planning.source_fileitem["path"], status=False, transfer_task_id=claimed.task_id))
        session.commit()

    with pytest.raises(TransferPlanningStateError):
        admissions.defer_planning(task_id=claimed.task_id, lease_token=claimed.lease_token,
                                  error="unavailable", retry_after=30, max_retries=0)


def test_missing_source_can_abandon_only_unplanned_backoff(store, tmp_path):
    """源文件被移除时，无计划、无步骤的退避记录仍可正常清理。"""
    admissions, _executions, factory, clock = store
    paths = _audio_files(tmp_path / "Artist - Album (2004)")
    claimed = _claim(admissions, _input(paths))
    admissions.defer_planning(task_id=claimed.task_id, lease_token=claimed.lease_token,
                              error="unavailable", retry_after=30, max_retries=3)
    paths[0].unlink()
    clock[0] += timedelta(seconds=31)
    next_claim = admissions.claim_recoverable(owner_id="new", limit=1, lease_seconds=120)[0]
    assert admissions.abandon_unstarted(task_id=next_claim.task_id, lease_token=next_claim.lease_token) == 1
    with factory() as session:
        assert session.execute(select(TransferPending)).scalars().all() == []


def test_accepted_replay_reads_current_album_instead_of_saved_failure(tmp_path, monkeypatch):
    """恢复重新读取当前音频并按原范围匹配，冻结输入中的旧失败信息保持审计用途。"""
    paths = _audio_files(tmp_path / "Artist - Album (2004)")
    planning = _input(paths)
    task = TransferTask(fileitem=LocalStorage().get_item(paths[0]), meta=MetaMusic.from_dict(planning.meta),
                        mediainfo=MusicInfo.from_dict(planning.mediainfo), mtype=MediaType.MUSIC)
    task.bind_planning_input(planning)
    task.mark_planning_context_restored()
    owner = make_transfer_chain()
    bind_empty_history_repositories(owner)
    album = MusicBrainzModule._release_to_album(_release_detail("release", "Album", "Artist", [("First Song", 3), ("Second Song", 3)]))
    source = Mock(match_music_album=Mock(return_value=album))
    monkeypatch.setattr("app.chain.media.album.MusicBrainzChain", lambda: source)
    monkeypatch.setattr(MediaChain(), "_album_dir_cache", AlbumDirectoryCache(4))

    refresh_music_retry_context(owner, task)

    assert task.mediainfo.media_id == "rec-1"
    assert task.meta.title == task.mediainfo.title == "First Song"
    assert task.planning_input == planning
    assert planning.mediainfo["raw_data"]["recognition"]["status"] == "service_error"


def test_accepted_replay_without_media_still_marks_music_scope_for_refresh(store, tmp_path, monkeypatch):
    """故障发生在准入后的识别阶段时，原输入没有mediainfo也必须恢复音乐识别范围。"""
    admissions, _executions, _factory, _clock = store
    paths = _audio_files(tmp_path / "Artist - Album (2004)")
    planning = replace(_input(paths), mediainfo=None)
    claimed = _claim(admissions, planning)
    owner = make_transfer_chain()
    queued = []
    monkeypatch.setattr(owner, "_TransferChain__bind_claimed_admission", lambda *_args: None)
    monkeypatch.setattr(owner, "put_to_queue", lambda task: queued.append(task) or True)

    assert owner._TransferChain__queue_accepted_replay(LocalStorage().get_item(paths[0]), claimed)
    assert queued[0].planning_context_restored
    assert queued[0].mediainfo is None and queued[0].plan_checkpoint is None
    assert queued[0].planning_input.fingerprint == planning.fingerprint


def test_transient_lookup_defers_without_planning_or_file_operations(store, tmp_path, monkeypatch):
    """临时故障在进入文件计划前退出，并调用已有恢复调度器。"""
    admissions, _executions, factory, _clock = store
    paths = _audio_files(tmp_path / "Artist - Album (2004)")
    planning = _input(paths)
    claimed = _claim(admissions, planning)
    task = TransferTask(fileitem=LocalStorage().get_item(paths[0]), meta=MetaMusic.from_dict(planning.meta),
                        mediainfo=MusicInfo.from_dict(planning.mediainfo), mtype=MediaType.MUSIC)
    task.bind_planning_input(planning)
    task.bind_admission_task_id(claimed.task_id)
    task.bind_execution_lease(owner_id="test", lease_token=claimed.lease_token)
    owner = make_transfer_chain()
    owner._transfer_admissions = admissions
    monkeypatch.setattr(owner, "_TransferChain__claim_task_for_execution", Mock())
    monkeypatch.setattr(owner, "_TransferChain__assert_owned_lease", Mock())
    forgotten, scheduled = Mock(), Mock()
    monkeypatch.setattr(owner, "_TransferChain__forget_owned_lease", forgotten)
    monkeypatch.setattr(owner, "_TransferChain__ensure_recovery_scheduler", scheduled)

    result = defer_music_recognition(owner, task)

    assert result[0] is False and "自动重新识别" in result[1]
    assert _state(factory, claimed.task_id)[1:4] == ("retry_wait", 1, None)
    scheduled.assert_called_once_with(immediate=False)
    forgotten.assert_called_once_with(claimed.task_id, claimed.lease_token)


def _rejection(planning):
    """构造真实零文件副作用拒绝检查点。"""
    return TransferPlanCheckpoint(planning_input=planning, target_storage="local",
                                  root_target_path=planning.source_fileitem["path"],
                                  final_target_path=planning.source_fileitem["path"], resolved_transfer_type="copy",
                                  items=(), rejection_error="识别暂时失败", resolved_meta_kind="MetaMusic",
                                  resolved_meta=planning.meta, resolved_mediainfo_kind="MusicInfo", resolved_mediainfo=planning.mediainfo)


def _failed_rejection(store, paths):
    """用正式步骤仓储提交拒绝证据，再建立已失败终态和历史用于重试测试。"""
    admissions, executions, factory, _clock = store
    planning = _input(paths)
    claimed = _claim(admissions, planning)
    checkpoint = _rejection(planning)
    admissions.checkpoint_plan(task_id=claimed.task_id, lease_token=claimed.lease_token,
                              input_fingerprint=planning.fingerprint, checkpoint=checkpoint)
    runner = _DurableTransferStepRunner(task_id=claimed.task_id, lease_token=claimed.lease_token,
                                        checkpoint_fingerprint=checkpoint.fingerprint, repository=executions)
    evidence = TransferStepResult(payload={"error": checkpoint.rejection_error})
    runner.run(phase="planning", kind="reject", payload=evidence.payload, execute=lambda: evidence,
               observe=lambda: TransferOperationObservation(state=TransferOperationObservationState.APPLIED, evidence=evidence))
    runner.checkpoint(TransferInfo(success=False, fileitem=LocalStorage().get_item(paths[0]), message="识别暂时失败"))
    with factory() as session:
        history = TransferHistory(src=str(paths[0]), title="旧失败记录", status=False, type="音乐",
                                  transfer_task_id=claimed.task_id, transfer_settlement_revision=1)
        session.add(history)
        session.flush()
        row = session.execute(select(TransferPending).where(TransferPending.task_id == claimed.task_id)).scalar_one()
        row.execution_state, row.terminal_history_id, row.settlement_revision = "failed", history.id, 1
        row.lease_owner, row.lease_token, row.lease_expires_at = None, None, None
        history_id = history.id
        session.add(TransferSettlementReceipt(
            task_id=claimed.task_id, history_id=history_id, settlement_revision=1, outcome="failed",
            execution_fingerprint=row.execution_fingerprint, lease_token=claimed.lease_token,
            history_status=False, src=str(paths[0]), src_storage="local", pending_deleted=False,
            error="识别暂时失败", created_at="2026-09-30 00:00:00", updated_at="2026-09-30 00:00:00",
        ))
        session.commit()
    return claimed, planning, history_id


def test_retry_replans_zero_effect_music_rejection_and_preserves_history(store, tmp_path):
    """人工重试零操作音乐拒绝会生成新准入，旧失败历史仍可审计。"""
    _admissions, executions, factory, _clock = store
    claimed, planning, history_id = _failed_rejection(store, _audio_files(tmp_path / "Artist - Album (2004)"))

    result = executions.request_retry(task_id=claimed.task_id, reason="retry", requested_by="test")

    assert result.accepted and result.state is TransferExecutionState.NOT_STARTED
    with factory() as session:
        pending = session.execute(select(TransferPending)).scalar_one()
        assert pending.task_id != claimed.task_id
        assert pending.state == "accepted" and pending.checkpoint_payload is None
        assert pending.planning_input["options"]["music_replanned_from"] == claimed.task_id
        assert pending.planning_input["source_fileitem"] == planning.source_fileitem
        assert session.get(TransferHistory, history_id).status is False
        assert session.get(TransferHistory, history_id).transfer_task_id is None
        receipt = session.execute(select(TransferSettlementReceipt)).scalar_one()
        assert receipt.task_id == claimed.task_id and receipt.history_id == history_id
        assert session.execute(select(TransferExecutionStep)).scalars().all() == []


def test_replanning_failure_rolls_back_old_internal_evidence(store, tmp_path, monkeypatch):
    """新准入写入失败时整个事务回滚，旧任务、拒绝证据和历史都保留。"""
    _admissions, executions, factory, _clock = store
    claimed, _planning, history_id = _failed_rejection(store, _audio_files(tmp_path / "Artist - Album (2004)"))
    monkeypatch.setattr("app.db.oper.transferpending.TransferPendingOper.stage_admit", Mock(side_effect=RuntimeError("write failed")))

    with pytest.raises(RuntimeError, match="write failed"):
        executions.request_retry(task_id=claimed.task_id, reason="retry", requested_by="test")
    assert _state(factory, claimed.task_id)[1] == "failed"
    with factory() as session:
        assert len(session.execute(select(TransferExecutionStep)).scalars().all()) == 1
        assert session.get(TransferHistory, history_id).status is False


def test_replanning_stale_history_revision_rolls_back_the_rejection_step(store, tmp_path):
    """历史版本已变化时，重规划CAS失败且不能丢失之前的拒绝证据。"""
    _admissions, executions, factory, _clock = store
    claimed, _planning, history_id = _failed_rejection(store, _audio_files(tmp_path / "Artist - Album (2004)"))
    with factory() as session:
        session.get(TransferHistory, history_id).transfer_settlement_revision = 2
        session.commit()

    with pytest.raises(TransferExecutionConflictError, match="状态已改变"):
        executions.request_retry(task_id=claimed.task_id, reason="retry", requested_by="test")

    assert _state(factory, claimed.task_id)[1] == "failed"
    with factory() as session:
        assert len(session.execute(select(TransferExecutionStep)).scalars().all()) == 1
        assert session.execute(select(TransferSettlementReceipt)).scalar_one().history_id == history_id


def test_unsafe_replay_scope_does_not_query_other_directory(tmp_path, monkeypatch):
    """损坏或越界的范围不进入识别，也不能借文件名恢复时读取无关文件。"""
    paths = _audio_files(tmp_path / "Artist - Album (2004)")
    planning = _input(paths)
    options = dict(planning.options)
    options["music_recognition_scope"] = {**options["music_recognition_scope"], "files": ["../private.flac"]}
    task = TransferTask(fileitem=LocalStorage().get_item(paths[0]), meta=MetaMusic.from_dict(planning.meta))
    task.bind_planning_input(replace(planning, options=options))
    task.mark_planning_context_restored()
    owner = make_transfer_chain()
    network = Mock(side_effect=AssertionError("越界不得识别"))
    monkeypatch.setattr(owner, "_match_music_album_context", network)

    refresh_music_retry_context(owner, task)

    assert task.mediainfo.raw_data["recognition"]["status"] == "conflict"
    network.assert_not_called()


def test_recovered_album_executes_real_copy_once_with_original_input(store, tmp_path, monkeypatch):
    """退避后用新识别结果冻结并执行真实复制，原输入不变，后续重放不重复写文件。"""
    admissions, executions, _factory, clock = store
    paths = _audio_files(tmp_path / "Artist - Album (2004)")
    original = paths[0].read_bytes()
    planning = _input(paths)
    claimed = _claim(admissions, planning)
    admissions.defer_planning(task_id=claimed.task_id, lease_token=claimed.lease_token,
                              error="unavailable", retry_after=30, max_retries=3)
    clock[0] += timedelta(seconds=31)
    replay = admissions.claim_recoverable(owner_id="recovered", limit=1, lease_seconds=120)[0]
    task = TransferTask(fileitem=LocalStorage().get_item(paths[0]), meta=MetaMusic.from_dict(planning.meta),
                        mediainfo=MusicInfo.from_dict(planning.mediainfo), mtype=MediaType.MUSIC)
    task.bind_planning_input(replay.planning_input)
    task.mark_planning_context_restored()
    owner = make_transfer_chain()
    bind_empty_history_repositories(owner)
    album = MusicBrainzModule._release_to_album(_release_detail("release", "Album", "Artist", [("First Song", 3), ("Second Song", 3)]))
    monkeypatch.setattr("app.chain.media.album.MusicBrainzChain", lambda: Mock(match_music_album=Mock(return_value=album)))
    monkeypatch.setattr(MediaChain(), "_album_dir_cache", AlbumDirectoryCache(4))
    refresh_music_retry_context(owner, task)
    handler, storage = TransHandler(), LocalStorage()
    monkeypatch.setattr("app.modules.filemanager.transhandler.eventmanager.send_event", Mock(return_value=None))
    checkpoint = handler.plan_transfer(planning, meta=task.meta, mediainfo=task.mediainfo, source_oper=storage,
                                       target_storage="local", target_path=tmp_path / "library", transfer_type="copy",
                                       need_scrape=False, need_rename=True, need_notify=False, overwrite_mode="never",
                                       episodes_info=None, preview=False)
    admissions.checkpoint_plan(task_id=replay.task_id, lease_token=replay.lease_token,
                              input_fingerprint=planning.fingerprint, checkpoint=checkpoint)
    copies = Mock(wraps=storage.copy)
    monkeypatch.setattr(storage, "copy", copies)

    def execute():
        """模拟重启后用同一计划及操作账本执行。"""
        runner = _DurableTransferStepRunner(task_id=replay.task_id, lease_token=replay.lease_token,
                                            checkpoint_fingerprint=checkpoint.fingerprint, repository=executions)
        return handler.execute_transfer_plan(checkpoint, meta=task.meta, mediainfo=task.mediainfo,
                                              source_oper=storage, target_oper=storage, step_runner=runner)

    first = execute()
    assert first.success, first.message
    assert execute().success
    assert copies.call_count == 1
    assert Path(checkpoint.final_target_path).read_bytes() == original
    assert paths[0].read_bytes() == original
    assert checkpoint.planning_input.fingerprint == planning.fingerprint


def test_music_retry_with_file_operation_evidence_never_replans(store, tmp_path):
    """即使拒绝记录意外混有外部操作证据，重试也必须保留原任务与操作账本。"""
    _admissions, executions, factory, _clock = store
    claimed, _planning, history_id = _failed_rejection(store, _audio_files(tmp_path / "Artist - Album (2004)"))
    with factory() as session:
        step = session.execute(select(TransferExecutionStep)).scalar_one()
        step.phase, step.kind = "file", "copy"
        operation_id, payload = step.operation_id, dict(step.intent_payload)
        session.commit()

    result = executions.request_retry(task_id=claimed.task_id, reason="retry", requested_by="test")

    assert result.accepted and result.state is TransferExecutionState.RETRY_WAIT
    assert _state(factory, claimed.task_id)[3] is not None
    with factory() as session:
        step = session.execute(select(TransferExecutionStep)).scalar_one()
        assert step.operation_id == operation_id and step.intent_payload == payload
        assert step.kind == "copy"
        assert session.get(TransferHistory, history_id).transfer_task_id == claimed.task_id


def test_remote_retry_never_reads_matching_local_path(tmp_path, monkeypatch):
    """远端重试仍只用名称与种子，不因路径在本机存在而读取本机标签。"""
    paths = _audio_files(tmp_path / "Artist - Album")
    planning = _input(paths)
    remote_item = {**planning.source_fileitem, "storage": "smb"}
    planning = replace(planning, source_fileitem=remote_item, options={**planning.options,
                         "music_recognition_scope": {"storage": "smb", "name_only": True}})
    task = TransferTask(fileitem=remote_item, meta=MetaMusic.from_dict(planning.meta), mediainfo=MusicInfo.from_dict(planning.mediainfo))
    task.bind_planning_input(planning)
    task.mark_planning_context_restored()
    owner = make_transfer_chain()
    bind_empty_history_repositories(owner)
    read = Mock(side_effect=AssertionError("远端不能读取本机标签"))
    monkeypatch.setattr("app.chain.transfer.music.AudioMetadataHelper.read_tags", read)

    refresh_music_retry_context(owner, task)

    assert task.meta.title == "First Song" and task.mediainfo is None
    read.assert_not_called()


def test_cue_companion_replay_uses_original_main_audio(tmp_path):
    """CUE旁挂恢复使用原主音频，保留整轨语义，不能把CUE文件当歌曲识别。"""
    audio, cue = _image_pair(tmp_path)
    planning = _input([audio])
    task = TransferTask(fileitem=LocalStorage().get_item(cue), meta=MetaMusic.from_dict(planning.meta),
                        mediainfo=MusicInfo.from_dict(planning.mediainfo))
    task.bind_planning_input(replace(planning, source_fileitem=task.fileitem.model_dump(mode="json")))
    task.mark_planning_context_restored()
    owner = make_transfer_chain()
    bind_empty_history_repositories(owner)

    refresh_music_retry_context(owner, task)

    assert task.fileitem.path == str(cue)
    assert task.meta.music_layout == "image_cue"
    assert task.mediainfo.music_type == "album" and task.mediainfo.title == "专辑示例"


def test_replanning_explicit_music_identity_refreshes_only_classification(tmp_path, monkeypatch):
    """显式选定的媒体身份和名称不被重新猜测，重规划仅重算当时未通过的分类。"""
    paths = _audio_files(tmp_path / "Artist - Album (2004)")
    selected = MusicInfo(media_source="musicbrainz", media_id="selected-recording", title="Selected Song", album="Album")
    planning = replace(_input(paths), media_source="musicbrainz", media_id=selected.media_id,
                       mediainfo=selected.to_dict(), options={"_meta_kind": "MetaMusic", "_mediainfo_kind": "MusicInfo"})
    refreshed_input = music_replanning_input(_rejection(planning))
    task = TransferTask(fileitem=LocalStorage().get_item(paths[0]), meta=MetaMusic.from_dict(planning.meta), mediainfo=selected)
    task.bind_planning_input(refreshed_input)
    task.mark_planning_context_restored()
    owner = make_transfer_chain()
    classify = Mock(return_value=selected)
    monkeypatch.setattr(owner, "_finalize_recognition_result", classify)
    monkeypatch.setattr("app.chain.transfer.music.AudioMetadataHelper.read", Mock(side_effect=AssertionError("不可重新猜测身份")))

    refresh_music_retry_context(owner, task)

    classify.assert_called_once_with(selected, refresh=True)
    assert task.mediainfo.media_id == selected.media_id
    assert task.mediainfo.title == "Selected Song"
