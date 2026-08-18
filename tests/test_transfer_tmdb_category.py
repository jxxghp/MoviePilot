from types import SimpleNamespace

from app.application.orchestration.transfer import TransferChain
from app.domain.context import MediaInfo
from app.domain.metainfo import MetaInfo
from app.schemas import FileItem, TransferDirectoryConf
from app.application.transfer import TransferTask
from app.schemas.types import MediaSource, MediaType


def test_transfer_rejects_partial_or_invalid_explicit_identity() -> None:
    """整理公共入口不得把半套身份或格式非法来源传入后台任务。"""
    chain = object.__new__(TransferChain)
    fileitem = FileItem(
        storage="local",
        path="/downloads/Test.Movie.2024.mkv",
        type="file",
    )

    partial_state, partial_message = chain.do_transfer(
        fileitem=fileitem,
        media_source=MediaSource.TMDB,
    )
    invalid_state, invalid_message = chain.do_transfer(
        fileitem=fileitem,
        media_source="plugin source:invalid",
        media_id="1234",
    )

    assert not partial_state
    assert "media_source" in partial_message
    assert not invalid_state
    assert "media_source" in invalid_message


def test_transfer_resolves_complete_identity_before_building_tasks(monkeypatch) -> None:
    """整理入口收到完整身份时应精确识别，失败后不得退化为标题识别。"""
    chain = object.__new__(TransferChain)
    fileitem = FileItem(
        storage="local",
        path="/downloads/Test.Movie.2024.mkv",
        type="file",
    )
    recognize = monkeypatch.setattr
    calls = []

    class FakeMediaChain:
        """记录精确识别参数并返回空结果。"""

        def recognize_media(self, **kwargs):
            """模拟显式身份识别失败。"""
            calls.append(kwargs)
            return None

    recognize("app.application.orchestration.transfer.MediaChain", FakeMediaChain)

    state, message = chain.do_transfer(
        fileitem=fileitem,
        media_source="tmdb",
        media_id="1234",
        mtype=MediaType.MOVIE,
    )

    assert not state
    assert "未识别到媒体信息" in message
    assert calls == [{
        "mtype": MediaType.MOVIE,
        "media_source": MediaSource.TMDB,
        "media_id": "1234",
        "music_type": None,
    }]


def test_transfer_stops_when_automatic_category_has_no_tmdb_result(monkeypatch) -> None:
    """启用自动类别目录时，缺少 TMDB 分类必须在文件操作前明确失败。"""
    chain = object.__new__(TransferChain)
    chain.jobview = SimpleNamespace(try_remove_job=lambda _task: None)
    monkeypatch.setattr(
        "app.application.orchestration.transfer.TransferHistoryOper",
        lambda: SimpleNamespace(),
    )
    monkeypatch.setattr("app.application.orchestration._transfer.TransferHistoryOper", lambda: SimpleNamespace())
    monkeypatch.setattr(
        "app.application.orchestration.transfer.MediaChain",
        lambda: SimpleNamespace(
            supplement_tmdb_info=lambda media, _meta: media,
        ),
    )
    monkeypatch.setattr("app.application.orchestration._transfer.MediaChain", lambda: SimpleNamespace(
            supplement_tmdb_info=lambda media, _meta: media,
        ))
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
            media_source=MediaSource.AniList,
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
    assert task.mediainfo.media_source == MediaSource.AniList
    assert task.mediainfo.media_id == "1234"
