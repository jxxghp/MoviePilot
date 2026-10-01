from unittest.mock import Mock

import pytest

from app.application.transfer.jobs import JobManager
from app.application.transfer.workflow import TransferQueueService
from app.schemas.file import FileItem
from app.schemas.transfer import TransferJob, TransferJobTask
from tests.test_transfer_job_manager import make_task


def _job(*paths: str) -> TransferJob:
    """构造只包含队列分页测试所需字段的整理作业。"""
    return TransferJob(
        season=1,
        tasks=[
            TransferJobTask(
                fileitem=FileItem(
                    name=path.rsplit("/", 1)[-1],
                    path=path,
                    size=1,
                    storage="local",
                    type="file",
                    children=[
                        FileItem(
                            name="cloud-child.mkv",
                            path=f"{path}.child",
                            size=1,
                            storage="local",
                            type="file",
                        )
                    ],
                ),
                state="waiting",
            )
            for path in paths
        ],
    )


def test_job_manager_page_keeps_flat_order_and_only_projects_the_requested_window():
    """大队列分页必须保持任务顺序，并按窗口复制作业投影。"""
    manager = JobManager()
    manager._job_view = {
        ("first", 1): _job("/downloads/first-1.mkv", "/downloads/first-2.mkv"),
        ("second", 1): _job(
            "/downloads/second-1.mkv",
            "/downloads/second-2.mkv",
            "/downloads/second-3.mkv",
        ),
    }

    first_page, total = manager.list_jobs_page(page=1, count=3)
    second_page, second_total = manager.list_jobs_page(page=2, count=3)

    assert total == second_total == 5
    assert [len(job.tasks or []) for job in first_page] == [2, 1]
    assert not hasattr(first_page[0].tasks[0].fileitem, "children")
    assert [task.fileitem.path for job in first_page for task in job.tasks or []] == [
        "/downloads/first-1.mkv",
        "/downloads/first-2.mkv",
        "/downloads/second-1.mkv",
    ]
    assert [task.fileitem.path for job in second_page for task in job.tasks or []] == [
        "/downloads/second-2.mkv",
        "/downloads/second-3.mkv",
    ]


@pytest.mark.parametrize("page,count", [(0, 1), (1, 0), (-1, 10)])
def test_job_manager_page_rejects_invalid_windows(page: int, count: int):
    """分页窗口不接受零或负数，避免异常切片产生隐性全量查询。"""
    with pytest.raises(ValueError, match="队列分页参数必须为正数"):
        JobManager().list_jobs_page(page, count)


def test_transfer_queue_service_uses_bounded_projection_after_expiring_tasks():
    """应用服务先清理失活任务，再调用可选的受限投影依赖。"""
    expire_tasks = Mock()
    list_tasks_page = Mock(return_value=([_job("/downloads/page.mkv")], 101))
    service = TransferQueueService(
        register_task=lambda _task: True,
        admit_task=lambda _task: None,
        enqueue=lambda _queue: None,
        before_enqueue=lambda _task: None,
        enqueue_failed=lambda _task, _error: None,
        remove_task=lambda _fileitem: None,
        list_tasks=lambda: [],
        expire_tasks=expire_tasks,
        list_tasks_page=list_tasks_page,
    )

    items, total = service.list_page(page=3, count=50)

    assert len(items) == 1
    assert total == 101
    expire_tasks.assert_called_once_with()
    list_tasks_page.assert_called_once_with(3, 50)


def test_job_manager_duplicate_index_is_released_when_a_cloud_task_is_removed():
    """批量云盘任务的 O(1) 去重索引不能阻止同一源文件后续重新入队。"""
    manager = JobManager()
    task = make_task(1)

    assert manager.add_task(task) is True
    assert manager.add_task(make_task(1)) is False

    manager.remove_task(task.fileitem)

    assert manager.add_task(make_task(1)) is True
