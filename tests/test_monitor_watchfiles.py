import threading
from pathlib import Path
from unittest.mock import MagicMock

from watchfiles import Change

from app.application.history import TransferHistorySnapshot
from app.monitor.dispatcher import TransferDispatcher
from app.monitor.monitor import Monitor
from app.monitor.watcher import DirectoryChangeEvent, LocalDirectoryWatcher
from app.schemas.system import TransferDirectoryConf
from app.schemas.types import MediaType


class CallbackRecorder:
    """
    测试用目录监控回调记录器。
    """

    def __init__(self):
        """
        初始化事件记录列表。
        """
        self.events = []

    def event_handler(self, event, text: str, event_path: str, file_size: int = None):
        """
        记录目录监控分发出来的事件。
        :param event: 目录监控事件
        :param text: 事件描述
        :param event_path: 事件路径
        :param file_size: 文件大小
        """
        self.events.append((event, text, event_path, file_size))


def _build_monitor_with_dispatcher(handle_file: MagicMock = None):
    """
    构造带分发器的测试用 Monitor 骨架。
    :param handle_file: 替换分发器 handle_file 的替身
    :return: (Monitor 骨架, 分发器)
    """
    monitor = object.__new__(Monitor)
    dispatcher = TransferDispatcher(all_exts=[".mkv"], cache={})
    if handle_file is not None:
        dispatcher.handle_file = handle_file
    monitor._dispatcher = dispatcher
    monitor._lifecycle_lock = threading.RLock()
    monitor._owner_lock = threading.Lock()
    monitor._work_stop_event = threading.Event()
    monitor._shutdown_event = threading.Event()
    monitor._closed = False
    monitor._compensation_threads = {}
    monitor._scheduler_shutdown_thread = None
    monitor._scheduler_shutdown_succeeded = False
    monitor._scheduler = None
    return monitor, dispatcher


def test_handle_changes_dispatches_added_and_modified_files(tmp_path):
    """
    新增和修改文件应转换成目录监控整理回调。
    """
    added_file = tmp_path / "a_added.mkv"
    modified_file = tmp_path / "b_modified.mkv"
    skipped_dir = tmp_path / "c_dir"
    added_file.write_bytes(b"added")
    modified_file.write_bytes(b"modified")
    skipped_dir.mkdir()

    callback = CallbackRecorder()
    watcher = LocalDirectoryWatcher(tmp_path, callback=callback, force_polling=True)
    watcher._handle_changes({
        (Change.added, added_file.as_posix()),
        (Change.modified, modified_file.as_posix()),
        (Change.deleted, added_file.as_posix()),
        (Change.added, skipped_dir.as_posix()),
    })

    assert len(callback.events) == 2
    assert (
        Change.added,
        "新增",
        added_file.as_posix(),
        5,
    ) == (
        callback.events[0][0].change_type,
        callback.events[0][1],
        callback.events[0][2],
        callback.events[0][3],
    )
    assert (
        Change.modified,
        "修改",
        modified_file.as_posix(),
        8,
    ) == (
        callback.events[1][0].change_type,
        callback.events[1][1],
        callback.events[1][2],
        callback.events[1][3],
    )


def test_handle_changes_skips_missing_paths(tmp_path):
    """
    事件到达时已经消失的路径不应触发整理。
    """
    missing_file = tmp_path / "missing.mkv"

    callback = CallbackRecorder()
    watcher = LocalDirectoryWatcher(tmp_path, callback=callback, force_polling=True)
    watcher._handle_changes({(Change.added, missing_file.as_posix())})

    assert callback.events == []


