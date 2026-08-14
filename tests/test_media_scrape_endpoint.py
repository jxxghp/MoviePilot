from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.api.endpoints import media as media_endpoint
from app.api.endpoints.media import recognize_file, scrape
from app.domain.context import Context, MediaInfo
from app.domain.meta.metabase import MetaBase
from app.domain.meta.metamusic import MetaMusic
from app.domain.context import MusicInfo
from app.schemas import FileItem, MediaType
from app.schemas.types import MediaSource


def test_scrape_uses_explicit_media_source_and_id() -> None:
    """手动刮削应使用请求指定的数据源原生ID，并传给后续刮削流程。"""
    fileitem = FileItem(storage="alist", path="/movies/Test Movie (2026).mkv", type="file")
    media_info = MediaInfo(title="测试电影", type=MediaType.MOVIE)
    chain = Mock()
    chain.recognize_media.return_value = media_info

    scraping_chain = Mock()
    with patch("app.api.endpoints.media.MediaChain", return_value=chain) as mock_chain, \
            patch("app.api.endpoints.media.ScrapingChain", return_value=scraping_chain):
        # mkv 非音频文件，需显式关闭 Mock 的 is_audio_path 避免误入音乐分支
        mock_chain.is_audio_path.return_value = False
        result = scrape(
            fileitem=fileitem,
            storage="alist",
            media_source=MediaSource.Douban,
            media_id="123456",
            type_name=MediaType.MOVIE,
            _=Mock(),
        )

    assert result.success is True
    chain.recognize_by_path.assert_not_called()
    recognize_kwargs = chain.recognize_media.call_args.kwargs
    assert recognize_kwargs["media_source"] == MediaSource.Douban
    assert recognize_kwargs["media_id"] == "123456"
    assert recognize_kwargs["mtype"] == MediaType.MOVIE
    chain.obtain_images.assert_called_once_with(mediainfo=media_info)
    assert media_info.scrape_source == MediaSource.Douban
    scrape_kwargs = scraping_chain.scrape_metadata.call_args.kwargs
    assert scrape_kwargs["fileitem"] is fileitem
    assert scrape_kwargs["mediainfo"] is media_info
    assert scrape_kwargs["overwrite"] is True


def test_scrape_keeps_automatic_recognition_compatible() -> None:
    """未指定媒体ID时应继续按路径识别，并允许仅限定请求级数据源。"""
    fileitem = FileItem(storage="alist", path="/tv/Test Show S01E01.mkv", type="file")
    meta_info = MetaBase("Test Show S01E01")
    media_info = MediaInfo(title="测试剧集", type=MediaType.TV)
    chain = Mock()
    chain.recognize_by_path.return_value = Context(meta_info=meta_info, media_info=media_info)

    scraping_chain = Mock()
    with patch("app.api.endpoints.media.MediaChain", return_value=chain) as mock_chain, \
            patch("app.api.endpoints.media.ScrapingChain", return_value=scraping_chain):
        # mkv 非音频文件，需显式关闭 Mock 的 is_audio_path 避免误入音乐分支
        mock_chain.is_audio_path.return_value = False
        result = scrape(
            fileitem=fileitem,
            storage="alist",
            media_source=MediaSource.Bangumi,
            _=Mock(),
        )

    assert result.success is True
    chain.recognize_by_path.assert_called_once_with(
        fileitem.path,
        media_source=MediaSource.Bangumi,
        obtain_images=True,
    )
    chain.recognize_media.assert_not_called()
    assert media_info.scrape_source == MediaSource.Bangumi
    scraping_chain.scrape_metadata.assert_called_once_with(
        fileitem=fileitem,
        meta=meta_info,
        mediainfo=media_info,
        overwrite=True,
    )


def test_scrape_rejects_media_id_without_source() -> None:
    """原生媒体ID缺少所属数据源时应直接返回明确错误。"""
    result = scrape(
        fileitem=FileItem(storage="alist", path="/movies/Test.mkv", type="file"),
        storage="alist",
        media_id="123456",
        _=Mock(),
    )

    assert result.success is False
    assert result.message == "指定媒体ID时必须同时指定媒体数据源"


def test_scrape_rejects_zero_media_id_before_recognition() -> None:
    """刮削入口收到零值身份时不得进入识别或刮削链。"""
    fileitem = FileItem(storage="local", path="/tmp/test.mkv", type="file")
    media_chain = Mock(side_effect=AssertionError("零值身份不应创建媒体链"))
    scraping_chain = Mock(side_effect=AssertionError("零值身份不应创建刮削链"))

    with patch("app.api.endpoints.media.MediaChain", media_chain), patch(
        "app.api.endpoints.media.ScrapingChain", scraping_chain
    ):
        result = scrape(
            fileitem=fileitem,
            media_source=MediaSource.TMDB,
            media_id="0",
            type_name=MediaType.MOVIE,
            _=Mock(),
        )

    assert result.success is False
    assert result.message == "媒体ID格式无效"
    media_chain.assert_not_called()
    scraping_chain.assert_not_called()


@pytest.mark.parametrize("media_source", list(MediaSource))
def test_source_media_id_validator_rejects_zero_for_every_source(
        media_source: MediaSource,
) -> None:
    """全部固定媒体来源都应把零值原生 ID 视为无效身份。"""
    assert not media_endpoint._is_valid_source_media_id(media_source, "0")


