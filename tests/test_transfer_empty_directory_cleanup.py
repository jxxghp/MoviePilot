from types import SimpleNamespace

from app.chain.transfer import TransferChain
from app.core.config import settings
from app.schemas import FileItem, TransferDirectoryConf, TransferInfo, TransferTask
from app.schemas.types import MediaType


def _run_move_callback(monkeypatch, target_directory):
    deleted_paths = []
    task = TransferTask(
        fileitem=FileItem(
            storage="cd2",
            path="/115/待整理/测试剧集/Show.S01E01.mkv",
            type="file",
            name="Show.S01E01.mkv",
            basename="Show.S01E01",
            extension="mkv",
            size=1024,
        ),
        meta=SimpleNamespace(begin_season=1),
        mediainfo=SimpleNamespace(type=MediaType.TV),
        target_directory=target_directory,
    )
    chain = object.__new__(TransferChain)
    chain._media_exts = settings.RMT_MEDIAEXT
    chain._subtitle_exts = settings.RMT_SUBEXT
    chain._audio_exts = settings.RMT_AUDIOEXT
    chain._success_target_files = {}
    chain.eventmanager = SimpleNamespace(send_event=lambda *args, **kwargs: None)
    chain.jobview = SimpleNamespace(
        finish_task=lambda current_task: None,
        is_finished=lambda current_task: False,
        is_success=lambda current_task: True,
        success_tasks=lambda mediainfo, season: [task],
    )

    monkeypatch.setattr(
        "app.chain.transfer.TransferHistoryOper",
        lambda: SimpleNamespace(add_success=lambda **kwargs: None),
    )
    monkeypatch.setattr(
        "app.chain.transfer.SystemConfigOper",
        lambda: SimpleNamespace(get=lambda key: None),
    )
    monkeypatch.setattr(
        "app.chain.transfer.StorageChain",
        lambda: SimpleNamespace(
            delete_media_file=lambda fileitem, delete_self=True: (
                deleted_paths.append(fileitem.path) or True
            ),
        ),
    )

    state, message = chain._TransferChain__default_callback(
        task,
        TransferInfo(success=True, transfer_type="move"),
    )
    return state, message, deleted_paths


def test_transfer_directory_enables_empty_directory_cleanup_by_default():
    """
    旧目录配置未声明开关时应保持原有清理行为。
    """
    directory = TransferDirectoryConf()

    assert directory.delete_empty_dirs is True


def test_transfer_directory_can_disable_empty_directory_cleanup():
    """
    目录配置应保留关闭空目录清理的显式设置。
    """
    directory = TransferDirectoryConf(delete_empty_dirs=False)

    assert directory.delete_empty_dirs is False


def test_move_transfer_cleans_empty_source_directory_by_default(monkeypatch):
    """
    旧目录配置未声明开关时，移动整理成功后应继续清理源目录。
    """
    state, message, deleted_paths = _run_move_callback(
        monkeypatch,
        TransferDirectoryConf(monitor_type="monitor"),
    )

    assert state is True
    assert message == ""
    assert deleted_paths == ["/115/待整理/测试剧集/Show.S01E01.mkv"]


def test_move_transfer_keeps_empty_source_directory_when_cleanup_is_disabled(
        monkeypatch,
):
    """
    目录监控关闭空目录清理后，移动整理成功也应保留源目录。
    """
    state, message, deleted_paths = _run_move_callback(
        monkeypatch,
        TransferDirectoryConf(
            monitor_type="monitor",
            delete_empty_dirs=False,
        ),
    )

    assert state is True
    assert message == ""
    assert deleted_paths == []
