"""
验证下载提交传递通用媒体来源，并校验标识和人工确认。
"""

from types import SimpleNamespace
from unittest.mock import Mock

from app import schemas
from app.api.endpoints import download as download_endpoint
from app.domain.context import MediaInfo
from app.schemas.types import MediaSource, MediaType


def test_download_add_passes_generic_media_source(monkeypatch) -> None:
    """
    不含媒体信息的下载应按统一来源ID执行精确识别。
    """
    captured = {}
    media = MediaInfo(
        anilist_info={
            "id": 154587,
            "title": {"english": "Frieren"},
            "format": "TV",
        }
    )

    class FakeMediaChain:
        """
        记录下载接口传入的媒体识别参数。
        """

        def recognize_media(self, **kwargs):
            """
            返回固定媒体信息并保存识别参数。
            """
            captured["recognize"] = kwargs
            return media

    class FakeDownloadChain:
        """
        模拟下载任务提交。
        """

        def download_single(self, **kwargs):
            """
            保存下载上下文并返回任务ID。
            """
            captured["download"] = kwargs
            return "download-1"

    monkeypatch.setattr(download_endpoint, "MediaChain", FakeMediaChain)
    monkeypatch.setattr(download_endpoint, "DownloadChain", FakeDownloadChain)

    response = download_endpoint.add(
        torrent_in=schemas.TorrentInfo(title="Frieren S01E01"),
        media_source="anilist",
        media_id="154587",
        current_user=SimpleNamespace(name="tester"),
    )

    assert response.success is True
    assert captured["recognize"]["media_source"] == MediaSource.AniList
    assert captured["recognize"]["media_id"] == "154587"
    assert captured["download"]["context"].media_info is media


def test_download_add_converts_imdb_to_tmdb_when_recognize_source_is_tmdb(monkeypatch) -> None:
    """
    站点资源携带 IMDb ID 且识别源为 TMDB 时，下载应优先转为 TMDB 本地化身份。
    """
    captured: dict[str, object] = {}
    tmdb_media = MediaInfo(
        tmdb_info={
            "id": 1101383,
            "media_type": MediaType.MOVIE,
            "title": "逃出绝命街",
            "original_title": "The End of Oak Street",
        }
    )

    class FakeMediaChain:
        """
        模拟 IMDb 到 TMDB 的身份转换和二次精确识别。
        """

        def convert_media_identity(self, **kwargs):
            """
            记录身份转换参数并返回 TMDB 条目。
            """
            captured["convert"] = kwargs
            return {"id": 1101383, "media_type": MediaType.MOVIE}

        def recognize_media(self, **kwargs):
            """
            记录识别参数并返回 TMDB 中文媒体信息。
            """
            captured["recognize"] = kwargs
            return tmdb_media

    class FakeDownloadChain:
        """
        模拟下载任务提交。
        """

        def download_single(self, **kwargs):
            """
            保存下载上下文并返回任务 ID。
            """
            captured["download"] = kwargs
            return "download-imdb-converted"

    monkeypatch.setattr(download_endpoint, "MediaChain", FakeMediaChain)
    monkeypatch.setattr(download_endpoint, "DownloadChain", FakeDownloadChain)
    monkeypatch.setattr(download_endpoint, "get_runtime_setting", lambda _key, _default=None: "themoviedb")

    response = download_endpoint.add(
        torrent_in=schemas.TorrentInfo(title="The.End.of.Oak.Street.2026.2160p"),
        media_source="imdb",
        media_id="tt27165187",
        current_user=SimpleNamespace(name="tester"),
    )

    assert response.success is True
    assert captured["convert"] == {
        "target_source": MediaSource.TMDB,
        "media_source": MediaSource.IMDb,
        "media_id": "tt27165187",
    }
    assert captured["recognize"]["media_source"] == MediaSource.TMDB
    assert captured["recognize"]["media_id"] == "1101383"
    assert captured["recognize"]["mtype"] == MediaType.MOVIE
    assert captured["download"]["context"].media_info is tmdb_media


