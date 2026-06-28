import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

from app.core.config import settings
from app.core.context import MediaInfo
from app.core.meta import MetaVideo
from app.chain.transfer import JobManager, TransferChain
from app.modules.filemanager.transhandler import TransHandler
from app.schemas import EpisodeFormat, FileItem, TransferInfo, TransferTask
from app.schemas.types import EventType, MediaType


class FakeMeta:
    def __init__(self, episode: int, season: int = 1):
        self.name = "Test Show"
        self.title = f"Test Show S{season:02d}E{episode:02d}"
        self.year = "2026"
        self.type = MediaType.TV
        self.begin_season = season
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
        return f"S{self.begin_season:02d}"

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
        self.type = MediaType.TV
        self.title_year = "Test Show (2026)"

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


def make_media_info() -> MediaInfo:
    media = MediaInfo()
    media.type = MediaType.TV
    media.title = "Test Show"
    media.title_year = "Test Show (2026)"
    media.year = "2026"
    media.tmdb_id = 12345
    media.category = ""
    media.actors = []
    media.season_years = {}
    media.vote_average = 0
    return media


def make_task(episode: int, season: int = 1) -> TransferTask:
    name = f"Test.Show.S{season:02d}E{episode:02d}.mkv"
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


def make_transfer_chain() -> TransferChain:
    chain = object.__new__(TransferChain)
    chain.jobview = JobManager()
    chain._media_exts = settings.RMT_MEDIAEXT
    chain._subtitle_exts = settings.RMT_SUBEXT
    chain._audio_exts = settings.RMT_AUDIOEXT
    chain._allowed_exts = (
        chain._media_exts + chain._audio_exts + chain._subtitle_exts
    )
    chain._success_target_files = {}
    chain._scrape_batches = {}
    return chain


def make_fileitem(path: str, size: int = 1024) -> FileItem:
    file_path = path
    name = file_path.rsplit("/", 1)[-1]
    suffix = name.rsplit(".", 1)[-1] if "." in name else ""
    basename = name[: -(len(suffix) + 1)] if suffix else name
    return FileItem(
        storage="local",
        path=file_path,
        type="file",
        name=name,
        basename=basename,
        extension=suffix,
        size=size,
    )


def migrate_to_media_job(jobview: JobManager, task: TransferTask):
    task.mediainfo = FakeMedia()
    jobview.migrate_task(task)
    jobview.running_task(task)
    jobview.finish_task(task)
    jobview.try_remove_job(task)


