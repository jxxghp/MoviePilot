import unittest

from app.chain.transfer import JobManager, TransferChain
from app.schemas import FileItem, TransferTask
from app.schemas.types import MediaType


class FakeMeta:
    def __init__(self, episode: int):
        self.name = "Test Show"
        self.title = f"Test Show S01E{episode:02d}"
        self.year = "2026"
        self.type = MediaType.TV
        self.begin_season = 1
        self.end_season = None
        self.total_season = 1
        self.begin_episode = episode
        self.end_episode = None
        self.total_episode = 1
        self.episode_list = [episode]
        self.season_episode = f"S01E{episode:02d}"
        self.part = None

    @property
    def season(self):
        return "S01"

    @property
    def episode(self):
        return f"E{self.begin_episode:02d}"

    def to_dict(self):
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


class FakeMedia:
    def __init__(self, tmdb_id: int = 12345):
        self.tmdb_id = tmdb_id
        self.douban_id = None

    def clear(self):
        pass

    def to_dict(self):
        return {
            "type": MediaType.TV.value,
            "title": "Test Show",
            "year": "2026",
            "title_year": "Test Show (2026)",
            "tmdb_id": self.tmdb_id,
            "douban_id": self.douban_id,
        }


def make_task(episode: int) -> TransferTask:
    name = f"Test.Show.S01E{episode:02d}.mkv"
    return TransferTask(
        fileitem=FileItem(
            storage="local",
            path=f"/downloads/Test Show/{name}",
            type="file",
            name=name,
            basename=name.removesuffix(".mkv"),
            extension="mkv",
            size=1024,
        ),
        meta=FakeMeta(episode),
    )


def migrate_to_media_job(jobview: JobManager, task: TransferTask):
    task.mediainfo = FakeMedia()
    jobview.migrate_task(task)
    jobview.running_task(task)
    jobview.finish_task(task)
    jobview.try_remove_job(task)


class TransferJobManagerTest(unittest.TestCase):
    def test_completed_media_job_is_removed_after_last_meta_task_fails(self):
        jobview = JobManager()
        tasks = [make_task(episode) for episode in range(1, 4)]
        for task in tasks:
            self.assertTrue(jobview.add_task(task))

        migrate_to_media_job(jobview, tasks[0])
        migrate_to_media_job(jobview, tasks[1])

        # 还有一个 meta 任务未处理时，media 组虽然已完成也不能提前清理。
        self.assertEqual(2, len(jobview.list_jobs()))

        # 最后一个仍在 meta 组中的任务未识别，__handle_transfer 会直接 remove_task 后 return。
        jobview.remove_task(tasks[2].fileitem)
        jobview.try_remove_job(tasks[2])

        self.assertEqual([], jobview.list_jobs())

    def test_completed_media_job_is_removed_after_all_meta_tasks_migrate(self):
        jobview = JobManager()
        tasks = [make_task(episode) for episode in range(1, 3)]
        for task in tasks:
            self.assertTrue(jobview.add_task(task))

        migrate_to_media_job(jobview, tasks[0])
        self.assertEqual(2, len(jobview.list_jobs()))

        migrate_to_media_job(jobview, tasks[1])
        self.assertEqual([], jobview.list_jobs())

    def test_exception_marks_unfinished_meta_task_failed_and_cleans_jobs(self):
        jobview = JobManager()
        tasks = [make_task(episode) for episode in range(1, 3)]
        for task in tasks:
            self.assertTrue(jobview.add_task(task))

        migrate_to_media_job(jobview, tasks[0])
        jobview.running_task(tasks[1])

        jobview.fail_unfinished_task(tasks[1])
        jobview.try_remove_job(tasks[1])

        self.assertEqual([], jobview.list_jobs())

    def test_exception_marks_unfinished_media_task_failed_and_cleans_jobs(self):
        jobview = JobManager()
        task = make_task(1)
        self.assertTrue(jobview.add_task(task))

        task.mediainfo = FakeMedia()
        jobview.migrate_task(task)
        jobview.running_task(task)

        jobview.fail_unfinished_task(task)
        jobview.try_remove_job(task)

        self.assertEqual([], jobview.list_jobs())

    def test_pre_recognized_jobs_with_same_meta_do_not_block_each_other(self):
        jobview = JobManager()
        task1 = make_task(1)
        task2 = make_task(2)
        task1.mediainfo = FakeMedia(100)
        task2.mediainfo = FakeMedia(200)

        self.assertTrue(jobview.add_task(task1))
        self.assertTrue(jobview.add_task(task2))

        jobview.running_task(task1)
        jobview.finish_task(task1)
        jobview.try_remove_job(task1)

        jobs = jobview.list_jobs()
        self.assertEqual(1, len(jobs))
        self.assertEqual(task2.fileitem, jobs[0].tasks[0].fileitem)

    def test_pre_recognized_migrations_with_same_meta_do_not_link_jobs(self):
        jobview = JobManager()
        task1 = make_task(1)
        task2 = make_task(2)
        task1.mediainfo = FakeMedia(100)
        task2.mediainfo = FakeMedia(200)

        self.assertTrue(jobview.add_task(task1))
        self.assertTrue(jobview.add_task(task2))

        self.assertTrue(jobview.migrate_task(task1))
        self.assertTrue(jobview.migrate_task(task2))
        jobview.running_task(task1)
        jobview.finish_task(task1)
        jobview.try_remove_job(task1)

        jobs = jobview.list_jobs()
        self.assertEqual(1, len(jobs))
        self.assertEqual(task2.fileitem, jobs[0].tasks[0].fileitem)

    def test_exception_failure_marks_downloader_hash_completed_before_cleanup(self):
        chain = object.__new__(TransferChain)
        chain.jobview = JobManager()
        completed = []

        def fake_transfer_completed(hashs, downloader):
            completed.append((hashs, downloader))

        chain.transfer_completed = fake_transfer_completed
        task = make_task(1)
        task.downloader = "qbittorrent"
        task.download_hash = "abc123"
        self.assertTrue(chain.jobview.add_task(task))
        chain.jobview.running_task(task)

        chain._TransferChain__fail_transfer_task(task)

        self.assertEqual([("abc123", "qbittorrent")], completed)
        self.assertEqual([], chain.jobview.list_jobs())


if __name__ == "__main__":
    unittest.main()
