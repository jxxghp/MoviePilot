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


def _patch_recognize_source(monkeypatch, source: str) -> None:
    """
    在当前用例中隔离替换 RECOGNIZE_SOURCE 配置值。
    """
    monkeypatch.setattr(
        download_endpoint,
        "get_runtime_settings",
        lambda: SimpleNamespace(get=lambda key, default=None: source if key == "RECOGNIZE_SOURCE" else default),
    )


def test_download_add_converts_imdb_site_resource_to_tmdb_when_recognize_source_is_tmdb(
    monkeypatch,
) -> None:
    """
    RECOGNIZE_SOURCE=themoviedb 时，站点资源携带的 IMDb 身份应转为 TMDB 主身份并保留中文标题。
    """
    _patch_recognize_source(monkeypatch, "themoviedb")
    captured: dict = {"recognize": []}
    tmdb_media = MediaInfo(
        tmdb_info={
            "id": 1101383,
            "media_type": MediaType.MOVIE,
            "title": "逃出绝命街",
            "original_title": "The End of Oak Street",
            "release_date": "2026-01-01",
        }
    )

    class FakeMediaChain:
        """
        模拟 IMDb 到 TMDB 的身份投影和本地化识别。
        """

        def convert_media_identity(self, **kwargs):
            """
            记录投影调用并返回对应的 TMDB 电影详情。
            """
            captured["convert"] = kwargs
            return {"id": 1101383, "media_type": MediaType.MOVIE, "title": "逃出绝命街"}

        def recognize_media(self, **kwargs):
            """
            记录每次识别参数并按 TMDB 身份返回中文电影信息。
            """
            captured["recognize"].append(kwargs)
            assert kwargs["media_source"] == MediaSource.TMDB
            assert kwargs["media_id"] == "1101383"
            return tmdb_media

    class FakeDownloadChain:
        """
        记录最终提交到下载链的上下文。
        """

        def download_single(self, **kwargs):
            """
            保存下载上下文并返回任务 ID。
            """
            captured["download"] = kwargs
            return "download-tmdb"

    monkeypatch.setattr(download_endpoint, "MediaChain", FakeMediaChain)
    monkeypatch.setattr(download_endpoint, "DownloadChain", FakeDownloadChain)

    response = download_endpoint.add(
        torrent_in=schemas.TorrentInfo(
            title="The.End.of.Oak.Street.2026.1080p.WEB-DL",
            category=MediaType.MOVIE.value,
            media_source=MediaSource.IMDb,
            media_id="tt27165187",
        ),
        media_source=MediaSource.IMDb,
        media_id="tt27165187",
        current_user=SimpleNamespace(name="tester"),
    )

    assert response.success is True
    assert captured["convert"] == {
        "target_source": MediaSource.TMDB,
        "media_source": MediaSource.IMDb,
        "media_id": "tt27165187",
        "season": None,
    }
    assert len(captured["recognize"]) == 1
    assert captured["recognize"][0]["mtype"] == MediaType.MOVIE
    context = captured["download"]["context"]
    assert context.media_info is tmdb_media
    assert context.media_info.media_source == MediaSource.TMDB
    assert context.media_info.media_id == "1101383"
    assert context.media_info.title == "逃出绝命街"