class TransferJobManagerTest(unittest.TestCase):
    def test_same_storage_success_uses_target_path_when_metadata_is_delayed(self):
        """
        网盘操作已成功但目标元数据暂不可见时，整理结果应按成功路径落库。
        """
        source_item = FileItem(
            storage="alist",
            path="/downloads/Test.Show.S01E01.mkv",
            type="file",
            name="Test.Show.S01E01.mkv",
            basename="Test.Show.S01E01",
            extension="mkv",
            size=1024,
            modify_time=1715939275.0,
        )
        target_path = Path(
            "/library/Test Show (2026)/Season 1/Test.Show.S01E01.mkv"
        )
        target_folder = FileItem(
            storage="alist",
            path=f"{target_path.parent.as_posix()}/",
            type="dir",
            name=target_path.parent.name,
            basename=target_path.parent.stem,
        )
        source_oper = SimpleNamespace(
            is_support_transtype=lambda transfer_type: True,
            move=lambda fileitem, path, name: True,
        )
        target_oper = SimpleNamespace(
            get_folder=lambda path: target_folder,
            get_item=lambda path: None,
        )

        new_item, errmsg = TransHandler._TransHandler__transfer_command(
            fileitem=source_item,
            target_storage="alist",
            source_oper=source_oper,
            target_oper=target_oper,
            target_file=target_path,
            transfer_type="move",
        )

        self.assertEqual("", errmsg)
        self.assertIsNotNone(new_item)
        self.assertEqual(target_path.as_posix(), new_item.path)
        self.assertEqual("alist", new_item.storage)
        self.assertEqual("file", new_item.type)
        self.assertEqual(1024, new_item.size)

    def test_transfer_media_uses_target_folder_returned_by_storage(self):
        """
        整理成功时直接使用存储层返回的目标目录项，回调和事件不再二次拼装。
        """
        handler = TransHandler()
        source_item = FileItem(
            storage="alist",
            path="/downloads/Test.Show.S01E01.mkv",
            type="file",
            name="Test.Show.S01E01.mkv",
            basename="Test.Show.S01E01",
            extension="mkv",
            size=1024,
            modify_time=1715939275.0,
        )
        target_path = Path("/library")
        target_file = Path(
            "/library/Test.Show.S01E01.mkv"
        )
        target_folder = FileItem(
            storage="alist",
            type="dir",
            path="/library/",
            name="library",
            basename="library",
        )
        target_item = FileItem(
            storage="alist",
            path=target_file.as_posix(),
            type="file",
            name=target_file.name,
            basename=target_file.stem,
            extension="mkv",
            size=1024,
        )
        source_oper = SimpleNamespace(
            is_support_transtype=lambda transfer_type: True,
            move=lambda fileitem, path, name: True,
        )
        target_oper = SimpleNamespace(
            get_folder=lambda path: target_folder,
            get_item=lambda path: None,
        )

        with patch.object(
                TransHandler, "get_rename_path", return_value=target_file
        ), patch(
                "app.modules.filemanager.transhandler.DirectoryHelper.get_media_root_path",
                return_value=Path("/library"),
        ), patch.object(
                TransHandler,
                "_TransHandler__transfer_command",
                return_value=(target_item, ""),
        ), patch("app.modules.filemanager.transhandler.eventmanager") as eventmanager_mock:
            eventmanager_mock.send_event.return_value = None
            transferinfo = handler.transfer_media(
                fileitem=source_item,
                in_meta=MetaVideo("Test.Show.S01E01"),
                mediainfo=make_media_info(),
                target_storage="alist",
                target_path=target_path,
                transfer_type="move",
                source_oper=source_oper,
                target_oper=target_oper,
                need_scrape=True,
                need_notify=True,
            )

        self.assertTrue(transferinfo.success)
        self.assertEqual(target_item, transferinfo.target_item)
        self.assertEqual(target_folder, transferinfo.target_diritem)

    def test_single_file_transfer_intercept_event_carries_file_meta(self):
        """
        单文件整理拦截事件应携带元数据，便于事件处理器按季集匹配。
        """
        handler = TransHandler()
        source_item = FileItem(
            storage="alist",
            path="/downloads/Test.Show.S02E03.mkv",
            type="file",
            name="Test.Show.S02E03.mkv",
            basename="Test.Show.S02E03",
            extension="mkv",
            size=1024,
            modify_time=1715939275.0,
        )
        target_path = Path("/library")
        target_file = Path("/library/Test.Show.S02E03.mkv")
        target_folder = FileItem(
            storage="alist",
            type="dir",
            path="/library/",
            name="library",
            basename="library",
        )
        target_item = FileItem(
            storage="alist",
            path=target_file.as_posix(),
            type="file",
            name=target_file.name,
            basename=target_file.stem,
            extension="mkv",
            size=1024,
        )
        source_oper = SimpleNamespace(
            is_support_transtype=lambda transfer_type: True,
            move=lambda fileitem, path, name: True,
        )
        target_oper = SimpleNamespace(
            get_folder=lambda path: target_folder,
            get_item=lambda path: None,
        )
        in_meta = MetaVideo("Test.Show.S02E03")

        with patch.object(
                TransHandler, "get_rename_path", return_value=target_file
        ), patch(
                "app.modules.filemanager.transhandler.DirectoryHelper.get_media_root_path",
                return_value=Path("/library"),
        ), patch.object(
                TransHandler,
                "_TransHandler__transfer_command",
                return_value=(target_item, ""),
        ), patch(
                "app.modules.filemanager.transhandler.eventmanager.send_event",
                return_value=None,
        ) as send_event:
            transferinfo = handler.transfer_media(
                fileitem=source_item,
                in_meta=in_meta,
                mediainfo=make_media_info(),
                target_storage="alist",
                target_path=target_path,
                transfer_type="move",
                source_oper=source_oper,
                target_oper=target_oper,
                need_scrape=True,
                need_notify=True,
            )

        self.assertTrue(transferinfo.success)
        event_data = send_event.call_args.args[1]
        self.assertIs(in_meta, event_data.meta)
        self.assertEqual(2, event_data.meta.begin_season)
        self.assertEqual(3, event_data.meta.begin_episode)

    def test_success_callback_uses_transfer_result_target_diritem(self):
        """
        回调发送刮削事件时应直接使用整理结果里的目标目录项。
        """
        chain = make_transfer_chain()
        chain.eventmanager = MagicMock()
        chain.transfer_completed = lambda *args, **kwargs: None

        task = make_task(1)
        task.mediainfo = FakeMedia()
        task.background = False
        task.manual = True
        self.assertTrue(chain._TransferChain__put_to_jobview(task))

        target_diritem = FileItem(
            storage="alist",
            path="/library/Test Show (2026)/Season 1/",
            type="dir",
            name="Season 1",
        )
        target_item = FileItem(
            storage="alist",
            path="/library/Test Show (2026)/Season 1/Test.Show.S01E01.mkv",
            type="file",
            name="Test.Show.S01E01.mkv",
            extension="mkv",
        )
        transferinfo = TransferInfo(
            success=True,
            fileitem=task.fileitem,
            target_item=target_item,
            target_diritem=target_diritem,
            file_list_new=[target_item.path],
            transfer_type="copy",
            need_scrape=True,
            need_notify=False,
        )

        with patch(
            "app.chain.transfer.TransferHistoryOper",
            return_value=SimpleNamespace(add_success=lambda **kwargs: SimpleNamespace(id=1)),
        ):
            state, errmsg = chain._TransferChain__default_callback(task, transferinfo)

        self.assertTrue(state)
        self.assertEqual("", errmsg)
        metadata_calls = [
            call
            for call in chain.eventmanager.send_event.call_args_list
            if call.args[0] == EventType.MetadataScrape
        ]
        self.assertEqual(1, len(metadata_calls))
        event_data = metadata_calls[0].args[1]
        self.assertEqual(target_diritem, event_data["fileitem"])
        self.assertEqual([target_item.path], event_data["file_list"])

    def test_manual_episode_offset_applies_once(self):
        chain = make_transfer_chain()
        source_fileitem = make_fileitem("/downloads/Test.Show.2026.S01E14.mkv")
        planned_episodes = []

        chain._TransferChain__get_trans_fileitems = lambda fileitem, predicate: [
            (source_fileitem, False)
        ]
        chain._TransferChain__put_to_jobview = lambda task: True
        chain._TransferChain__register_scrape_batch_task = lambda task: None
        chain._TransferChain__close_scrape_batch = lambda batch_id: None

        def fake_handle_transfer(task, callback=None):
            planned_episodes.append(task.meta.begin_episode)
            return True, ""

        chain._TransferChain__handle_transfer = fake_handle_transfer

        transfer_history_oper = SimpleNamespace(get_by_src=lambda src, storage=None: None)
        download_history_oper = SimpleNamespace(
            get_by_hash=lambda download_hash: None,
            get_file_by_fullpath=lambda fullpath: None,
            get_files_by_savepath=lambda savepath: [],
            get_by_path=lambda path: None,
        )
        system_config_oper = SimpleNamespace(get=lambda key: None)

        with patch("app.chain.transfer.TransferHistoryOper", return_value=transfer_history_oper), \
                patch("app.chain.transfer.DownloadHistoryOper", return_value=download_history_oper), \
                patch("app.chain.transfer.SystemConfigOper", return_value=system_config_oper), \
                patch("app.chain.transfer.MetaInfoPath", lambda *args, **kwargs: FakeMeta(14)):
            state, errmsg = chain.do_transfer(
                fileitem=source_fileitem,
                mediainfo=FakeMedia(),
                target_path=Path("/library"),
                epformat=EpisodeFormat(offset="-1"),
                background=False,
            )

        self.assertTrue(state, errmsg)
        # 手动集数偏移只能应用一次，避免 E14 + (-1) 被二次处理成 E12。
        self.assertEqual([13], planned_episodes)

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

    def test_same_source_file_is_deduped_across_media_jobs(self):
        """
        同一个源文件即使识别到不同媒体作业，也不能重复加入整理视图。
        """
        jobview = JobManager()
        task1 = make_task(1)
        task2 = make_task(1)
        task1.mediainfo = FakeMedia(100)
        task2.mediainfo = FakeMedia(200)

        self.assertTrue(jobview.add_task(task1))
        self.assertFalse(jobview.add_task(task2))

        jobs = jobview.list_jobs()
        self.assertEqual(1, len(jobs))
        self.assertEqual(task1.fileitem, jobs[0].tasks[0].fileitem)

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

    def test_exception_failure_does_not_mark_downloader_without_history(self):
        chain = make_transfer_chain()
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

        self.assertEqual([], completed)
        self.assertEqual([], chain.jobview.list_jobs())

    def test_successful_history_skip_marks_downloader_hash_completed(self):
        chain = make_transfer_chain()
        completed = []

        def fake_transfer_completed(hashs, downloader):
            completed.append((hashs, downloader))

        chain.transfer_completed = fake_transfer_completed
        chain._TransferChain__get_trans_fileitems = lambda fileitem, predicate: [
            (fileitem, False)
        ]

        fileitem = make_task(1).fileitem
        history = SimpleNamespace(
            status=True,
            download_hash="abc123",
            downloader="qbittorrent",
        )
        transfer_history_oper = SimpleNamespace(
            get_by_src=lambda src, storage=None: history
        )
        system_config_oper = SimpleNamespace(get=lambda key: None)

        with patch(
            "app.chain.transfer.TransferHistoryOper",
            return_value=transfer_history_oper,
        ), patch(
            "app.chain.transfer.SystemConfigOper",
            return_value=system_config_oper,
        ):
            state, errmsg = TransferChain.do_transfer(
                chain,
                fileitem=fileitem,
                downloader="qbittorrent",
                download_hash="abc123",
                background=False,
            )

        self.assertTrue(state)
        self.assertEqual("Test.Show.S01E01.mkv 已整理过", errmsg)
        self.assertEqual([("abc123", "qbittorrent")], completed)

    def test_failed_history_skip_still_marks_downloader_hash_completed(self):
        chain = make_transfer_chain()
        completed = []

        def fake_transfer_completed(hashs, downloader):
            completed.append((hashs, downloader))

        chain.transfer_completed = fake_transfer_completed
        chain._TransferChain__get_trans_fileitems = lambda fileitem, predicate: [
            (fileitem, False)
        ]

        fileitem = make_task(1).fileitem
        history = SimpleNamespace(
            status=False,
            download_hash="abc123",
            downloader="qbittorrent",
        )
        transfer_history_oper = SimpleNamespace(
            get_by_src=lambda src, storage=None: history
        )
        system_config_oper = SimpleNamespace(get=lambda key: None)

        with patch(
            "app.chain.transfer.TransferHistoryOper",
            return_value=transfer_history_oper,
        ), patch(
            "app.chain.transfer.SystemConfigOper",
            return_value=system_config_oper,
        ):
            state, errmsg = TransferChain.do_transfer(
                chain,
                fileitem=fileitem,
                downloader="qbittorrent",
                download_hash="abc123",
                background=False,
            )

        self.assertFalse(state)
        self.assertEqual("Test.Show.S01E01.mkv 已整理过", errmsg)
        self.assertEqual([("abc123", "qbittorrent")], completed)

    def test_unrecognized_task_marks_downloader_hash_completed(self):
        chain = make_transfer_chain()
        chain.post_message = lambda *_args, **_kwargs: None
        completed = []

        def fake_transfer_completed(hashs, downloader):
            completed.append((hashs, downloader))

        chain.transfer_completed = fake_transfer_completed
        task = make_task(1)
        task.downloader = "qbittorrent"
        task.download_hash = "abc123"
        self.assertTrue(chain.jobview.add_task(task))

        transfer_history_oper = SimpleNamespace(
            add_fail=lambda **kwargs: SimpleNamespace(id=1)
        )

        with patch(
            "app.chain.transfer.TransferHistoryOper",
            return_value=transfer_history_oper,
        ), patch(
            "app.chain.transfer.MediaChain"
        ) as media_chain_cls, patch(
            "app.chain.transfer.settings.AI_AGENT_ENABLE", False
        ), patch(
            "app.chain.transfer.settings.AI_AGENT_RETRY_TRANSFER", False
        ):
            media_chain_cls.return_value.recognize_by_meta.return_value = None
            state, errmsg = chain._TransferChain__handle_transfer(task)

        self.assertFalse(state)
        self.assertEqual("未识别到媒体信息", errmsg)
        self.assertEqual([("abc123", "qbittorrent")], completed)
        self.assertEqual([], chain.jobview.list_jobs())

    def test_do_transfer_syncs_same_stem_extra_files_by_default(self):
        chain = make_transfer_chain()
        planned = []
        main_fileitem = make_fileitem(
            "/downloads/Test Show (2026)/Test.Show.S01E01.2026.mkv"
        )
        subtitle_fileitem = make_fileitem(
            "/downloads/Test Show (2026)/Test.Show.S01E01.2026.zh-cn.srt"
        )

        chain._TransferChain__get_trans_fileitems = lambda fileitem, predicate: [
            (main_fileitem, False)
        ]
        chain._TransferChain__put_to_jobview = lambda task: True
        chain._TransferChain__register_scrape_batch_task = lambda task: None
        chain._TransferChain__close_scrape_batch = lambda batch_id: None

        def fake_handle_transfer(task, callback=None):
            planned.append(task.fileitem.path)
            return True, ""

        chain._TransferChain__handle_transfer = fake_handle_transfer
        transfer_history_oper = SimpleNamespace(get_by_src=lambda src, storage=None: None)
        download_history_oper = SimpleNamespace(
            get_by_hash=lambda download_hash: None,
            get_file_by_fullpath=lambda fullpath: None,
            get_files_by_savepath=lambda savepath: [],
            get_by_path=lambda path: None,
        )
        system_config_oper = SimpleNamespace(get=lambda key: None)
        storage_chain = SimpleNamespace(
            get_parent_item=lambda fileitem: FileItem(
                storage="local",
                path="/downloads/Test Show (2026)/",
                type="dir",
                name="Test Show (2026)",
            ),
            list_files=lambda fileitem, recursion=False: [
                main_fileitem,
                subtitle_fileitem,
            ],
        )

        with patch(
            "app.chain.transfer.TransferHistoryOper",
            return_value=transfer_history_oper,
        ), patch(
            "app.chain.transfer.DownloadHistoryOper",
            return_value=download_history_oper,
        ), patch(
            "app.chain.transfer.SystemConfigOper",
            return_value=system_config_oper,
        ), patch(
            "app.chain.transfer.StorageChain",
            return_value=storage_chain,
        ):
            state, errmsg = TransferChain.do_transfer(
                chain,
                fileitem=main_fileitem,
                background=False,
            )

        self.assertTrue(state)
        self.assertEqual("", errmsg)
        self.assertEqual(
            [
                main_fileitem.path,
                subtitle_fileitem.path,
            ],
            planned,
        )

    def test_manual_transfer_enables_sync_extra_files(self):
        chain = make_transfer_chain()
        captured = {}
        fileitem = make_fileitem("/downloads/Test Show (2026)/Test.Show.S01E01.2026.mkv")

        def fake_do_transfer(**kwargs):
            captured.update(kwargs)
            return True, ""

        chain.do_transfer = fake_do_transfer

        state, errmsg = TransferChain.manual_transfer(
            chain,
            fileitem=fileitem,
            preview=True,
        )

        self.assertTrue(state)
        self.assertEqual("", errmsg)
        self.assertTrue(captured["manual"])
        self.assertTrue(captured["sync_extra_files"])

    def test_manual_transfer_respects_sync_extra_files_argument(self):
        chain = make_transfer_chain()
        captured = {}
        fileitem = make_fileitem("/downloads/Test Show (2026)/Test.Show.S01E01.2026.mkv")

        def fake_do_transfer(**kwargs):
            captured.update(kwargs)
            return True, ""

        chain.do_transfer = fake_do_transfer

        state, errmsg = TransferChain.manual_transfer(
            chain,
            fileitem=fileitem,
            preview=True,
            sync_extra_files=False,
        )

        self.assertTrue(state)
        self.assertEqual("", errmsg)
        self.assertFalse(captured["sync_extra_files"])

    def test_do_transfer_keeps_manual_single_extra_file_when_epformat_misses(self):
        chain = make_transfer_chain()
        planned = []
        subtitle_fileitem = make_fileitem(
            "/downloads/Test Show (2026)/Show - 01.sc.ass"
        )

        chain._TransferChain__put_to_jobview = lambda task: True
        chain._TransferChain__register_scrape_batch_task = lambda task: None
        chain._TransferChain__close_scrape_batch = lambda batch_id: None

        def fake_handle_transfer(task, callback=None):
            planned.append((task.fileitem.path, task.meta.begin_episode))
            return True, ""

        chain._TransferChain__handle_transfer = fake_handle_transfer
        transfer_history_oper = SimpleNamespace(get_by_src=lambda src, storage=None: None)
        download_history_oper = SimpleNamespace(
            get_by_hash=lambda download_hash: None,
            get_file_by_fullpath=lambda fullpath: None,
            get_files_by_savepath=lambda savepath: [],
            get_by_path=lambda path: None,
        )
        system_config_oper = SimpleNamespace(get=lambda key: None)
        storage_chain = SimpleNamespace(get_item=lambda fileitem: subtitle_fileitem)

        with patch(
            "app.chain.transfer.TransferHistoryOper",
            return_value=transfer_history_oper,
        ), patch(
            "app.chain.transfer.DownloadHistoryOper",
            return_value=download_history_oper,
        ), patch(
            "app.chain.transfer.SystemConfigOper",
            return_value=system_config_oper,
        ), patch(
            "app.chain.transfer.StorageChain",
            return_value=storage_chain,
        ), patch(
            "app.chain.transfer.MetaInfoPath",
            side_effect=lambda path, custom_words=None: FakeMeta(1),
        ):
            state, errmsg = TransferChain.do_transfer(
                chain,
                fileitem=subtitle_fileitem,
                background=False,
                manual=True,
                sync_extra_files=True,
                epformat=EpisodeFormat(format="Show - {ep}.mkv"),
            )

        self.assertTrue(state)
        self.assertEqual("", errmsg)
        self.assertEqual([(subtitle_fileitem.path, 1)], planned)

    def test_do_transfer_syncs_extra_files_when_epformat_only_matches_main_video(self):
        chain = make_transfer_chain()
        planned = []
        main_fileitem = make_fileitem(
            "/downloads/Test Show (2026)/Show - 01.mkv"
        )
        subtitle_fileitem = make_fileitem(
            "/downloads/Test Show (2026)/Show - 01.sc.ass"
        )
        parent_fileitem = FileItem(
            storage="local",
            path="/downloads/Test Show (2026)/",
            type="dir",
            name="Test Show (2026)",
        )

        chain._TransferChain__get_trans_fileitems = lambda fileitem, predicate: [
            (main_fileitem, False)
        ]
        chain._TransferChain__put_to_jobview = lambda task: True
        chain._TransferChain__register_scrape_batch_task = lambda task: None
        chain._TransferChain__close_scrape_batch = lambda batch_id: None

        def fake_handle_transfer(task, callback=None):
            planned.append((task.fileitem.path, task.meta.begin_episode))
            return True, ""

        chain._TransferChain__handle_transfer = fake_handle_transfer
        transfer_history_oper = SimpleNamespace(get_by_src=lambda src, storage=None: None)
        download_history_oper = SimpleNamespace(
            get_by_hash=lambda download_hash: None,
            get_file_by_fullpath=lambda fullpath: None,
            get_files_by_savepath=lambda savepath: [],
            get_by_path=lambda path: None,
        )
        system_config_oper = SimpleNamespace(get=lambda key: None)
        storage_chain = SimpleNamespace(
            get_parent_item=lambda fileitem: parent_fileitem,
            list_files=lambda fileitem, recursion=False: [
                main_fileitem,
                subtitle_fileitem,
            ],
        )

        with patch(
            "app.chain.transfer.TransferHistoryOper",
            return_value=transfer_history_oper,
        ), patch(
            "app.chain.transfer.DownloadHistoryOper",
            return_value=download_history_oper,
        ), patch(
            "app.chain.transfer.SystemConfigOper",
            return_value=system_config_oper,
        ), patch(
            "app.chain.transfer.StorageChain",
            return_value=storage_chain,
        ), patch(
            "app.chain.transfer.MetaInfoPath",
            side_effect=lambda path, custom_words=None: FakeMeta(1),
        ):
            state, errmsg = TransferChain.do_transfer(
                chain,
                fileitem=main_fileitem,
                background=False,
                manual=True,
                sync_extra_files=True,
                epformat=EpisodeFormat(format="Show - {ep}.mkv"),
            )

        self.assertTrue(state)
        self.assertEqual("", errmsg)
        self.assertEqual(
            [
                (main_fileitem.path, 1),
                (subtitle_fileitem.path, 1),
            ],
            planned,
        )

    def test_do_transfer_syncs_matching_extra_files_for_each_main_video(self):
        chain = make_transfer_chain()
        planned = []
        main_ep1_fileitem = make_fileitem(
            "/downloads/Test Show (2026)/Test.Show.S01E01.2026.mkv"
        )
        main_ep2_fileitem = make_fileitem(
            "/downloads/Test Show (2026)/Test.Show.S01E02.2026.mkv"
        )
        ep1_subtitle_fileitem = make_fileitem(
            "/downloads/Test Show (2026)/Test.Show.S01E01.2026.zh-cn.srt"
        )
        ep1_audio_fileitem = make_fileitem(
            "/downloads/Test Show (2026)/Test.Show.S01E01.2026.commentary.mka"
        )
        ep2_subtitle_fileitem = make_fileitem(
            "/downloads/Test Show (2026)/Test.Show.S01E02.2026.zh-cn.srt"
        )
        other_title_fileitem = make_fileitem(
            "/downloads/Test Show (2026)/Other.Show.S01E01.2026.zh-cn.srt"
        )
        parent_fileitem = FileItem(
            storage="local",
            path="/downloads/Test Show (2026)/",
            type="dir",
            name="Test Show (2026)",
        )

        chain._TransferChain__get_trans_fileitems = lambda fileitem, predicate: [
            (main_ep1_fileitem, False),
            (main_ep2_fileitem, False),
            (ep1_subtitle_fileitem, False),
            (ep1_audio_fileitem, False),
            (ep2_subtitle_fileitem, False),
            (other_title_fileitem, False),
        ]
        chain._TransferChain__put_to_jobview = lambda task: True
        chain._TransferChain__register_scrape_batch_task = lambda task: None
        chain._TransferChain__close_scrape_batch = lambda batch_id: None

        def fake_handle_transfer(task, callback=None):
            planned.append((task.fileitem.path, task.meta.begin_episode))
            return True, ""

        chain._TransferChain__handle_transfer = fake_handle_transfer
        transfer_history_oper = SimpleNamespace(get_by_src=lambda src, storage=None: None)
        download_history_oper = SimpleNamespace(
            get_by_hash=lambda download_hash: None,
            get_file_by_fullpath=lambda fullpath: None,
            get_files_by_savepath=lambda savepath: [],
            get_by_path=lambda path: None,
        )
        system_config_oper = SimpleNamespace(get=lambda key: None)
        list_files_calls = []

        def fake_list_files(fileitem, recursion=False):
            list_files_calls.append((fileitem.path, recursion))
            return [
                main_ep1_fileitem,
                main_ep2_fileitem,
                ep1_subtitle_fileitem,
                ep1_audio_fileitem,
                ep2_subtitle_fileitem,
                other_title_fileitem,
            ]

        storage_chain = SimpleNamespace(
            get_parent_item=lambda fileitem: parent_fileitem,
            list_files=fake_list_files,
        )

        with patch(
            "app.chain.transfer.TransferHistoryOper",
            return_value=transfer_history_oper,
        ), patch(
            "app.chain.transfer.DownloadHistoryOper",
            return_value=download_history_oper,
        ), patch(
            "app.chain.transfer.SystemConfigOper",
            return_value=system_config_oper,
        ), patch(
            "app.chain.transfer.StorageChain",
            return_value=storage_chain,
        ):
            state, errmsg = TransferChain.do_transfer(
                chain,
                fileitem=parent_fileitem,
                background=False,
                sync_extra_files=True,
            )

        self.assertTrue(state)
        self.assertEqual("", errmsg)
        self.assertEqual(
            [
                (main_ep1_fileitem.path, 1),
                (ep1_subtitle_fileitem.path, 1),
                (ep1_audio_fileitem.path, 1),
                (main_ep2_fileitem.path, 2),
                (ep2_subtitle_fileitem.path, 2),
                (other_title_fileitem.path, 1),
            ],
            planned,
        )
        self.assertEqual([], list_files_calls)

    def test_scrape_event_is_aggregated_by_transfer_batch_across_seasons(self):
        chain = make_transfer_chain()
        chain.eventmanager = MagicMock()
        chain.transfer_completed = lambda *args, **kwargs: None

        tasks = [make_task(1, season=1), make_task(1, season=2)]
        target_diritem = FileItem(
            storage="local",
            path="/library/Test Show (2026)",
            type="dir",
            name="Test Show (2026)",
        )
        batch_id = "batch-tv-multi-season"

        for task in tasks:
            task.mediainfo = FakeMedia()
            task.transfer_batch_id = batch_id
            task.background = False
            task.manual = True
            self.assertTrue(chain._TransferChain__put_to_jobview(task))
            chain._TransferChain__register_scrape_batch_task(task)

        chain._TransferChain__close_scrape_batch(batch_id)

        transferinfos = [
            TransferInfo(
                success=True,
                fileitem=tasks[0].fileitem,
                target_diritem=target_diritem,
                target_item=FileItem(
                    storage="local",
                    path="/library/Test Show (2026)/Season 1/Test.Show.S01E01.mkv",
                    type="file",
                    name="Test.Show.S01E01.mkv",
                    extension="mkv",
                ),
                file_list_new=[
                    "/library/Test Show (2026)/Season 1/Test.Show.S01E01.mkv"
                ],
                transfer_type="copy",
                need_scrape=True,
                need_notify=False,
            ),
            TransferInfo(
                success=True,
                fileitem=tasks[1].fileitem,
                target_diritem=target_diritem,
                target_item=FileItem(
                    storage="local",
                    path="/library/Test Show (2026)/Season 2/Test.Show.S02E01.mkv",
                    type="file",
                    name="Test.Show.S02E01.mkv",
                    extension="mkv",
                ),
                file_list_new=[
                    "/library/Test Show (2026)/Season 2/Test.Show.S02E01.mkv"
                ],
                transfer_type="copy",
                need_scrape=True,
                need_notify=False,
            ),
        ]

        with patch(
            "app.chain.transfer.TransferHistoryOper",
            return_value=SimpleNamespace(add_success=lambda **kwargs: SimpleNamespace(id=1)),
        ), patch(
            "app.chain.transfer.StorageChain"
        ) as storage_chain_cls:
            storage_chain_cls.return_value.is_bluray_folder.return_value = False
            for task, transferinfo in zip(tasks, transferinfos):
                chain._TransferChain__default_callback(task, transferinfo)
                chain._TransferChain__finish_scrape_batch_task(task)

        metadata_calls = [
            call
            for call in chain.eventmanager.send_event.call_args_list
            if call.args[0] == EventType.MetadataScrape
        ]
        self.assertEqual(1, len(metadata_calls))
        event_data = metadata_calls[0].args[1]
        self.assertEqual(target_diritem, event_data["fileitem"])
        self.assertEqual(
            [
                "/library/Test Show (2026)/Season 1/Test.Show.S01E01.mkv",
                "/library/Test Show (2026)/Season 2/Test.Show.S02E01.mkv",
            ],
            event_data["file_list"],
        )
        self.assertEqual({}, chain._scrape_batches)

    def test_scrape_event_keeps_immediate_behavior_without_transfer_batch(self):
        chain = make_transfer_chain()
        chain.eventmanager = MagicMock()
        chain.transfer_completed = lambda *args, **kwargs: None

        task = make_task(1)
        task.mediainfo = FakeMedia()
        task.background = False
        task.manual = True
        self.assertTrue(chain._TransferChain__put_to_jobview(task))

        target_diritem = FileItem(
            storage="local",
            path="/library/Test Show (2026)",
            type="dir",
            name="Test Show (2026)",
        )
        transferinfo = TransferInfo(
            success=True,
            fileitem=task.fileitem,
            target_diritem=target_diritem,
            target_item=FileItem(
                storage="local",
                path="/library/Test Show (2026)/Season 1/Test.Show.S01E01.mkv",
                type="file",
                name="Test.Show.S01E01.mkv",
                extension="mkv",
            ),
            file_list_new=[
                "/library/Test Show (2026)/Season 1/Test.Show.S01E01.mkv"
            ],
            transfer_type="copy",
            need_scrape=True,
            need_notify=False,
        )

        with patch(
            "app.chain.transfer.TransferHistoryOper",
            return_value=SimpleNamespace(add_success=lambda **kwargs: SimpleNamespace(id=1)),
        ), patch(
            "app.chain.transfer.StorageChain"
        ) as storage_chain_cls:
            storage_chain_cls.return_value.is_bluray_folder.return_value = False
            chain._TransferChain__default_callback(task, transferinfo)

        metadata_calls = [
            call
            for call in chain.eventmanager.send_event.call_args_list
            if call.args[0] == EventType.MetadataScrape
        ]
        self.assertEqual(1, len(metadata_calls))
        event_data = metadata_calls[0].args[1]
        self.assertEqual(target_diritem, event_data["fileitem"])
        self.assertEqual(
            ["/library/Test Show (2026)/Season 1/Test.Show.S01E01.mkv"],
            event_data["file_list"],
        )