def test_handle_changes_expands_added_directory_files(tmp_path):
    """
    整体移入的新增目录应递归转换成内部文件事件且不重复分发。
    """
    added_dir = tmp_path / "task"
    nested_dir = added_dir / "season"
    nested_dir.mkdir(parents=True)
    movie_file = added_dir / "movie.mkv"
    episode_file = nested_dir / "episode.mkv"
    ignored_file = added_dir / ".DS_Store"
    movie_file.write_bytes(b"movie")
    episode_file.write_bytes(b"episode")
    ignored_file.write_bytes(b"ignored")

    callback = CallbackRecorder()
    watcher = LocalDirectoryWatcher(tmp_path, callback=callback, force_polling=False)
    watcher._handle_changes({
        (Change.added, added_dir.as_posix()),
        (Change.added, movie_file.as_posix()),
    })

    assert [
        (event.change_type, event_path, file_size)
        for event, _, event_path, file_size in callback.events
    ] == [
        (Change.added, movie_file.as_posix(), 5),
        (Change.added, episode_file.as_posix(), 7),
    ]


def test_event_handler_routes_file_events_to_transfer_handler():
    """
    文件事件应继续按 local 存储交给整理流程。
    """
    handle_file = MagicMock()
    monitor, _ = _build_monitor_with_dispatcher(handle_file)
    event_path = Path("/downloads/movie.mkv")
    event = DirectoryChangeEvent(
        change_type=Change.added,
        src_path=event_path.as_posix(),
        is_directory=False
    )

    monitor.event_handler(
        event=event,
        text="新增",
        event_path=event_path.as_posix(),
        file_size=1024
    )

    handle_file.assert_called_once_with(
        storage="local",
        event_path=event_path,
        file_size=1024
    )


def test_event_handler_ignores_directory_events():
    """
    目录事件不应进入文件整理流程。
    """
    handle_file = MagicMock()
    monitor, _ = _build_monitor_with_dispatcher(handle_file)
    event_path = Path("/downloads/folder")
    event = DirectoryChangeEvent(
        change_type=Change.added,
        src_path=event_path.as_posix(),
        is_directory=True
    )

    monitor.event_handler(
        event=event,
        text="新增",
        event_path=event_path.as_posix()
    )

    handle_file.assert_not_called()


def test_event_handler_ignores_download_temp_files():
    """
    下载器临时文件不应进入整理流程。
    """
    handle_file = MagicMock()
    monitor, _ = _build_monitor_with_dispatcher(handle_file)
    event_path = Path("/downloads/movie.mkv.!qB")
    event = DirectoryChangeEvent(
        change_type=Change.modified,
        src_path=event_path.as_posix(),
        is_directory=False
    )

    monitor.event_handler(
        event=event,
        text="修改",
        event_path=event_path.as_posix(),
        file_size=1024
    )

    handle_file.assert_not_called()


def test_event_handler_ignores_non_transferable_files():
    """
    非可整理后缀文件不应进入整理流程。
    """
    handle_file = MagicMock()
    monitor, _ = _build_monitor_with_dispatcher(handle_file)
    event_path = Path("/downloads/movie.nfo")
    event = DirectoryChangeEvent(
        change_type=Change.added,
        src_path=event_path.as_posix(),
        is_directory=False
    )

    monitor.event_handler(
        event=event,
        text="新增",
        event_path=event_path.as_posix(),
        file_size=1024
    )

    handle_file.assert_not_called()