def test_download_add_converts_manual_imdb_id_and_fills_projected_tv_season(monkeypatch) -> None:
    """
    手动提交 IMDb 剧集/单集标识时，应转为 TMDB 剧集身份并在标题缺季号时补齐投影季号。
    """
    _patch_recognize_source(monkeypatch, "tmdb")
    captured: dict = {}
    tmdb_media = MediaInfo(
        tmdb_info={
            "id": 209867,
            "media_type": MediaType.TV,
            "name": "葬送的芙莉莲",
            "first_air_date": "2023-09-29",
        }
    )

    class FakeMediaChain:
        """
        模拟返回带季号的 TMDB 剧集投影与识别。
        """

        def convert_media_identity(self, **kwargs):
            """
            返回带第二季信息的剧集投影结果。
            """
            captured["convert"] = kwargs
            return {"id": 209867, "media_type": MediaType.TV, "season": 2}

        def recognize_media(self, **kwargs):
            """
            保存传给 TMDB 识别的元数据与参数。
            """
            captured["recognize"] = kwargs
            return tmdb_media

    download_chain = Mock()
    download_chain.download_single.return_value = "download-tv"
    monkeypatch.setattr(download_endpoint, "MediaChain", FakeMediaChain)
    monkeypatch.setattr(download_endpoint, "DownloadChain", lambda: download_chain)

    response = download_endpoint.add(
        torrent_in=schemas.TorrentInfo(title="Frieren Special"),
        media_source="imdb",
        media_id="tt29000001",
        current_user=SimpleNamespace(name="tester"),
    )

    assert response.success is True
    assert captured["recognize"]["media_source"] == MediaSource.TMDB
    assert captured["recognize"]["media_id"] == "209867"
    assert captured["recognize"]["mtype"] == MediaType.TV
    context = download_chain.download_single.call_args.kwargs["context"]
    assert context.meta_info.begin_season == 2
    assert context.media_info is tmdb_media


def test_download_add_converts_torrent_embedded_douban_identity_to_tmdb(monkeypatch) -> None:
    """
    站点资源自带豆瓣身份且默认识别源为 TMDB 时，同样优先转换为 TMDB 整剧身份。
    """
    _patch_recognize_source(monkeypatch, "themoviedb")
    tmdb_media = MediaInfo(
        tmdb_info={
            "id": 124101,
            "media_type": MediaType.TV,
            "name": "绝望写手",
            "first_air_date": "2021-05-13",
        }
    )
    media_chain = Mock()
    media_chain.convert_media_identity.return_value = {
        "id": 124101,
        "media_type": MediaType.TV,
        "season": 3,
    }
    media_chain.recognize_media.return_value = tmdb_media
    download_chain = Mock()
    download_chain.download_single.return_value = "download-douban-to-tmdb"
    monkeypatch.setattr(download_endpoint, "MediaChain", lambda: media_chain)
    monkeypatch.setattr(download_endpoint, "DownloadChain", lambda: download_chain)

    response = download_endpoint.add(
        torrent_in=schemas.TorrentInfo(
            title="Hacks.S03E01.1080p.WEB-DL",
            media_source=MediaSource.Douban,
            media_id="35932853",
        ),
        media_source=MediaSource.Douban,
        media_id="35932853",
        current_user=SimpleNamespace(name="tester"),
    )

    assert response.success is True
    media_chain.convert_media_identity.assert_called_once_with(
        target_source=MediaSource.TMDB,
        media_source=MediaSource.Douban,
        media_id="35932853",
        season=3,
    )
    assert media_chain.recognize_media.call_args.kwargs["media_source"] == MediaSource.TMDB
    assert media_chain.recognize_media.call_args.kwargs["media_id"] == "124101"
    context = download_chain.download_single.call_args.kwargs["context"]
    assert context.media_info is tmdb_media


def test_download_add_falls_back_to_imdb_without_polluting_meta_when_tmdb_fails(monkeypatch) -> None:
    """
    TMDB 投影失败或 TMDB 识别失败时，应保留未污染的原元数据并回退 IMDb 识别结果。
    """
    _patch_recognize_source(monkeypatch, "themoviedb")
    imdb_media = MediaInfo(
        media_source=MediaSource.IMDb,
        media_id="tt27165187",
        type=MediaType.MOVIE,
        title="The End of Oak Street",
    )
    recognize_calls: list[dict] = []

    class FakeMediaChain:
        """
        模拟 TMDB 识别先修改元数据后失败，再回退 IMDb 识别成功。
        """

        @staticmethod
        def convert_media_identity(**_kwargs):
            """
            返回 TMDB 候选以触发 TMDB 识别路径。
            """
            return {"id": 1101383, "media_type": MediaType.MOVIE}

        @staticmethod
        def recognize_media(**kwargs):
            """
            TMDB 识别模拟写入元数据后失败，IMDb 识别返回英文媒体信息。
            """
            recognize_calls.append(kwargs)
            if kwargs["media_source"] == MediaSource.TMDB:
                kwargs["meta"].media_source = MediaSource.TMDB
                kwargs["meta"].media_id = "1101383"
                return None
            return imdb_media

    download_chain = Mock()
    download_chain.download_single.return_value = "download-fallback"
    monkeypatch.setattr(download_endpoint, "MediaChain", FakeMediaChain)
    monkeypatch.setattr(download_endpoint, "DownloadChain", lambda: download_chain)

    response = download_endpoint.add(
        torrent_in=schemas.TorrentInfo(
            title="The.End.of.Oak.Street.2026.1080p.WEB-DL",
            media_source=MediaSource.IMDb,
            media_id="tt27165187",
        ),
        media_source=MediaSource.IMDb,
        media_id="tt27165187",
        current_user=SimpleNamespace(name="tester"),
    )

    assert response.success is True
    assert [call["media_source"] for call in recognize_calls] == [
        MediaSource.TMDB,
        MediaSource.IMDb,
    ]
    fallback_meta = recognize_calls[1]["meta"]
    assert fallback_meta.media_source is None
    assert fallback_meta.media_id is None
    context = download_chain.download_single.call_args.kwargs["context"]
    assert context.media_info is imdb_media


