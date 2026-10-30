"""
整理执行的空元数据拒绝、季集选择与失败历史回归。
"""

from pathlib import Path
from types import SimpleNamespace
from typing import Optional
from unittest.mock import Mock

import pytest

from app.application.transfer.execution import TransferExecutionState
from app.application.transfer.jobs import JobManager
from app.application.transfer.workflow import TransferTask
from app.db.adapters.chain import TransactionalChainDurableEventWriter
from app.db.adapters.transfer.admission import TransactionalTransferAdmissionRepository
from app.db.adapters.transfer.execution import TransactionalTransferExecutionRepository
from app.db.models.transferexecutionstep import TransferExecutionStep
from app.db.models.transferhistory import TransferHistory
from app.db.models.transferpending import TransferPending
from app.db.models.transfersettlementreceipt import TransferSettlementReceipt
from app.db.session import SessionFactory
from app.domain.context import MediaInfo
from app.domain.metainfo import MetaInfoPath
from app.schemas.file import FileItem
from app.schemas.system import TransferDirectoryConf
from app.schemas.transfer import TransferInfo
from app.schemas.types import MediaSource, MediaType
from tests.test_transfer_history_write_path import _repository
from tests.test_transfer_planning_checkpoint import _chain, _planned_admission


def _task(*, preview: bool = False, has_media: bool = True) -> TransferTask:
    """
    构造有 TMDB 身份、缺少文件名元数据的第二季单集任务。
    """
    name = "Tantei.wa.mou.Shindeiru.S02E04.2026.1080p.Baha.WEB-DL.x264.AAC-ADWeb.mp4"
    return TransferTask(
        fileitem=FileItem(
            storage="local", path=f"/downloads/{name}", type="file", name=name,
            basename=Path(name).stem, extension="mp4", size=1024,
        ),
        mediainfo=MediaInfo(
            type=MediaType.TV, title="侦探已死", tmdb_id=117061,
            media_source=MediaSource.TMDB, media_id="117061",
        ) if has_media else None,
        target_directory=TransferDirectoryConf(library_storage="local", library_path="/library"),
        transfer_type="copy",
        preview=preview,
    )


def _execution_chain(monkeypatch, task: TransferTask):
    """
    隔离在线识别与文件执行，保留真实规划拒绝和持久步骤编排。
    """
    chain = _chain()
    chain.jobview = JobManager()
    chain.jobview.add_task(task)
    chain.runtime_config = SimpleNamespace(scrape_follow_tmdb=True)
    chain._success_target_files = {}
    task.bind_admission_task_id("task-execution-metadata")
    chain._finish_scrape_batch_task = Mock()
    chain._transfer_admissions.checkpoint_plan.side_effect = (
        lambda **kwargs: _planned_admission(task, kwargs["checkpoint"])
    )
    media = Mock()
    media.recognize_by_meta.return_value = MediaInfo(type=MediaType.TV, tmdb_id=117061)
    media.supplement_tmdb_info.side_effect = lambda info, _meta: info
    tmdb = Mock()
    tmdb.tmdb_episodes.return_value = []
    monkeypatch.setattr("app.chain.transfer.execution.MediaChain", lambda: media)
    monkeypatch.setattr("app.chain.transfer.execution.TmdbChain", lambda: tmdb)
    return chain, media, tmdb


@pytest.mark.parametrize("preview", [False, True])
@pytest.mark.parametrize("has_media", [False, True])
@pytest.mark.parametrize("with_callback", [False, True])
def test_missing_meta_is_rejected_before_recognition_and_file_operations(
    monkeypatch, preview: bool, has_media: bool, with_callback: bool,
) -> None:
    """
    空元数据须返回可操作失败；正式任务提交拒绝检查点，预览保持零写入。
    """
    task = _task(preview=preview, has_media=has_media)
    chain, media, tmdb = _execution_chain(monkeypatch, task)
    callback = Mock(side_effect=lambda _task, info: (info.success, info.message)) if with_callback else None

    state, message = chain._TransferChain__handle_transfer(task, callback)

    assert state is False
    assert "缺少文件名元数据" in message
    assert "重新预览" in message
    assert "季集" in message
    assert task.meta is None
    media.recognize_by_meta.assert_not_called()
    media.supplement_tmdb_info.assert_not_called()
    tmdb.tmdb_episodes.assert_not_called()
    chain.plan_transfer.assert_not_called()
    chain.execute_transfer_plan.assert_not_called()
    if with_callback:
        callback.assert_called_once()
    if preview:
        assert task.plan_checkpoint is None
        assert task.execution_checkpoint is None
        chain._transfer_admissions.checkpoint_plan.assert_not_called()
    else:
        assert task.plan_checkpoint.rejection_error == message
        assert task.plan_checkpoint.items == ()
        assert task.execution_checkpoint.payload["outcome"] == "failed"
        assert [step.kind for step in chain._transfer_executions.steps.values()] == ["reject"]


