"""
缺少解析元数据的整理任务在作业视图与恢复入口中的回归。
"""

import queue
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.application.transfer.execution import TransferExecutionState
from app.application.transfer.jobs import JobManager
from app.application.transfer.models import TransferTask
from app.domain.context import MediaInfo
from app.domain.meta.metavideo import MetaVideo
from app.schemas.file import FileItem
from app.schemas.system import TransferDirectoryConf
from app.schemas.transfer import TransferInfo
from app.schemas.types import MediaType
from tests.test_transfer_pending_replay import _admission, _build_chain
from tests.test_transfer_planning_checkpoint import _chain, _checkpoint


def _task(*, has_media: bool = False, path: str = "/downloads/Movie.mkv") -> TransferTask:
    """
    构造未解析文件名、可选已识别媒体的任务。
    """
    media = None
    if has_media:
        media = MediaInfo()
        media.title = "Movie"
        media.type = MediaType.MOVIE
        media.tmdb_id = 123
    return TransferTask(
        fileitem=FileItem(
            storage="local", path=path, type="file", name="Movie.mkv", size=1024,
        ),
        mediainfo=media,
    )


@pytest.mark.parametrize("has_media", [False, True])
def test_unregistered_task_without_meta_can_be_queried_and_cleaned(has_media: bool) -> None:
    """
    规划前失败或恢复尚未登记时，查询和重复清理不能再抛元数据空值异常。
    """
    manager = JobManager()
    task = _task(has_media=has_media)

    manager.get_job_id(task)
    manager.running_task(task)
    manager.finish_task(task)
    manager.fail_task(task)
    manager.fail_unfinished_task(task)
    assert manager.is_done(task)
    assert manager.is_finished(task)
    assert manager.is_success(task)
    manager.try_remove_job(task)
    manager.try_remove_job(task)
    assert manager.remove_job(task) is None
    assert manager.total() == 0


@pytest.mark.parametrize("has_media", [False, True])
@pytest.mark.parametrize("terminal", ["finish_task", "fail_task", "fail_unfinished_task"])
def test_task_without_meta_is_registered_and_settled(has_media: bool, terminal: str) -> None:
    """
    无元数据任务仍应登记、去重并按真实结果进入终态，不得误报重复或残留视图。
    """
    manager = JobManager()
    task = _task(has_media=has_media)
    assert manager.add_task(task)
    assert not manager.add_task(task.model_copy(deep=True))
    job = manager.list_jobs()[0]
    assert job.media.title == ("Movie" if has_media else "Movie.mkv")
    assert job.season is None
    assert job.tasks[0].meta is None
    assert not manager.is_done(task)
    assert not manager.is_finished(task)
    assert not manager.is_success(task)

    manager.start_execution(task)
    manager.running_task(task)
    assert job.tasks[0].state == "running"
    manager.try_remove_job(task)
    assert manager.total() == 1
    getattr(manager, terminal)(task)
    assert manager.is_done(task)
    assert manager.is_success(task) is (terminal == "finish_task")
    if has_media:
        assert manager.is_finished(task) is (terminal == "finish_task")
        assert manager.season_episodes(task.mediainfo) == []
    manager.finish_execution(task)
    manager.try_remove_job(task)
    assert manager.total() == 0


def test_unrecognized_tasks_without_meta_are_isolated_by_source() -> None:
    """
    无识别身份时按存储和路径隔离，同名文件的结束清理不能影响其它源任务。
    """
    manager = JobManager()
    task = _task()
    sibling = _task(path="/other/Movie.mkv")
    remote = task.model_copy(deep=True)
    remote.fileitem.storage = "alist"
    for item in (task, sibling, remote):
        assert manager.add_task(item)
    assert len({manager.get_job_id(item) for item in (task, sibling, remote)}) == 3

    manager.finish_task(task)
    manager.try_remove_job(task)

    assert manager.total() == 2
    assert not manager.is_done(sibling)
    assert not manager.is_done(remote)


def test_task_without_meta_can_migrate_after_recognition() -> None:
    """
    识别成功后从源文件作业迁入媒体作业，保留运行状态且正常结束清理。
    """
    manager = JobManager()
    task = _task()
    assert manager.add_task(task)
    manager.running_task(task)
    manager.start_execution(task)
    original_id = manager.get_job_id(task)
    task.mediainfo = _task(has_media=True).mediainfo

    assert manager.migrate_task(task)
    assert manager.get_job_id(task) != original_id
    assert manager.total() == 1
    assert manager.list_jobs()[0].tasks[0].state == "running"
    manager.finish_task(task)
    manager.finish_execution(task)
    manager.try_remove_job(task)
    assert manager.total() == 0


def test_migrated_tasks_without_meta_keep_unfinished_siblings() -> None:
    """
    多个未知源任务识别为同一媒体后，保留未完成成员且不残留源文件关联。
    """
    manager = JobManager()
    tasks = [_task(), _task(path="/other/Movie.mkv")]
    for task in tasks:
        assert manager.add_task(task)
        task.mediainfo = _task(has_media=True).mediainfo
        assert manager.migrate_task(task)
    manager.finish_task(tasks[0])
    manager.try_remove_job(tasks[0])
    assert manager.total() == 2
    assert not manager.is_done(tasks[1])
    manager.fail_task(tasks[1])
    manager.try_remove_job(tasks[1])
    assert manager.total() == 0
    assert not manager._meta_to_media_ids
    assert not manager._season_episodes
    assert not manager._task_state_changed_at