def test_download_add_keeps_imdb_when_recognize_source_is_not_tmdb(monkeypatch) -> None:
    """
    当系统 RECOGNIZE_SOURCE 不是 TMDB 时，下载接口不应强制转换 IMDb 身份。
    """
    _patch_recognize_source(monkeypatch, "imdb")
    imdb_media = MediaInfo(
        media_source=MediaSource.IMDb,
        media_id="tt27165187",
        type=MediaType.MOVIE,
        title="The End of Oak Street",
    )
    media_chain = Mock()
    media_chain.recognize_media.return_value = imdb_media
    download_chain = Mock()
    download_chain.download_single.return_value = "download-imdb"
    monkeypatch.setattr(download_endpoint, "MediaChain", lambda: media_chain)
    monkeypatch.setattr(download_endpoint, "DownloadChain", lambda: download_chain)

    response = download_endpoint.add(
        torrent_in=schemas.TorrentInfo(
            title="The.End.of.Oak.Street.2026.1080p.WEB-DL",
            media_source=MediaSource.IMDb,
            media_id="tt27165187",
        ),
        media_source=MediaSource.IMDb,
        media_id="tt27165187",
        current_user=SimpleNamespace(name="tester"),
    )

    assert response.success is True
    media_chain.convert_media_identity.assert_not_called()
    assert media_chain.recognize_media.call_args.kwargs["media_source"] == MediaSource.IMDb
    assert media_chain.recognize_media.call_args.kwargs["media_id"] == "tt27165187"
    assert download_chain.download_single.call_args.kwargs["context"].media_info is imdb_media


def test_download_add_skips_conversion_when_source_is_already_tmdb(monkeypatch) -> None:
    """
    已是 TMDB 身份的下载请求直接识别，不重复触发跨来源投影。
    """
    _patch_recognize_source(monkeypatch, "themoviedb")
    tmdb_media = MediaInfo(
        tmdb_info={"id": 1101383, "media_type": MediaType.MOVIE, "title": "逃出绝命街"}
    )
    media_chain = Mock()
    media_chain.recognize_media.return_value = tmdb_media
    download_chain = Mock()
    download_chain.download_single.return_value = "download-already-tmdb"
    monkeypatch.setattr(download_endpoint, "MediaChain", lambda: media_chain)
    monkeypatch.setattr(download_endpoint, "DownloadChain", lambda: download_chain)

    response = download_endpoint.add(
        torrent_in=schemas.TorrentInfo(
            title="The.End.of.Oak.Street.2026.1080p.WEB-DL",
            media_source=MediaSource.TMDB,
            media_id="1101383",
        ),
        media_source=MediaSource.TMDB,
        media_id="1101383",
        current_user=SimpleNamespace(name="tester"),
    )

    assert response.success is True
    media_chain.convert_media_identity.assert_not_called()
    assert media_chain.recognize_media.call_args.kwargs["media_source"] == MediaSource.TMDB
    assert media_chain.recognize_media.call_args.kwargs["media_id"] == "1101383"