@pytest.mark.parametrize(
    ("filename", "media_season", "expected_season"),
    [
        ("Tantei.wa.mou.Shindeiru.S02E04.2026.mp4", None, 2),
        ("Test.Show.S00E04.mp4", None, 0),
        ("Test.Show.S02E04.mp4", 0, 0),
        ("Test.Show.S02E04.mp4", 3, 3),
        ("Test.Show.E04.mp4", None, 1),
    ],
)
def test_tv_episode_lookup_preserves_resolved_season(
    monkeypatch, filename: str, media_season: Optional[int], expected_season: int,
) -> None:
    """
    剧集详情优先使用媒体季，其次文件名季；保留特别篇和有效元数据的第一季约定。
    """
    task = _task(preview=True)
    task.meta = MetaInfoPath(Path("/downloads") / filename)
    task.mediainfo.season = media_season
    chain, _media, tmdb = _execution_chain(monkeypatch, task)
    chain._plan_checkpoint_and_execute = Mock(return_value=TransferInfo(success=True))

    assert chain._TransferChain__handle_transfer(task)[0] is True

    tmdb.tmdb_episodes.assert_called_once_with(
        tmdbid=117061, season=expected_season, episode_group=None,
    )
    assert task.meta.begin_episode == 4


@pytest.mark.parametrize("restored", [False, True])
@pytest.mark.parametrize("has_media", [False, True])
def test_missing_meta_failure_is_atomically_written_to_history(
    db, tmp_path, monkeypatch, restored: bool, has_media: bool,
) -> None:
    """
    accepted 恢复与普通执行均写入失败历史并结束任务，源文件不变且不进入人工复核。
    """
    db.watermark(TransferExecutionStep, TransferHistory, TransferPending, TransferSettlementReceipt)
    task = _task(has_media=has_media)
    source = tmp_path / task.fileitem.name
    source.write_bytes(b"unchanged episode")
    task.fileitem.path = source.as_posix()
    chain, _media, tmdb = _execution_chain(monkeypatch, task)
    chain._transfer_admissions = TransactionalTransferAdmissionRepository(SessionFactory)
    chain._transfer_executions = TransactionalTransferExecutionRepository(SessionFactory)
    chain.durable_event_writer = TransactionalChainDurableEventWriter(SessionFactory)
    chain.transfer_history_repository = _repository()
    chain._is_overwrite_declined = Mock(return_value=False)
    chain._durable_transfer_event = Mock(return_value=None)
    chain.queue_failed_transfer_notification = Mock()
    chain._TransferChain__mark_torrent_completed_if_done = Mock()
    chain._maybe_take_over_failed_transfer = Mock()
    chain.notify_successful_transfer = Mock()
    chain._send_metadata_scrape_event = Mock()
    chain._TransferChain__admit_transfer(task)
    if restored:
        chain.jobview.remove_job(task)
        chain._transfer_admissions.release_claim(
            task_id=task.admission_task_id, lease_token=task.lease_token,
        )
        admission = chain._transfer_admissions.claim_task(
            task_id=task.admission_task_id, owner_id=chain._worker_owner_id, lease_seconds=120,
        )
        queued = []
        chain.put_to_queue = Mock(side_effect=lambda replay: queued.append(replay) or True)
        assert chain._TransferChain__queue_accepted_replay(task.fileitem, admission)
        task = queued[0]
        assert task.meta is None
        assert task.planning_context_restored is has_media
        assert chain.jobview.add_task(task)

    state, message = chain._TransferChain__handle_transfer(
        task, chain._TransferChain__default_callback,
    )

    history = chain.transfer_history_repository.get_by_src(source.as_posix(), "local")
    snapshot = chain._transfer_executions.get_snapshot(task_id=task.admission_task_id)
    assert state is False
    assert history is not None
    assert history.status is False
    assert history.errmsg == message
    assert "缺少文件名元数据" in message
    assert history.failure_stage == "planning"
    assert "重新生成计划" in history.recovery_action
    assert history.transfer_task_id == task.admission_task_id
    assert history.seasons is None
    assert history.episodes is None
    assert history.year is None
    assert history.media_id == ("117061" if has_media else None)
    assert task.terminal_settled
    assert snapshot.state is TransferExecutionState.FAILED
    assert snapshot.terminal_history_id == history.id
    assert [step.kind for step in snapshot.steps] == ["reject"]
    assert source.read_bytes() == b"unchanged episode"
    assert chain.jobview.total() == 0
    tmdb.tmdb_episodes.assert_not_called()
    chain.queue_failed_transfer_notification.assert_called_once()
    chain.notify_successful_transfer.assert_not_called()
    chain._send_metadata_scrape_event.assert_not_called()
