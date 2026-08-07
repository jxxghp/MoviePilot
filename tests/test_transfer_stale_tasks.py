"""整理任务失活收敛行为测试。"""

from app.chain import transfer
from app.chain.transfer import JobManager
from app.schemas import FileItem, TransferTask
from app.schemas.types import MediaType


class _FakeMeta:
    """提供整理任务分组需要的最小元数据。"""

    def __init__(self):
        self.name = "Test Show"
        self.title = "Test Show S01E01"
        self.year = "2026"
        self.type = MediaType.TV
        self.begin_season = 1
        self.end_season = None
        self.total_season = 1
        self.begin_episode = 1
        self.end_episode = None
        self.total_episode = 1
        self.episode_list = [1]
        self.season_episode = "S01E01"
        self.part = None

    def to_dict(self):
        """返回 TransferJobTask 所需的元数据字典。"""
        return {
            "title": self.title,
            "name": self.name,
            "year": self.year,
            "type": self.type.value,
            "begin_season": self.begin_season,
            "end_season": self.end_season,
            "total_season": self.total_season,
            "begin_episode": self.begin_episode,
            "end_episode": self.end_episode,
            "total_episode": self.total_episode,
            "season_episode": self.season_episode,
            "episode_list": self.episode_list,
            "part": self.part,
        }


def _make_task(name: str = "Test.Show.S01E01.mkv") -> TransferTask:
    """创建失活检测使用的整理任务。"""
    return TransferTask(
        fileitem=FileItem(
            storage="local",
            path=f"/downloads/{name}",
            type="file",
            name=name,
            basename=name.removesuffix(".mkv"),
            extension="mkv",
            size=1024,
        ),
        meta=_FakeMeta(),
    )


def test_external_running_task_expires_without_heartbeat(monkeypatch):
    """外部接管的运行中任务超过心跳期限后应被标记失败并清理。"""
    clock = [100.0]
    monkeypatch.setattr(transfer, "monotonic", lambda: clock[0])
    manager = JobManager()
    task = _make_task()
    assert manager.add_task(task)
    manager.running_task(task)

    clock[0] = 221.0
    expired = manager.expire_stale_running_tasks(timeout_seconds=120)

    assert expired == [(task.fileitem, 121)]
    assert manager.list_jobs() == []


def test_main_thread_execution_is_not_expired(monkeypatch):
    """主程序整理线程仍在执行的任务不应被失活检测伪清理。"""
    clock = [100.0]
    monkeypatch.setattr(transfer, "monotonic", lambda: clock[0])
    manager = JobManager()
    task = _make_task()
    assert manager.add_task(task)
    manager.start_execution(task)
    manager.running_task(task)

    clock[0] = 500.0
    assert manager.expire_stale_running_tasks(timeout_seconds=120) == []
    assert manager.total() == 1

    manager.finish_execution(task)
    expired = manager.expire_stale_running_tasks(timeout_seconds=120)
    assert expired == [(task.fileitem, 400)]
    assert manager.list_jobs() == []


def test_waiting_task_and_refreshed_heartbeat_do_not_expire(monkeypatch):
    """等待中任务不受失活期限影响，重复运行状态更新可刷新外部心跳。"""
    clock = [100.0]
    monkeypatch.setattr(transfer, "monotonic", lambda: clock[0])
    manager = JobManager()
    waiting_task = _make_task("Test.Show.S01E01.waiting.mkv")
    running_task = _make_task("Test.Show.S01E02.running.mkv")
    running_task.meta.begin_episode = 2
    running_task.meta.episode_list = [2]
    assert manager.add_task(waiting_task)
    assert manager.add_task(running_task)
    manager.running_task(running_task)

    clock[0] = 180.0
    manager.running_task(running_task)
    clock[0] = 250.0

    assert manager.expire_stale_running_tasks(timeout_seconds=120) == []
    assert manager.total() == 2
