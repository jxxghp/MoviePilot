from types import SimpleNamespace

from app.chain.transfer import TransferChain
from app.core.context import MediaInfo
from app.core.metainfo import MetaInfo
from app.schemas import FileItem, TransferDirectoryConf, TransferTask
from app.schemas.types import MediaType


def test_transfer_stops_when_automatic_category_has_no_tmdb_result(monkeypatch) -> None:
    """启用自动类别目录时，缺少 TMDB 分类必须在文件操作前明确失败。"""
    chain = object.__new__(TransferChain)
    chain.jobview = SimpleNamespace(try_remove_job=lambda _task: None)
    monkeypatch.setattr(
        "app.chain.transfer.TransferHistoryOper",
        lambda: SimpleNamespace(),
    )
    monkeypatch.setattr(
        "app.chain.transfer.MediaChain",
        lambda: SimpleNamespace(
            supplement_tmdb_info=lambda media, _meta: media,
        ),
    )
    task = TransferTask(
        fileitem=FileItem(
            storage="local",
            path="/downloads/Test.Movie.2024.mkv",
            type="file",
            name="Test.Movie.2024.mkv",
            extension="mkv",
            size=1024,
        ),
        meta=MetaInfo("Test Movie 2024"),
        mediainfo=MediaInfo(
            source="anilist",
            media_id="1234",
            anilist_id=1234,
            type=MediaType.MOVIE,
            title="Test Movie",
            year="2024",
        ),
        target_directory=TransferDirectoryConf(
            library_storage="local",
            library_path="/library",
            library_category_folder=True,
        ),
        library_category_folder=True,
        preview=True,
    )

    state, message = chain._TransferChain__handle_transfer(task)

    assert not state
    assert message == "未识别到 TMDB 辅助信息，无法按媒体类别整理"
    assert task.mediainfo.source == "anilist"
    assert task.mediainfo.media_id == "1234"