def test_successful_recognition_without_meta_does_not_skip_transfer() -> None:
    """
    识别得到媒体的无元数据任务继续规划执行，不得被迁移失败误报为重复。
    """
    task = _task()
    task.preview = True
    task.target_directory = TransferDirectoryConf(library_path="/library")
    task.mark_planning_context_restored()
    chain = _chain()
    chain.jobview = JobManager()
    chain.runtime_config = SimpleNamespace(scrape_follow_tmdb=True)
    chain._recognize_task_meta = Mock(return_value=_task(has_media=True).mediainfo)
    chain._plan_checkpoint_and_execute = Mock(
        return_value=TransferInfo(success=True, message="完成", fileitem=task.fileitem),
    )
    chain._finish_scrape_batch_task = Mock()
    assert chain.jobview.add_task(task)

    def settle(recovered: TransferTask, info: TransferInfo) -> tuple[bool, str]:
        """
        回读真实迁移后的任务并结束作业，避免保留未完成测试状态。
        """
        assert recovered.meta is None
        chain.jobview.finish_task(recovered)
        return info.success, info.message

    assert chain._TransferChain__perform_transfer(task, settle) == (True, "完成")
    chain._plan_checkpoint_and_execute.assert_called_once()
    assert chain.jobview.total() == 0


@pytest.mark.parametrize("season", [0, 1])
def test_parsed_job_lookup_keeps_explicit_season(season: int) -> None:
    """
    无元数据兜底不改变已解析任务的按名称查找与特别篇季号。
    """
    manager = JobManager()
    task = _task()
    task.meta = MetaVideo(f"Show.S{season:02d}E01.mkv")
    assert manager.add_task(task)
    assert manager.has_tasks(task.meta, season=season)
    assert not manager.has_tasks(task.meta, season=season + 1)
    manager.fail_task(task)
    manager.try_remove_job(task)
    assert not manager.has_tasks(task.meta, season=season)


@pytest.mark.parametrize("planned", [False, True])
def test_replay_without_meta_enters_real_jobview(planned: bool) -> None:
    """
    accepted 媒体快照和无元数据的拒绝计划均可经真实作业登记恢复入队。
    """
    task = _task(has_media=not planned)
    admission = _admission(task.fileitem.path)
    planning_input = replace(
        admission.planning_input,
        mediainfo=task.mediainfo.to_dict() if task.mediainfo else None,
    )
    checkpoint = replace(
        _checkpoint(), planning_input=planning_input, items=(),
        resolved_meta=None, resolved_mediainfo=None,
        rejection_error="未识别到媒体信息",
    ) if planned else None
    admission = replace(admission, planning_input=planning_input, checkpoint=checkpoint)
    chain = _build_chain(Mock())
    chain._queue = queue.Queue()
    chain.jobview = JobManager()
    chain._register_scrape_batch_task = Mock()
    chain._TransferChain__register_claimed_admission(admission)

    enqueue = (
        chain._TransferChain__queue_planned_replay
        if planned else chain._TransferChain__queue_accepted_replay
    )
    assert enqueue(task.fileitem, admission)
    recovered = chain._queue.get_nowait().task
    assert recovered.meta is None
    assert chain.jobview.total() == 1
    assert recovered.planning_context_restored
    chain._transfer_admissions.release_claim.assert_not_called()
    chain._finish_queue_item(recovered)


def test_rejected_plan_without_meta_preserves_original_failure_on_replay() -> None:
    """
    拒绝计划回放保留业务失败原因，作业清理不能掩盖结果或派生人工复核。
    """
    task = _task()
    task.bind_admission_task_id("task-missing-meta")
    chain = _chain()
    chain.jobview = JobManager()
    chain._finish_scrape_batch_task = Mock()
    planning_input = replace(
        _checkpoint().planning_input, meta=None, mediainfo=None,
    )
    checkpoint = replace(
        _checkpoint(), planning_input=planning_input, items=(),
        resolved_meta=None, resolved_mediainfo=None,
        rejection_error="未识别到媒体信息",
    )
    task.bind_planning_input(planning_input)
    task.bind_plan_checkpoint(checkpoint)
    chain._TransferChain__claim_task_for_execution(task)
    assert chain.jobview.add_task(task)

    def settle(recovered: TransferTask, info: TransferInfo) -> tuple[bool, str]:
        """
        在真实执行拒绝步骤后标记失败，保留原始错误供调用方结算。
        """
        assert recovered is task
        assert info.success is False
        chain.jobview.fail_task(recovered)
        return info.success, info.message

    assert chain._TransferChain__handle_transfer(task, settle) == (False, "未识别到媒体信息")
    assert task.meta is None
    assert task.execution_checkpoint.payload["outcome"] == "failed"
    assert [step.kind for step in chain._transfer_executions.steps.values()] == ["reject"]
    assert chain.jobview.total() == 0
    assert chain._transfer_executions.state is TransferExecutionState.SETTLING
    chain.execute_transfer_plan.assert_not_called()