def test_handle_file_skips_transfer_when_history_exists(monkeypatch):
    """
    已有整理记录的源文件不应再次进入整理链。
    """
    dispatcher = TransferDispatcher(all_exts=[".mkv"], cache={})
    event_path = Path("/downloads/movie.mkv")
    lookups = []

    class FakeTransferHistoryOper:
        """
        测试用整理历史查询。
        """

        def get_by_src(self, src: str, storage: str = None):
            """
            记录查询参数并返回已有记录。
            """
            lookups.append((src, storage))
            return TransferHistorySnapshot(
                id=1,
                src=src,
                src_storage=storage,
                src_fileitem={"size": 1024},
                status=True,
            )

        def get_success_by_src(self, src: str, storage: str = None):
            """成功记录已由首次查询返回，无需二次回退。"""
            return None

    transfer_chain = MagicMock()
    logger_info = MagicMock()
    logger_debug = MagicMock()
    monkeypatch.setattr(
        "app.monitor.dispatcher.get_transfer_history_repository",
        FakeTransferHistoryOper,
    )
    monkeypatch.setattr("app.monitor.dispatcher.TransferChain", transfer_chain)
    monkeypatch.setattr("app.monitor.dispatcher.logger.info", logger_info)
    monkeypatch.setattr("app.monitor.dispatcher.logger.debug", logger_debug)

    handled = dispatcher.handle_file(
        storage="local",
        event_path=event_path,
        file_size=1024,
    )

    assert not handled
    assert lookups == [(event_path.as_posix(), "local")]
    transfer_chain.assert_not_called()
    logger_info.assert_not_called()
    assert "已整理过且文件未变化" in logger_debug.call_args.args[0]


def test_handle_file_invokes_transfer_when_history_missing(monkeypatch):
    """
    没有整理记录的源文件应继续进入整理链。
    """
    dispatcher = TransferDispatcher(all_exts=[".mkv"], cache={})
    event_path = Path("/downloads/movie.mkv")

    class FakeTransferHistoryOper:
        """
        测试用空整理历史查询。
        """

        def get_by_src(self, src: str, storage: str = None):
            """
            返回空整理记录。
            """
            return None

        def get_success_by_src(self, src: str, storage: str = None):
            """空仓储没有成功历史。"""
            return None

    transfer_chain_instance = MagicMock()
    transfer_chain = MagicMock(return_value=transfer_chain_instance)
    monkeypatch.setattr(
        "app.monitor.dispatcher.get_transfer_history_repository",
        FakeTransferHistoryOper,
    )
    monkeypatch.setattr("app.monitor.dispatcher.TransferChain", transfer_chain)

    handled = dispatcher.handle_file(
        storage="local",
        event_path=event_path,
        file_size=1024,
    )

    assert handled
    transfer_chain_instance.do_transfer.assert_called_once()
    fileitem = transfer_chain_instance.do_transfer.call_args.kwargs["fileitem"]
    assert fileitem.storage == "local"
    assert fileitem.path == event_path.as_posix()
    assert fileitem.size == 1024


def test_handle_file_prefers_music_type_from_monitor_directory(monkeypatch):
    """音乐目录监控触发整理时应透传音乐类型，避免音频按影视名称识别。"""
    dispatcher = TransferDispatcher(all_exts=[".flac"], cache={})
    event_path = Path("/downloads/music/album/track.flac")
    directories = [
        TransferDirectoryConf(
            storage="local",
            download_path="/downloads",
            media_type=MediaType.MOVIE.value,
            monitor_type="monitor",
        ),
        TransferDirectoryConf(
            storage="local",
            download_path="/downloads/music",
            media_type=MediaType.MUSIC.value,
            monitor_type="monitor",
        ),
    ]
    transfer_chain_instance = MagicMock()

    # 历史查重已由 _should_skip_by_history 统一承担（含失败重试预算与版本变化判定），
    # 这里放行以便验证 mtype 的传递
    monkeypatch.setattr(dispatcher, "_should_skip_by_history", MagicMock(return_value=False))
    monkeypatch.setattr(
        "app.monitor.dispatcher.DirectoryHelper",
        MagicMock(return_value=MagicMock(get_download_dirs=MagicMock(return_value=directories))),
    )
    monkeypatch.setattr(
        "app.monitor.dispatcher.TransferChain",
        MagicMock(return_value=transfer_chain_instance),
    )

    handled = dispatcher.handle_file(
        storage="local",
        event_path=event_path,
        file_size=1024,
    )

    assert handled
    assert transfer_chain_instance.do_transfer.call_args.kwargs["mtype"] == MediaType.MUSIC
