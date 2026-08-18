import inspect

from app.application.orchestration.download import DownloadChain
from app.application.orchestration.search import SearchChain
from app.application.orchestration.subscribe import SubscribeChain
from app.application.orchestration.transfer import TransferChain


LEGACY_MEDIA_ID_PARAMETERS = {
    "tmdbid",
    "doubanid",
    "bangumiid",
    "anilistid",
    "imdbid",
    "tvdbid",
    "mediaid",
}


def _assert_unified_media_identity(method, *, required: bool = True) -> None:
    """断言通用媒体方法仅暴露成对的统一身份参数。"""
    parameters = inspect.signature(method).parameters
    assert "media_source" in parameters
    assert "media_id" in parameters
    assert not LEGACY_MEDIA_ID_PARAMETERS.intersection(parameters)
    if required:
        assert parameters["media_source"].default is inspect.Parameter.empty
        assert parameters["media_id"].default is inspect.Parameter.empty


def test_search_chain_uses_unified_media_identity() -> None:
    """精确资源与字幕搜索必须只接收统一媒体身份。"""
    for method in (
        SearchChain.search_by_id,
        SearchChain.async_search_by_id,
        SearchChain.async_search_by_id_stream,
        SearchChain.async_search_subtitles_by_id,
        SearchChain.async_search_subtitles_by_id_stream,
    ):
        _assert_unified_media_identity(method)


def test_download_chain_uses_unified_media_identity() -> None:
    """字幕下载必须只接收统一媒体身份。"""
    _assert_unified_media_identity(DownloadChain.download_subtitle)


def test_transfer_chain_uses_unified_media_identity() -> None:
    """手动整理可选身份也必须使用成对的统一字段。"""
    _assert_unified_media_identity(TransferChain.manual_transfer, required=False)


def test_subscribe_chain_uses_unified_media_identity() -> None:
    """订阅入口可选身份也必须使用成对的统一字段。"""
    _assert_unified_media_identity(SubscribeChain.add, required=False)
    _assert_unified_media_identity(SubscribeChain.async_add, required=False)