def test_download_add_falls_back_to_imdb_when_tmdb_conversion_misses(monkeypatch) -> None:
    """
    TMDB 无法按 IMDb ID 命中时，应回退使用原始 IMDb 识别结果。
    """
    captured: dict[str, object] = {}
    imdb_media = MediaInfo(
        media_source=MediaSource.IMDb,
        media_id="tt99999999",
        type=MediaType.MOVIE,
        title="Indie Film",
    )

    class FakeMediaChain:
        """
        模拟 TMDB 转换未命中但 IMDb 原生识别成功。
        """

        def convert_media_identity(self, **_kwargs):
            """
            返回空值表示 TMDB 未收录该 IMDb 映射。
            """
            return None

        def recognize_media(self, **kwargs):
            """
            记录回退后的识别参数并返回 IMDb 媒体。
            """
            captured["recognize"] = kwargs
            return imdb_media

    class FakeDownloadChain:
        """
        模拟下载任务提交。
        """

        def download_single(self, **kwargs):
            """
            保存下载上下文并返回任务 ID。
            """
            captured["download"] = kwargs
            return "download-imdb-fallback"

    monkeypatch.setattr(download_endpoint, "MediaChain", FakeMediaChain)
    monkeypatch.setattr(download_endpoint, "DownloadChain", FakeDownloadChain)
    monkeypatch.setattr(download_endpoint, "get_runtime_setting", lambda _key, _default=None: "themoviedb")

    response = download_endpoint.add(
        torrent_in=schemas.TorrentInfo(title="Indie.Film.2026.1080p"),
        media_source="imdb",
        media_id="tt99999999",
        current_user=SimpleNamespace(name="tester"),
    )

    assert response.success is True
    assert captured["recognize"]["media_source"] == MediaSource.IMDb
    assert captured["recognize"]["media_id"] == "tt99999999"
    assert captured["download"]["context"].media_info is imdb_media


def test_download_add_keeps_imdb_when_recognize_source_is_imdb(monkeypatch) -> None:
    """
    用户显式将识别源设为 IMDb 时，不应将其改写为 TMDB。
    """
    captured: dict[str, object] = {}
    imdb_media = MediaInfo(
        media_source=MediaSource.IMDb,
        media_id="tt27165187",
        type=MediaType.MOVIE,
        title="The End of Oak Street",
    )

    class FakeMediaChain:
        """
        禁止在非 TMDB 主识别源时触发 IMDb 到 TMDB 的转换。
        """

        def convert_media_identity(self, **_kwargs):
            """
            非 TMDB 识别源不应调用身份转换。
            """
            raise AssertionError("RECOGNIZE_SOURCE 为 imdb 时不应转换")

        def recognize_media(self, **kwargs):
            """
            记录原生 IMDb 识别调用。
            """
            captured["recognize"] = kwargs
            return imdb_media

    class FakeDownloadChain:
        """
        模拟下载任务提交。
        """

        def download_single(self, **kwargs):
            """
            保存下载上下文并返回任务 ID。
            """
            captured["download"] = kwargs
            return "download-imdb-direct"

    monkeypatch.setattr(download_endpoint, "MediaChain", FakeMediaChain)
    monkeypatch.setattr(download_endpoint, "DownloadChain", FakeDownloadChain)
    monkeypatch.setattr(download_endpoint, "get_runtime_setting", lambda _key, _default=None: "imdb")

    response = download_endpoint.add(
        torrent_in=schemas.TorrentInfo(title="The.End.of.Oak.Street.2026.2160p"),
        media_source="imdb",
        media_id="tt27165187",
        current_user=SimpleNamespace(name="tester"),
    )

    assert response.success is True
    assert captured["recognize"]["media_source"] == MediaSource.IMDb
    assert captured["recognize"]["media_id"] == "tt27165187"
    assert captured["download"]["context"].media_info is imdb_media


def test_download_add_rejects_source_without_media_id() -> None:
    """
    显式媒体来源必须和原生 ID 成对提供，不能退回标题猜测。
    """
    response = download_endpoint.add(
        torrent_in=schemas.TorrentInfo(title="测试动画 S01E01"),
        media_source="bangumi",
        current_user=SimpleNamespace(name="tester"),
    )

    assert response.success is False
    assert response.message == "媒体来源和媒体 ID 必须同时提供"


def test_download_add_requires_confirmation_when_recognition_fails(monkeypatch) -> None:
    """
    未识别的影视资源必须先由用户确认，不能直接提交下载。
    """
    media_chain = Mock()
    media_chain.recognize_by_meta.return_value = None
    download_chain = Mock()
    monkeypatch.setattr(download_endpoint, "MediaChain", lambda: media_chain)
    monkeypatch.setattr(download_endpoint, "DownloadChain", lambda: download_chain)

    response = download_endpoint.add(
        torrent_in=schemas.TorrentInfo(
            title="Harry Potter Complete Collection",
            category=MediaType.MOVIE.value,
        ),
        current_user=SimpleNamespace(name="tester"),
    )

    assert response.success is False
    assert response.message == "无法识别媒体信息"
    assert response.data.requires_confirmation is True
    download_chain.download_single.assert_not_called()


