from unittest.mock import Mock, patch

from app.api.endpoints.media import scrape
from app.core.context import Context, MediaInfo
from app.core.meta import MetaBase
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
