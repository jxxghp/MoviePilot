from unittest.mock import AsyncMock, Mock, patch

from app.api.endpoints.media import recognize_file, scrape
from app.core.context import Context, MediaInfo
from app.core.meta import MetaBase, MetaMusic
from app.core.context import MusicInfo
from app.schemas import FileItem, MediaType


def test_scrape_uses_explicit_media_source_and_id() -> None:
    """手动刮削应使用请求指定的数据源原生ID，并传给后续刮削流程。"""
    fileitem = FileItem(storage="alist", path="/movies/Test Movie (2026).mkv", type="file")
    media_info = MediaInfo(title="测试电影", type=MediaType.MOVIE)
    chain = Mock()
    chain.recognize_media.return_value = media_info

    with patch("app.api.endpoints.media.MediaChain", return_value=chain):
        result = scrape(
            fileitem=fileitem,
            storage="alist",
            media_source="douban",
            media_id="123456",
            type_name=MediaType.MOVIE,
            _=Mock(),
        )

    assert result.success is True
    chain.recognize_by_path.assert_not_called()
    recognize_kwargs = chain.recognize_media.call_args.kwargs
    assert recognize_kwargs["source"] == "douban"
    assert recognize_kwargs["mediaid"] == "123456"
    assert recognize_kwargs["mtype"] == MediaType.MOVIE
    chain.obtain_images.assert_called_once_with(mediainfo=media_info)
    assert media_info.scrape_source == "douban"
    scrape_kwargs = chain.scrape_metadata.call_args.kwargs
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

    with patch("app.api.endpoints.media.MediaChain", return_value=chain):
        result = scrape(
            fileitem=fileitem,
            storage="alist",
            media_source="bangumi",
            _=Mock(),
        )

    assert result.success is True
    chain.recognize_by_path.assert_called_once_with(
        fileitem.path,
        source="bangumi",
        obtain_images=True,
    )
    chain.recognize_media.assert_not_called()
    assert media_info.scrape_source == "bangumi"
    chain.scrape_metadata.assert_called_once_with(
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


def test_recognize_file_routes_audio_to_music_chain() -> None:
    """文件管理识别音频文件时应返回音乐专属上下文。"""
    music_chain = Mock()
    music_chain.async_recognize_by_path = AsyncMock(
        return_value=(
            MetaMusic(title="晴天", artists=["周杰伦"]),
            MusicInfo(
                source="musicbrainz",
                media_id="977e6978-139d-425c-bb98-6b0c62d1e45e",
                title="晴天",
                artists=["周杰伦"],
            ),
        )
    )

    import asyncio

    with patch("app.api.endpoints.media.MusicChain", return_value=music_chain):
        result = asyncio.run(recognize_file(path="/music/晴天.flac", _=Mock()))

    assert result["meta_info"]["type"] == "音乐"
    assert result["media_info"]["title"] == "晴天"
    music_chain.async_recognize_by_path.assert_awaited_once_with(
        path="/music/晴天.flac",
        source="musicbrainz",
    )


def test_scrape_music_uses_musicbrainz_uuid_and_music_scraper() -> None:
    """手动音乐刮削应接受 MusicBrainz UUID 并经统一识别入口后写入音乐标签。"""
    fileitem = FileItem(storage="local", path="/music/晴天.flac", type="file")
    info = MusicInfo(
        source="musicbrainz",
        media_id="977e6978-139d-425c-bb98-6b0c62d1e45e",
        title="晴天",
    )
    media_chain = Mock()
    media_chain.recognize_media.return_value = info
    media_chain.scrape_music_metadata.return_value = (True, "已刮削 1 个音频文件")

    with patch("app.api.endpoints.media.MediaChain", return_value=media_chain):
        result = scrape(
            fileitem=fileitem,
            storage="local",
            media_source="musicbrainz",
            media_id="977e6978-139d-425c-bb98-6b0c62d1e45e",
            type_name=MediaType.MUSIC,
            _=Mock(),
        )

    assert result.success is True
    media_chain.recognize_media.assert_called_once_with(
        source="musicbrainz",
        mediaid="977e6978-139d-425c-bb98-6b0c62d1e45e",
        mtype=MediaType.MUSIC,
    )
    media_chain.scrape_music_metadata.assert_called_once_with(
        fileitem=fileitem,
        mediainfo=info,
        overwrite=True,
    )