def test_download_add_allows_confirmed_unrecognized_video(monkeypatch) -> None:
    """
    用户确认后应使用种子元数据提交未识别的影视合集。
    """
    media_chain = Mock()
    media_chain.recognize_by_meta.return_value = None
    download_chain = Mock()
    download_chain.download_single.return_value = "download-collection"
    monkeypatch.setattr(download_endpoint, "MediaChain", lambda: media_chain)
    monkeypatch.setattr(download_endpoint, "DownloadChain", lambda: download_chain)

    response = download_endpoint.add(
        torrent_in=schemas.TorrentInfo(
            title="Harry Potter Complete Collection",
            category="movie",
        ),
        allow_unrecognized=True,
        current_user=SimpleNamespace(name="tester"),
    )

    assert response.success is True
    context = download_chain.download_single.call_args.kwargs["context"]
    assert context.media_info.type == MediaType.MOVIE
    assert context.media_info.title == "Harry Potter Complete Collection"
    assert context.media_info.media_id is None


def test_download_add_requires_confirmation_for_unrecognized_music(monkeypatch) -> None:
    """
    未识别的音乐资源同样需要先由用户确认，不能直接提交下载。
    """
    media_chain = Mock()
    media_chain.recognize_by_meta.return_value = None
    download_chain = Mock()
    monkeypatch.setattr(download_endpoint, "MediaChain", lambda: media_chain)
    monkeypatch.setattr(download_endpoint, "DownloadChain", lambda: download_chain)

    response = download_endpoint.add(
        torrent_in=schemas.TorrentInfo(
            title="Various Artists - 90s Collection",
            category=MediaType.MUSIC.value,
        ),
        music_type="album",
        current_user=SimpleNamespace(name="tester"),
    )

    assert response.success is False
    assert response.message == "无法识别媒体信息"
    assert response.data.requires_confirmation is True
    download_chain.download_single.assert_not_called()


def test_download_add_allows_confirmed_unrecognized_music(monkeypatch) -> None:
    """
    用户确认后音乐资源也应使用种子元数据继续下载。
    """
    media_chain = Mock()
    media_chain.recognize_by_meta.return_value = None
    download_chain = Mock()
    download_chain.download_single.return_value = "download-music"
    monkeypatch.setattr(download_endpoint, "MediaChain", lambda: media_chain)
    monkeypatch.setattr(download_endpoint, "DownloadChain", lambda: download_chain)

    response = download_endpoint.add(
        torrent_in=schemas.TorrentInfo(
            title="Various Artists - 90s Collection",
            category="music",
        ),
        music_type="album",
        allow_unrecognized=True,
        current_user=SimpleNamespace(name="tester"),
    )

    assert response.success is True
    context = download_chain.download_single.call_args.kwargs["context"]
    assert context.media_info.type == MediaType.MUSIC
    assert context.media_info.music_type == "album"
    assert context.media_info.title == "90s Collection"


def test_subtitle_download_passes_generic_media_source(monkeypatch) -> None:
    """
    字幕下载接口应把统一来源ID传递到下载链。
    """
    captured = {}

    class FakeDownloadChain:
        """
        记录字幕下载参数。
        """

        def download_subtitle(self, **kwargs):
            """
            保存参数并返回固定成功结果。
            """
            captured.update(kwargs)
            return True, "字幕下载成功", ["/tmp/subtitle.ass"]

    monkeypatch.setattr(
        download_endpoint,
        "_prepare_subtitle_download",
        lambda _subtitle: (True, ""),
    )
    monkeypatch.setattr(download_endpoint, "DownloadChain", FakeDownloadChain)

    response = download_endpoint.download_subtitle(
        subtitle_in=schemas.SubtitleInfo(
            title="Frieren S01E01",
            enclosure="https://example.com/subtitle.ass",
        ),
        media_source="anilist",
        media_id="154587",
        current_user=SimpleNamespace(name="tester"),
    )

    assert response.success is True
    assert captured["media_source"] == "anilist"
    assert captured["media_id"] == "154587"