def test_recognize_file_routes_audio_to_music_chain() -> None:
    """文件管理识别音频文件时应经统一路径识别入口返回音乐专属上下文。"""
    chain = Mock()
    chain.async_recognize_by_path = AsyncMock(
        return_value=Context(
            meta_info=MetaMusic(title="晴天", artists=["周杰伦"]),
            media_info=MusicInfo(
                media_source="musicbrainz",
                media_id="977e6978-139d-425c-bb98-6b0c62d1e45e",
                title="晴天",
                artists=["周杰伦"],
            ),
        )
    )

    import asyncio

    with patch("app.api.endpoints.media.MediaChain", return_value=chain):
        result = asyncio.run(recognize_file(path="/music/晴天.flac", _=Mock()))

    assert result["meta_info"]["type"] == "音乐"
    assert result["media_info"]["title"] == "晴天"
    chain.async_recognize_by_path.assert_awaited_once_with(
        "/music/晴天.flac", media_source=None
    )


def test_scrape_music_uses_musicbrainz_uuid_and_music_scraper() -> None:
    """手动音乐刮削应接受 MusicBrainz UUID 并经统一识别入口后写入音乐标签。"""
    fileitem = FileItem(storage="local", path="/music/晴天.flac", type="file")
    info = MusicInfo(
        media_source="musicbrainz",
        media_id="977e6978-139d-425c-bb98-6b0c62d1e45e",
        title="晴天",
    )
    media_chain = Mock()
    media_chain.recognize_media.return_value = info
    scraping_chain = Mock()
    scraping_chain.scrape_music_metadata.return_value = (True, "已刮削 1 个音频文件")

    with patch("app.api.endpoints.media.MediaChain", return_value=media_chain), \
            patch("app.api.endpoints.media.ScrapingChain", return_value=scraping_chain):
        result = scrape(
            fileitem=fileitem,
            storage="local",
        media_source="musicbrainz",
        media_id="977e6978-139d-425c-bb98-6b0c62d1e45e",
        type_name=MediaType.MUSIC,
        music_type="recording",
            _=Mock(),
        )

    assert result.success is True
    media_chain.recognize_media.assert_called_once_with(
        media_source="musicbrainz",
        media_id="977e6978-139d-425c-bb98-6b0c62d1e45e",
        mtype=MediaType.MUSIC,
        music_type="recording",
    )
    scraping_chain.scrape_music_metadata.assert_called_once_with(
        fileitem=fileitem,
        mediainfo=info,
        overwrite=True,
        media_source="musicbrainz",
    )


def test_scrape_music_without_source_keeps_automatic_recognition() -> None:
    """未选择音乐源时刮削入口应传递空来源，让底层比较全部识别源。"""
    fileitem = FileItem(storage="local", path="/music/晴天.flac", type="file")
    media_chain = Mock()
    scraping_chain = Mock()
    scraping_chain.scrape_music_metadata.return_value = (True, "已刮削 1 个音频文件")

    with patch("app.api.endpoints.media.MediaChain", return_value=media_chain), \
            patch("app.api.endpoints.media.ScrapingChain", return_value=scraping_chain):
        result = scrape(
            fileitem=fileitem,
            storage="local",
            type_name=MediaType.MUSIC,
            _=Mock(),
        )

    assert result.success is True
    scraping_chain.scrape_music_metadata.assert_called_once_with(
        fileitem=fileitem,
        mediainfo=None,
        overwrite=True,
        media_source=None,
    )


def test_scrape_music_album_forwards_album_namespace() -> None:
    """手动专辑刮削必须把 Release Group ID 标记为 album。"""
    fileitem = FileItem(storage="local", path="/music/叶惠美", type="dir")
    info = MusicInfo(
        media_source="musicbrainz",
        media_id="977e6978-139d-425c-bb98-6b0c62d1e45e",
        music_type="album",
        title="叶惠美",
    )
    media_chain = Mock()
    media_chain.recognize_media.return_value = info
    scraping_chain = Mock()
    scraping_chain.scrape_music_metadata.return_value = (True, "已刮削专辑")

    with patch("app.api.endpoints.media.MediaChain", return_value=media_chain), \
            patch("app.api.endpoints.media.ScrapingChain", return_value=scraping_chain):
        result = scrape(
            fileitem=fileitem,
            storage="local",
            media_source="musicbrainz",
            media_id="977e6978-139d-425c-bb98-6b0c62d1e45e",
            type_name=MediaType.MUSIC,
            music_type="album",
            _=Mock(),
        )

    assert result.success is True
    assert media_chain.recognize_media.call_args.kwargs["music_type"] == "album"


def test_scrape_music_accepts_douban_recording_composite_id() -> None:
    """豆瓣音乐曲目 ID 使用“专辑ID:曲序”时应通过入口校验。"""
    fileitem = FileItem(storage="local", path="/music/晴天.flac", type="file")
    info = MusicInfo(
        media_source="doubanmusic",
        media_id="1401853:3",
        music_type="recording",
        title="晴天",
    )
    media_chain = Mock()
    media_chain.recognize_media.return_value = info
    scraping_chain = Mock()
    scraping_chain.scrape_music_metadata.return_value = (True, "已刮削 1 个音频文件")

    with patch("app.api.endpoints.media.MediaChain", return_value=media_chain), \
            patch("app.api.endpoints.media.ScrapingChain", return_value=scraping_chain):
        result = scrape(
            fileitem=fileitem,
            storage="local",
            media_source="doubanmusic",
            media_id="1401853:3",
            type_name=MediaType.MUSIC,
            music_type="recording",
            _=Mock(),
        )

    assert result.success is True
    assert media_chain.recognize_media.call_args.kwargs["media_id"] == "1401853:3"
